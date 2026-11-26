"""FastAPI backend powering the Unicraft Web Dashboard and recurring job scheduler."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from scraper.exporter import export_csv, export_json, export_markdown
from scraper.models import ScraperConfig
from scraper.scheduler import JobManager
from scraper.validator import validate_seed_urls

STATIC_DIR = Path(__file__).parent / "static"
manager = JobManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await manager.start_scheduler_loop()
    try:
        yield
    finally:
        await manager.stop_scheduler_loop()


app = FastAPI(
    title="Unicraft Web Scraper Dashboard",
    version="0.1.0",
    lifespan=lifespan,
)


class ValidateUrlsRequest(BaseModel):
    urls: list[str]
    verify_reachability: bool = True


class CreateScheduleRequest(BaseModel):
    name: str = Field(..., min_length=1)
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    run_immediately: bool = False
    config: ScraperConfig


@app.get("/", response_class=HTMLResponse)
async def dashboard_home() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI file missing")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/validate")
async def api_validate_urls(payload: ValidateUrlsRequest) -> list[dict]:
    results = await validate_seed_urls(
        payload.urls, verify_reachability=payload.verify_reachability
    )
    return [r.model_dump(mode="json") for r in results]


@app.post("/api/jobs")
async def api_create_job(config: ScraperConfig) -> dict:
    if not config.query and not config.seed_urls:
        raise HTTPException(
            status_code=400,
            detail="Provide either a search query or at least one seed URL.",
        )
    record = manager.submit_job(config)
    return manager.serialize_job(record)


@app.get("/api/jobs")
async def api_list_jobs() -> list[dict]:
    ordered = sorted(manager.jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [manager.serialize_job(j) for j in ordered]


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str) -> dict:
    record = manager.jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return manager.serialize_job(record)


@app.get("/api/jobs/{job_id}/export/{fmt}")
async def api_export_job(job_id: str, fmt: str):
    record = manager.jobs.get(job_id)
    if not record or not record.summary:
        raise HTTPException(status_code=404, detail="Completed job summary not available yet")

    fmt = fmt.lower()
    out_dir = Path(record.config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"scrape_{job_id}"

    if fmt == "json":
        path = export_json(record.summary, out_dir / f"{stem}.json")
        return FileResponse(path, media_type="application/json", filename=f"{stem}.json")
    if fmt == "csv":
        path = export_csv(record.summary.companies, out_dir / f"{stem}.csv")
        return FileResponse(path, media_type="text/csv", filename=f"{stem}.csv")
    if fmt in ("md", "markdown"):
        path = export_markdown(record.summary, out_dir / f"{stem}.md")
        return FileResponse(path, media_type="text/markdown", filename=f"{stem}.md")

    return Response(status_code=400, content=f"Unsupported export format: {fmt}")


@app.get("/api/schedules")
async def api_list_schedules() -> list[dict]:
    return [manager.serialize_schedule(s) for s in manager.schedules.values()]


@app.post("/api/schedules")
async def api_create_schedule(payload: CreateScheduleRequest) -> dict:
    if not payload.config.query and not payload.config.seed_urls:
        raise HTTPException(
            status_code=400,
            detail="Provide either a search query or at least one seed URL.",
        )
    sched = manager.create_schedule(
        name=payload.name,
        config=payload.config,
        interval_minutes=payload.interval_minutes,
        run_immediately=payload.run_immediately,
    )
    return manager.serialize_schedule(sched)


@app.post("/api/schedules/{schedule_id}/toggle")
async def api_toggle_schedule(schedule_id: str) -> dict:
    sched = manager.schedules.get(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sched.enabled = not sched.enabled
    return manager.serialize_schedule(sched)


@app.post("/api/schedules/{schedule_id}/run")
async def api_trigger_schedule_now(schedule_id: str) -> dict:
    sched = manager.schedules.get(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    job = manager.submit_job(sched.config)
    sched.last_job_id = job.job_id
    sched.run_count += 1
    return manager.serialize_job(job)


@app.delete("/api/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: str) -> dict[str, str]:
    if schedule_id not in manager.schedules:
        raise HTTPException(status_code=404, detail="Schedule not found")
    del manager.schedules[schedule_id]
    return {"deleted": schedule_id}
