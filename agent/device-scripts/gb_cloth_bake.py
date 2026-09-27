"""
gb_cloth_bake.py - record Ghostboy's cloth sheet simulation onto the Mask skirt bones
so Godot (no cloth solver) plays the same drape as Blender.

Usage over the bridge (each call is short, so a slow frame can't freeze Blender for long):
    cb = bpy.app.driver_namespace["gb_cloth_bake"]
    cb.prepare()                       # once per file: production mask -> Head-only skin
    cb.setup("Walk_Swagger")           # shifts keys by a pre-roll, adds cycles, resets cache
    while not cb.step(120): pass       # simulate + record 120 frames per call
    rep = cb.finish()                  # restore clip timing, write Mask_* keys, report
"""
import bpy, math
from mathutils import Vector as V, Quaternion as Q, Matrix

RIG = "GB_Rig"
MASK = "GB_Mask • cloth shell"
CLOTH = "Cloth • skull-draped mask"
NS = "gb_cloth_bake_state"


def _fcurves(act):
    return [fc for l in act.layers for st in l.strips for cb in st.channelbags for fc in cb.fcurves]


def _chains(rig):
    out = []
    for b in rig.data.bones:
        if b.name.startswith("Mask_") and b.name.endswith("_0"):
            c1 = rig.data.bones.get(b.name[:-1] + "1")
            out.append((b.name, c1.name if c1 else None))
    return sorted(out)


def prepare():
    """Production cloth mask: all skin weight on Head (cloth does the motion).
    Keeps Cloth_Pin. Idempotent."""
    m = bpy.data.objects[MASK]
    for g in list(m.vertex_groups):
        if g.name.startswith("Mask_"):
            m.vertex_groups.remove(g)
    head = m.vertex_groups.get("Head") or m.vertex_groups.new(name="Head")
    head.add(list(range(len(m.data.vertices))), 1.0, 'REPLACE')
    return sorted(g.name for g in m.vertex_groups)


def setup(action_name, preroll=None, loop=None):
    scn = bpy.context.scene
    rig = bpy.data.objects[RIG]; m = bpy.data.objects[MASK]
    act = bpy.data.actions[action_name]
    loop = act.use_cyclic if loop is None else loop
    end = act.frame_end
    # Loops: pre-roll exactly one full cycle, so the simulation starts from the clip's
    # own (calm) first pose and the recorded pass is the second, settled one.
    # Starting mid-move can begin with an arm inside the sheet and snag it.
    P = int(preroll if preroll is not None else (math.ceil(end) if loop else 45))
    # drop old Mask_* keys in this clip (re-bake safe)
    for l in act.layers:
        for strip in l.strips:
            for cb in strip.channelbags:
                for fc in [c for c in cb.fcurves if 'pose.bones["Mask_' in c.data_path]:
                    cb.fcurves.remove(fc)
    fcs = _fcurves(act)
    for fc in fcs:
        for k in fc.keyframe_points:
            k.co.x += P; k.handle_left.x += P; k.handle_right.x += P
        if loop:
            mod = fc.modifiers.new('CYCLES')
            if fc.data_path.startswith('pose.bones["Root"].location'):
                mod.mode_before = 'REPEAT_OFFSET'; mod.mode_after = 'REPEAT_OFFSET'
        fc.update()
    rig.animation_data.action = act
    rig.animation_data.action_slot = act.slots[0]
    # evaluated mask must keep original vertex order -> hide subdivision/solidify
    hidden = []
    for md in m.modifiers:
        if md.type in ('SUBSURF', 'SOLIDIFY') and md.show_viewport:
            md.show_viewport = False; hidden.append(md.name)
    cl = m.modifiers[CLOTH]; cl.show_viewport = True
    total = int(math.ceil(end)) + P
    cl.point_cache.frame_start = 0; cl.point_cache.frame_end = total + 1
    scn.frame_start = 0; scn.frame_end = total
    # tracked fabric: for each skirt bone, the vertices in its angular slice of the
    # sheet and height band. Their centroid (rest vs now) drives the bone, so the
    # jagged hem notches can't bias it and rest maps exactly to identity.
    toarm = rig.matrix_world.inverted() @ m.matrix_world
    rest = [toarm @ v.co for v in m.data.vertices]
    hem_z = min(p.z for p in rest)
    chains = []
    ch = _chains(rig)
    for b0, b1 in ch:
        h = rig.data.bones[b0].head_local
        ang = math.atan2(h.y, h.x)
        half = math.pi / len(ch)
        t0z = rig.data.bones[b0].tail_local.z
        def in_slice(p):
            d = math.atan2(p.y, p.x) - ang
            d = (d + math.pi) % (2 * math.pi) - math.pi
            return abs(d) <= half
        s0 = [i for i, p in enumerate(rest) if in_slice(p) and abs(p.z - t0z) <= 0.035]
        s1 = [i for i, p in enumerate(rest) if in_slice(p) and hem_z - 0.01 <= p.z <= t0z - 0.06] if b1 else []
        c0 = sum((rest[i] for i in s0), V()) / max(1, len(s0))
        c1 = sum((rest[i] for i in s1), V()) / max(1, len(s1)) if s1 else None
        chains.append((b0, b1, s0, s1, c0, c1))
    bpy.app.driver_namespace[NS] = dict(action=action_name, P=P, loop=loop, end=end, total=total,
                                         next=0, hidden=hidden, chains=chains, rec={})
    scn.frame_set(0)
    return dict(preroll=P, loop=loop, sim_frames=total + 1, chains=len(chains),
                slice_sizes=[(len(c[2]), len(c[3])) for c in chains])


def _aim(parent_pose, R_parent, R_bone, target_rest, target_now):
    """Rotate a bone (head following its parent) so the point it carried at rest
    (target_rest, armature space) now lies towards target_now.
    Returns (pose-space matrix, local quaternion)."""
    base = parent_pose @ (R_parent.inverted() @ R_bone)
    local = R_bone.inverted() @ target_rest          # rest target in bone-local coords
    carried = base @ local                            # where it would be with no rotation
    a = carried - base.translation
    d = target_now - base.translation
    if a.length < 1e-6 or d.length < 0.45 * a.length:
        # fabric folded back onto the pivot: direction is meaningless -> hold (None)
        return base, None
    qw = a.normalized().rotation_difference(d.normalized())
    rot = base.to_quaternion()
    ql = rot.inverted() @ qw @ rot
    return base @ ql.to_matrix().to_4x4(), ql


def _ang(a, b):
    return math.degrees(2 * math.acos(min(1.0, abs(a.dot(b)))))


def _to_v(q):
    """Quaternion -> rotation vector (axis * angle, radians), shortest rotation."""
    q = q.normalized()
    if q.w < 0:
        q = -q
    axis, ang = q.to_axis_angle()
    return V(axis) * ang


def _to_q(v):
    a = v.length
    return Q() if a < 1e-9 else Q(v / a, a)


def _clean(qs, loop, max_step=12.0, max_total=100.0, close=15):
    """Fill held frames, cap total swing, rate-limit per frame, close loops.
    Works in rotation-vector space: the +/-max_total range is a ball there, so a panel
    swinging from one side to the other passes through hanging straight (as fabric
    does) instead of 'over the top' through 180 degrees."""
    n = len(qs)
    valid = [i for i, q in enumerate(qs) if q is not None]
    if not valid:
        return [Q() for _ in qs]
    step = math.radians(max_step); cap = math.radians(max_total)
    vs = [None if q is None else _to_v(q) for q in qs]
    for i in range(n):
        if vs[i] is None:
            p = max((j for j in valid if j < i), default=None)
            nx = min((j for j in valid if j > i), default=None)
            if p is None: vs[i] = vs[nx].copy()
            elif nx is None: vs[i] = vs[p].copy()
            else: vs[i] = vs[p].lerp(vs[nx], (i - p) / (nx - p))
    for i in range(n):
        if vs[i].length > cap:
            vs[i] = vs[i] * (cap / vs[i].length)
    def limited(seq, start):
        res = []; prev = start
        for v in seq:
            d = v - prev
            cur = prev + d * (step / d.length) if d.length > step else v.copy()
            res.append(cur); prev = cur
        return res
    r = limited(vs, vs[0])
    if loop:
        r = limited(vs, r[-1])                  # second lap starts where the first ended
        k = min(close, n // 3)
        for j in range(k):                      # ease the tail into frame 0
            t = (j + 1) / (k + 1); t = t * t * (3 - 2 * t)
            i = n - k + j
            r[i] = r[i].lerp(r[0], t)
    return [_to_q(v) for v in r]


def step(n=120):
    st = bpy.app.driver_namespace[NS]
    scn = bpy.context.scene
    rig = bpy.data.objects[RIG]; m = bpy.data.objects[MASK]
    R = {b.name: b.matrix_local.copy() for b in rig.data.bones}
    toarm = rig.matrix_world.inverted() @ m.matrix_world
    f = st['next']
    last = min(st['total'], f + n - 1)
    while f <= last:
        scn.frame_set(f)
        if f >= st['P']:
            dg = bpy.context.evaluated_depsgraph_get()
            ev = m.evaluated_get(dg); me = ev.to_mesh()
            head = rig.pose.bones["Head"].matrix
            keys = {}
            vs = me.vertices
            prev = st['rec'].get(f - st['P'] - 1, {})
            for b0, b1, s0, s1, c0, c1 in st['chains']:
                p0 = sum((toarm @ vs[i].co for i in s0), V()) / max(1, len(s0))
                M0, q0 = _aim(head, R["Head"], R[b0], c0, p0)
                if q0 is None and prev.get(b0) is not None:
                    # held: rebuild the parent matrix from the last usable rotation
                    base = head @ (R["Head"].inverted() @ R[b0])
                    M0 = base @ prev[b0].to_matrix().to_4x4()
                keys[b0] = q0
                if b1 and s1:
                    p1 = sum((toarm @ vs[i].co for i in s1), V()) / len(s1)
                    _, q1 = _aim(M0, R[b0], R[b1], c1, p1)
                    keys[b1] = q1
            ev.to_mesh_clear()
            st['rec'][f - st['P']] = keys
        f += 1
    st['next'] = f
    return f > st['total']


def finish():
    st = bpy.app.driver_namespace[NS]
    rig = bpy.data.objects[RIG]; m = bpy.data.objects[MASK]
    act = bpy.data.actions[st['action']]; P = st['P']
    for fc in _fcurves(act):
        for md in list(fc.modifiers):
            if md.type == 'CYCLES':
                fc.modifiers.remove(md)
        for k in fc.keyframe_points:
            k.co.x -= P; k.handle_left.x -= P; k.handle_right.x -= P
        fc.update()
    from bpy_extras import anim_utils
    cb = anim_utils.action_ensure_channelbag_for_slot(act, act.slots[0])
    frames = sorted(x for x in st['rec'] if x <= math.ceil(st['end']))
    bones = sorted({b for x in frames for b in st['rec'][x]})
    maxang = 0.0; maxjump = 0.0; seam = 0.0; held = 0
    for b in bones:
        raw = [st['rec'][x].get(b) for x in frames]
        held += sum(1 for q in raw if q is None)
        qs = _clean([q.copy() if q is not None else None for q in raw], st['loop'])
        for i in range(1, len(qs)):
            if qs[i].dot(qs[i - 1]) < 0:
                qs[i] = -qs[i]
        maxang = max(maxang, max(_ang(Q(), q) for q in qs))
        maxjump = max(maxjump, max((_ang(qs[i - 1], qs[i]) for i in range(1, len(qs))), default=0))
        if st['loop']:
            seam = max(seam, _ang(qs[0], qs[-1]))
        for i in range(4):
            fc = cb.fcurves.new(f'pose.bones["{b}"].rotation_quaternion', index=i, group_name=b)
            fc.keyframe_points.add(len(frames))
            co = []
            for x, q in zip(frames, qs):
                co += [x, q[i]]
            fc.keyframe_points.foreach_set("co", co)
            for k in fc.keyframe_points:
                k.interpolation = 'LINEAR'
            fc.update()
    for name in st['hidden']:
        m.modifiers[name].show_viewport = True
    scn = bpy.context.scene
    m.modifiers[CLOTH].point_cache.frame_start = 0
    m.modifiers[CLOTH].point_cache.frame_end = int(math.ceil(st['end'])) + 1
    scn.frame_start = 0; scn.frame_end = int(math.ceil(st['end']))
    rep = dict(action=act.name, keyed_frames=len(frames), bones=len(bones),
               max_swing_deg=round(maxang, 1), max_step_deg=round(maxjump, 1),
               loop_seam_deg=round(seam, 1) if st['loop'] else None, held_samples=held)
    bpy.app.driver_namespace.pop(NS, None)
    return rep


import types as _t
bpy.app.driver_namespace["gb_cloth_bake"] = _t.SimpleNamespace(prepare=prepare, setup=setup, step=step, finish=finish)
