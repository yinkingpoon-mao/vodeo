import os
import secrets
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
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
    if request.url.path == "/health" or not SITE_PASSWORD:
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


@app.get("/health")
async def health():
    return {"status": "ok"}


def job_public_state(job: Job) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "error": job.error,
        "warning": job.warning,
        "duration": job.duration,
        "highlights": job.highlights,
        "has_final": job.final_path is not None and job.final_path.exists(),
    }


def auto_plan(duration: float) -> tuple[int, tuple[float, float]]:
    """Aim for an 8-13 min YouTube-ready compilation regardless of source length,
    scaling down gracefully for shorter sources. Clip length varies per highlight
    within clip_range rather than being a fixed size."""
    clip_range = (30.0, 180.0)
    avg_clip = sum(clip_range) / 2

    target_total = min(630.0, duration * 0.6)  # 630s ~= 10.5 min, mid of 8-13
    max_highlights = max(3, min(20, round(target_total / avg_clip)))
    return max_highlights, clip_range


# No real cap by default (e.g. local use with plenty of RAM). Constrained
# hosts like a small Railway plan should set MAX_DURATION_SECONDS explicitly.
MAX_DURATION_SECONDS = float(os.environ.get("MAX_DURATION_SECONDS", 4 * 60 * 60))


def run_pipeline(job: Job):
    try:
        job_dir = UPLOADS_DIR / job.id
        audio_path = job_dir / "audio.wav"

        job.status = "extracting_audio"
        pipeline.extract_audio(job.video_path, audio_path)
        job.duration = pipeline.get_video_duration(job.video_path)

        if job.duration > MAX_DURATION_SECONDS:
            raise RuntimeError(
                f"呢個伺服器記憶體有限，只可以處理 {MAX_DURATION_SECONDS / 60:.0f} 分鐘以內嘅片"
                f"（呢條片 {job.duration / 60:.1f} 分鐘）。想剪長片可以用返本機版工具。"
            )

        job.status = "transcribing"
        transcript = pipeline.transcribe(audio_path)

        max_highlights, clip_seconds = auto_plan(job.duration)

        job.status = "analyzing_audio"
        peaks = pipeline.find_volume_peaks(audio_path, top_k=max_highlights * 4)

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        highlights = None
        if anthropic_key:
            job.status = "analyzing_with_ai"
            try:
                highlights = pipeline.analyze_highlights(
                    api_key=anthropic_key,
                    transcript=transcript,
                    volume_peaks=peaks,
                    duration=job.duration,
                    max_highlights=max_highlights,
                    clip_seconds=clip_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                job.warning = f"Claude 分析失敗，已改用免費音量分析。原因: {exc}"

        if highlights is None:
            job.status = "picking_highlights"
            highlights = pipeline.pick_highlights(
                transcript=transcript,
                volume_peaks=peaks,
                duration=job.duration,
                max_highlights=max_highlights,
                clip_seconds=clip_seconds,
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
def upload(file: UploadFile = File(...)):
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
        args=(tmp_job,),
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


@app.get("/api/jobs/{job_id}/candidates.zip")
def get_candidates_zip(job_id: str):
    import zipfile

    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "搵唔到呢個 job")

    candidates_dir = OUTPUTS_DIR / job_id / "candidates"
    if not candidates_dir.exists():
        raise HTTPException(404, "仲未有精華片段")

    zip_path = OUTPUTS_DIR / job_id / "candidates.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for h in job.highlights:
            src = candidates_dir / f"{h['index']}.mp4"
            if not src.exists():
                continue
            label = h.get("title") or f"highlight_{h['index']}"
            safe_label = "".join(c for c in label if c.isalnum() or c in " _-")[:40].strip() or f"highlight_{h['index']}"
            zf.write(src, arcname=f"{h['index']:02d}_{safe_label}.mp4")

    return FileResponse(zip_path, media_type="application/zip", filename=f"highlights_{job_id}.zip")


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
