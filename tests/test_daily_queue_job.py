import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_daily_pins.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("daily_queue_job", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeDatabase:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SuccessfulScheduler:
    def __init__(self, db):
        self.db = db

    def schedule_daily(self):
        return type("Result", (), {
            "already_prepared": 0,
            "prepared": (object(),),
            "mockups_prepared": 1,
            "ai_prepared": 0,
            "pending_ai_jobs": 0,
            "remaining": 14,
        })()


class _FailingScheduler:
    def __init__(self, db):
        self.db = db

    def schedule_daily(self):
        raise RuntimeError("expected test failure")


def test_daily_queue_job_completes_and_closes_database(monkeypatch):
    module = _load_script_module()
    db = _FakeDatabase()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module, "DailyPinScheduler", _SuccessfulScheduler)

    assert module.run_daily_queue() == 0
    assert db.closed is True


def test_daily_queue_job_logs_failure_and_preserves_web_service(monkeypatch, caplog):
    module = _load_script_module()
    db = _FakeDatabase()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module, "DailyPinScheduler", _FailingScheduler)

    assert module.run_daily_queue() == 1
    assert db.closed is True
    assert "Daily Pin queue failed" in caplog.text
