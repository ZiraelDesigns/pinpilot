"""Run the local Etsy read-only synchronization once for systemd."""

from __future__ import annotations

import logging

from app.database import SessionLocal
from app.models import EtsyAccount
from app.services.etsy import EtsyApiService, EtsyIntegrationError

logger = logging.getLogger(__name__)


def run_etsy_sync() -> int:
    """Synchronize the active account once; never invoke AI or Pinterest providers."""
    db = SessionLocal()
    try:
        account = db.query(EtsyAccount).filter_by(is_active=True).first()
        if not account:
            logger.info("Etsy sync skipped: no active Etsy account")
            return 0
        result = EtsyApiService(db, account).sync()
        logger.info(
            "Etsy sync completed: listings=%s new_products=%s changed_products=%s "
            "new_mockups=%s inactive=%s",
            result.processed_listings, result.new_products, result.changed_products,
            result.new_mockup_creatives, result.inactive_listings,
        )
        return 0
    except EtsyIntegrationError:
        logger.exception("Etsy sync failed")
        return 1
    except Exception:
        logger.exception("Etsy sync failed unexpectedly")
        return 1
    finally:
        db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run_etsy_sync()


if __name__ == "__main__":
    raise SystemExit(main())
