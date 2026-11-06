"""Local-only Hugging Face Spaces Gradio bridge. Never auto-resubmit remote jobs."""
from __future__ import annotations
import hashlib, json, os, re, shutil, sqlite3, subprocess, threading, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, ConfigDict

VERSION = "0.4.0"
ROOT = Path(__file__).resolve().parent
UP = ROOT / "uploads"
OUT = ROOT / "outputs"
STATE = ROOT / "state"
for folder in (UP, OUT, STATE):
    folder.mkdir(parents=True, exist_ok=True)
DB = STATE / "jobs.sqlite3"
LOCK = threading.RLock()
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-bridge")
MAX_BYTES = 500 * 1024 * 1024
SPACE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,94}/[a-zA-Z0-9][a-zA-Z0-9._-]{0,94}")
API_NAME = re.compile(r"/[a-zA-Z][a-zA-Z0-9_]*")
ALLOWED_INPUT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".wav", ".mp3", ".flac", ".ogg", ".mp4", ".mov", ".webm"}
ALLOWED_OUTPUT = {".mp4", ".webm", ".mov"}
TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED"}

def now():
    return datetime.now(timezone.utc).isoformat()

def conn():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db

def init():
    with LOCK, conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,signature TEXT NOT NULL,
          state TEXT NOT NULL,space TEXT NOT NULL,api_name TEXT NOT NULL,
          error TEXT,outputs TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT,stamp TEXT,state TEXT,message TEXT);
        """)
        db.execute("""UPDATE jobs SET state='INTERRUPTED',error='Process restarted; remote execution may have started; DO NOT resubmit',updated=?
                      WHERE state NOT IN ('SUCCEEDED','FAILED','INTERRUPTED')""",(now(),))

init()
app = FastAPI(title="HF Remote Bridge",version=VERSION)

class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space: str
    api_name: str = "/generate_video"
    request_id: str = Field(min_length=8,max_length=128)
    file_ids: dict[str,str]
    parameters: dict[str,Any] = Field(default_factory=dict)
    expect_extensions: list[str] = Field(default_factory=lambda:[".mp4",".webm",".mov"])

def record(id,state,message="",outputs=None):
    stamp=now()
    with LOCK,conn() as db:
        db.execute("UPDATE jobs SET state=?,error=?,outputs=?,updated=? WHERE id=?",
            (state,message[:1000] or None,json.dumps(outputs or []),stamp,id))
        db.execute("INSERT INTO events(id,stamp,state,message) VALUES(?,?,?,?)",(id,stamp,state,message[:1000]))

def asdict(row):
    return {"job_id":row["id"],"request_id":row["request_id"],"space":row["space"],
      "api_name":row["api_name"],"state":row["state"],"error":row["error"],
      "outputs":json.loads(row["outputs"]),"created_at":row["created"],"updated_at":row["updated"]}

def verify_video(path):
    ffprobe=shutil.which("ffprobe")
    if not ffprobe: raise RuntimeError("ffprobe missing; refusing unverified media")
    result=subprocess.run([ffprobe,"-v","error","-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height","-of","json",str(path)],
        capture_output=True,text=True,timeout=45)
    if result.returncode: raise RuntimeError("ffprobe rejected output")
    data=json.loads(result.stdout)
    streams=[s for s in data.get("streams",[]) if s.get("codec_type")=="video"]
    dur=float(data.get("format",{}).get("duration") or 0)
    if not streams or dur<=0 or int(streams[0].get("width") or 0)<=0 or int(streams[0].get("height") or 0)<=0:
        raise RuntimeError("No valid video stream/duration")
    return {"duration_seconds":round(dur,3),"width":streams[0]["width"],
      "height":streams[0]["height"],"codec":streams[0].get("codec_name"),
      "validation":"ffprobe"}

@app.get("/health")
def health():
    ff=bool(shutil.which("ffprobe"))
    return {"status":"ok" if ff else "needs_ffprobe","version":VERSION,
      "ffprobe_available":ff,"bind":"127.0.0.1","no_cloudflare":True}

@app.get("/api/capabilities")
def capabilities():
    return {"adapters":["hf-gradio"],"max_upload_bytes":MAX_BYTES,
      "supports":["upload","preflight","submit","status","events","verified-output","idempotency"],
      "output_directory":str(OUT),"fallback_triggered_by":"ChatGPT caller, not globally installed",
      "no_cloudflare":True}

@app.get("/api/connector_spec")
def spec():
    return {"adapter":"rest","job_spec":{"submit_path":"/api/jobs",
      "id_field":"job_id","status_path":"/api/jobs/{job_id}","state_field":"state",
      "success_states":["SUCCEEDED"],"failure_states":["FAILED","INTERRUPTED"]},
      "upload_path":"/api/uploads","upload_field":"file",
      "output_directory":str(OUT),"automatic_routing_across_chats":False}

@app.post("/api/uploads",status_code=201)
async def upload(file:UploadFile=File(...)):
    suffix=Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_INPUT: raise HTTPException(415,"Unsupported file extension")
    id=str(uuid.uuid4()); path=UP/(id+suffix); size=0; digest=hashlib.sha256()
    try:
        with path.open("xb") as f:
            while chunk:=await file.read(4*1024*1024):
                size+=len(chunk)
                if size>MAX_BYTES: raise HTTPException(413,"Input exceeds 500 MiB")
                digest.update(chunk); f.write(chunk)
        if not size: raise HTTPException(400,"Empty file")
        with LOCK,conn() as db:
            db.execute("INSERT INTO files VALUES(?,?,?,?)",(id,str(path),digest.hexdigest(),size))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally: await file.close()
    return {"file_id":id,"sha256":digest.hexdigest(),"size_bytes":size}

def resolve_inputs(mapping):
    if not mapping or len(mapping)>8: raise HTTPException(422,"Expected 1–8 file inputs")
    found={}; hashes={}
    with LOCK,conn() as db:
        for param,id in mapping.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}",param):
                raise HTTPException(422,"Invalid input parameter")
            row=db.execute("SELECT * FROM files WHERE id=?",(id,)).fetchone()
            if row is None: raise HTTPException(422,"Unknown file_id")
            try: path=Path(row["path"]).resolve(strict=True)
            except OSError: raise HTTPException(422,"File missing")
            if not path.is_relative_to(UP.resolve()): raise HTTPException(422,"Unsafe input location")
            with path.open("rb") as f: sha=hashlib.file_digest(f,"sha256").hexdigest()
            if sha!=row["sha256"]: raise HTTPException(422,"Upload SHA-256 mismatch")
            found[param]=path; hashes[param]=sha
    return found,hashes

def schema_check(client,api_name,names):
    info=client.view_api(print_info=False,return_format="dict")
    d=(info or {}).get("named_endpoints",{})
    endpoint=d.get(api_name) or d.get(api_name.lstrip("/"))
    if not isinstance(endpoint,dict): raise RuntimeError("No matching Gradio named endpoint")
    accepted={p.get("parameter_name") for p in endpoint.get("parameters",[]) if isinstance(p,dict)}
    extra=names-accepted
    if extra: raise RuntimeError("Unexpected Gradio parameters: "+",".join(sorted(extra)))
    return {"endpoint":api_name,"accepted_params":sorted(str(x) for x in accepted if x)}

@app.post("/api/preflight")
def preflight(payload:Request):
    if not SPACE.fullmatch(payload.space) or not API_NAME.fullmatch(payload.api_name):
        raise HTTPException(422,"Invalid Hugging Face Space or endpoint")
    try:
        from gradio_client import Client
        c=Client(payload.space,token=os.getenv("HF_TOKEN") or None,verbose=False,download_files=False)
        return {"status":"ok","space":payload.space,**schema_check(c,payload.api_name,set(payload.parameters)|set(payload.file_ids))}
    except Exception as e: raise HTTPException(424,"Preflight rejected: "+str(e)[:500])

def result_media(value,job_dir,extensions):
    items=[]; visited=set(); root=job_dir.resolve()
    def visit(obj,depth=0):
        if depth>8 or len(items)>=24:return
        if isinstance(obj,(str,Path)):
            p=Path(obj)
            if not p.is_file():return
            p=p.resolve()
            if p in visited or not p.is_relative_to(root) or p.suffix.lower() not in extensions:return
            if p.stat().st_size<1 or p.stat().st_size>MAX_BYTES:return
            meta=verify_video(p); visited.add(p)
            with p.open("rb") as f: digest=hashlib.file_digest(f,"sha256").hexdigest()
            items.append({"path":str(p),"name":p.name,"size_bytes":p.stat().st_size,
              "sha256":digest,**meta})
        elif isinstance(obj,dict):
            for x in obj.values():visit(x,depth+1)
        elif isinstance(obj,(list,tuple)):
            for x in obj:visit(x,depth+1)
        else:
            p=getattr(obj,"path",None)
            if isinstance(p,(str,Path)):visit(p,depth+1)
    visit(value)
    return items

def run_job(id,payload,paths):
    job_dir=OUT/id
    try:
        job_dir.mkdir(exist_ok=False);record(id,"CONNECTING")
        from gradio_client import Client,handle_file
        client=Client(payload.space,token=os.getenv("HF_TOKEN") or None,
             verbose=False,download_files=str(job_dir))
        params=dict(payload.parameters)
        params.update({name:handle_file(str(p)) for name,p in paths.items()})
        schema_check(client,payload.api_name,set(params))
        record(id,"SUBMITTING")
        job=client.submit(api_name=payload.api_name,**params)
        record(id,"RUNNING")
        res=job.result()
        record(id,"VERIFYING")
        outputs=result_media(res,job_dir,{x.lower() for x in payload.expect_extensions})
        if not outputs: raise RuntimeError("No verified downloaded video in job folder; inspect original remote job before retry")
        record(id,"SUCCEEDED",outputs=outputs)
    except Exception as exc:
        msg=type(exc).__name__+": "+str(exc)
        token=os.getenv("HF_TOKEN")
        if token:msg=msg.replace(token,"[REDACTED]")
        record(id,"FAILED",msg)

@app.post("/api/jobs",status_code=202)
def submit(payload:Request):
    if not SPACE.fullmatch(payload.space) or not API_NAME.fullmatch(payload.api_name):
        raise HTTPException(422,"Space must be owner/name; API must be a named endpoint")
    if set(payload.file_ids)&set(payload.parameters):raise HTTPException(422,"Duplicate parameter names")
    if len(payload.parameters)>32 or any(isinstance(x,(dict,list)) for x in payload.parameters.values()):
        raise HTTPException(422,"Complex parameters require a reviewed Space adapter")
    if not payload.expect_extensions or not set(payload.expect_extensions).issubset(ALLOWED_OUTPUT):
        raise HTTPException(422,"Unsupported output type")
    if not shutil.which("ffprobe"):raise HTTPException(503,"ffprobe required for video validation")
    paths,hashes=resolve_inputs(payload.file_ids)
    sig=hashlib.sha256(json.dumps({"space":payload.space,"api_name":payload.api_name,
         "parameters":payload.parameters,"hashes":hashes,"extensions":payload.expect_extensions},
         sort_keys=True,separators=(",",":")).encode()).hexdigest()
    with LOCK,conn() as db:
        row=db.execute("SELECT * FROM jobs WHERE request_id=?",(payload.request_id,)).fetchone()
        if row:
            if row["signature"]!=sig:raise HTTPException(409,"Request ID reused with different request")
            return asdict(row)|{"reused":True}
        id=str(uuid.uuid4());stamp=now()
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
          (id,payload.request_id,sig,"QUEUED",payload.space,payload.api_name,None,"[]",stamp,stamp))
        db.execute("INSERT INTO events VALUES(?,?,?,?)",(id,stamp,"QUEUED",""))
        row=db.execute("SELECT * FROM jobs WHERE id=?",(id,)).fetchone()
    WORKER.submit(run_job,id,payload,paths)
    return asdict(row)|{"reused":False}

@app.get("/api/jobs/{job_id}")
def status(job_id:str):
    with LOCK,conn() as db:row=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
    if not row:raise HTTPException(404,"Unknown job")
    return asdict(row)

@app.get("/api/jobs/{job_id}/events")
def events(job_id:str):
    status(job_id)
    with LOCK,conn() as db:
        rows=db.execute("SELECT stamp,state,message FROM events WHERE id=? ORDER BY rowid",(job_id,)).fetchall()
    return {"job_id":job_id,"events":[dict(r) for r in rows]}

if __name__=="__main__":
    import uvicorn
    port=int(os.getenv("BRIDGE_PORT","9769"))
    print(f"Bridge ready at http://127.0.0.1:{port}",flush=True)
    uvicorn.run(app,host="127.0.0.1",port=port,access_log=False)
