"""
hero_retarget.py  -  Retarget Meshy / Mixamo skeleton animation onto Ghostboy (GB_Rig)
===============================================================================
(Falls back to the old HeroGhost_Rig if GB_Rig isn't in the file. For Ghostboy, run
 gb_cloth_bake.py on the new clip afterwards so the sheet's skirt bones get cloth motion,
 then export_glb() writes GhostBoy.glb from the Godot export meshes.)

What it does (one call):
  1. Finds the source skeleton (any armature with a Mixamo-style "Hips" bone).
  2. Maps Mixamo bone names -> Godot humanoid names on the hero rig.
  3. Transfers every bone's rotation in WORLD space, corrected for rest-pose
     differences (A-pose vs T-pose, different bone roll/direction).
     -> works even if the FBX/GLB came in rotated 90 deg or scaled 0.01.
  4. Scales hip travel by the leg-length ratio so the feet don't skate.
  5. Drives the hero's leg IK: foot controls follow the retargeted feet,
     knee (pole) controls follow the source's real knee direction.
  6. Floor pass: lifts the whole body per frame (smoothed) so the hero's own
     shoe soles never go below the floor (Meshy presets often sink).
  7. Gives the fingers a relaxed curl (Meshy has no finger bones).
  8. Writes a new action on the hero rig, flags it cyclic if it loops,
     and returns a verification report (max bone error, lowest point).

Usage (Blender Python / bridge):
    hr = bpy.app.driver_namespace["hero_retarget"]      # after loading, see bottom
    hr.list_sources()
    rep = hr.retarget("target_character.001", action_name="Dance_Foo")
    hr.export_glb()     # saves .blend, exports HeroGhost.glb with hero clips only

Tuning knobs are retarget() keyword args.
"""
import bpy, math, re
from mathutils import Matrix, Quaternion as Q, Vector as V

DST_RIG = "GB_Rig" if "GB_Rig" in bpy.data.objects else "HeroGhost_Rig"
# Ghostboy's Godot export set (exported instead of every production part)
EXPORT_MESHES = ("GhostBoy_Body", "GhostBoy_Mask", "GhostBoy_Skull")


def _skin_meshes(dst):
    """Mesh children that are skinned, visible geometry (skip cloth sims and collision helpers)."""
    out = []
    for c in dst.children:
        if c.type != 'MESH':
            continue
        if any(m.type == 'CLOTH' for m in c.modifiers) or 'Collision' in c.name:
            continue
        out.append(c)
    return out

# Mixamo (prefix stripped) -> hero (Godot SkeletonProfileHumanoid names)
MAP_CORE = {"Hips": "Hips", "Spine": "Spine", "Spine2": "Chest", "Neck": "Neck", "Head": "Head"}
MAP_LIMB = {"Shoulder": "Shoulder", "Arm": "UpperArm", "ForeArm": "LowerArm", "Hand": "Hand",
            "UpLeg": "UpperLeg", "Leg": "LowerLeg", "Foot": "Foot", "ToeBase": "Toes"}
# relaxed hand when the source has no finger data (degrees about local X = curl)
FINGER_REST = {"IndexProximal": 20, "IndexIntermediate": 25, "MiddleProximal": 25,
               "MiddleIntermediate": 30, "RingProximal": 30, "RingIntermediate": 35,
               "ThumbProximal": 12}


# ----------------------------------------------------------------- helpers
def _strip(name):
    return re.sub(r'^mixamorig\d*[:_]', '', name)

def _ydir(m3):
    return (m3 @ V((0, 1, 0))).normalized()

def _walk(bones):
    order = []
    def rec(b):
        order.append(b.name)
        for c in b.children:
            rec(c)
    for b in bones:
        if b.parent is None:
            rec(b)
    return order

def _fcurves(action, slot):
    try:
        from bpy_extras import anim_utils
        return anim_utils.action_ensure_channelbag_for_slot(action, slot).fcurves
    except Exception:
        return action.fcurves  # pre-slotted-action Blender

def _leg_len(pts):
    return (pts[0] - pts[1]).length + (pts[1] - pts[2]).length


def list_sources():
    """Armatures that look like Mixamo/Meshy skeletons."""
    out = []
    for o in bpy.data.objects:
        if o.type == 'ARMATURE' and o.name != DST_RIG and \
                any(_strip(b.name) == "Hips" for b in o.data.bones):
            a = o.animation_data.action if o.animation_data else None
            out.append(dict(object=o.name, action=a.name if a else None,
                            frames=[round(x, 1) for x in a.frame_range] if a else None,
                            skinned=any(c.type == 'MESH' for c in o.children)))
    return out


def build_map(src):
    names = {_strip(b.name): b.name for b in src.data.bones}
    m = {}
    for k, v in MAP_CORE.items():
        if k in names:
            m[names[k]] = v
    if "Spine2" not in names and "Spine1" in names:
        m[names["Spine1"]] = "Chest"
    for s in ("Left", "Right"):
        for k, v in MAP_LIMB.items():
            if s + k in names:
                m[names[s + k]] = s + v
    return m


def _sole_points(dst, rest):
    """Rest-space sole samples per side, rigidly attached to Foot/Toes bones,
    taken from the hero's actual mesh (lowest part of each shoe)."""
    pts = {"Left": [], "Right": []}
    for mesh in _skin_meshes(dst):
        if mesh.name in EXPORT_MESHES and len(_skin_meshes(dst)) > len(EXPORT_MESHES):
            continue  # production parts already cover the feet; skip the merged duplicate
        gi = {g.index: g.name for g in mesh.vertex_groups}
        mw = dst.matrix_world.inverted() @ mesh.matrix_world
        for v in mesh.data.vertices:
            if not v.groups:
                continue
            g = max(v.groups, key=lambda g: g.weight)
            n = gi.get(g.group, "")
            for s in ("Left", "Right"):
                if n in (s + "Foot", s + "Toes"):
                    pts[s].append((n, mw @ v.co))
    for s in pts:
        if pts[s]:
            zs = sorted(p[1].z for p in pts[s])
            cut = zs[max(0, len(zs) // 5)]
            low = [p for p in pts[s] if p[1].z <= cut]
            step = max(1, len(low) // 60)
            pts[s] = [(n, rest[n].inverted() @ co) for n, co in low[::step]]
    return pts


# ----------------------------------------------------------------- main
def retarget(src_name=None, action_name=None, dst_name=DST_RIG,
             floor=True, floor_z=0.0, knee_follow=True, finger_curl=True,
             frame_start=None, frame_end=None, smooth_radius=2):
    scn = bpy.context.scene
    dst = bpy.data.objects[dst_name]
    if src_name is None:
        c = list_sources()
        if len(c) != 1:
            raise RuntimeError(f"Specify src_name; candidates: {[x['object'] for x in c]}")
        src_name = c[0]["object"]
    src = bpy.data.objects[src_name]
    sact = src.animation_data.action if src.animation_data else None
    if sact is None:
        raise RuntimeError(f"{src_name} has no action")
    MAP = build_map(src)
    inv = {v: k for k, v in MAP.items()}
    missing = [v for v in inv if v not in dst.data.bones]
    for v in missing:
        inv.pop(v)

    f0 = int(math.floor(sact.frame_range[0] if frame_start is None else frame_start))
    f1 = int(math.ceil(sact.frame_range[1] if frame_end is None else frame_end))
    frames = list(range(f0, f1 + 1))

    # --- rest data (world space) ---
    scn.frame_set(f0)
    SW0 = src.matrix_world.copy()
    DW = dst.matrix_world.copy(); DWi = DW.inverted(); DWr = DW.to_quaternion()
    SR = {s: SW0 @ src.data.bones[s].matrix_local for s in MAP}
    R = {b.name: b.matrix_local.copy() for b in dst.data.bones}          # dst armature space
    RW = {n: DW @ m for n, m in R.items()}
    corr = {d: _ydir(RW[d].to_3x3()).rotation_difference(_ydir(SR[s].to_3x3()))
            for d, s in inv.items()}

    def srest_head(stripped):
        n = next(k for k in MAP if _strip(k) == stripped)
        return SR[n].translation
    try:
        src_leg = _leg_len([srest_head("LeftUpLeg"), srest_head("LeftLeg"), srest_head("LeftFoot")])
        dst_leg = _leg_len([RW["LeftUpperLeg"].translation, RW["LeftLowerLeg"].translation,
                            RW["LeftFoot"].translation])
        K = dst_leg / src_leg
    except StopIteration:
        K = 1.0; dst_leg = 0.35

    # --- sample source in world space ---
    S = {}
    for f in frames:
        scn.frame_set(f)
        mw = src.matrix_world
        S[f] = {s: mw @ src.pose.bones[s].matrix for s in MAP}

    order = _walk(dst.data.bones)
    has_ik = all(n in R for n in ("IK_LeftFoot", "IK_RightFoot"))
    has_pole = all(n in R for n in ("Pole_LeftKnee", "Pole_RightKnee"))
    soles = _sole_points(dst, R) if floor else {"Left": [], "Right": []}

    def base_rest_q(n):
        name = n.replace("Left", "").replace("Right", "")
        return Q((1, 0, 0), math.radians(FINGER_REST.get(name, 0))) if finger_curl else Q()

    FK, need = [], []
    for f in frames:
        M, row = {}, {}
        for n in order:
            b = dst.data.bones[n]; loc = V((0, 0, 0))
            base = (M[b.parent.name] @ (R[b.parent.name].inverted() @ R[n])) if b.parent else R[n].copy()
            if n in inv:
                s = inv[n]; sp = S[f][s]
                want_w = (sp.to_quaternion() @ SR[s].to_quaternion().inverted()) @ corr[n] @ RW[n].to_quaternion()
                want = DWr.inverted() @ want_w
                q = base.to_quaternion().inverted() @ want
                if n == "Hips":
                    head_w = RW[n].translation + (sp.translation - SR[s].translation) * K
                    loc = base.to_3x3().inverted() @ ((DWi @ head_w) - base.translation)
            else:
                q = base_rest_q(n)
            M[n] = base @ (Matrix.Translation(loc) @ q.to_matrix().to_4x4())
            row[n] = [loc, q]
        low = 1e9
        for side, lst in soles.items():
            for bn, p in lst:
                low = min(low, (M[bn] @ p).z)
        need.append(max(0.0, floor_z - low) if low < 1e8 else 0.0)
        FK.append((M, row))

    # --- floor lift: dilate then smooth so it never pops ---
    n = len(need); r = smooth_radius
    dil = [max(need[max(0, i - r):i + r + 1]) for i in range(n)]
    lift = [sum(dil[max(0, i - r):i + r + 1]) / len(dil[max(0, i - r):i + r + 1]) for i in range(n)]

    out = {}; prevdir = {}
    for (M, row), L in zip(FK, lift):
        up = V((0, 0, L))
        row["Hips"][0] = row["Hips"][0] + R["Hips"].to_3x3().inverted() @ up
        for side in ("Left", "Right"):
            F = Matrix.Translation(up) @ M[f"{side}Foot"]
            if has_ik:
                ik = f"IK_{side}Foot"
                row[ik] = [R[ik].to_3x3().inverted() @ (F.translation - R[ik].translation),
                           R[ik].to_quaternion().inverted() @ F.to_quaternion()]
            if has_pole:
                hip = M[f"{side}UpperLeg"].translation; knee = M[f"{side}LowerLeg"].translation
                ank = M[f"{side}Foot"].translation
                pn = f"Pole_{side}Knee"
                if knee_follow:
                    ax = (ank - hip).normalized(); d = knee - hip; d = d - ax * d.dot(ax)
                    if d.length < 0.005:
                        d = prevdir.get(side, V((0, -1, 0)))
                    d = d.normalized(); prevdir[side] = d
                    pole = knee + up + d * dst_leg
                    row[pn] = [R[pn].to_3x3().inverted() @ (pole - R[pn].translation), Q()]
                else:
                    row[pn] = [V((0, 0, 0)), Q()]
        for k, v in row.items():
            out.setdefault(k, []).append(v)
    for k, l in out.items():                       # quaternion hemisphere continuity
        for i in range(1, len(l)):
            if l[i][1].dot(l[i - 1][1]) < 0:
                l[i][1] = -l[i][1]

    # IK on
    for side in ("Left", "Right"):
        for bn in (f"{side}LowerLeg", f"{side}Foot"):
            pb = dst.pose.bones.get(bn)
            if pb:
                for c in pb.constraints:
                    c.influence = 1.0

    # --- write action ---
    name = action_name or f"{sact.name}_Hero"
    old = bpy.data.actions.get(name)
    if old:
        bpy.data.actions.remove(old)
    act = bpy.data.actions.new(name); act.use_fake_user = True
    if dst.animation_data is None:
        dst.animation_data_create()
    prev = dst.animation_data.action
    if prev:
        prev.use_fake_user = True
    dst.animation_data.action = act
    slot = None
    if hasattr(act, "slots"):
        slot = dst.animation_data.action_slot or act.slots.new(id_type='OBJECT', name=dst.name)
        dst.animation_data.action_slot = slot
    fcs = _fcurves(act, slot)
    xs = [f - f0 for f in frames]
    for k, l in out.items():
        for prop, cnt, j in (("rotation_quaternion", 4, 1), ("location", 3, 0)):
            for i in range(cnt):
                fc = fcs.new(f'pose.bones["{k}"].{prop}', index=i, action_group=k) \
                    if not hasattr(act, "slots") else fcs.new(f'pose.bones["{k}"].{prop}', index=i, group_name=k)
                fc.keyframe_points.add(len(xs)); co = []
                for x, e in zip(xs, l):
                    co += [x, e[j][i]]
                fc.keyframe_points.foreach_set("co", co); fc.update()

    # --- knee solve: rotate each pole target about the hip-ankle axis until the
    #     IK knee lands on the retargeted (FK) knee. Blender's pole_angle is not a
    #     reliable fixed offset on this rig (differs per leg), so solve numerically.
    if has_pole and has_ik and knee_follow:
        want_knee = [{s: M[f"{s}LowerLeg"].translation + V((0, 0, L)) for s in ("Left", "Right")}
                     for (M, _), L in zip(FK, lift)]
        knee_rep = solve_knees(dst, act, xs, want_knee)
    else:
        knee_rep = None

    # loop detection (first vs last pose)
    loop_err = max(math.degrees(_ydir(S[frames[0]][s].to_3x3()).angle(_ydir(S[frames[-1]][s].to_3x3())))
                   for s in MAP)
    act.use_frame_range = True; act.frame_start = 0; act.frame_end = xs[-1]
    act.use_cyclic = loop_err < 8.0
    scn.frame_start = 0; scn.frame_end = xs[-1]

    rep = verify(src_name, name, f0=f0)
    rep.update(action=name, source=f"{src_name}:{sact.name}", frames=len(xs),
               seconds=round(len(xs) / scn.render.fps, 2), leg_ratio=round(K, 3),
               max_floor_lift_cm=round(max(lift) * 100, 1), loops=act.use_cyclic,
               loop_mismatch_deg=round(loop_err, 1), unmapped_on_hero=missing,
               knee_solve=knee_rep)
    scn.frame_set(0)
    return rep


def _perp(v, ax):
    v = v - ax * v.dot(ax)
    return v.normalized() if v.length > 1e-6 else None

def _signed(ax, a, b):
    return math.atan2(ax.dot(a.cross(b)), a.dot(b))

def solve_knees(dst, act, xs, want_knee=None, iters=6, tol_deg=0.5):
    """Per frame, rotate Pole_<side>Knee keys about the hip->ankle axis until the
    evaluated IK knee points where it should.
    want_knee: list (per x) of {side: armature-space knee position}. If None, the
    target is the current pole direction itself (knee should face its pole)."""
    scn = bpy.context.scene; pb = dst.pose.bones
    R = {b.name: b.matrix_local.copy() for b in dst.data.bones}
    curves = {}
    for side in ("Left", "Right"):
        path = f'pose.bones["Pole_{side}Knee"].location'
        fcs = [fc for fc in _all_fcurves(act) if fc.data_path == path]
        curves[side] = sorted(fcs, key=lambda f: f.array_index)
    worst_before = worst_after = 0.0
    for i, x in enumerate(xs):
        scn.frame_set(x)
        for side in ("Left", "Right"):
            fcs = curves[side]
            if len(fcs) != 3:
                continue
            kp = [next((k for k in fc.keyframe_points if abs(k.co.x - x) < 1e-3), None) for fc in fcs]
            if None in kp:
                continue
            pn = f"Pole_{side}Knee"
            first = None
            for it in range(iters):
                hip = pb[f"{side}UpperLeg"].head.copy(); ank = pb[f"{side}Foot"].head.copy()
                ax = (ank - hip).normalized()
                have = _perp(pb[f"{side}LowerLeg"].head - hip, ax)
                pole_w = pb[pn].head.copy()
                ref = _perp((want_knee[i][side] if want_knee else pole_w) - hip, ax)
                if have is None or ref is None:
                    break
                err = _signed(ax, have, ref)
                if first is None:
                    first = abs(err)
                if abs(math.degrees(err)) < tol_deg:
                    break
                new = hip + Q(ax, err) @ (pole_w - hip)
                loc = R[pn].to_3x3().inverted() @ (new - R[pn].translation)
                for j in range(3):
                    kp[j].co.y = loc[j]; kp[j].handle_left.y = loc[j]; kp[j].handle_right.y = loc[j]
                scn.frame_set(x)
            hip = pb[f"{side}UpperLeg"].head; ank = pb[f"{side}Foot"].head
            ax = (ank - hip).normalized()
            have = _perp(pb[f"{side}LowerLeg"].head - hip, ax)
            ref = _perp((want_knee[i][side] if want_knee else pb[pn].head) - hip, ax)
            if have is not None and ref is not None:
                worst_after = max(worst_after, abs(math.degrees(_signed(ax, have, ref))))
            if first is not None:
                worst_before = max(worst_before, math.degrees(first))
    for side in curves:
        for fc in curves[side]:
            fc.update()
    return dict(knee_err_before_deg=round(worst_before, 1), knee_err_after_deg=round(worst_after, 1))

def _all_fcurves(act):
    try:
        return [fc for l in act.layers for st in l.strips for cb in st.channelbags for fc in cb.fcurves]
    except Exception:
        return list(act.fcurves)


def verify(src_name, action_name, dst_name=DST_RIG, f0=None, step=3):
    """Max world-direction error per mapped bone + lowest evaluated mesh point."""
    scn = bpy.context.scene
    src = bpy.data.objects[src_name]; dst = bpy.data.objects[dst_name]
    MAP = build_map(src)
    act = bpy.data.actions[action_name]
    dst.animation_data.action = act
    if f0 is None:
        f0 = int(src.animation_data.action.frame_range[0])
    # floor check on the shoe geometry only (fast on a many-part character)
    feet = [c for c in _skin_meshes(dst)
            if any(g.name.endswith(("Foot", "Toes")) for g in c.vertex_groups)
            and not (c.name in EXPORT_MESHES and len(_skin_meshes(dst)) > len(EXPORT_MESHES))]
    mesh = bool(feet)
    err = {}; minz = 1e9
    for x in range(0, int(act.frame_range[1]) + 1, step):
        scn.frame_set(x + f0)
        sd = {s: _ydir((src.matrix_world @ src.pose.bones[s].matrix).to_3x3()) for s in MAP}
        scn.frame_set(x)
        for s, d in MAP.items():
            if d in dst.pose.bones:
                e = math.degrees(sd[s].angle(_ydir((dst.matrix_world @ dst.pose.bones[d].matrix).to_3x3())))
                err[d] = max(err.get(d, 0), e)
        dg = bpy.context.evaluated_depsgraph_get()
        for fm in feet:
            ev = fm.evaluated_get(dg); em = ev.to_mesh()
            minz = min(minz, min((fm.matrix_world @ v.co).z for v in em.vertices)); ev.to_mesh_clear()
    body = [v for k, v in err.items() if "Leg" not in k and "Foot" not in k and "Toes" not in k]
    legs = [v for k, v in err.items() if k not in body]
    return dict(max_err_body_deg=round(max(body, default=0), 1),
                max_err_legs_deg=round(max(legs, default=0), 1),
                worst_bones={k: round(v, 1) for k, v in sorted(err.items(), key=lambda kv: -kv[1])[:4]},
                lowest_point_cm=round(minz * 100, 1) if mesh else None)


def export_glb(path=None, dst_name=DST_RIG):
    """Save the .blend, export mesh + rig + hero actions only, then reload the saved file.
    Source skeletons and their actions are stripped from the export copy only."""
    import os
    bpy.ops.wm.save_mainfile()
    dst = bpy.data.objects[dst_name]
    export_set = [bpy.data.objects[n] for n in EXPORT_MESHES if n in bpy.data.objects]
    if export_set:
        keep = {dst.name} | {o.name for o in export_set}      # Ghostboy: Godot export meshes only
    else:
        keep = {dst.name} | {c.name for c in dst.children if c.type == 'MESH'}
    keep_bones = set(b.name for b in dst.data.bones)
    for o in list(bpy.data.objects):
        if o.name not in keep:
            bpy.data.objects.remove(o)
    for a in list(bpy.data.actions):
        if a.name.startswith("GB_QA"):
            bpy.data.actions.remove(a); continue
        paths = set()
        try:
            for l in a.layers:
                for st in l.strips:
                    for cb in st.channelbags:
                        for fc in cb.fcurves:
                            m = re.match(r'pose\.bones\["(.+?)"\]', fc.data_path)
                            if m: paths.add(m.group(1))
        except Exception:
            pass
        if paths and not paths <= keep_bones:
            bpy.data.actions.remove(a)
    name = "GhostBoy.glb" if export_set else "HeroGhost.glb"
    path = path or os.path.join(os.path.dirname(bpy.data.filepath), name)
    for o in bpy.data.objects:   # hidden objects can't be selected -> GLB would lose skin/clips
        o.hide_set(False); o.hide_viewport = False
    for o in bpy.context.view_layer.objects:
        o.select_set(o.name in keep)
    bpy.context.view_layer.objects.active = dst
    bpy.ops.export_scene.gltf(filepath=path, export_format='GLB', use_selection=True,
                              export_skins=True, export_def_bones=True, export_animations=True,
                              export_animation_mode='ACTIONS', export_force_sampling=True,
                              export_frame_range=False, export_apply=False, export_yup=True)
    import json, struct
    with open(path, 'rb') as fh:
        fh.read(12); ln, _ = struct.unpack('<II', fh.read(8)); js = json.loads(fh.read(ln))
    clips = [a['name'] for a in js.get('animations', [])]
    size = round(os.path.getsize(path) / 1e6, 2)
    bpy.ops.wm.revert_mainfile()
    return dict(path=path, clips=clips, mb=size)


# register as a module-like object so the bridge can call it after exec()
import types as _t
_mod = _t.SimpleNamespace(list_sources=list_sources, build_map=build_map, retarget=retarget,
                          verify=verify, export_glb=export_glb, solve_knees=solve_knees)
bpy.app.driver_namespace["hero_retarget"] = _mod
