"""Tests for the FastAPI Web Dashboard API and job scheduler."""

from fastapi.testclient import TestClient

from scraper.web.app import app, manager


def test_dashboard_and_scheduler_endpoints():
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Unicraft Web Intelligence Scraper" in home.text

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        bad_job = client.post("/api/jobs", json={"seed_urls": []})
        assert bad_job.status_code == 400

        sched = client.post(
            "/api/schedules",
            json={
                "name": "Test Schedule",
                "interval_minutes": 30,
                "run_immediately": False,
                "config": {"seed_urls": ["https://example.com"], "level": "basic"},
            },
        )
        assert sched.status_code == 200
        sched_id = sched.json()["schedule_id"]

        listed = client.get("/api/schedules")
        assert any(s["schedule_id"] == sched_id for s in listed.json())

        toggled = client.post(f"/api/schedules/{sched_id}/toggle")
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is False

        deleted = client.delete(f"/api/schedules/{sched_id}")
        assert deleted.status_code == 200
        assert sched_id not in manager.schedules
