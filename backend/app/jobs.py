import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Job:
    id: str
    video_path: Path
    status: str = "queued"
    error: Optional[str] = None
    warning: Optional[str] = None
    duration: float = 0.0
    highlights: list = field(default_factory=list)
    final_path: Optional[Path] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, video_path: Path) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, video_path=video_path)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)


store = JobStore()
