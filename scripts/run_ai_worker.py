"""Run the local AI creative worker under systemd."""

from __future__ import annotations

import logging

from app.database import SessionLocal
from app.services.ai_worker import AIGenerationWorker


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    AIGenerationWorker(SessionLocal).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
