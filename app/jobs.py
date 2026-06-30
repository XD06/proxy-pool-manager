from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_INACTIVE_STATUSES = {"done", "error", "canceled"}


class JobManager(Generic[T]):
    """Encapsulates shared state for background job tracking.

    Each job type (node test, port test, proxy admin, local proxy check) creates
    its own instance, eliminating duplicated state variables and boilerplate
    cancel/get/cleanup logic.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, T] = {}
        self.lock = asyncio.Lock()
        self.canceled: set[str] = set()

    def get(self, job_id: str) -> T | None:
        return self.jobs.get(job_id)

    def request_cancel(self, job_id: str) -> T | None:
        """Mark a job for cancellation. Returns the job if found, None otherwise."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        self.canceled.add(job_id)
        if getattr(job, "status", "") == "running":
            job.status = "canceling"  # type: ignore[attr-defined]
        return job

    def is_canceled(self, job_id: str) -> bool:
        return job_id in self.canceled

    def finish(self, job_id: str) -> None:
        """Discard from canceled set. Call in runner finally block."""
        self.canceled.discard(job_id)

    def cleanup(self, active_id: str | None, retention_seconds: float, max_jobs: int) -> None:
        """Remove old inactive jobs beyond retention time and cap total job count."""
        now = asyncio.get_event_loop().time()
        removable = [
            (job_id, job)
            for job_id, job in self.jobs.items()
            if job_id != active_id and getattr(job, "status", "") in _INACTIVE_STATUSES
        ]
        for job_id, job in list(removable):
            if now - getattr(job, "touched_at", now) > retention_seconds:
                self.jobs.pop(job_id, None)
        removable = [
            (job_id, job)
            for job_id, job in self.jobs.items()
            if job_id != active_id and getattr(job, "status", "") in _INACTIVE_STATUSES
        ]
        removable.sort(key=lambda item: getattr(item[1], "touched_at", 0), reverse=True)
        for job_id, _job in removable[max_jobs:]:
            self.jobs.pop(job_id, None)
