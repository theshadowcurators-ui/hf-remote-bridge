"""Loopback-only HTTP adapter for local Vault; no public exposure, no network access."""
import argparse,base64,hashlib,io,json,os,secrets,sys
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
try: from .vault import Vault,valid_id
except ImportError: from vault import Vault,valid_id

APP_NAME='Master AI Video Director'

def default_root():
    x=os.environ.get('MAVD_DATA_DIR')
    if x:return Path(x).expanduser()
    if os.name=='nt':
        return Path(os.environ.get('USERPROFILE',str(Path.home()))) / 'Documents' / 'Master_AI_Video_Director_Data'
    return Path.home()/'.local'/'share'/'master-ai-video-director'

def ensure_token(root):
    p=Path(root)/'config'/'auth_token.txt';p.parent.mkdir(parents=True,exist_ok=True)
    if not p.exists():
        with open(p,'x',encoding='ascii') as f:
            f.write(secrets.token_urlsafe(48));f.flush();os.fsync(f.fileno())
        try:os.chmod(p,0o600)
        except OSError:pass
    return p.read_text(encoding='ascii').strip()

class Handler(BaseHTTPRequestHandler):
    server_version='MAVD/0.1'
    def log_message(self,fmt,*args):
        # Avoid logging credentials, URLs, request bodies.
        sys.stderr.write('MAVD '+(fmt%args).split(' ')[0]+'\n')
    def _send(self,code,data):
        payload=json.dumps(data,sort_keys=True,ensure_ascii=False).encode('utf8')
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(payload)
    def _allowed_host(self):
        host=self.headers.get('Host','')
        return host.split(':')[0].strip('[]').lower() in {'localhost','127.0.0.1'}
    def _auth(self):
        raw=self.headers.get('Authorization','')
        if not raw.startswith('Bearer '):return False
        return secrets.compare_digest(raw[7:],self.server.token)
    def _load_json(self,max_bytes=1024*1024):
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise ValueError('JSON_REQUIRED')
        n=int(self.headers.get('Content-Length','0'))
        if n<0 or n>max_bytes:raise ValueError('BODY_TOO_LARGE')
        return json.loads(self.rfile.read(n))
    def _preflight(self,needs_auth=True):
        if not self._allowed_host():
            self._send(403,{'error':'HOST_FORBIDDEN'});return False
        if needs_auth and not self._auth():
            self._send(401,{'error':'AUTH_REQUIRED'});return False
        return True
    def do_OPTIONS(self):self._send(403,{'error':'CROSS_ORIGIN_REJECTED'})
    def do_GET(self):
        from urllib.parse import urlsplit
        path=urlsplit(self.path).path
        if not self._preflight(needs_auth=path!='/health'):return
        try:
            if path=='/health':out={'service':'master-ai-video-director-backend','status':'ONLINE','version':'0.1.0','storage':'LOCAL_ONLY','retention':{'projects':5,'generations_per_project':10}}
            elif path=='/v1/status':out=self.server.vault.status()
            elif path=='/v1/inventory':out=self.server.vault.inventory()
            elif path=='/v1/retention/plan':out=self.server.vault.retention_plan()
            elif path=='/v1/verify':out=self.server.vault.verify()
            else:self._send(404,{'error':'UNKNOWN_ENDPOINT'});return
            self._send(200,out)
        except Exception as e:self._send(500,{'error':type(e).__name__,'message':str(e)[:200]})
    def do_POST(self):
        from urllib.parse import urlsplit
        path=urlsplit(self.path).path
        if not self._preflight():return
        try:
            if path=='/v1/projects':
                a=self._load_json();out=self.server.vault.add_project(a['project_id'])
            elif path=='/v1/generations':
                a=self._load_json();out=self.server.vault.add_generation(a['project_id'],a['generation_id'],status=a.get('status','GENERATED'))
            elif path=='/v1/generations/finalize':
                a=self._load_json();out=self.server.vault.finalize_generation(a['project_id'],a['generation_id'],a.get('expected_roles'),active_generation_ids=a.get('active_generation_ids',[]))
            elif path=='/v1/queue':
                a=self._load_json();out=self.server.vault.enqueue(a['operation_id'],a['kind'],a['project_id'],a.get('generation_id'),a.get('payload'))
            elif path=='/v1/queue/receipt':
                a=self._load_json();out=self.server.vault.receipt(a['operation_id'],a['state'],a.get('evidence'),a.get('error'))
            elif path=='/v1/retention/commit':
                a=self._load_json();out=self.server.vault.prune(confirmed_new_index=bool(a.get('readback_verified')),
                     active_generation_ids=a.get('active_generation_ids',[]))
            elif path=='/v1/media':
                from urllib.parse import parse_qs
                q=parse_qs(urlsplit(self.path).query)
                project_id=valid_id(q.get('project_id',[''])[0]);generation_id=valid_id(q.get('generation_id',[''])[0]);role=q.get('role',[''])[0]
                ctype=self.headers.get('Content-Type','application/octet-stream').split(';')[0]
                size=int(self.headers.get('Content-Length','0'))
                if size<1 or size>500*1024*1024:raise ValueError('MEDIA_TOO_LARGE')
                out=self.server.vault.import_bytes(project_id,generation_id,role,io.BytesIO(self.rfile.read(size)),ctype)
            else:self._send(404,{'error':'UNKNOWN_ENDPOINT'});return
            self._send(200,out)
        except (ValueError,KeyError,json.JSONDecodeError) as e:self._send(400,{'error':type(e).__name__,'message':str(e)[:250]})
        except Exception as e:self._send(500,{'error':type(e).__name__,'message':str(e)[:250]})

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['serve','status','verify','retention-plan','prune','demo'])
    p.add_argument('--data-root',type=Path,default=default_root());p.add_argument('--port',type=int,default=8768)
    args=p.parse_args(argv)
    vault=Vault(args.data_root)
    if args.command=='status':print(json.dumps(vault.status(),indent=2));return 0
    if args.command=='verify':res=vault.verify();print(json.dumps(res,indent=2));return 0 if res['pass'] else 2
    if args.command=='retention-plan':print(json.dumps(vault.retention_plan(),indent=2));return 0
    if args.command=='prune':
        # CLI is an explicit user action; planner + verification before pruning.
        pre=vault.verify()
        if not pre['pass']:print(json.dumps({'error':'VAULT_VERIFICATION_FAILED', 'details':pre}));return 2
        print(json.dumps(vault.prune(confirmed_new_index=True),indent=2));return 0
    if args.command=='demo':
        vault.add_project('demo-1');vault.add_generation('demo-1','demo-1-gen-001')
        print(json.dumps(vault.status(),indent=2));return 0
    if args.command=='serve':
        token=ensure_token(args.data_root)
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
        server.token=token;server.vault=vault
        print(f'MAVD backend listening only at http://127.0.0.1:{args.port} (token stored in data-root/config/auth_token.txt, never printed)',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close();vault.close()
        return 0
if __name__=='__main__':raise SystemExit(main())