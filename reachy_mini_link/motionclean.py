"""Clean a sampled motion channel before turning it into keyframes.

Pure Python on purpose (no bpy, no numpy): import_move.py runs this inside
Blender, the unit tests run it with the system interpreter. Recorded moves
are short (minutes at ~100 Hz, a handful of scalar channels), so O(n·window)
loops are plenty fast.

The pipeline for one channel is:

    unwrap         -> remove artificial ±2π jumps from angle series so
                      smoothing and key selection see a continuous signal
    smooth         -> time-aware Gaussian low-pass; sigma is in seconds
                      because real recordings (e.g. Marionette's) have
                      irregular timestamps
    salient_select -> pick the key samples, jointly for all channels of
                      the pose, against free-tangent Bézier reconstruction
                      (Salient-Poses-style; rdp remains as the simpler
                      per-channel alternative)
"""

import bisect
import math

TWO_PI = 2.0 * math.pi


def unwrap(values, period=TWO_PI):
    """Remove jumps larger than half a period from an angle series.

    Same contract as numpy.unwrap: the first value is untouched, every
    subsequent value is shifted by a multiple of `period` so consecutive
    deltas stay within ±period/2.
    """
    if not values:
        return []
    out = [float(values[0])]
    offset = 0.0
    half = period / 2.0
    for prev, cur in zip(values, values[1:]):
        delta = cur - prev
        if delta > half:
            offset -= period
        elif delta < -half:
            offset += period
        out.append(float(cur) + offset)
    return out


def gaussian_smooth(times, values, sigma):
    """Low-pass filter `values` with a Gaussian kernel of `sigma` seconds.

    Weighting by actual timestamps (not sample index) keeps the cutoff
    frequency honest on recordings with irregular sample rates. Window is
    truncated at 3 sigma. sigma <= 0 returns the input unchanged.
    """
    n = len(values)
    if sigma <= 0.0 or n < 3:
        return [float(v) for v in values]

    reach = 3.0 * sigma
    inv = -0.5 / (sigma * sigma)
    out = []
    lo = 0
    for i in range(n):
        t0 = times[i]
        while times[lo] < t0 - reach:
            lo += 1
        acc = 0.0
        wsum = 0.0
        j = lo
        while j < n and times[j] <= t0 + reach:
            w = math.exp(inv * (times[j] - t0) ** 2)
            acc += w * values[j]
            wsum += w
            j += 1
        out.append(acc / wsum)
    return out


def rdp(times, values, epsilon):
    """Indices of the samples to keep so the polyline stays within `epsilon`.

    Vertical-distance Ramer-Douglas-Peucker: the error of a dropped sample
    is measured as |value - chord(t)| at its own timestamp, so `epsilon` is
    in the channel's value units (metres, radians, ...) - exactly the
    "how far may the curve drift" number a user would set.

    Always keeps the first and last sample. epsilon <= 0 keeps everything.
    """
    n = len(values)
    if n <= 2:
        return list(range(n))
    if epsilon <= 0.0:
        return list(range(n))

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ta, va = times[a], values[a]
        tb, vb = times[b], values[b]
        span = tb - ta
        worst = -1.0
        worst_i = -1
        for i in range(a + 1, b):
            if span > 0.0:
                chord = va + (vb - va) * (times[i] - ta) / span
            else:
                chord = va
            err = abs(values[i] - chord)
            if err > worst:
                worst = err
                worst_i = i
        if worst > epsilon:
            keep[worst_i] = True
            stack.append((a, worst_i))
            stack.append((worst_i, b))
    return [i for i in range(n) if keep[i]]


def fit_bezier(xs, ys, lo, hi, ends):
    """Least-squares inner control values for one cubic Bézier segment.

    ends is (x0, y0, x1, y1) - the segment's pinned endpoints. The
    samples xs[lo:hi] (all inside [x0, x1]) are fitted by choosing the
    two inner control values c1, c2; the handles' x positions sit at the
    thirds of the span, which makes the curve's time parameterization
    exactly linear - the same curve Blender draws for keys whose handle
    x sit at the thirds. Returns (c1, c2).

    With free tangents a single segment can hug a whole arc that
    chord-based simplification (rdp) would need several keys for; this
    is where most of the key-count win comes from.
    """
    x0, y0, x1, y1 = ends
    span = x1 - x0
    if hi - lo < 1 or span <= 0.0:
        return y0 + (y1 - y0) / 3.0, y0 + (y1 - y0) * (2.0 / 3.0)

    a11 = a12 = a22 = b1 = b2 = 0.0
    for k in range(lo, hi):
        u = (xs[k] - x0) / span
        w = 1.0 - u
        f1 = 3.0 * u * w * w
        f2 = 3.0 * u * u * w
        r = ys[k] - (w * w * w * y0 + u * u * u * y1)
        a11 += f1 * f1
        a12 += f1 * f2
        a22 += f2 * f2
        b1 += f1 * r
        b2 += f2 * r
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-12:
        # One interior sample (or collinear weights): the 2x2 system is
        # underdetermined; fall back to the chord's control values.
        return y0 + (y1 - y0) / 3.0, y0 + (y1 - y0) * (2.0 / 3.0)
    c1 = (b1 * a22 - b2 * a12) / det
    c2 = (a11 * b2 - a12 * b1) / det
    return c1, c2


def eval_bezier(y0, c1, c2, y1, u):
    """Value of the cubic with inner controls c1, c2 at parameter u."""
    w = 1.0 - u
    return (w * w * w * y0 + 3.0 * u * w * w * c1
            + 3.0 * u * u * w * c2 + u * u * u * y1)


def salient_select(xs, channels, limit=1.0):
    """Key sample indices shared by all channels of a pose.

    Salient-Poses-style selection (Roberts et al. 2019): instead of
    simplifying each channel on its own, pick one set of key times for
    the whole pose, so the animator gets aligned key columns. `channels`
    hold values normalised by their own tolerance, which makes an error
    of `limit` mean "at tolerance" for every channel alike.

    Greedy variant: start from the endpoints and repeatedly key the
    sample the current reconstruction misses worst - by construction a
    salient point of the motion - refitting only the split segment,
    until every channel fits everywhere. The exact dynamic program is
    quadratic in the sample count; greedy lands within a few keys of it
    at this scale and stays interactive. Reconstruction error is
    measured against free-tangent Bézier fits (fit_bezier), matching
    what actually gets written to the fcurves.

    Always keeps the first and last sample. Returns sorted indices.
    """
    n = len(xs)
    if n <= 2 or not channels:
        return list(range(n))

    def seg_worst(i0, i1):
        """(max normalised error, sample index) inside segment (i0, i1)."""
        worst_err, worst_i = 0.0, -1
        x0, x1 = xs[i0], xs[i1]
        span = x1 - x0
        for ch in channels:
            c1, c2 = fit_bezier(xs, ch, i0 + 1, i1,
                                (x0, ch[i0], x1, ch[i1]))
            for k in range(i0 + 1, i1):
                u = (xs[k] - x0) / span if span > 0.0 else 0.0
                err = abs(eval_bezier(ch[i0], c1, c2, ch[i1], u) - ch[k])
                if err > worst_err:
                    worst_err, worst_i = err, k
        return worst_err, worst_i

    keys = [0, n - 1]
    worsts = {(0, n - 1): seg_worst(0, n - 1)}
    while True:
        seg, (err, k) = max(worsts.items(), key=lambda item: item[1][0])
        if err <= limit or k < 0:
            break
        del worsts[seg]
        worsts[(seg[0], k)] = seg_worst(seg[0], k)
        worsts[(k, seg[1])] = seg_worst(k, seg[1])
        bisect.insort(keys, k)
    return keys


def subset_select(xs, ys, columns, limit):
    """The subset of `columns` one channel actually needs, same greedy.

    salient_select picks key columns for the whole pose; writing every
    column to every channel would pay for the alignment in redundant
    keys (a still antenna does not need the head's keys). This keeps a
    channel's keys on the shared columns - aligned whenever present -
    but only where that channel's own reconstruction demands support.

    Insertion candidates are restricted to `columns`; the worst sample
    is fixed by keying the nearest column inside its segment. Because
    the full column set satisfies `limit` by construction, the loop
    always terminates. Returns sorted sample indices (a subset of
    columns plus both endpoints).
    """
    n = len(xs)
    if n <= 2 or limit <= 0.0:
        return list(range(n))

    def seg_worst(i0, i1):
        x0, x1 = xs[i0], xs[i1]
        span = x1 - x0
        c1, c2 = fit_bezier(xs, ys, i0 + 1, i1, (x0, ys[i0], x1, ys[i1]))
        worst_err, worst_i = 0.0, -1
        for k in range(i0 + 1, i1):
            u = (xs[k] - x0) / span if span > 0.0 else 0.0
            err = abs(eval_bezier(ys[i0], c1, c2, ys[i1], u) - ys[k])
            if err > worst_err:
                worst_err, worst_i = err, k
        return worst_err, worst_i

    inner = [c for c in columns if 0 < c < n - 1]
    keys = [0, n - 1]
    worsts = {(0, n - 1): seg_worst(0, n - 1)}
    while True:
        seg, (err, k) = max(worsts.items(), key=lambda item: item[1][0])
        if err <= limit or k < 0:
            break
        candidates = [c for c in inner if seg[0] < c < seg[1]]
        if not candidates:
            # No column left inside this segment: the residual is below
            # what the full column set allows; leave it to the caller's
            # final enforcement pass.
            worsts[seg] = (0.0, -1)
            continue
        c = min(candidates, key=lambda idx: abs(idx - k))
        del worsts[seg]
        worsts[(seg[0], c)] = seg_worst(seg[0], c)
        worsts[(c, seg[1])] = seg_worst(c, seg[1])
        bisect.insort(keys, c)
    return keys


def clean(times, values, sigma=0.0, epsilon=0.0, angular=False):
    """unwrap (if angular) + smooth + simplify one channel in one call.

    Returns (kept_times, kept_values).
    """
    vals = unwrap(values) if angular else [float(v) for v in values]
    vals = gaussian_smooth(times, vals, sigma)
    idx = rdp(times, vals, epsilon)
    return [times[i] for i in idx], [vals[i] for i in idx]
