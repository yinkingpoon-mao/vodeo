import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

_whisper_model = None


def get_ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "ffmpeg 未搵到，請先安裝 (brew install ffmpeg) 再重啟後端"
        )
    return path


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


def extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg = get_ffmpeg_path()
    cmd = [
        ffmpeg, "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"抽取音軌失敗: {result.stderr[-2000:]}")


def get_video_duration(video_path: Path) -> float:
    ffmpeg = get_ffmpeg_path()
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"讀取片長失敗: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def transcribe(audio_path: Path) -> list[dict]:
    model = get_whisper_model()
    segments, _info = model.transcribe(str(audio_path), beam_size=5, vad_filter=True)
    return [
        {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
        for seg in segments
    ]


def find_volume_peaks(audio_path: Path, window_sec: float = 1.0, top_k: int = 25) -> list[dict]:
    with wave.open(str(audio_path), "rb") as wf:
        n_frames = wf.getnframes()
        rate = wf.getframerate()
        raw = wf.readframes(n_frames)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return []

    window = max(1, int(window_sec * rate))
    n_windows = samples.size // window
    if n_windows == 0:
        return []

    trimmed = samples[: n_windows * window].reshape(n_windows, window)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))

    order = np.argsort(rms)[::-1]
    threshold = np.percentile(rms, 90)
    peaks = []
    seen_windows = set()
    for idx in order:
        if rms[idx] < threshold:
            break
        if len(peaks) >= top_k:
            break
        if any(abs(idx - s) < 3 for s in seen_windows):
            continue
        seen_windows.add(idx)
        peaks.append({"time": round(float(idx * window_sec), 2), "level": round(float(rms[idx]), 1)})

    peaks.sort(key=lambda p: p["time"])
    return peaks


HIGHLIGHT_TOOL = {
    "name": "return_highlights",
    "description": "回傳揀選好嘅精華片段清單",
    "input_schema": {
        "type": "object",
        "properties": {
            "highlights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "開始時間（秒）"},
                        "end": {"type": "number", "description": "結束時間（秒）"},
                        "title": {"type": "string", "description": "簡短標題（十字以內）"},
                        "reason": {"type": "string", "description": "點解呢段精華"},
                    },
                    "required": ["start", "end", "title", "reason"],
                },
            }
        },
        "required": ["highlights"],
    },
}


def analyze_highlights(
    api_key: str,
    transcript: list[dict],
    volume_peaks: list[dict],
    duration: float,
    video_kind: str,
    max_highlights: int,
    clip_seconds: tuple[float, float],
) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    transcript_text = "\n".join(
        f"[{seg['start']}s-{seg['end']}s] {seg['text']}" for seg in transcript
    ) or "(冇偵測到人聲對白)"
    peaks_text = "\n".join(
        f"[{p['time']}s] 音量突增 (level={p['level']})" for p in volume_peaks
    ) or "(冇明顯音量爆點)"

    prompt = f"""你係一個影片剪輯助手，負責由一條 {video_kind} 影片入面揀出最精華嘅片段，用嚟剪成一條精華reel。

影片全長: {duration:.1f} 秒

逐句字幕/對白時間軸:
{transcript_text}

音量突增時間點（可能代表爆發、歡呼、擊殺、緊張時刻）:
{peaks_text}

要求:
- 揀出最多 {max_highlights} 段精華片段，每段長度介乎 {clip_seconds[0]:.0f} 至 {clip_seconds[1]:.0f} 秒
- 段與段之間唔好重疊
- 揀嘅時候要考慮對白內容嘅精彩程度，同埋音量爆點反映嘅畫面高潮
- start/end 一定要喺 0 至 {duration:.1f} 秒範圍內
- 用 return_highlights 呢個 tool 回傳結果，唔好用文字回答"""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        tools=[HIGHLIGHT_TOOL],
        tool_choice={"type": "tool", "name": "return_highlights"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "return_highlights":
            highlights = block.input.get("highlights", [])
            cleaned = []
            for h in highlights:
                start = max(0.0, min(float(h["start"]), duration))
                end = max(start, min(float(h["end"]), duration))
                if end - start < 1:
                    continue
                cleaned.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "title": h.get("title", ""),
                    "reason": h.get("reason", ""),
                })
            cleaned.sort(key=lambda h: h["start"])
            return cleaned

    raise RuntimeError("Claude 冇回傳有效嘅精華清單")


def cut_clip(video_path: Path, out_path: Path, start: float, end: float) -> None:
    ffmpeg = get_ffmpeg_path()
    duration = max(0.1, end - start)
    cmd = [
        ffmpeg, "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"剪片失敗: {result.stderr[-2000:]}")


def concat_clips(clip_paths: list[Path], out_path: Path) -> None:
    ffmpeg = get_ffmpeg_path()
    list_file = out_path.with_suffix(".txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"合併片段失敗: {result.stderr[-2000:]}")
