"""Master AI Video Director's local bounded SQLite media library.

No model inference or remote calls. Configurable data root; never uses paths from
untrusted requests. Imports only bytes from local uploads or explicitly local CLI.
"""
from __future__ import annotations
import hashlib, io, json, os, re, sqlite3, tempfile, threading, time, uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

MAX_PROJECTS = 5
MAX_GENERATIONS = 10
MAX_FILE_BYTES = 500 * 1024 * 1024
ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$')
ROLES = {'RAW_MP4','EXPORT_MP4','START_FRAME','QC_CONTACT_SHEET','AUDIO','OTHER_MEDIA'}
EXT = {'video/mp4':'.mp4','image/jpeg':'.jpg','image/png':'.png','audio/wav':'.wav','application/octet-stream':'.bin'}

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def valid_id(x):
    if not isinstance(x,str) or not ID_RE.fullmatch(x): raise ValueError('INVALID_ID')
    return x

def stable_json(x): return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)

class Vault:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.blobs=self.root/'blobs'; self.staging=self.root/'staging';self.snapshots=self.root/'snapshots';
        for d in (self.root,self.blobs,self.staging,self.snapshots): d.mkdir(parents=True,exist_ok=True)
        self.dbpath=self.root/'director.sqlite3'
        self._lock=threading.RLock()
        self._db=sqlite3.connect(self.dbpath,timeout=15,check_same_thread=False,isolation_level=None)
        self._db.row_factory=sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL');self._db.execute('PRAGMA foreign_keys=ON');self._db.execute('PRAGMA synchronous=FULL')
        self._schema()
    def close(self): self._db.close()
    def _schema(self):
        self._db.executescript('''
        CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE');
        CREATE TABLE IF NOT EXISTS generations(generation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), created_at TEXT NOT NULL, status TEXT NOT NULL, approved INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS assets(asset_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES generations(generation_id), role TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, relative_path TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT, generation_id TEXT, payload_json TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, operation TEXT NOT NULL, details_json TEXT NOT NULL, previous_sha256 TEXT, event_sha256 TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_generations_project ON generations(project_id,created_at);
        ''')
    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK'); raise
    def _event(self, db, kind, details):
        row=db.execute('SELECT event_sha256 FROM audit ORDER BY sequence DESC LIMIT 1').fetchone()
        prev=row['event_sha256'] if row else None
        obj={'at':now(),'operation':kind,'details':details,'previous_sha256':prev}
        hashval=hashlib.sha256(stable_json(obj).encode()).hexdigest()
        db.execute('INSERT INTO audit(at,operation,details_json,previous_sha256,event_sha256) VALUES(?,?,?,?,?)',
                   (obj['at'],kind,stable_json(details),prev,hashval))
        return hashval
    def add_project(self, project_id, *, created_at=None):
        valid_id(project_id); ts=created_at or now()
        with self._tx() as db:
            if db.execute('SELECT 1 FROM projects WHERE project_id=?',(project_id,)).fetchone():
                return {'project_id':project_id,'status':'EXISTING'}
            db.execute('INSERT INTO projects(project_id,created_at) VALUES(?,?)',(project_id,ts))
            self._event(db,'PROJECT_CREATED',{'project_id':project_id})
        return {'project_id':project_id,'status':'CREATED'}
    def add_generation(self, project_id,generation_id,*,created_at=None,status='GENERATED',approved=False):
        valid_id(project_id);valid_id(generation_id);ts=created_at or now()
        with self._tx() as db:
            if not db.execute('SELECT 1 FROM projects WHERE project_id=?',(project_id,)).fetchone():
                raise ValueError('UNKNOWN_PROJECT')
            row=db.execute('SELECT project_id FROM generations WHERE generation_id=?',(generation_id,)).fetchone()
            if row:
                if row['project_id']!=project_id:raise ValueError('GENERATION_ID_REUSED_IN_DIFFERENT_PROJECT')
                return {'generation_id':generation_id,'status':'EXISTING'}
            db.execute('INSERT INTO generations(generation_id,project_id,created_at,status,approved) VALUES(?,?,?,?,?)',
                       (generation_id,project_id,ts,status,int(approved)))
            self._event(db,'GENERATION_CREATED',{'project_id':project_id,'generation_id':generation_id})
        return {'generation_id':generation_id,'status':'CREATED'}
    def import_bytes(self,project_id,generation_id,role,stream,mime='application/octet-stream',*,max_bytes=MAX_FILE_BYTES):
        valid_id(project_id);valid_id(generation_id)
        if role not in ROLES:raise ValueError('UNSUPPORTED_ROLE')
        if mime not in EXT:raise ValueError('UNSUPPORTED_MIME')
        fd,tmp=tempfile.mkstemp(dir=self.staging,prefix='incoming_',suffix='.partial')
        hasher=hashlib.sha256();total=0
        try:
            with os.fdopen(fd,'wb') as target:
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk:break
                    total+=len(chunk)
                    if total>max_bytes:raise ValueError('MEDIA_TOO_LARGE')
                    hasher.update(chunk);target.write(chunk)
                target.flush();os.fsync(target.fileno())
            if total<1:raise ValueError('EMPTY_MEDIA')
            digest=hasher.hexdigest();rel=Path('blobs')/digest[:2]/(digest+EXT[mime]);target=self.root/rel
            with self._tx() as db:
                if not db.execute('SELECT 1 FROM generations WHERE generation_id=? AND project_id=?', (generation_id,project_id)).fetchone():
                    raise ValueError('UNKNOWN_GENERATION')
                existing=db.execute('SELECT asset_id,sha256,size_bytes,relative_path FROM assets WHERE generation_id=? AND role=?',(generation_id,role)).fetchone()
                if existing:
                    if existing['sha256']==digest and existing['size_bytes']==total:
                        return {'status':'EXISTING_VERIFIED','asset_id':existing['asset_id'],'sha256':digest,'bytes':total}
                    raise ValueError('ASSET_ROLE_ALREADY_HAS_DIFFERENT_APPROVED_BYTES')
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():os.replace(tmp,target)
                else:
                    if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise IOError('CONTENT_ADDRESS_HASH_MISMATCH')
                aid=str(uuid.uuid4())
                db.execute('INSERT INTO assets(asset_id,generation_id,role,sha256,size_bytes,relative_path,created_at) VALUES(?,?,?,?,?,?,?)',
                           (aid,generation_id,role,digest,total,str(rel),now()))
                self._event(db,'MEDIA_IMPORTED',{'asset_id':aid,'generation_id':generation_id,'role':role,'sha256':digest,'bytes':total})
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
                raise IOError('READBACK_HASH_MISMATCH')
            return {'status':'SAVED_VERIFIED','asset_id':aid,'sha256':digest,'bytes':total}
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    def finalize_generation(self, project_id, generation_id, expected_roles=None, *, active_generation_ids=()):
        """Only a verified committed media bundle may trigger automatic bounded prune."""
        valid_id(project_id);valid_id(generation_id)
        roles=set(expected_roles or {'EXPORT_MP4'})
        if not roles or not roles.issubset(ROLES):raise ValueError('INVALID_EXPECTED_ROLES')
        with self._lock:
            row=self._db.execute('SELECT * FROM generations WHERE generation_id=? AND project_id=?',(generation_id,project_id)).fetchone()
            if not row:raise ValueError('UNKNOWN_GENERATION')
            assets=[dict(x) for x in self._db.execute('SELECT * FROM assets WHERE generation_id=?',(generation_id,))]
        actual={x['role'] for x in assets}
        missing=roles-actual
        if missing:raise ValueError('MEDIA_BUNDLE_INCOMPLETE:'+','.join(sorted(missing)))
        for a in assets:
            f=self.root/a['relative_path']
            if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=a['sha256']:
                raise ValueError('GENERATION_MEDIA_READBACK_FAILED:'+a['asset_id'])
        with self._tx() as db:
            db.execute('UPDATE generations SET status=? WHERE generation_id=?',('SAVED_VERIFIED',generation_id))
            self._event(db,'GENERATION_FINALIZED',{'project_id':project_id,'generation_id':generation_id,'expected_roles':sorted(roles)})
        result=self.prune(confirmed_new_index=True,active_generation_ids=active_generation_ids)
        return {'generation_id':generation_id,'state':'SAVED_VERIFIED','auto_retention':result}

    def enqueue(self, operation_id, kind,project_id,generation_id=None,payload=None):
        valid_id(operation_id);valid_id(project_id)
        if generation_id is not None:valid_id(generation_id)
        if kind not in {'UPLOAD_GENERATION','SAVE_CHECKPOINT','SYNC_METADATA','DELETE_RETIRED'}:raise ValueError('INVALID_OPERATION')
        with self._tx() as db:
            existing=db.execute('SELECT state FROM operations WHERE operation_id=?',(operation_id,)).fetchone()
            if existing:return {'operation_id':operation_id,'state':existing['state'],'idempotent':True}
            t=now();db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',(operation_id,kind,project_id,generation_id,stable_json(payload or {}),'PENDING',t,t,None))
            self._event(db,'QUEUED',{'operation_id':operation_id,'kind':kind,'project_id':project_id,'generation_id':generation_id})
        return {'operation_id':operation_id,'state':'PENDING','idempotent':False}
    def receipt(self,operation_id,status,evidence=None,error=None):
        valid_id(operation_id)
        if status not in {'SAVED_VERIFIED','DELETED_VERIFIED','FAILED','PENDING','SUPERSEDED_NOT_UPLOADED'}:raise ValueError('INVALID_RECEIPT_STATE')
        if status.endswith('_VERIFIED') and not evidence:raise ValueError('VERIFICATION_EVIDENCE_REQUIRED')
        with self._tx() as db:
            op=db.execute('SELECT * FROM operations WHERE operation_id=?',(operation_id,)).fetchone()
            if not op:raise ValueError('UNKNOWN_OPERATION')
            if op['state'] in ('SAVED_VERIFIED','DELETED_VERIFIED','SUPERSEDED_NOT_UPLOADED'):
                if op['state'] != status:raise ValueError('FINAL_RECEIPT_CANNOT_BE_OVERWRITTEN')
                return {'operation_id':operation_id,'state':op['state'],'idempotent':True}
            db.execute('UPDATE operations SET state=?,updated_at=?,error=? WHERE operation_id=?',(status,now(),error,operation_id))
            self._event(db,'SYNC_RECEIPT',{'operation_id':operation_id,'status':status,'evidence':evidence,'error':error})
        return {'operation_id':operation_id,'state':status,'idempotent':False}
    def status(self):
        with self._lock:
            p=self._db.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
            g=self._db.execute('SELECT COUNT(*) FROM generations').fetchone()[0]
            assets=self._db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM assets').fetchone()
            queued=self._db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('PENDING','FAILED')").fetchone()[0]
            event=self._db.execute('SELECT event_sha256 FROM audit ORDER BY sequence DESC LIMIT 1').fetchone()
            return {'project_count':p,'generation_count':g,'asset_count':assets[0], 'asset_bytes':assets[1],
                    'pending_operations':queued,'audit_head_sha256':event[0] if event else None,
                    'max_projects':MAX_PROJECTS,'max_generations_per_project':MAX_GENERATIONS}
    def inventory(self):
        with self._lock:
            return {'projects':[dict(x) for x in self._db.execute('SELECT * FROM projects ORDER BY created_at DESC,project_id DESC')],
                    'generations':[dict(x) for x in self._db.execute('SELECT * FROM generations ORDER BY created_at DESC,generation_id DESC')],
                    'assets':[dict(x) for x in self._db.execute('SELECT * FROM assets')],
                    'operations':[dict(x) for x in self._db.execute('SELECT operation_id,kind,project_id,generation_id,state,created_at,updated_at,error FROM operations ORDER BY created_at DESC LIMIT 100')]}
    def retention_plan(self):
        inventory=self.inventory()
        p=inventory['projects'];g=inventory['generations'];assets=inventory['assets']
        keep_p={x['project_id'] for x in p[:MAX_PROJECTS]}
        keep_g=set();rem_g=set()
        for pid in keep_p:
            rows=sorted((x for x in g if x['project_id']==pid),key=lambda x:(x['created_at'],x['generation_id']),reverse=True)
            keep_g.update(x['generation_id'] for x in rows[:MAX_GENERATIONS]);rem_g.update(x['generation_id'] for x in rows[MAX_GENERATIONS:])
        rem_g.update(x['generation_id'] for x in g if x['project_id'] not in keep_p)
        retain_hashes={a['sha256'] for a in assets if a['generation_id'] in keep_g}
        blocked=[]; candidates=[]
        for a in assets:
            if a['generation_id'] not in rem_g: continue
            if a['sha256'] in retain_hashes: blocked.append({'asset_id':a['asset_id'],'reason':'SHARED_HASH_WITH_RETAINED'});continue
            candidates.append({'asset_id':a['asset_id'],'generation_id':a['generation_id'],'bytes':a['size_bytes']})
        return {'keep_project_ids':sorted(keep_p),'keep_generation_ids':sorted(keep_g),
                'prune_project_ids':sorted({x['project_id'] for x in p}-keep_p),
                'prune_generation_ids':sorted(rem_g),'candidate_assets':candidates,'blocked_assets':blocked,
                'prune_count':len(rem_g),'candidate_reclaim_bytes':sum(a['bytes'] for a in candidates),
                'status':'DRY_RUN_NO_FILES_DELETED'}
    def prune(self, *, confirmed_new_index: bool, active_generation_ids=()):
        """Bound retention; never act on stale or incomplete index; emit exact receipts."""
        if not confirmed_new_index:raise ValueError('NEW_INDEX_NOT_VERIFIED')
        # Independent server-side check; do not trust the caller's Boolean alone.
        integrity=self.verify()
        if not integrity['pass']:raise ValueError('SERVER_SIDE_VAULT_INTEGRITY_FAILED')
        p=self.retention_plan();blocked=set(active_generation_ids)
        tombstones=[];orphans=[];pruned=[]
        # All writes first; deleting blobs only after commit. Crash in between leaves harmless orphan blob.
        with self._tx() as db:
            for gid in p['prune_generation_ids']:
                if gid in blocked:
                    tombstones.append({'generation_id':gid,'state':'DELETION_BLOCKED_ACTIVE_EDIT'});continue
                aids=[dict(x) for x in db.execute('SELECT * FROM assets WHERE generation_id=?',(gid,))]
                db.execute('DELETE FROM assets WHERE generation_id=?',(gid,))
                db.execute('DELETE FROM generations WHERE generation_id=?',(gid,))
                self._event(db,'GENERATION_RETIRED',{'generation_id':gid,'sha256s':[x['sha256'] for x in aids]})
                pruned.append(gid)
                for a in aids:orphans.append(a)
            for pid in p['prune_project_ids']:
                if db.execute('SELECT 1 FROM generations WHERE project_id=?',(pid,)).fetchone():
                    tombstones.append({'project_id':pid,'state':'PROJECT_HAS_ACTIVE_GENERATIONS'});continue
                db.execute('DELETE FROM projects WHERE project_id=?',(pid,))
                self._event(db,'PROJECT_RETIRED',{'project_id':pid})
        deleted=[]; skipped=[]
        for a in orphans:
            with self._lock:
                n=self._db.execute('SELECT COUNT(*) FROM assets WHERE sha256=?',(a['sha256'],)).fetchone()[0]
            if n:
                skipped.append({'sha256':a['sha256'],'state':'SHARED_BY_RETAINED_ASSET'});continue
            path=self.root/a['relative_path']
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==a['sha256']:
                bytes_=path.stat().st_size;path.unlink()
                deleted.append({'sha256':a['sha256'],'bytes':bytes_,'status':'DELETED_VERIFIED' if not path.exists() else 'DELETE_FAILED'})
            else:
                skipped.append({'sha256':a['sha256'],'state':'NOT_FOUND_OR_HASH_MISMATCH_NOT_DELETED'})
        return {'status':'COMPLETED_WITH_RECEIPTS','retired_generation_ids':pruned,'media_deletion_receipts':deleted,
                'blocked':tombstones,'skipped':skipped,'reclaimed_bytes':sum(x['bytes'] for x in deleted)}
    def verify(self):
        errors=[];checked=0
        with self._lock:
            for a in self._db.execute('SELECT * FROM assets'):
                path=self.root/a['relative_path'];checked+=1
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=a['sha256']:
                    errors.append({'asset_id':a['asset_id'],'reason':'MISSING_OR_HASH_MISMATCH'})
            prev=None
            for e in self._db.execute('SELECT * FROM audit ORDER BY sequence'):
                contents={'at':e['at'],'operation':e['operation'],'details':json.loads(e['details_json']),'previous_sha256':e['previous_sha256']}
                expect=hashlib.sha256(stable_json(contents).encode()).hexdigest()
                if e['previous_sha256']!=prev or e['event_sha256']!=expect:errors.append({'audit_sequence':e['sequence'],'reason':'AUDIT_CHAIN_MISMATCH'})
                prev=e['event_sha256']
        return {'pass':not errors,'errors':errors,'checked_assets':checked,'audit_head':prev,'at':now()}
