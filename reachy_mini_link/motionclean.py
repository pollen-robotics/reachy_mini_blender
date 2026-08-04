"""Clean a sampled motion channel before turning it into keyframes.

Pure Python on purpose (no bpy, no numpy): import_move.py runs this inside
Blender, the unit tests run it with the system interpreter. Recorded moves
are short (minutes at ~100 Hz, a handful of scalar channels), so O(n·window)
loops are plenty fast.

The pipeline for one channel is:

    unwrap  -> remove artificial ±2π jumps from angle series so smoothing
               and simplification see a continuous signal
    smooth  -> time-aware Gaussian low-pass; sigma is in seconds because
               real recordings (e.g. Marionette's) have irregular timestamps
    rdp     -> Ramer-Douglas-Peucker on the (t, v) polyline, keeping only
               the samples needed to stay within `epsilon` of the original;
               the survivors become editable keyframes
"""

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


def clean(times, values, sigma=0.0, epsilon=0.0, angular=False):
    """unwrap (if angular) + smooth + simplify one channel in one call.

    Returns (kept_times, kept_values).
    """
    vals = unwrap(values) if angular else [float(v) for v in values]
    vals = gaussian_smooth(times, vals, sigma)
    idx = rdp(times, vals, epsilon)
    return [times[i] for i in idx], [vals[i] for i in idx]
