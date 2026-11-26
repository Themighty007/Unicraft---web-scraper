"""In-memory async job manager and recurring interval scheduler for CLI and Web Dashboard."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from scraper.logging_utils import JobLogBuffer, get_logger
from scraper.models import ScraperConfig, ScrapeRunSummary
from scraper.pipeline import run_scrape_pipeline

logger = get_logger("scheduler")


@dataclass
class JobRecord:
    """Live state of a single scraping job."""

    job_id: str
    config: ScraperConfig
    status: str = "queued"
    stage: str = "queued"
    message: str = "Waiting to start"
    processed: int = 0
    total: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    summary: ScrapeRunSummary | None = None
    error: str | None = None
    logs: list[dict[str, str]] = field(default_factory=list)
    task: asyncio.Task | None = None


@dataclass
class ScheduledJob:
    """Recurring interval schedule definition."""

    schedule_id: str
    name: str
    config: ScraperConfig
    interval_minutes: int
    enabled: bool = True
    last_run_at: str | None = None
    next_run_at: str = ""
    last_job_id: str | None = None
    run_count: int = 0


class JobManager:
    """Coordinates background scraping jobs and recurring interval schedules."""

    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.schedules: dict[str, ScheduledJob] = {}
        self._ticker_task: asyncio.Task | None = None
        self._running = False

    async def start_scheduler_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self._ticker_task = asyncio.create_task(self._loop())

    async def stop_scheduler_loop(self) -> None:
        self._running = False
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
            self._ticker_task = None

    async def _loop(self) -> None:
        while self._running:
            now = datetime.now(timezone.utc)
            for sched in list(self.schedules.values()):
                if not sched.enabled or not sched.next_run_at:
                    continue
                try:
                    due = datetime.fromisoformat(sched.next_run_at)
                except ValueError:
                    continue
                if now >= due:
                    logger.info("Triggering scheduled job '%s' (%s)", sched.name, sched.schedule_id)
                    job = self.submit_job(sched.config)
                    sched.last_job_id = job.job_id
                    sched.last_run_at = now.isoformat()
                    sched.next_run_at = (
                        now + timedelta(minutes=sched.interval_minutes)
                    ).isoformat()
                    sched.run_count += 1
            await asyncio.sleep(2.0)

    def submit_job(self, config: ScraperConfig) -> JobRecord:
        """Queue and start a scraping job asynchronously."""
        job_id = uuid.uuid4().hex[:8]
        record = JobRecord(job_id=job_id, config=config)
        self.jobs[job_id] = record
        record.task = asyncio.create_task(self._execute_job(record))
        return record

    async def _execute_job(self, record: JobRecord) -> None:
        record.status = "running"
        record.stage = "starting"

        def _append_log(entry: dict[str, str]) -> None:
            record.logs.append(entry)
            if len(record.logs) > 400:
                record.logs.pop(0)

        log_handler = JobLogBuffer(on_record=_append_log)
        root_logger = logging.getLogger("unicraft")
        root_logger.addHandler(log_handler)

        async def _on_progress(event: dict[str, Any]) -> None:
            ev = event.get("event")
            if ev == "validating":
                record.stage = "validating"
                record.message = event.get("message", "Validating URLs...")
            elif ev == "discovering":
                record.stage = "discovering"
                record.message = event.get("message", "Discovering companies...")
            elif ev == "targets_ready":
                record.stage = "scraping"
                record.total = int(event.get("total", 0))
                record.message = f"Scraping {record.total} target company website(s)"
            elif ev == "target_started":
                record.message = f"Fetching {event.get('url')} ({event.get('index')}/{event.get('total')})"
            elif ev == "target_completed":
                record.processed = int(event.get("processed", record.processed))
                record.total = int(event.get("total", record.total))
                record.message = f"Extracted {event.get('company')} ({record.processed}/{record.total})"
            elif ev == "completed":
                record.stage = "completed"
                record.message = "Completed"

        try:
            summary = await run_scrape_pipeline(
                config=record.config,
                job_id=record.job_id,
                on_progress=_on_progress,
                save_outputs=True,
            )
            record.summary = summary
            record.status = "completed"
            record.stage = "completed"
            record.processed = summary.processed_targets
            record.total = summary.total_targets
            record.message = (
                f"Done: {summary.succeeded} succeeded, {summary.failed} failed in {summary.duration_seconds:.2f}s"
            )
        except Exception as exc:
            record.status = "failed"
            record.stage = "failed"
            record.error = str(exc)
            record.message = f"Failed: {exc}"
            logger.exception("Job %s failed", record.job_id)
        finally:
            root_logger.removeHandler(log_handler)

    def create_schedule(
        self,
        name: str,
        config: ScraperConfig,
        interval_minutes: int,
        run_immediately: bool = False,
    ) -> ScheduledJob:
        sched_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        next_run = now if run_immediately else (now + timedelta(minutes=interval_minutes))
        sched = ScheduledJob(
            schedule_id=sched_id,
            name=name.strip() or f"Schedule-{sched_id}",
            config=config,
            interval_minutes=max(1, interval_minutes),
            next_run_at=next_run.isoformat(),
        )
        self.schedules[sched_id] = sched
        return sched

    def serialize_job(self, record: JobRecord) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "status": record.status,
            "stage": record.stage,
            "message": record.message,
            "processed": record.processed,
            "total": record.total,
            "created_at": record.created_at,
            "error": record.error,
            "logs": record.logs[-100:],
            "config": record.config.model_dump(mode="json"),
            "summary": record.summary.model_dump(mode="json") if record.summary else None,
        }

    def serialize_schedule(self, sched: ScheduledJob) -> dict[str, Any]:
        return {
            "schedule_id": sched.schedule_id,
            "name": sched.name,
            "interval_minutes": sched.interval_minutes,
            "enabled": sched.enabled,
            "last_run_at": sched.last_run_at,
            "next_run_at": sched.next_run_at,
            "last_job_id": sched.last_job_id,
            "run_count": sched.run_count,
            "query": sched.config.query,
            "seed_urls": sched.config.seed_urls,
            "level": sched.config.level.value,
        }
