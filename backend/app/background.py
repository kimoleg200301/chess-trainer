"""Durable analysis intent in PostgreSQL, bounded work in the API lifespan.

No browser heartbeat is needed. Each iteration checkpoints one position using
the same ownership, locks and engine limits as the interactive step routes.
"""
import logging
from threading import Event, Thread

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from .db import engine
from .models import GameAnalysis, ImportJob, User

logger = logging.getLogger(__name__)


class BackgroundStart(BaseModel):
    background: bool = False


def request_background(db: Session, record, version: int, enabled: bool):
    """Only change execution intent, even if a step is currently computing.

    Deliberately do not take the long-lived user/engine lock: Pause must be
    accepted during a search. The current position may still be checkpointed.
    """
    model = type(record)
    if enabled and record.analysis_version != version:
        raise HTTPException(409, "Метод проверки обновлён. Запустите пересчёт анализа.")
    values = {"background_requested": enabled}
    if enabled:
        values.update(status=case((model.status == "failed", "ready"), else_=model.status), error=None)
    db.execute(update(model).where(model.id == record.id, model.status != "completed",
                                  model.analysis_version == version if enabled else True)
               .values(**values).execution_options(synchronize_session=False))
    db.commit()
    db.refresh(record)


class AnalysisWorker:
    def __init__(self):
        self.stopping = Event()
        self.thread = Thread(target=self.run, name="analysis-worker", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        # Finish the bounded current step before the application exits.
        if self.thread.is_alive():
            self.thread.join()

    def run(self):
        while not self.stopping.is_set():
            try:
                progressed = self.run_once()
            except Exception:
                logger.exception("Background analysis iteration failed; saved checkpoints retained")
                progressed = False
            self.stopping.wait(0.2 if progressed else 2)

    def run_once(self) -> bool:
        from . import game_analysis_api, imports_api, coach_worker
        targets = ((ImportJob, imports_api.ANALYSIS_VERSION, imports_api.process_step),
                   (GameAnalysis, game_analysis_api.ANALYSIS_VERSION, game_analysis_api.process_step))
        # A transaction-scoped coordinator lock also works with multiple API
        # processes. It is released automatically on crash/connection loss.
        with Session(engine()) as coordinator:
            if not coordinator.scalar(select(func.pg_try_advisory_xact_lock(782106, 0))):
                return False
            pending = []
            for model, version, process in targets:
                rows = coordinator.execute(select(model.id, model.user_id, model.updated_at).where(
                    model.background_requested.is_(True), model.status.in_(["ready", "running"]),
                    model.analysis_version == version)).all()
                pending.extend((row.updated_at, row.id, row.user_id, process) for row in rows)
            pending.extend((row.updated_at, row.id, row.user_id, coach_worker.process_step)
                           for row in coach_worker.pending_profiles(coordinator))
            # Oldest checkpoint first prevents a long batch starving a report.
            for _, record_id, user_id, process in sorted(pending, key=lambda item: item[0]):
                if self.stopping.is_set():
                    return False
                with Session(engine(), expire_on_commit=False) as db:
                    user = db.get(User, user_id)
                    if user is None:
                        continue
                    try:
                        result = process(record_id, db, user, background=True)
                        if result is not None:
                            return True
                    except HTTPException as exc:
                        if exc.status_code not in (404, 409, 503):
                            raise
                        # Busy engine/user: retry later. Engine failures disable
                        # the job in process_step and preserve its last checkpoint.
            return False
