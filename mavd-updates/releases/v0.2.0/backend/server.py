"""Loopback-only HTTP adapter for local Vault; no public exposure, no network access."""
import argparse,base64,hashlib,io,json,os,secrets,sys,time,threading,tempfile,shutil
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
try: from .vault import Vault,valid_id
except ImportError: from vault import Vault,valid_id

APP_NAME='Master AI Video Director'
VERSION='0.2.0'
PAIR_TTL_SECONDS=120
SESSION_TTL_SECONDS=900
UPLOAD_TTL_SECONDS=90
MAX_BODY_BYTES=4*1024*1024

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

def bridge_openapi():
    # Documentation only, no credentials or host secrets.
    methods=('pair','status','verify','inventory','retention/plan','retention/commit',
             'projects','generations','generations/finalize','queue','queue/receipt',
             'media/from-pinokio','smoke','update/check','update/apply','update/status','update/rollback')
    paths={}
    for method in methods:
        route='/v1/bridge/'+method
        paths[route]={'post':{'summary':'Local paired operation: '+method,
                              'operationId':'bridge_'+method.replace('/','_').replace('-','_'),
                              'requestBody':{'required':True,'content':{'application/json':{
                                  'schema':{'type':'object','additionalProperties':True,
                                            'description':'JSON object: one-time pair_code (preferred for Pinokio MCP) OR session_token (other clients). Permanent Bearer remains private.'}}}},
                              'responses':{'200':{'description':'Success'},'400':{'description':'Invalid input'},
                                           '401':{'description':'Session unavailable'}}}}
    paths['/health']={'get':{'summary':'Public loopback health only','responses':{'200':{'description':'Healthy'}}}}
    return {'openapi':'3.1.0','info':{'title':'Master AI Video Director Paired Local API',
            'version':VERSION,'description':'127.0.0.1 only. One-time Pinokio pairing; bearer secret remains on PC.'},
            'paths':paths}

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
        return host.split(':')[0].strip('[]').lower() in {'localhost','127.0.0.1'} and self.client_address[0]=='127.0.0.1'
    def _auth(self):
        raw=self.headers.get('Authorization','')
        if not raw.startswith('Bearer '):return False
        return secrets.compare_digest(raw[7:],self.server.token)
    def _load_json(self,max_bytes=1024*1024):
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise ValueError('JSON_REQUIRED')
        n=int(self.headers.get('Content-Length','0'))
        if n<2 or n>max_bytes:raise ValueError('BODY_TOO_LARGE')
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
        if not self._preflight(needs_auth=path not in {'/health','/openapi.json'}):return
        try:
            if path=='/health':out={'service':'master-ai-video-director-backend','status':'ONLINE','version':VERSION,'storage':'LOCAL_ONLY','retention':{'projects':5,'generations_per_project':10}}
            elif path=='/openapi.json':out=bridge_openapi()
            elif path=='/v1/status':out=self.server.vault.status()
            elif path=='/v1/inventory':out=self.server.vault.inventory()
            elif path=='/v1/retention/plan':out=self.server.vault.retention_plan()
            elif path=='/v1/verify':out=self.server.vault.verify()
            else:self._send(404,{'error':'UNKNOWN_ENDPOINT'});return
            self._send(200,out)
        except Exception as e:self._send(500,{'error':type(e).__name__,'message':str(e)[:200]})
    def _connector_preflight(self):
        if not self._preflight(needs_auth=False):return False
        if (self.headers.get('Origin') or self.headers.get('Referer') or
                self.headers.get('Sec-Fetch-Site','').lower() not in {'','none','same-origin'}):
            self._send(403,{'error':'BROWSER_ORIGIN_FORBIDDEN'});return False
        return True

    def _session_required(self,token):
        if not isinstance(token,str) or not 30 <= len(token) <= 160:
            raise PermissionError('BRIDGE_SESSION_REQUIRED')
        digest=hashlib.sha256(token.encode()).hexdigest()
        with self.server.bridge_lock:
            expires=self.server.bridge_sessions.get(digest,0)
            if time.monotonic()>=expires:
                self.server.bridge_sessions.pop(digest,None)
                raise PermissionError('BRIDGE_SESSION_EXPIRED')

    def _consume_pair_code(self,code):
        """Authorize exactly one local action by atomically consuming a short-lived pairing code.

        The opaque value is never returned by the HTTP server or copied to the
        project vault. This supports the Pinokio MCP connector, which deliberately
        redacts session_token fields in responses before exposing them to a chat.
        """
        if not isinstance(code,str) or not 30<=len(code)<=120:
            raise PermissionError('INVALID_PAIR_CODE')
        pair_file=self.server.vault.root/'config'/'bridge_pair.json'
        with self.server.bridge_lock:
            t=time.time()
            self.server.pair_failures=[v for v in self.server.pair_failures if v>t-60]
            if len(self.server.pair_failures)>=5:raise PermissionError('PAIRING_RATE_LIMIT')
            try:req=json.loads(pair_file.read_text('utf-8'))
            except (FileNotFoundError,json.JSONDecodeError):raise PermissionError('PAIR_NOT_PREPARED')
            digest=hashlib.sha256(code.encode('utf-8')).hexdigest()
            if t>req.get('expires_at',0) or not secrets.compare_digest(digest,req.get('digest','')):
                self.server.pair_failures.append(t)
                raise PermissionError('PAIR_INVALID_OR_EXPIRED')
            pair_file.unlink(missing_ok=True)

    def _pair(self,body):
        # Legacy ephemeral sessions are kept for callers which can hold a real
        # opaque secret. The ChatGPT Pinokio MCP connector must use one-time
        # per-operation pairing instead: response tokens are redacted there.
        self._consume_pair_code(body.get('pair_code',''))
        with self.server.bridge_lock:
            token=secrets.token_urlsafe(48)
            if len(self.server.bridge_sessions)>12:
                self.server.bridge_sessions={k:v for k,v in self.server.bridge_sessions.items() if v>time.monotonic()}
            self.server.bridge_sessions[hashlib.sha256(token.encode()).hexdigest()]=time.monotonic()+SESSION_TTL_SECONDS
        self._send(200,{'status':'PAIRED','session_token':token,'expires_in_seconds':SESSION_TTL_SECONDS,
                        'auth':'ephemeral','permanent_token_shared':False})

    def _import_from_pinokio(self,a):
        v=self.server.vault
        project=valid_id(a['project_id']);generation=valid_id(a['generation_id'])
        role=a['role'];mime=a.get('mime','application/octet-stream')
        if role not in {'RAW_MP4','EXPORT_MP4','START_FRAME','QC_CONTACT_SHEET','AUDIO','OTHER_MEDIA'}:
            raise ValueError('UNSUPPORTED_ROLE')
        if mime not in {'video/mp4','image/jpeg','image/png','audio/wav','application/octet-stream'}:
            raise ValueError('UNSUPPORTED_MIME')
        # Input must have arrived through Pinokio's authorized receive_input_file,
        # never through a client-provided arbitrary PC path.
        relative=a.get('input_relative_path','')
        if not isinstance(relative,str) or not relative or len(relative)>400:
            raise ValueError('INPUT_RELATIVE_PATH_REQUIRED')
        if relative.startswith(('/','\\')) or ':' in relative or '\\' in relative:
            raise ValueError('ABSOLUTE_OR_WINDOWS_PATH_FORBIDDEN')
        root=self.server.pinokio_inputs.resolve()
        source=(root/relative).resolve()
        if source==root or root not in source.parents or not source.is_file():
            raise ValueError('PINOKIO_BRIDGE_INPUT_NOT_FOUND_OR_OUT_OF_SCOPE')
        if source.is_symlink():raise ValueError('SYMLINK_INPUT_FORBIDDEN')
        expected=a.get('sha256','')
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError('EXPECTED_SHA256_REQUIRED')
        if source.stat().st_size>500*1024*1024:raise ValueError('FILE_TOO_LARGE')
        h=hashlib.sha256()
        with source.open('rb') as f:
            for buf in iter(lambda:f.read(1024*1024),b''):h.update(buf)
        if not secrets.compare_digest(h.hexdigest(),expected):
            raise ValueError('SOURCE_SHA256_MISMATCH')
        with source.open('rb') as f:out=v.import_bytes(project,generation,role,f,mime)
        return {**out,'source':'PINOKIO_BRIDGE_INPUT','input_sha256':expected}

    def _smoke(self):
        # Operates only in its own temporary SQLite vault. Does not touch user projects.
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix='mavd_pair_smoke_',dir=self.server.vault.staging) as tmp:
            scratch=Vault(tmp)
            try:
                scratch.add_project('smoke')
                for i in range(12):scratch.add_generation('smoke',f'gen_{i:03d}')
                plan=scratch.retention_plan()
                if plan['prune_count']!=2:raise RuntimeError('RETENTION_PLAN_FAILED')
                result=scratch.prune(confirmed_new_index=True)
                verify=scratch.verify()
                state=scratch.status()
                if not verify['pass'] or state['generation_count']!=10:raise RuntimeError('SMOKE_INTEGRITY_FAILED')
                return {'status':'PASS','scratch_generations_created':12,'scratch_retired':len(result['retired_generation_ids']),
                        'scratch_generations_retained':state['generation_count'],'real_vault_modified':False}
            finally:scratch.close()

    def _bridge_post(self,path):
        if not self._connector_preflight():return
        try:
            body=self._load_json(MAX_BODY_BYTES)
            if not isinstance(body,dict):raise ValueError('BODY_NOT_OBJECT')
            if path=='/v1/bridge/pair':return self._pair(body)
            token=body.pop('session_token',None)
            code=body.pop('pair_code',None)
            if code is not None:
                if token is not None:raise ValueError('MULTIPLE_AUTH_METHODS_FORBIDDEN')
                self._consume_pair_code(code)
            else:
                self._session_required(token)
            vault=self.server.vault
            if path=='/v1/bridge/status':out=vault.status()
            elif path=='/v1/bridge/inventory':out=vault.inventory()
            elif path=='/v1/bridge/verify':out=vault.verify()
            elif path=='/v1/bridge/retention/plan':out=vault.retention_plan()
            elif path=='/v1/bridge/projects':out=vault.add_project(body['project_id'])
            elif path=='/v1/bridge/generations':
                out=vault.add_generation(body['project_id'],body['generation_id'],status=body.get('status','GENERATED'))
            elif path=='/v1/bridge/queue':
                out=vault.enqueue(body['operation_id'],body['kind'],body['project_id'],body.get('generation_id'),body.get('payload'))
            elif path=='/v1/bridge/queue/receipt':
                out=vault.receipt(body['operation_id'],body['state'],body.get('evidence'),body.get('error'))
            elif path=='/v1/bridge/generations/finalize':
                out=vault.finalize_generation(body['project_id'],body['generation_id'],body.get('expected_roles'),
                                              active_generation_ids=body.get('active_generation_ids',[]))
            elif path=='/v1/bridge/retention/commit':
                if body.get('confirm_retention') is not True:raise ValueError('EXPLICIT_RETENTION_CONFIRMATION_REQUIRED')
                out=vault.prune(confirmed_new_index=True,active_generation_ids=body.get('active_generation_ids',[]))
            elif path=='/v1/bridge/media/from-pinokio':out=self._import_from_pinokio(body)
            elif path=='/v1/bridge/update/check':
                from updater_core import check
                out=check(self.server.app_root)
            elif path=='/v1/bridge/update/apply':
                from updater_core import request
                out=request(self.server.app_root,'apply',{'version':body.get('approved_version'),
                    'fingerprint':body.get('approved_fingerprint'),
                    'explicit_owner_approval':body.get('explicit_owner_approval') is True})
            elif path=='/v1/bridge/update/status':
                from updater_core import report
                out=report(self.server.app_root,verbose=body.get('verbose') is True)
            elif path=='/v1/bridge/update/rollback':
                from updater_core import request
                out=request(self.server.app_root,'rollback',{'explicit_owner_approval':body.get('explicit_owner_approval') is True})
            elif path=='/v1/bridge/smoke':out=self._smoke()
            else:self._send(404,{'error':'UNKNOWN_BRIDGE_ENDPOINT'});return
            self._send(200,out)
        except PermissionError as e:self._send(401,{'error':str(e)})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:
            self._send(400,{'error':type(e).__name__,'message':str(e)[:180]})
        except Exception as e:self._send(500,{'error':type(e).__name__,'message':str(e)[:180]})

    def do_POST(self):
        from urllib.parse import urlsplit
        path=urlsplit(self.path).path
        if path.startswith('/v1/bridge/'):
            return self._bridge_post(path)
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
    p.add_argument('command',choices=['serve','status','verify','retention-plan','prune','demo','pair'])
    p.add_argument('--data-root',type=Path,default=default_root());p.add_argument('--port',type=int,default=8768)
    args=p.parse_args(argv)
    vault=Vault(args.data_root)
    if args.command=='pair':
        print(json.dumps(issue_pairing(vault.root),indent=2));return 0
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
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'supervisor'))
        token=ensure_token(args.data_root)
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
        server.token=token;server.vault=vault
        server.bridge_lock=threading.RLock();server.bridge_sessions={};server.pair_failures=[]
        server.app_root=Path(__file__).resolve().parents[1]
        server.pinokio_inputs=Path(args.data_root).parents[1]/'Pinokio'/'api'/'master-ai-video-director-backend'/'app'/'bridge-inputs'
        server.pinokio_inputs.mkdir(parents=True,exist_ok=True)
        print(f'MAVD backend listening only at http://127.0.0.1:{args.port} (token stored in data-root/config/auth_token.txt, never printed)',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close();vault.close()
        return 0
def issue_pairing(root):
    root=Path(root);d=root/'config';d.mkdir(parents=True,exist_ok=True)
    code=secrets.token_urlsafe(36)
    path=d/'bridge_pair.json';temp=d/('pair_'+secrets.token_hex(8)+'.tmp')
    obj={'digest':hashlib.sha256(code.encode()).hexdigest(),'expires_at':time.time()+PAIR_TTL_SECONDS}
    with open(temp,'x',encoding='utf-8') as f:
        json.dump(obj,f);f.flush();os.fsync(f.fileno())
    try:os.chmod(temp,0o600)
    except OSError:pass
    os.replace(temp,path)
    return {'status':'PAIRING_PREPARED','pair_code':code,'expires_in_seconds':PAIR_TTL_SECONDS,
            'note':'Use once; never store in ChatGPT Library or project files.'}

if __name__=='__main__':raise SystemExit(main())
