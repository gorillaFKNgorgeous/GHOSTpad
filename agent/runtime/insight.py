# SPDX-License-Identifier: GPL-2.0-or-later
"""Embedded GhostBlender conversation and workspace-insight UI.

The chat protocol deliberately rides the existing authenticated device exchange.
It does not start a second network poller or execute Blender work off the main
thread.
"""
import os
import textwrap
import time
import uuid

import bpy

_REPORTS = {}
_MESSAGES = []
_NEXT_MESSAGE_ID = 1
_CHAT_CURSOR = 0
_CHAT_STATUS = "Waiting for chat relay"
_CHAT_ONLINE = False
_PENDING = []
_REPORT_LIMIT = 12
_ASSET_LIMIT = 100


def append_message(role, text, source="local"):
    if role not in {"user", "assistant", "system"}:
        raise ValueError("invalid message role")
    if source not in {"local", "bridge", "model"}:
        raise ValueError("invalid message source")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty message")
    global _NEXT_MESSAGE_ID
    message = {
        "id": _NEXT_MESSAGE_ID,
        "role": role,
        "source": source,
        "text": text.strip()[:2000],
        "created_at": time.time(),
    }
    _NEXT_MESSAGE_ID += 1
    _MESSAGES.append(message)
    del _MESSAGES[:-30]
    _redraw()
    return message["id"]


def list_messages(after_id=0):
    return [dict(item) for item in _MESSAGES if item["id"] > after_id]


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def chat_payload():
    """Return the chat extension added to the next normal device heartbeat."""
    return {
        "cursor": _CHAT_CURSOR,
        "messages": [dict(_PENDING[0])] if _PENDING else [],
    }


def apply_chat(chat):
    """Consume a chat extension from a normal relay response on Blender's main thread."""
    global _CHAT_CURSOR, _CHAT_ONLINE, _CHAT_STATUS
    if chat is None:
        _CHAT_ONLINE = False
        _CHAT_STATUS = "Chat service unavailable on relay"
        _redraw()
        return
    if not isinstance(chat, dict):
        _CHAT_ONLINE = False
        _CHAT_STATUS = "Chat response error"
        _redraw()
        return

    _CHAT_ONLINE = True
    for message_id in chat.get("ack_ids", []):
        _PENDING[:] = [message for message in _PENDING if message["id"] != message_id]

    for event in chat.get("events", []):
        if not isinstance(event, dict):
            continue
        seq = event.get("seq", 0)
        if type(seq) is not int or seq <= _CHAT_CURSOR:
            continue
        _CHAT_CURSOR = seq
        event_type = event.get("type")
        content = str(event.get("text", ""))
        if event_type == "status":
            _CHAT_STATUS = content[:100]
        elif event_type == "final" and content.strip():
            append_message("assistant", content, "model")
            _CHAT_STATUS = "Ready"
        elif event_type == "error":
            append_message("system", content or "Chat error", "bridge")
            _CHAT_STATUS = "Chat error"

    cursor = chat.get("cursor", _CHAT_CURSOR)
    if type(cursor) is int:
        _CHAT_CURSOR = max(_CHAT_CURSOR, cursor)
    if _CHAT_STATUS in {"Waiting for chat relay", "Chat service unavailable on relay"}:
        _CHAT_STATUS = "Ready"
    _redraw()


def _bridge_status():
    try:
        import ghostbridge

        runtime = getattr(ghostbridge, "_runtime", None)
        status = getattr(ghostbridge, "_status", "Unavailable")
        configured = bool(getattr(ghostbridge, "_config", {}).get("enabled"))
        recent = list(runtime.journal.values())[-1] if runtime else None
        return {
            "status": str(status),
            "configured": configured,
            "waiting": bool(getattr(ghostbridge, "_waiting", False)),
            "task": (
                {
                    "operation": str(recent.get("operation", "command")),
                    "state": str(recent.get("state", "unknown")),
                }
                if recent
                else None
            ),
        }
    except Exception:
        return {
            "status": "Bridge unavailable",
            "configured": False,
            "waiting": False,
            "task": None,
        }


def _scene_report(context):
    scene = context.scene
    objects = scene.objects
    counts = {}
    for obj in objects:
        counts[obj.type] = counts.get(obj.type, 0) + 1
    selected = [obj.name for obj in context.selected_objects]
    return {
        "kind": "scene",
        "scene_pointer": scene.as_pointer(),
        "name": scene.name,
        "total": len(objects),
        "types": sorted(counts.items()),
        "collections": len(bpy.data.collections),
        "camera": scene.camera.name if scene.camera else "None",
        "frame": scene.frame_current,
        "selected": selected[:_REPORT_LIMIT],
        "selected_more": max(0, len(selected) - _REPORT_LIMIT),
    }


def _object_report(context):
    obj = context.active_object
    if obj is None:
        return {
            "kind": "object",
            "scene_pointer": context.scene.as_pointer(),
            "name": None,
        }
    report = {
        "kind": "object",
        "scene_pointer": context.scene.as_pointer(),
        "name": obj.name,
        "type": obj.type,
        "location": tuple(round(float(x), 3) for x in obj.location),
        "dimensions": tuple(round(float(x), 3) for x in obj.dimensions),
        "materials": [
            slot.material.name for slot in obj.material_slots if slot.material
        ][:_REPORT_LIMIT],
        "modifiers": [mod.name for mod in obj.modifiers][:_REPORT_LIMIT],
    }
    if obj.type == "MESH":
        report["geometry"] = (len(obj.data.vertices), len(obj.data.polygons))
    return report


def _external_assets():
    groups = (
        ("Image", bpy.data.images),
        ("Sound", bpy.data.sounds),
        ("Movie", bpy.data.movieclips),
        ("Font", bpy.data.fonts),
        ("Library", bpy.data.libraries),
        ("Cache", bpy.data.cache_files),
    )
    missing = []
    inspected = 0
    unchecked = 0
    for label, datablocks in groups:
        for block in datablocks:
            raw = getattr(block, "filepath", "")
            if not raw or getattr(block, "packed_file", None):
                continue
            if label == "Image" and getattr(block, "source", "") in {"GENERATED", "VIEWER"}:
                continue
            if label == "Font" and raw == "<builtin>":
                continue
            path = bpy.path.abspath(raw, library=getattr(block, "library", None))
            if label == "Image" and ("<UDIM>" in path or "#" in path):
                unchecked += 1
                continue
            inspected += 1
            if not os.path.exists(path):
                missing.append((label, block.name, raw))
    return {
        "kind": "assets",
        "inspected": inspected,
        "unchecked": unchecked,
        "missing": missing[:_ASSET_LIMIT],
        "more": max(0, len(missing) - _ASSET_LIMIT),
    }


_COLLECTORS = {
    "scene": _scene_report,
    "object": _object_report,
    "assets": lambda context: _external_assets(),
}


class GHOSTBLENDER_OT_insight_report(bpy.types.Operator):
    bl_idname = "ghostblender.insight_report"
    bl_label = "Refresh report"
    bl_description = "Read Blender data into the local GhostBlender sidebar"
    bl_options = {"INTERNAL"}

    kind: bpy.props.EnumProperty(
        items=[
            ("scene", "Scene", ""),
            ("object", "Active Object", ""),
            ("assets", "Missing Files", ""),
        ]
    )

    def execute(self, context):
        try:
            data = _COLLECTORS[self.kind](context)
        except Exception as exc:
            self.report({"ERROR"}, f"Report failed: {type(exc).__name__}: {exc}")
            return {"CANCELLED"}
        _REPORTS[self.kind] = (time.time(), data)
        self.report({"INFO"}, f"{self.kind.title()} report refreshed")
        return {"FINISHED"}


class GHOSTBLENDER_OT_add_note(bpy.types.Operator):
    bl_idname = "ghostblender.add_local_note"
    bl_label = "Add Local Note"
    bl_description = "Keep the text in this Blender session; it is not sent to an AI"

    def execute(self, context):
        text = context.window_manager.ghostblender_note.strip()
        if not text:
            self.report({"WARNING"}, "Enter a note first")
            return {"CANCELLED"}
        append_message("user", text, "local")
        context.window_manager.ghostblender_note = ""
        return {"FINISHED"}


class GHOSTBLENDER_OT_send_chat(bpy.types.Operator):
    bl_idname = "ghostblender.send_chat"
    bl_label = "Send to AI"
    bl_description = "Queue this message for the connected GhostBlender chat relay"

    def execute(self, context):
        text = context.window_manager.ghostblender_note.strip()
        if not text:
            self.report({"WARNING"}, "Enter a message first")
            return {"CANCELLED"}
        if not _CHAT_ONLINE or not _bridge_status()["configured"]:
            self.report({"ERROR"}, "Chat relay is not connected; message was not sent")
            return {"CANCELLED"}
        if _PENDING:
            self.report({"WARNING"}, "Wait for the current message to be accepted")
            return {"CANCELLED"}
        _PENDING.append({"id": uuid.uuid4().hex, "text": text[:2000]})
        append_message("user", text[:2000], "local")
        context.window_manager.ghostblender_note = ""
        return {"FINISHED"}


class GHOSTBLENDER_PT_conversation(bpy.types.Panel):
    bl_idname = "GHOSTBLENDER_PT_conversation"
    bl_label = "Conversation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GhostBlender"
    bl_parent_id = "GHOSTBRIDGE_PT_connection"

    def draw(self, context):
        layout = self.layout
        layout.label(
            text=_CHAT_STATUS[:70],
            icon="LINKED" if _CHAT_ONLINE else "INFO",
        )
        if _PENDING:
            layout.label(text="Message queued; awaiting relay")
        for message in _MESSAGES[-8:]:
            box = layout.box()
            box.label(text=f"{message['role'].title()} · {message['source']}")
            for line in message["text"].splitlines()[:6]:
                for wrapped in textwrap.wrap(line, 42)[:8]:
                    box.label(text=wrapped)
        layout.prop(context.window_manager, "ghostblender_note", text="Message")
        row = layout.row(align=True)
        row.enabled = _CHAT_ONLINE
        row.operator("ghostblender.send_chat", icon="FORWARD")
        layout.operator("ghostblender.add_local_note", icon="ADD")


def _report_box(layout, context, kind, title, icon):
    box = layout.box()
    row = box.row(align=True)
    row.label(text=title, icon=icon)
    op = row.operator("ghostblender.insight_report", text="", icon="FILE_REFRESH")
    op.kind = kind
    cached = _REPORTS.get(kind)
    if not cached:
        box.label(text="Tap refresh to inspect")
        return
    _, data = cached
    if kind != "assets" and data["scene_pointer"] != context.scene.as_pointer():
        box.label(text="Scene changed; refresh report")
        return
    if kind == "scene":
        box.label(text=f"{data['name']} · {data['total']} objects")
        box.label(text=f"Camera: {data['camera']} · Frame: {data['frame']}")
        box.label(text=f"Collections: {data['collections']}")
        for obj_type, count in data["types"]:
            box.label(text=f"{obj_type}: {count}")
        box.label(text=f"Selected: {len(data['selected']) + data['selected_more']}")
        for name in data["selected"]:
            box.label(text=name, icon="DOT")
        if data["selected_more"]:
            box.label(text=f"+{data['selected_more']} more")
    elif kind == "object":
        if not data["name"]:
            box.label(text="No active object")
            return
        box.label(text=f"{data['name']} ({data['type']})")
        box.label(text="Location: " + ", ".join(map(str, data["location"])))
        box.label(text="Size: " + ", ".join(map(str, data["dimensions"])))
        if "geometry" in data:
            verts, faces = data["geometry"]
            box.label(text=f"Mesh: {verts} vertices, {faces} faces")
        box.label(
            text=f"Materials: {len(data['materials'])} · Modifiers: {len(data['modifiers'])}"
        )
        for name in data["materials"]:
            box.label(text=name, icon="MATERIAL")
        for name in data["modifiers"]:
            box.label(text=name, icon="MODIFIER")
    else:
        missing = data["missing"]
        box.label(
            text=f"Checked {data['inspected']} paths · {len(missing) + data['more']} missing"
        )
        if data["unchecked"]:
            box.label(text=f"{data['unchecked']} sequence/tile paths unchecked")
        for label, name, path in missing[:_REPORT_LIMIT]:
            box.label(text=f"{label}: {name}", icon="ERROR")
            box.label(text=path[:70])
        remaining = len(missing) - _REPORT_LIMIT + data["more"]
        if remaining > 0:
            box.label(text=f"+{remaining} more")


class GHOSTBLENDER_PT_insight(bpy.types.Panel):
    bl_idname = "GHOSTBLENDER_PT_insight"
    bl_label = "Workspace Insight"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GhostBlender"
    bl_parent_id = "GHOSTBRIDGE_PT_connection"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        status = _bridge_status()
        layout.label(
            text=status["status"][:70],
            icon="LINKED" if status["configured"] else "UNLINKED",
        )
        task = status["task"]
        if task:
            layout.label(text=f"Last task: {task['operation']} · {task['state']}")
        else:
            layout.label(text="No task recorded")
        _report_box(layout, context, "scene", "Scene", "SCENE_DATA")
        _report_box(layout, context, "object", "Active Object", "OBJECT_DATA")
        _report_box(layout, context, "assets", "Missing Files", "FILE_FOLDER")


_CLASSES = (
    GHOSTBLENDER_OT_insight_report,
    GHOSTBLENDER_OT_add_note,
    GHOSTBLENDER_OT_send_chat,
    GHOSTBLENDER_PT_conversation,
    GHOSTBLENDER_PT_insight,
)


def register():
    bpy.types.WindowManager.ghostblender_note = bpy.props.StringProperty(
        name="Message",
        description="Message for the embedded GhostBlender assistant",
        options={"SKIP_SAVE"},
    )
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    global _CHAT_ONLINE
    _CHAT_ONLINE = False
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.WindowManager, "ghostblender_note"):
        del bpy.types.WindowManager.ghostblender_note
    _REPORTS.clear()
    _MESSAGES.clear()
    _PENDING.clear()
