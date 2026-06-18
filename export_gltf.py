#!/usr/bin/env python3
"""Export the lightweight rigged Reachy Mini viewer model from the .blend to glb.

Exports the `MiniReachyRetopo` collection (~569k tris, 12x lighter than the full
CAD) + the `Armature`, Draco-compressed. Bakes procedural node materials down to
flat PBR factors (glTF can't carry node networks), using each material's viewport
diffuse_color as the intended flat color.

Usage:
  # distribution glb (Y-up, Draco) next to the .blend:
  blender --background "3D_model/MiniReachy012bis.Test(1).blend" \
      --python 3D_model/export_gltf.py

  # desktop-app copy (Z-up, no Draco) into the app assets:
  blender --background "3D_model/MiniReachy012bis.Test(1).blend" \
      --python 3D_model/export_gltf.py -- --zup --no-draco --out /path/to/reachy_mini_viz.glb
"""
import bpy, os, sys

# CLI args after `--`. Defaults = distribution glb (Y-up, Draco, next to .blend).
#   --zup       keep Blender Z-up (robot's native frame); head_pose applies to
#               the rig with no basis change. Used by the desktop app.
#   --no-draco  skip Draco compression (no decoder to host; bigger file).
#   --no-uv     drop UVs (materials are flat-colour, no textures use them).
#   --no-color  drop vertex colours (unused by the flat materials).
#   --out PATH  output path.
argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
YUP = '--zup' not in argv
DRACO = '--no-draco' not in argv
UV = '--no-uv' not in argv
COLORS = '--no-color' not in argv
OUT = (argv[argv.index('--out') + 1] if '--out' in argv
       else os.path.join(os.path.dirname(bpy.data.filepath), "reachy_mini_viz.glb"))

# --- Bake procedural materials down to flat glTF-friendly PBR factors ---
fixed = []
for m in bpy.data.materials:
    if not m.use_nodes:
        continue
    for nd in m.node_tree.nodes:
        if nd.type != 'BSDF_PRINCIPLED':
            continue
        bc = nd.inputs['Base Color']
        if bc.is_linked:  # procedural color -> bake to viewport diffuse_color
            for l in list(bc.links):
                m.node_tree.links.remove(l)
            dc = m.diffuse_color
            bc.default_value = (dc[0], dc[1], dc[2], 1.0)
            fixed.append(m.name)
        for ch in ('Metallic', 'Roughness'):
            inp = nd.inputs.get(ch)
            if inp and inp.is_linked:
                for l in list(inp.links):
                    m.node_tree.links.remove(l)
                inp.default_value = getattr(m, ch.lower())
print(f"Rebaked {len(set(fixed))} procedural base colors")

# --- Select retopo meshes + armature ---
# Force OBJECT mode + deselect via the data API: operators like select_all can
# fail in headless runs if the .blend was saved in edit/pose mode.
try:
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
except RuntimeError:
    pass
for o in bpy.context.view_layer.objects:
    o.select_set(False)

def all_objs(c):
    o = list(c.objects)
    for ch in c.children:
        o += all_objs(ch)
    return o

retopo = all_objs(bpy.data.collections['MiniReachyRetopo'])
arm = bpy.data.objects['Armature']
sel = [o for o in retopo if o.type == 'MESH'] + [arm]
for o in sel:
    o.hide_set(False)
    o.hide_viewport = False
    o.select_set(True)
bpy.context.view_layer.objects.active = arm
print(f"Selected {len(sel)} objects")

bpy.ops.export_scene.gltf(
    filepath=OUT,
    use_selection=True,
    export_format='GLB',
    export_apply=True,        # apply modifiers
    export_yup=YUP,
    export_texcoords=UV,
    export_vertex_color='MATERIAL' if COLORS else 'NONE',
    export_all_vertex_colors=COLORS,
    export_draco_mesh_compression_enable=DRACO,
    export_draco_mesh_compression_level=6,
    export_animations=False,  # no actions in the file; drive bone nodes live in-app
    export_skins=True,
    export_def_bones=False,
)
print(f"DONE -> {OUT}")
