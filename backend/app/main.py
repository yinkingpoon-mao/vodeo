import os
import secrets
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .jobs import Job, store

BASE_DIR = Path(__file__).resolve().parents[2]
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
FRONTEND_DIR = BASE_DIR / "frontend"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="剪片精華助手")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SITE_USERNAME = os.environ.get("SITE_USERNAME", "admin")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not SITE_PASSWORD:
        return await call_next(request)

    header = request.headers.get("authorization")
    if header and header.startswith("Basic "):
        import base64

        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:  # noqa: BLE001
            user, pwd = "", ""
        if secrets.compare_digest(user, SITE_USERNAME) and secrets.compare_digest(pwd, SITE_PASSWORD):
            return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Video Highlights"'},
        content="需要登入",
    )


def job_public_state(job: Job) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "error": job.error,
        "duration": job.duration,
        "highlights": job.highlights,
        "has_final": job.final_path is not None and job.final_path.exists(),
    }


def auto_max_highlights(duration: float) -> int:
    return max(3, min(12, round(duration / 90)))


def run_pipeline(job: Job, video_kind: str, clip_min: float, clip_max: float):
    try:
        job_dir = UPLOADS_DIR / job.id
        audio_path = job_dir / "audio.wav"

        job.status = "extracting_audio"
        pipeline.extract_audio(job.video_path, audio_path)
        job.duration = pipeline.get_video_duration(job.video_path)

        job.status = "transcribing"
        transcript = pipeline.transcribe(audio_path)

        job.status = "analyzing_audio"
        peaks = pipeline.find_volume_peaks(audio_path)

        job.status = "analyzing_with_ai"
        highlights = pipeline.analyze_highlights(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            transcript=transcript,
            volume_peaks=peaks,
            duration=job.duration,
            video_kind=video_kind,
            max_highlights=auto_max_highlights(job.duration),
            clip_seconds=(clip_min, clip_max),
        )

        job.status = "cutting_previews"
        candidates_dir = OUTPUTS_DIR / job.id / "candidates"
        candidates_dir.mkdir(parents=True, exist_ok=True)
        for i, h in enumerate(highlights):
            clip_path = candidates_dir / f"{i}.mp4"
            pipeline.cut_clip(job.video_path, clip_path, h["start"], h["end"])
            h["index"] = i
            h["selected"] = True

        job.highlights = highlights
        job.status = "awaiting_review"
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        job.status = "error"


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    video_kind: str = Form("一般影片"),
    clip_min: float = Form(5.0),
    clip_max: float = Form(20.0),
):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "伺服器未設定 ANTHROPIC_API_KEY")

    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp_job = store.create(video_path=Path())
    job_dir = UPLOADS_DIR / tmp_job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    video_path = job_dir / f"source{ext}"

    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    tmp_job.video_path = video_path

    thread = threading.Thread(
        target=run_pipeline,
        args=(tmp_job, video_kind, clip_min, clip_max),
        daemon=True,
    )
    thread.start()

    return {"job_id": tmp_job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "搵唔到呢個 job")
    return job_public_state(job)


@app.get("/api/jobs/{job_id}/candidate/{index}")
async def get_candidate(job_id: str, index: int):
    path = OUTPUTS_DIR / job_id / "candidates" / f"{index}.mp4"
    if not path.exists():
        raise HTTPException(404, "搵唔到呢段片段")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/jobs/{job_id}/render")
async def render(job_id: str, selection: dict):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "搵唔到呢個 job")
    if job.status != "awaiting_review":
        raise HTTPException(400, f"job 而家狀態係 {job.status}，未可以 render")

    selected_indices = selection.get("selected_indices", [])
    if not selected_indices:
        raise HTTPException(400, "請至少揀一段片段")

    def do_render():
        try:
            job.status = "rendering"
            candidates_dir = OUTPUTS_DIR / job.id / "candidates"
            clip_paths = [candidates_dir / f"{i}.mp4" for i in selected_indices]
            for p in clip_paths:
                if not p.exists():
                    raise RuntimeError(f"片段 {p.name} 唔存在")
            final_path = OUTPUTS_DIR / job.id / "final.mp4"
            pipeline.concat_clips(clip_paths, final_path)
            job.final_path = final_path
            job.status = "done"
        except Exception as exc:  # noqa: BLE001
            job.error = str(exc)
            job.status = "error"

    thread = threading.Thread(target=do_render, daemon=True)
    thread.start()
    return {"status": "rendering"}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str):
    job = store.get(job_id)
    if not job or not job.final_path or not job.final_path.exists():
        raise HTTPException(404, "精華片仲未整好")
    return FileResponse(job.final_path, media_type="video/mp4", filename=f"highlights_{job_id}.mp4")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
