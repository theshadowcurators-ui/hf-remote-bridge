import hashlib, sys, uuid, time, subprocess, shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server as bridge
from fastapi.testclient import TestClient
api=TestClient(bridge.app)

def upload():
    raw=b"\x89PNG\r\n\x1a\n"+b"fixture"  # transfer only: not for image inference
    resp=api.post("/api/uploads",files={"file":("test.png",raw,"image/png")})
    assert resp.status_code==201,resp.text
    assert resp.json()["sha256"]==hashlib.sha256(raw).hexdigest()
    return resp.json()["file_id"]

def req(file_id):
    return {"space":"zerogpu-aoti/wan2-2-fp8da-aoti-faster","api_name":"/generate_video",
      "request_id":"pytest-"+str(uuid.uuid4()),
      "file_ids":{"input_image":file_id},
      "parameters":{"prompt":"synthetic test","steps":4,"seed":271828},
      "expect_extensions":[".mp4"]}

def terminal(id):
    for i in range(120):
        job=api.get("/api/jobs/"+id).json()
        if job["state"] in ("SUCCEEDED","FAILED","INTERRUPTED"):return job
        time.sleep(.05)
    raise AssertionError("test job timeout")

def test_health_and_spec():
    assert api.get("/health").json()["bind"]=="127.0.0.1"
    spec=api.get("/api/connector_spec").json()
    assert spec["adapter"]=="rest" and spec["job_spec"]["id_field"]=="job_id"

def test_input_hash_and_validation():
    file_id=upload()
    assert bridge.resolve_inputs({"input_image":file_id})[0]["input_image"].is_file()
    assert api.post("/api/uploads",files={"file":("malware.exe",b"x","application/octet-stream")}).status_code==415
    assert api.post("/api/uploads",files={"file":("empty.png",b"","image/png")}).status_code==400

def test_refuse_unsafe_space_and_file():
    p=req(upload());p["space"]="https://example.com"
    assert api.post("/api/jobs",json=p).status_code==422
    p["space"]="owner/name";p["file_ids"]={"input_image":"unknown"}
    assert api.post("/api/jobs",json=p).status_code in (422,503)

def test_success_no_duplicate(monkeypatch,tmp_path):
    import gradio_client
    calls=[]
    class Dummy:
        def __init__(self,*a,download_files=None,**kw):self.out=Path(download_files)
        def view_api(self,**kw):return {"named_endpoints":{"/generate_video":{"parameters":[{"parameter_name":x} for x in ("input_image","prompt","steps","seed")]}}}
        def submit(self,**kw):
            calls.append("submitted")
            f=self.out/"result.mp4"
            if shutil.which("ffmpeg"):
                r=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i",
                  "color=c=black:s=64x64:r=4:d=1","-c:v","mpeg4",str(f)],capture_output=True)
                assert r.returncode==0,r.stderr
            else:f.write_bytes(b"fake-mp4")
            return type("Job",(),{"result":lambda x:{"video":str(f)}})()
    monkeypatch.setattr(gradio_client,"Client",Dummy)
    monkeypatch.setattr(gradio_client,"handle_file",lambda p:p)
    if not shutil.which("ffprobe"):monkeypatch.setattr(bridge,"verify_video",lambda p:{"validation":"fixture"})
    p=req(upload())
    r=api.post("/api/jobs",json=p)
    assert r.status_code==202,r.text
    row=terminal(r.json()["job_id"]);assert row["state"]=="SUCCEEDED",row
    assert row["outputs"] and row["outputs"][0]["sha256"]
    again=api.post("/api/jobs",json=p)
    assert again.json()["reused"] and len(calls)==1
    p["parameters"]["prompt"]="modified"
    assert api.post("/api/jobs",json=p).status_code==409

def test_no_retry_on_remote_quota(monkeypatch):
    import gradio_client
    calls=[]
    class Dummy:
        def __init__(self,*a,**kw):pass
        def view_api(self,**kw):return {"named_endpoints":{"/generate_video":{"parameters":[{"parameter_name":x} for x in ("input_image","prompt","steps","seed")]}}}
        def submit(self,**kw):calls.append(1);raise RuntimeError("ZeroGPU quota exceeded")
    monkeypatch.setattr(gradio_client,"Client",Dummy)
    p=req(upload())
    r=api.post("/api/jobs",json=p)
    row=terminal(r.json()["job_id"])
    assert row["state"]=="FAILED",row
    assert "quota exceeded" in row["error"]
    assert api.post("/api/jobs",json=p).json()["reused"]
    assert len(calls)==1

def test_bad_video_rejected(tmp_path):
    path=tmp_path/"fake.mp4";path.write_bytes(b"not a video")
    if shutil.which("ffprobe"):
        import pytest
        with pytest.raises(Exception):bridge.verify_video(path)
