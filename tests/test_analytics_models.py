from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import (
    AnalyticsCollectionRun,
    AnalyticsSnapshot,
    Pin,
    PinCreative,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    PinterestBoard,
    Product,
    PublishedPinterestPin,
)


def _local_pin(db):
    product = Product(title="Analytics product", url="https://etsy.example.test/listing/1")
    creative = PinCreative(
        product=product,
        creative_type="lifestyle",
        title="Quiet candle evening",
        description="A candle for a quiet evening.",
        keywords=["candle"],
        seo_metadata={
            "primary_keyword": "handmade candle",
            "creative_angle": "quiet evening ritual",
            "secondary_keywords": ["soy candle"],
        },
        call_to_action="Explore",
        image_path="/media/generated/candle.png",
        source_type="ai",
        destination_url="https://etsy.example.test/listing/1",
        generation_key="analytics-test-creative",
    )
    pin = Pin(
        product=product,
        creative=creative,
        title=creative.title,
        description=creative.description,
        image_path=creative.image_path,
        destination_url=creative.destination_url,
        status="published",
        published_at=datetime(2026, 9, 20, 10),
    )
    db.add(pin)
    db.flush()
    return product, creative, pin


def _account(db, identifier="analytics-account"):
    account = PinterestAccount(account_name=identifier, account_identifier=identifier, is_active=True)
    db.add(account)
    db.flush()
    return account


def test_published_pin_relationships_and_external_id_uniqueness():
    db = SessionLocal()
    try:
        product, creative, pin = _local_pin(db)
        account = _account(db)
        board = PinterestBoard(account=account, board_id="board-1", name="Ideas")
        published = PublishedPinterestPin(
            pin=pin,
            account=account,
            board=board,
            external_pin_id="remote-pin-1",
            published_at=datetime(2026, 9, 20, 10),
            metadata_snapshot=PublishedPinterestPin.capture_metadata(pin),
        )
        db.add(published)
        db.commit()

        loaded = db.get(PublishedPinterestPin, published.id)
        assert loaded.pin.id == pin.id
        assert loaded.account.id == account.id
        assert loaded.board.id == board.id
        assert loaded.metadata_snapshot["product_id"] == product.id
        assert loaded.metadata_snapshot["creative_type"] == creative.creative_type

        db.add(PublishedPinterestPin(
            pin=pin,
            account=account,
            external_pin_id="remote-pin-1",
            published_at=datetime(2026, 9, 21, 10),
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("Duplicate account/external Pin ID must be rejected")
    finally:
        db.close()


def test_external_pin_id_can_repeat_across_accounts_and_local_pin_can_be_republished():
    db = SessionLocal()
    try:
        _, _, pin = _local_pin(db)
        first = _account(db, "account-one")
        second = _account(db, "account-two")
        db.add_all([
            PublishedPinterestPin(pin=pin, account=first, external_pin_id="same-remote-id", published_at=datetime(2026, 9, 20)),
            PublishedPinterestPin(pin=pin, account=second, external_pin_id="same-remote-id", published_at=datetime(2026, 9, 21)),
            # No local-pin/account unique constraint: null external IDs may represent
            # separate future variant or re-publish events.
            PublishedPinterestPin(pin=pin, account=first, external_pin_id=None, published_at=datetime(2026, 9, 22)),
            PublishedPinterestPin(pin=pin, account=first, external_pin_id=None, published_at=datetime(2026, 9, 23)),
        ])
        db.commit()
        assert db.query(PublishedPinterestPin).filter_by(pin_id=pin.id).count() == 4
    finally:
        db.close()


def test_pin_snapshots_allow_history_and_prevent_same_run_period_duplicates():
    db = SessionLocal()
    try:
        _, _, pin = _local_pin(db)
        account = _account(db)
        published = PublishedPinterestPin(pin=pin, account=account, published_at=datetime(2026, 9, 20))
        run = AnalyticsCollectionRun(account=account, status="completed", scope="pin")
        db.add_all([published, run])
        db.flush()
        first = AnalyticsSnapshot(
            pin=pin,
            published_pin=published,
            collection_run=run,
            metric_date=date(2026, 9, 20),
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
            fetched_at=datetime(2026, 9, 20, 11),
            impressions=12,
            saves=0,
            pin_clicks=3,
            engagements=4,
            engagement_rate=Decimal("0.333333"),
            metric_schema_version="v1",
        )
        db.add(first)
        db.commit()
        assert first.pin_click_rate is None
        assert first.outbound_click_rate is None
        assert first.engagement_rate == Decimal("0.333333")

        db.add(AnalyticsSnapshot(
            pin=pin,
            published_pin=published,
            collection_run=run,
            metric_date=date(2026, 9, 20),
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
            fetched_at=datetime(2026, 9, 20, 12),
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("Same run and reporting period must be idempotent")

        next_run = AnalyticsCollectionRun(account=account, status="completed", scope="pin")
        db.add(next_run)
        db.flush()
        db.add(AnalyticsSnapshot(
            pin=pin,
            published_pin=published,
            collection_run=next_run,
            metric_date=date(2026, 9, 20),
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
            fetched_at=datetime(2026, 9, 20, 13),
        ))
        db.commit()
        assert db.query(AnalyticsSnapshot).filter_by(published_pin_id=published.id).count() == 2
        assert first.impressions == 12 and first.saves == 0
    finally:
        db.close()


def test_account_snapshots_are_nullable_and_idempotent_per_run_period():
    db = SessionLocal()
    try:
        account = _account(db)
        run = AnalyticsCollectionRun(account=account, status="completed", scope="account")
        db.add(run)
        db.flush()
        snapshot = PinterestAccountAnalyticsSnapshot(
            account=account,
            collection_run=run,
            metric_date=date(2026, 9, 20),
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
            fetched_at=datetime(2026, 9, 20, 11),
        )
        db.add(snapshot)
        db.commit()
        assert snapshot.profile_visits is None
        assert snapshot.follows is None
        assert snapshot.total_audience is None
        assert snapshot.engaged_audience is None

        db.add(PinterestAccountAnalyticsSnapshot(
            account=account,
            collection_run=run,
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("Same account run and period must be idempotent")

        next_run = AnalyticsCollectionRun(account=account, status="completed", scope="account")
        db.add(next_run)
        db.flush()
        db.add(PinterestAccountAnalyticsSnapshot(
            account=account,
            collection_run=next_run,
            period_start=datetime(2026, 9, 19),
            period_end=datetime(2026, 9, 20),
            follows=0,
        ))
        db.commit()
        assert db.query(PinterestAccountAnalyticsSnapshot).count() == 2
    finally:
        db.close()


def test_published_metadata_is_an_immutable_copy_of_creative_fields():
    db = SessionLocal()
    try:
        _, creative, pin = _local_pin(db)
        captured = PublishedPinterestPin.capture_metadata(pin)
        published = PublishedPinterestPin(
            pin=pin,
            published_at=datetime(2026, 9, 20),
            metadata_snapshot=captured,
        )
        db.add(published)
        db.commit()

        creative.title = "Edited title"
        creative.seo_metadata = {"primary_keyword": "changed keyword"}
        db.commit()
        db.refresh(published)
        assert published.metadata_snapshot["creative_title"] == "Quiet candle evening"
        assert published.metadata_snapshot["primary_keyword"] == "handmade candle"
        assert published.metadata_snapshot["creative_angle"] == "quiet evening ritual"
    finally:
        db.close()


def test_account_delete_preserves_publication_and_analytics_history():
    db = SessionLocal()
    try:
        _, _, pin = _local_pin(db)
        account = _account(db)
        board = PinterestBoard(account=account, board_id="delete-test-board", name="History")
        run = AnalyticsCollectionRun(account=account, scope="all")
        published = PublishedPinterestPin(
            pin=pin,
            account=account,
            board=board,
            account_identifier_snapshot=account.account_identifier,
            external_pin_id="delete-test-pin",
            published_at=datetime(2026, 9, 20),
            metadata_snapshot=PublishedPinterestPin.capture_metadata(pin),
        )
        pin_snapshot = AnalyticsSnapshot(pin=pin, published_pin=published, collection_run=run)
        account_snapshot = PinterestAccountAnalyticsSnapshot(account=account, collection_run=run, follows=7)
        db.add_all([run, published, pin_snapshot, account_snapshot])
        db.commit()
        published_id, run_id = published.id, run.id
        pin_snapshot_id, account_snapshot_id = pin_snapshot.id, account_snapshot.id

        db.delete(account)
        db.commit()

        preserved = db.get(PublishedPinterestPin, published_id)
        assert preserved is not None
        assert preserved.account_id is None and preserved.board_id is None
        assert preserved.external_pin_id == "delete-test-pin"
        assert preserved.account_identifier_snapshot == "analytics-account"
        assert db.get(AnalyticsCollectionRun, run_id) is not None
        assert db.get(AnalyticsSnapshot, pin_snapshot_id) is not None
        preserved_account_snapshot = db.get(PinterestAccountAnalyticsSnapshot, account_snapshot_id)
        assert preserved_account_snapshot is not None
        assert preserved_account_snapshot.account_id is None
        assert preserved_account_snapshot.follows == 7
    finally:
        db.close()
