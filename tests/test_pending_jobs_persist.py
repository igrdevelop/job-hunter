"""Round-trip test for _pending_jobs serialization."""
import dataclasses
import json
from pathlib import Path
import pytest

from hunter.models import Job


def _make_job(url: str = "https://example.com/job/1") -> Job:
    return Job(
        title="Senior Angular Developer",
        company="Acme Corp",
        location="Remote",
        salary="15 000–20 000 PLN",
        url=url,
        source="justjoin",
        raw={"id": 42, "tags": ["angular"]},
    )


def test_job_roundtrip_via_asdict():
    job = _make_job()
    data = dataclasses.asdict(job)
    restored = Job(**data)
    assert restored == job


def test_pending_jobs_save_and_load(tmp_path, monkeypatch):
    import hunter.telegram_bot as bot

    # Point the file to a temp location
    monkeypatch.setattr(bot, "_PENDING_JOBS_FILE", tmp_path / "pending_jobs.json")
    monkeypatch.setattr(bot, "_pending_jobs", {})

    job1 = _make_job("https://example.com/job/1")
    job2 = _make_job("https://example.com/job/2")
    bot._pending_jobs[job1.job_id()] = job1
    bot._pending_jobs[job2.job_id()] = job2

    bot._save_pending()
    assert (tmp_path / "pending_jobs.json").exists()

    # Clear and reload
    bot._pending_jobs.clear()
    bot._load_pending()

    assert len(bot._pending_jobs) == 2
    assert bot._pending_jobs[job1.job_id()] == job1
    assert bot._pending_jobs[job2.job_id()] == job2


def test_load_pending_missing_file(tmp_path, monkeypatch):
    import hunter.telegram_bot as bot

    monkeypatch.setattr(bot, "_PENDING_JOBS_FILE", tmp_path / "nonexistent.json")
    monkeypatch.setattr(bot, "_pending_jobs", {})

    bot._load_pending()  # must not raise
    assert bot._pending_jobs == {}


def test_load_pending_corrupt_file(tmp_path, monkeypatch):
    import hunter.telegram_bot as bot

    corrupt = tmp_path / "pending_jobs.json"
    corrupt.write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(bot, "_PENDING_JOBS_FILE", corrupt)
    monkeypatch.setattr(bot, "_pending_jobs", {})

    bot._load_pending()  # must not raise
    assert bot._pending_jobs == {}


def test_save_pending_empty_dict(tmp_path, monkeypatch):
    import hunter.telegram_bot as bot

    monkeypatch.setattr(bot, "_PENDING_JOBS_FILE", tmp_path / "pending_jobs.json")
    monkeypatch.setattr(bot, "_pending_jobs", {})

    bot._save_pending()
    data = json.loads((tmp_path / "pending_jobs.json").read_text())
    assert data == {}
