import json
import os
import random
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

        model_size = os.environ.get("WHISPER_MODEL", "tiny")
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
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
    # Read + compute RMS one window at a time instead of loading the whole
    # file into memory — otherwise peak memory scales with video length and
    # long recordings OOM on memory-constrained hosts.
    rms_values = []
    with wave.open(str(audio_path), "rb") as wf:
        rate = wf.getframerate()
        window = max(1, int(window_sec * rate))
        while True:
            chunk = wf.readframes(window)
            if not chunk:
                break
            arr = np.frombuffer(chunk, dtype=np.int16)
            if arr.size < window:
                break
            rms_values.append(float(np.sqrt(np.mean(arr.astype(np.float64) ** 2))))

    if not rms_values:
        return []

    rms = np.array(rms_values)

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


def _overlapping_text(transcript: list[dict], start: float, end: float) -> str:
    parts = [seg["text"] for seg in transcript if seg["start"] < end and seg["end"] > start]
    text = " ".join(parts).strip()
    return text[:80]


def pick_highlights(
    transcript: list[dict],
    volume_peaks: list[dict],
    duration: float,
    max_highlights: int,
    clip_seconds: tuple[float, float],
) -> list[dict]:
    """Free, local heuristic: turn volume peaks into highlight windows.

    No AI call — peaks are already sorted strongest-first by find_volume_peaks'
    caller ordering, so a simple greedy non-overlap pass is enough. Each
    highlight's length is randomized within clip_seconds so clips vary in size
    instead of all being the same fixed duration.
    """
    candidates = sorted(volume_peaks, key=lambda p: p["level"], reverse=True)

    chosen = []
    for peak in candidates:
        if len(chosen) >= max_highlights:
            break
        clip_len = random.uniform(*clip_seconds)
        lead = clip_len * 0.3
        trail = clip_len * 0.7
        start = max(0.0, peak["time"] - lead)
        end = min(duration, peak["time"] + trail)
        if end - start < 1:
            continue
        if any(start < c["end"] and end > c["start"] for c in chosen):
            continue
        text = _overlapping_text(transcript, start, end)
        chosen.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "title": (text[:20] if text else f"音量爆點 {round(peak['time'])}s"),
            "reason": text if text else "呢段音量突然提高，可能係精彩位",
        })

    chosen.sort(key=lambda h: h["start"])
    return chosen


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

    prompt = f"""你係一個影片剪輯助手，負責由一條影片入面揀出最精華嘅片段，用嚟剪成一條精華reel。

影片全長: {duration:.1f} 秒

逐句字幕/對白時間軸:
{transcript_text}

音量突增時間點（可能代表爆發、歡呼、擊殺、緊張時刻）:
{peaks_text}

要求:
- 揀出最多 {max_highlights} 段精華片段，每段長度介乎 {clip_seconds[0]:.0f} 至 {clip_seconds[1]:.0f} 秒
- 段與段之間唔好重疊
- 揀嘅時候要考慮對白內容嘅精彩程度，同埋音量爆點反映嘅畫面高潮
- 剪接點盡量對齊句子/動作嘅自然開始同結束，唔好切斷緊要嘅一句說話中途
- start/end 一定要喺 0 至 {duration:.1f} 秒範圍內
- 用 return_highlights 呢個 tool 回傳結果，唔好用文字回答"""

    response = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
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
