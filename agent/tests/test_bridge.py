import base64
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
from server import App, Server
from store import Store
from oauth import OAuth, digest
import chat as chat_backend
spec = importlib.util.spec_from_file_location('bridge_core', ROOT / 'agent/runtime/core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def heartbeat(boot='boot_one', scene='scene_one'):
    return {'boot_id':boot, 'scene_id':scene, 'blender_version':'5.2.0', 'native':{'foreground':True}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'state.sqlite')
        self.store = Store(self.path)
        self.store.exchange('ipad', heartbeat())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def submit(self, key='request_01'):
        return self.store.submit('ipad','execute_python',{'code':'result=42'},key,'scene_one')

    def test_lost_delivery_is_not_reissued(self):
        job = self.submit()
        first = self.store.exchange('ipad', heartbeat())
        self.assertEqual(first['job']['job_id'],job['job_id'])
        self.assertIsNone(self.store.exchange('ipad', heartbeat())['job'])
        self.assertEqual(self.submit()['job_id'],job['job_id'])

    def test_ack_loss_keeps_result_and_no_duplicate_execution(self):
        job = self.submit()
        self.store.exchange('ipad', heartbeat())
        completion = {'job_id':job['job_id'],'boot_id':'boot_one','result':{'ok':True,'value':42}}
        first = self.store.exchange('ipad',heartbeat(),completion)
        second = self.store.exchange('ipad',heartbeat(),completion)
        self.assertEqual(first['ack'],second['ack'])
        self.assertEqual(self.store.result('ipad',job['job_id'])['result']['value'],42)

    def test_scene_switch_expires_queued_job(self):
        job = self.submit()
        self.assertIsNone(self.store.exchange('ipad',heartbeat(scene='another'))['job'])
        self.assertEqual(self.store.result('ipad',job['job_id'])['state'],'expired')

    def test_restart_persists_issued_state_without_replaying(self):
        job = self.submit()
        self.store.exchange('ipad',heartbeat())
        self.store.db.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.exchange('ipad',heartbeat(boot='restarted'))['job'])
        self.assertEqual(self.store.result('ipad',job['job_id'])['state'],'issued')

    def test_key_conflict_and_offline_and_device_isolation(self):
        job = self.submit()
        with self.assertRaises(ValueError):
            self.store.submit('ipad','execute_python',{'code':'different'},'request_01','scene_one')
        with self.assertRaises(ValueError):
            self.store.result('other',job['job_id'])
        with self.store.db:
            self.store.db.execute('UPDATE device SET seen=0')
        with self.assertRaisesRegex(ValueError,'offline'):
            self.submit('request_02')

    def test_cancel_and_queue_bound(self):
        job = self.submit()
        self.assertEqual(self.store.cancel('ipad',job['job_id'])['state'],'cancelled')
        for n in range(8):
            self.submit(f'request_{n+10}')
        with self.assertRaisesRegex(ValueError,'queue_full'):
            self.submit('request_overflow')

    def test_expired_issued_is_uncertain_and_late_result_is_accepted(self):
        job = self.submit()
        self.store.exchange('ipad',heartbeat())
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET expires=0')
        self.assertEqual(self.store.result('ipad',job['job_id'])['state'],'uncertain')
        completion={'job_id':job['job_id'],'boot_id':'boot_one','result':{'ok':True,'value':1}}
        self.store.exchange('ipad',heartbeat(),completion)
        self.assertEqual(self.store.result('ipad',job['job_id'])['state'],'completed')

    def test_chat_delivery_is_idempotent_and_cursor_driven(self):
        message={'id':'a'*32,'text':'Move the cube up'}
        first=self.store.chat_exchange('ipad',{'cursor':0,'messages':[message]})
        second=self.store.chat_exchange('ipad',{'cursor':0,'messages':[message]})
        self.assertEqual(first['ack_ids'],[message['id']])
        self.assertEqual(second['ack_ids'],[message['id']])
        count=self.store.db.execute(
            'SELECT count(*) FROM chat_messages WHERE device_id=?',('ipad',)
        ).fetchone()[0]
        self.assertEqual(count,1)

        claimed=self.store.chat_claim('ipad')
        self.assertEqual(claimed,message)
        self.assertIsNone(self.store.chat_claim('ipad'))
        self.store.chat_event('ipad',message['id'],'status','AI working')
        self.store.chat_complete('ipad',message['id'],'Done')
        replay=self.store.chat_exchange('ipad',{'cursor':0,'messages':[]})
        self.assertEqual([event['type'] for event in replay['events']],['status','final'])
        self.assertEqual(replay['events'][-1]['text'],'Done')
        caught_up=self.store.chat_exchange(
            'ipad',{'cursor':replay['cursor'],'messages':[]}
        )
        self.assertEqual(caught_up['events'],[])

    def test_chat_restart_never_replays_running_turn(self):
        message={'id':'b'*32,'text':'Inspect the scene'}
        self.store.chat_exchange('ipad',{'cursor':0,'messages':[message]})
        self.assertEqual(self.store.chat_claim('ipad')['id'],message['id'])
        self.store.db.close()
        self.store=Store(self.path)
        row=self.store.db.execute(
            'SELECT state FROM chat_messages WHERE device_id=? AND message_id=?',
            ('ipad',message['id']),
        ).fetchone()
        self.assertEqual(row['state'],'uncertain')
        replay=self.store.chat_exchange('ipad',{'cursor':0,'messages':[]})
        self.assertEqual(replay['events'][-1]['type'],'error')
        self.assertIn('not replayed',replay['events'][-1]['text'])

    def test_chat_message_id_conflict_and_thread_state(self):
        message_id='c'*32
        self.store.chat_exchange(
            'ipad',{'cursor':0,'messages':[{'id':message_id,'text':'one'}]}
        )
        with self.assertRaisesRegex(ValueError,'reused'):
            self.store.chat_exchange(
                'ipad',{'cursor':0,'messages':[{'id':message_id,'text':'two'}]}
            )
        self.store.chat_set_thread_id('ipad','thr_test')
        self.assertEqual(self.store.chat_thread_id('ipad'),'thr_test')
        self.store.chat_set_thread_id('ipad',None)
        self.assertIsNone(self.store.chat_thread_id('ipad'))
        self.assertTrue(hasattr(chat_backend,'ChatWorker'))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bpy = types.SimpleNamespace(app=types.SimpleNamespace(is_job_running=lambda _:False))
        self.native = types.SimpleNamespace(status=lambda: {'foreground':True})
        self.runtime = core.Runtime(self.bpy,self.native,self.temp.name)
        self.calls=[]
        self.runtime.dispatch=lambda op,args: self.calls.append(op) or {'done':True}
        self.job={'job_id':'command_01','boot_id':self.runtime.boot_id,'scene_id':self.runtime.scene_id,
                  'operation':'execute_python','arguments':{},'expires_at':time.time()+30}

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_never_reexecutes(self):
        first=self.runtime.execute(self.job)
        self.runtime.acknowledge(self.job['job_id'])
        duplicate=self.runtime.execute(self.job)
        self.assertTrue(first['result']['ok'])
        self.assertFalse(duplicate['result']['ok'])
        self.assertEqual(self.calls,['execute_python'])

    def test_disk_journal_survives_crash_and_outbox_survives_restart(self):
        self.runtime.execute(self.job)
        loaded=core.Runtime(self.bpy,self.native,self.temp.name)
        self.assertEqual(loaded.outbox['job_id'],self.job['job_id'])
        loaded.acknowledge(self.job['job_id'])
        loaded.journal['incomplete']={'state':'running'}
        loaded._save()
        restarted=core.Runtime(self.bpy,self.native,self.temp.name)
        self.assertEqual(restarted.journal['incomplete']['state'],'uncertain')

    def test_new_scene_and_expired_commands_do_not_run(self):
        self.runtime.scene_changed()
        value=self.runtime.execute(self.job)
        self.assertFalse(value['result']['ok'])
        self.assertEqual(self.calls,[])

    def test_switching_scenes_without_loading_a_file_is_detected(self):
        self.bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace(as_pointer=lambda:1))
        self.runtime._sync_scene()
        self.bpy.context.scene = types.SimpleNamespace(as_pointer=lambda:2)
        result = self.runtime.execute(self.job)
        self.assertFalse(result['result']['ok'])
        self.assertEqual(self.calls, [])

    def test_script_workspace_and_compare_before_write(self):
        r=self.runtime.write_script('scene.py','result=1')
        self.assertEqual(self.runtime.read_script('scene.py')['sha256'],r['sha256'])
        with self.assertRaises(ValueError):
            self.runtime.write_script('scene.py','result=2')
        self.runtime.write_script('scene.py','result=2',expected_sha256=r['sha256'])
        with self.assertRaises(ValueError):
            self.runtime.read_script('../config.py')
        target=Path(self.temp.name)/'secret.py'; target.write_text('secret')
        (self.runtime.scripts/'link.py').symlink_to(target)
        with self.assertRaises(ValueError):
            self.runtime.read_script('link.py')

    def test_python_timeout_and_stdout_bound(self):
        bpy=types.SimpleNamespace(context=types.SimpleNamespace(view_layer=types.SimpleNamespace(update=lambda:None),window_manager=types.SimpleNamespace(windows=[])))
        self.runtime.bpy=bpy
        result=self.runtime.execute_python('print("x"*50000); result=2')
        self.assertEqual(result['result'],2)
        self.assertLess(len(result['stdout']),25000)
        with self.assertRaisesRegex(RuntimeError,'time limit'):
            self.runtime.execute_python('while True:\n    pass',time_limit=.1)
        self.assertIsNone(sys.gettrace())


class HttpAndOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=App(str(Path(self.temp.name)/'state.sqlite'),'https://relay.example','ipad',
                     'd'*40,'a'*40,'client','c'*40,'o'*40,['https://client.example/callback'])
        self.server=Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.store.db.close(); self.temp.cleanup()

    def request(self,path,body=None,token=None,method='POST',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        h={'Host':'relay.example','Accept':'application/json, text/event-stream','Content-Type':'application/json'}
        if token: h['Authorization']='Bearer '+token
        h.update(headers or {})
        conn.request(method,path,json.dumps(body) if body is not None else None,h)
        r=conn.getresponse(); raw=r.read(); status=r.status; hdr=dict(r.getheaders()); conn.close()
        return status, json.loads(raw) if raw else None, hdr

    def test_auth_separation_and_origin_validation(self):
        rpc={'jsonrpc':'2.0','id':1,'method':'tools/list'}
        self.assertEqual(self.request('/mcp',rpc)[0],401)
        self.assertEqual(self.request('/mcp',rpc,token='d'*40)[0],401)
        self.assertEqual(self.request('/device/exchange',{},token='a'*40)[0],401)
        self.assertEqual(self.request('/mcp',rpc,token='a'*40,headers={'Origin':'https://evil.example'})[0],403)

    def test_mcp_handshake_notification_and_pending_job(self):
        message={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}}
        status,result,_=self.request('/mcp',message,token='a'*40)
        self.assertEqual(status,200); self.assertEqual(result['result']['protocolVersion'],'2025-11-25')
        self.assertEqual(self.request('/mcp',{'jsonrpc':'2.0','method':'notifications/initialized'},token='a'*40)[0],202)
        self.assertEqual(self.request('/mcp',token='a'*40,method='GET')[0],405)
        self.request('/device/exchange',{'protocol':1,'device_id':'ipad','heartbeat':heartbeat()},token='d'*40)
        value=self.app.call('inspect_scene',{'request_id':'inspect_01','scene_id':'scene_one'})
        rpc={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'job_result','arguments':{'job_id':value['job_id']}}}
        status,reply,_=self.request('/mcp',rpc,token='a'*40)
        self.assertEqual(status,200); self.assertFalse(reply['result']['isError'])

    def test_device_exchange_carries_chat_only_when_backend_is_enabled(self):
        self.app.chat=object()
        payload={'protocol':1,'device_id':'ipad','heartbeat':heartbeat(),
                 'chat':{'cursor':0,'messages':[{'id':'d'*32,'text':'hello'}]}}
        status,reply,_=self.request('/device/exchange',payload,token='d'*40)
        self.assertEqual(status,200)
        self.assertEqual(reply['chat']['ack_ids'],['d'*32])

    def test_capture_is_returned_as_mcp_image(self):
        self.app.store.exchange('ipad',heartbeat())
        job=self.app.call('capture',{'request_id':'capture_01','scene_id':'scene_one'})
        self.app.store.exchange('ipad',heartbeat())
        # Protocol fixture, not a claim of device render validation.
        data=base64.b64encode(b'fixture').decode()
        self.app.store.exchange('ipad',heartbeat(),{'job_id':job['job_id'],'boot_id':'boot_one',
            'result':{'ok':True,'value':{'mime_type':'image/png','data':data,'source':'screenshot'}}})
        reply=self.app.rpc({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'job_result','arguments':{'job_id':job['job_id']}}})
        self.assertEqual(reply['result']['content'][1]['type'],'image')
        self.assertEqual(reply['result']['content'][1]['data'],data)

    def test_oauth_pkce_single_use_and_refresh_rotation(self):
        oauth=self.app.oauth
        verifier='v'*64
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params={'client_id':'client','response_type':'code','redirect_uri':'https://client.example/callback',
                'resource':'https://relay.example/mcp','code_challenge_method':'S256','code_challenge':challenge,'state':'original-state'}
        page=oauth.authorize_form(params)
        ticket=re.search('name="ticket" value="([^"]+)"',page)[1]
        with self.assertRaises(ValueError): oauth.approve(ticket,'wrong')
        redirect=oauth.approve(ticket,'o'*40)
        self.assertEqual(oauth.approve(ticket,'o'*40),redirect)
        query=parse_qs(urlsplit(redirect).query)
        self.assertEqual(query['state'],['original-state'])
        self.assertNotIn('iss', query)
        self.assertFalse(oauth.metadata()['authorization_response_iss_parameter_supported'])
        token_params={'client_id':'client','client_secret':'c'*40,'resource':oauth.resource,
                      'grant_type':'authorization_code','code':query['code'][0],
                      'redirect_uri':params['redirect_uri'],'code_verifier':verifier}
        tokens=oauth.token(token_params)
        self.assertTrue(oauth.authenticate(tokens['access_token']))
        with self.assertRaises(ValueError): oauth.token(token_params)
        refresh={**token_params,'grant_type':'refresh_token','refresh_token':tokens['refresh_token']}
        newer=oauth.token(refresh)
        self.assertTrue(oauth.authenticate(newer['access_token']))
        with self.assertRaises(ValueError): oauth.token(refresh)
        oauth.revision='changed'
        self.assertFalse(oauth.authenticate(newer['access_token']))
        with self.assertRaises(ValueError): oauth.authorize_form({**params,'redirect_uri':'https://evil.example'})

    def test_oauth_owner_key_accepts_markdown_escaped_underscores(self):
        oauth = self.app.oauth
        params = {'client_id':'client', 'response_type':'code',
                  'redirect_uri':'https://client.example/callback',
                  'code_challenge_method':'S256', 'code_challenge':'a'*43,
                  'resource':oauth.resource, 'scope':'blender'}
        oauth.owner_key = 'owner_key_with_underscores'
        oauth.revision = digest(oauth.client_secret + '\n' + oauth.owner_key)
        page = oauth.authorize_form(params)
        ticket = re.search(r'name="ticket" value="([^"]+)"', page).group(1)
        target = oauth.approve(ticket, r'owner\_key\_with\_underscores')
        self.assertTrue(target.startswith('https://client.example/callback?'))

    def test_oauth_public_client_pkce_exchange(self):
        oauth = self.app.oauth
        self.assertIn('none', oauth.metadata()['token_endpoint_auth_methods_supported'])
        verifier = 'p' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {'client_id':'client', 'response_type':'code',
                  'redirect_uri':'https://client.example/callback',
                  'code_challenge_method':'S256', 'code_challenge':challenge,
                  'resource':oauth.resource, 'scope':'blender'}
        page = oauth.authorize_form(params)
        ticket = re.search(r'name="ticket" value="([^"]+)"', page).group(1)
        redirect = oauth.approve(ticket, 'o' * 40)
        code = parse_qs(urlsplit(redirect).query)['code'][0]
        tokens = oauth.token({'client_id':'client', 'client_secret':'stale-registration-secret',
                              'resource':oauth.resource, 'grant_type':'authorization_code',
                              'code':code, 'redirect_uri':params['redirect_uri'],
                              'code_verifier':verifier})
        self.assertTrue(oauth.authenticate(tokens['access_token']))


if __name__ == '__main__':
    unittest.main()
