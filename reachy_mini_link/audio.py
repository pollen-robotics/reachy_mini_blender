"""Scene audio for moves: detect it, mix it down to WAV.

The animator workflow is a sound strip in the Video Sequencer that the
timeline is animated against. When one exists, Play on Robot and
Publish to Hub carry the audio along; the daemon plays it in lockstep
with the motion (GStreamer playbin), and Marionette finds it as the
move's .wav sidecar in the dataset.

WAV, not OGG, deliberately: Marionette's loader looks exclusively for
`<move>.wav` next to the JSON (marionette/recording.py), so any other
container publishes fine but plays silent over there. Moves are seconds
long, so the PCM size penalty is noise.

Blender renders the mixdown itself (bpy.ops.sound.mixdown with its
bundled FFmpeg), so this stays dependency-free.
"""

import os
import tempfile

import bpy


def scene_has_audio(scene):
    """True if any unmuted sound strip exists in the sequencer."""
    seq = scene.sequence_editor
    if seq is None:
        return False
    # Blender 5 renamed SequenceEditor.sequences_all to strips_all.
    strips = seq.strips_all if hasattr(seq, "strips_all") else seq.sequences_all
    return any(s.type == "SOUND" and not s.mute for s in strips)


def mixdown_wav(scene, frame_start=None, frame_end=None):
    """Render the scene's audio over the given range. Returns WAV bytes.

    Mixdown always uses the scene's frame range, so the range is
    swapped in and restored, mirroring what bake() does for frames.
    """
    saved = (scene.frame_start, scene.frame_end)
    if frame_start is not None:
        scene.frame_start = frame_start
    if frame_end is not None:
        scene.frame_end = frame_end

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="reachy_move_audio_")
    os.close(fd)
    try:
        bpy.ops.sound.mixdown(
            filepath=path, check_existing=False, relative_path=False,
            container="WAV", codec="PCM")
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        scene.frame_start, scene.frame_end = saved
        try:
            os.remove(path)
        except OSError:
            pass
