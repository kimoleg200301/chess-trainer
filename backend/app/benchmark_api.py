"""Authenticated, bounded diagnostic jobs; no changes to games or training."""
from copy import deepcopy
from datetime import datetime, timezone
import logging
import math
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from .config import settings
from .engine_resources import engine_slots
from .models import User
from .security import current_user
from .stockfish_benchmark import BenchmarkError, PROFILE, run_benchmark

router = APIRouter(prefix="/api/diagnostics/stockfish-benchmark")
logger = logging.getLogger(__name__)
RESULT_TTL_SECONDS = 30 * 60
COOLDOWN_SECONDS = 30
MAX_STORED_JOBS = 8


class StartBenchmark(BaseModel):
    # The authenticated client can only run the fixed, bounded profile. No
    # process arguments, executable paths, FENs or resource limits come from it.
    model_config = ConfigDict(extra="forbid")


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class BenchmarkJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.running_id: str | None = None
        self.cooldown_until = 0.0
        self.worker: threading.Thread | None = None

    def _prune(self):
        expired = [key for key, job in self.jobs.items()
                   if key != self.running_id and time.monotonic() - job["created_monotonic"] > RESULT_TTL_SECONDS]
        for key in expired:
            del self.jobs[key]

    @staticmethod
    def view(job):
        return deepcopy({key: value for key, value in job.items()
                         if key not in {"owner_id", "created_monotonic"}})

    def start(self, user_id: int, engine_path: str):
        with self.lock:
            self._prune()
            if self.running_id:
                job = self.jobs[self.running_id]
                if job["owner_id"] == user_id:
                    return self.view(job)
                raise HTTPException(503, "Бенчмарк уже выполняется. Повторите позже.", headers={"Retry-After": "5"})
            remaining = self.cooldown_until - time.monotonic()
            if remaining > 0:
                raise HTTPException(429, "Предыдущий замер завершён. Подождите перед новым запуском.",
                                    headers={"Retry-After": str(math.ceil(remaining))})
            if not engine_slots.acquire(blocking=False):
                raise HTTPException(503, "Stockfish занят анализом. Запустите бенчмарк после его завершения.",
                                    headers={"Retry-After": "5"})
            job_id = uuid4().hex
            job = {"id": job_id, "owner_id": user_id, "created_monotonic": time.monotonic(),
                   "status": "running", "started_at": timestamp(), "finished_at": None,
                   "profile": deepcopy(PROFILE), "repetitions": 3,
                   "result": None, "error": None}
            while len(self.jobs) >= MAX_STORED_JOBS:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[job_id] = job
            self.running_id = job_id
            self.worker = threading.Thread(target=self._execute, args=(job_id, engine_path),
                                           name="stockfish-benchmark", daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.running_id = None
                del self.jobs[job_id]
                engine_slots.release()
                raise HTTPException(503, "Не удалось запустить бенчмарк. Повторите позже.") from None
            return self.view(job)

    def _execute(self, job_id: str, engine_path: str):
        result, error, status = None, None, "failed"
        try:
            result = run_benchmark(engine_path, repetitions=3, timeout_per_run=60)
            status = "completed"
        except BenchmarkError as exc:
            result = exc.partial_report
            error = {"code": exc.code, "detail": str(exc)}
        except Exception:
            logger.exception("Stockfish diagnostic failed")
            error = {"code": "benchmark_failed", "detail": "Бенчмарк не завершён. Повторите позже."}
        finally:
            with self.lock:
                self.jobs[job_id].update(status=status, result=result, error=error, finished_at=timestamp())
                self.running_id = None
                self.cooldown_until = time.monotonic() + COOLDOWN_SECONDS
                engine_slots.release()

    def get(self, job_id: str, user_id: int):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if job is None or job["owner_id"] != user_id:
                raise HTTPException(404, "Результат бенчмарка не найден или истёк после перезапуска сервера.")
            return self.view(job)


jobs = BenchmarkJobs()


@router.get("")
def benchmark_profile(user: User = Depends(current_user)):
    return {"profile": PROFILE, "repetitions": 3, "max_seconds_per_run": 60,
            "result_ttl_seconds": RESULT_TTL_SECONDS, "cooldown_seconds": COOLDOWN_SECONDS}


@router.post("", status_code=202)
def start_benchmark(body: StartBenchmark, user: User = Depends(current_user)):
    return jobs.start(user.id, settings().stockfish_path)


@router.get("/{job_id}")
def get_benchmark(job_id: str, user: User = Depends(current_user)):
    return jobs.get(job_id, user.id)
