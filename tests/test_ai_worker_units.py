from pathlib import Path


SYSTEMD_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def test_ai_worker_is_a_restartable_systemd_service():
    service = (SYSTEMD_DIR / "pinpilot-ai-worker.service").read_text(encoding="utf-8")

    assert "Type=simple" in service
    assert "scripts/run_ai_worker.py" in service
    assert "Restart=on-failure" in service
    assert "RestartSec=10" in service
