# Run through Ghostblender Simple execute_python, which supplies scripts_dir.
# Requires a saved scene and an idle render job. Runs the current frame unchanged.
# Stage memory measurements are snapshots, not continuous peak measurements.
import bpy
if bpy.data.is_dirty or not bpy.data.filepath:
    raise RuntimeError('Save the current scene before this diagnostic render')
if bpy.app.is_job_running('RENDER'):
    raise RuntimeError('A render is already running')
import bpy, os, json, time
import _ghostbridge_transport as native
from pathlib import Path
root=Path(scripts_dir).parent/'render-investigation-2026-09-13'
root.mkdir(exist_ok=True)
log=root/'events.jsonl'
def record(stage):
    row={'stage':stage,'time':time.time(),'memory':native.status()}
    with open(log,'a') as f:
        f.write(json.dumps(row)+'\n'); f.flush(); os.fsync(f.fileno())
def make_handler(stage):
    def handler(*args):
        record(stage)
    return handler
handlers=[]
for stage in ['render_init','render_pre','render_post','render_complete','render_cancel']:
    fn=make_handler(stage)
    getattr(bpy.app.handlers,stage).append(fn)
    handlers.append((stage,fn))
def run():
    record('sync_before')
    try:
        outcome=bpy.ops.render.render('EXEC_DEFAULT')
        record('sync_return_'+str(outcome))
    except Exception as exc:
        record('sync_error_'+str(exc))
    finally:
        for stage,fn in handlers:
            if fn in getattr(bpy.app.handlers,stage): getattr(bpy.app.handlers,stage).remove(fn)
    return None
record('armed_unchanged_baseline')
bpy.app.timers.register(run,first_interval=2.0)
result={'log':str(log),'test':'unchanged synchronous render','original_saved':not bpy.data.is_dirty}
