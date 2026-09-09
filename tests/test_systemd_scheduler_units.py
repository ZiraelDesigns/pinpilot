from pathlib import Path


SYSTEMD_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def test_daily_queue_service_is_oneshot_and_uses_existing_script():
    service = (SYSTEMD_DIR / "pinpilot-daily-queue.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "WorkingDirectory=/opt/pinpilot" in service
    assert "scripts/generate_daily_pins.py" in service
    assert "StandardOutput=journal" in service


def test_daily_queue_timer_runs_daily_and_recovers_after_reboot():
    timer = (SYSTEMD_DIR / "pinpilot-daily-queue.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 00:05:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "pinpilot-daily-queue.service" in timer
