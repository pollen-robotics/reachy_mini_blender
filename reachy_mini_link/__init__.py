"""Reachy Mini Live Link — stream the Blender rig to a Reachy Mini daemon.

This module must stay import-safe: no class registration or property
assignment at import time, because the test suite imports submodules
directly. Registration happens in register(), which imports lazily.
"""

bl_info = {
    "name": "Reachy Mini Live Link",
    "author": "Pollen Robotics",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar (N) > Reachy Mini",
    "description": "Stream the rig to a Reachy Mini daemon; bake the timeline to a move.",
    "category": "Animation",
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
