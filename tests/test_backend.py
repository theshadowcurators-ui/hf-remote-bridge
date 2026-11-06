import io, tempfile, unittest, os, json, sys, hashlib, http.client, threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'backend'))
from vault import Vault
from server import Handler,ensure_token
from http.server import ThreadingHTTPServer

class BackendTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.v=Vault(self.tmp.name)
 def tearDown(self):
  self.v.close();self.tmp.cleanup()
 def test_persist_restart(self):
  self.v.add_project('fox');self.v.add_generation('fox','v001');h=self.v.import_bytes('fox','v001','EXPORT_MP4',io.BytesIO(b'original mp4 bytes'),'video/mp4')
  self.assertEqual(h['status'],'SAVED_VERIFIED');self.assertTrue(self.v.verify()['pass'])
  self.v.close();self.v=Vault(self.tmp.name);self.assertEqual(self.v.status()['asset_count'],1);self.assertTrue(self.v.verify()['pass'])
 def test_dedupe_file(self):
  self.v.add_project('fox');self.v.add_generation('fox','v001');self.v.add_generation('fox','v002')
  a=self.v.import_bytes('fox','v001','RAW_MP4',io.BytesIO(b'abc'),'video/mp4');b=self.v.import_bytes('fox','v002','RAW_MP4',io.BytesIO(b'abc'),'video/mp4');self.assertEqual(a['sha256'],b['sha256'])
  self.assertEqual(len(list((Path(self.tmp.name)/'blobs').rglob('*.mp4'))),1)
 def test_last_ten_after_fifty(self):
  self.v.add_project('fox')
  for i in range(50):
   self.v.add_generation('fox',f'v{i:03}',created_at=f'2026-10-01T00:{i:02}:00Z')
   self.v.import_bytes('fox',f'v{i:03}','EXPORT_MP4',io.BytesIO(f'video{i}'.encode()),'video/mp4')
  p=self.v.retention_plan();self.assertEqual(len(p['prune_generation_ids']),40)
  res=self.v.prune(confirmed_new_index=True)
  self.assertEqual(len(res['retired_generation_ids']),40);self.assertEqual(len(res['media_deletion_receipts']),40)
  self.assertEqual(self.v.status()['generation_count'],10);self.assertEqual(self.v.status()['asset_count'],10)
  self.assertTrue(self.v.verify()['pass'])
 def test_project_cap_five(self):
  for i in range(8):
   name=f'p{i:02}';self.v.add_project(name,created_at=f'2026-10-{i+1:02}T00:00:00Z')
   self.v.add_generation(name,f'v{i:03}',created_at=f'2026-10-01T00:{i:02}:00Z')
  r=self.v.prune(confirmed_new_index=True);self.assertEqual(self.v.status()['project_count'],5);self.assertEqual(len(r['retired_generation_ids']),3)
 def test_integrity_failure_blocks_retention_even_if_client_says_verified(self):
  self.v.add_project('fox')
  for i in range(12):
   gid=f'v{i:03}';self.v.add_generation('fox',gid,created_at=f'2026-10-01T00:{i:02}:00Z')
   self.v.import_bytes('fox',gid,'EXPORT_MP4',io.BytesIO(f'data{i}'.encode()),'video/mp4')
  asset=self.v.inventory()['assets'][0]
  (Path(self.tmp.name)/asset['relative_path']).write_bytes(b'CORRUPTED')
  with self.assertRaisesRegex(ValueError,'SERVER_SIDE_VAULT_INTEGRITY_FAILED'):
   self.v.prune(confirmed_new_index=True)
  self.assertEqual(self.v.status()['generation_count'],12)
 def test_no_prune_without_readback(self):
  self.v.add_project('fox')
  with self.assertRaises(ValueError):self.v.prune(confirmed_new_index=False)
 def test_preserve_active_generation(self):
  self.v.add_project('fox')
  for i in range(12): self.v.add_generation('fox',f'v{i:03}',created_at=f'2026-11-01T00:{i:02}:00Z')
  res=self.v.prune(confirmed_new_index=True,active_generation_ids=['v000']);self.assertIn({'generation_id':'v000','state':'DELETION_BLOCKED_ACTIVE_EDIT'},res['blocked']);self.assertEqual(self.v.status()['generation_count'],11)
 def test_idempotent_queue(self):
  a=self.v.enqueue('op1','SAVE_CHECKPOINT','fox','v001');b=self.v.enqueue('op1','SAVE_CHECKPOINT','fox','v001');self.assertFalse(a['idempotent']);self.assertTrue(b['idempotent'])
  with self.assertRaises(ValueError):self.v.receipt('op1','SAVED_VERIFIED')
  self.assertEqual(self.v.receipt('op1','SAVED_VERIFIED',{'sha256':'abc'})['state'],'SAVED_VERIFIED')
  self.assertTrue(self.v.verify()['pass'])
 def test_auto_cleanup_only_after_completed_media_bundle(self):
  self.v.add_project('fox')
  for i in range(12):
   gid=f'v{i:03}';self.v.add_generation('fox',gid,created_at=f'2026-10-01T00:{i:02}:00Z')
   if i<11:
    self.v.import_bytes('fox',gid,'EXPORT_MP4',io.BytesIO(f'v{i}'.encode()),'video/mp4')
  with self.assertRaisesRegex(ValueError,'MEDIA_BUNDLE_INCOMPLETE'):self.v.finalize_generation('fox','v011')
  self.assertEqual(self.v.status()['generation_count'],12)
  self.v.import_bytes('fox','v011','EXPORT_MP4',io.BytesIO(b'v11'),'video/mp4')
  result=self.v.finalize_generation('fox','v011',expected_roles=['EXPORT_MP4'])
  self.assertEqual(result['state'],'SAVED_VERIFIED')
  self.assertEqual(self.v.status()['generation_count'],10)
  self.assertEqual(len(result['auto_retention']['retired_generation_ids']),2)
  self.assertTrue(self.v.verify()['pass'])
 def test_wrong_target_immutable_asset_role(self):
  self.v.add_project('fox');self.v.add_generation('fox','v001');self.v.import_bytes('fox','v001','RAW_MP4',io.BytesIO(b'one'),'video/mp4')
  with self.assertRaises(ValueError):self.v.import_bytes('fox','v001','RAW_MP4',io.BytesIO(b'two'),'video/mp4')
 def test_no_unapproved_arbitrary_paths(self):
  with self.assertRaises(ValueError):self.v.add_project('../escape')
  with self.assertRaises(ValueError):self.v.add_generation('fox','foo/bar')
 def test_token_persists(self):
  a=ensure_token(self.tmp.name);b=ensure_token(self.tmp.name);self.assertEqual(a,b);self.assertGreater(len(a),45)
 def test_loopback_api_authenticated(self):
  token=ensure_token(self.tmp.name)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.token=token;server.vault=self.v
  t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
  try:
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('GET','/health');r=c.getresponse();data=json.loads(r.read());self.assertEqual(r.status,200);self.assertEqual(data['retention']['projects'],5);c.close()
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('GET','/v1/status');r=c.getresponse();r.read();self.assertEqual(r.status,401);c.close()
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('POST','/v1/projects',body=json.dumps({'project_id':'fox'}),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'});r=c.getresponse();r.read();self.assertEqual(r.status,200);c.close()
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('GET','/v1/status',headers={'Authorization':'Bearer '+token});r=c.getresponse();state=json.loads(r.read());self.assertEqual(state['project_count'],1);c.close()
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('POST','/v1/projects',body='{}',headers={'Authorization':'Bearer '+token,'Host':'example.com','Content-Type':'application/json'});r=c.getresponse();r.read();self.assertEqual(r.status,403);c.close()
  finally:server.shutdown();server.server_close();t.join(timeout=3)
if __name__=='__main__':unittest.main()