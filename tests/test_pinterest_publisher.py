from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - register every model before creating the isolated test schema.
from app.database import Base
from app.models import (
    Pin,
    PinCreative,
    PinterestAccount,
    PinterestBoard,
    PinterestPublishIntent,
    PinterestPublishIntentStatus,
    Product,
    PublishedPinterestPin,
)
from app.models.core import PinStatus
from app.services.daily_pin_scheduler import DailyPinScheduler
from app.services.pinterest_publisher import (
    DisabledPinterestPinPublishingProvider,
    PinterestPinPublishRequest,
    PinterestPinPublishResult,
    PinterestPublishAlreadyInProgress,
    PinterestPublishOutcomeUnknown,
    PinterestPublishRejected,
    PinterestPublisher,
    PinterestPublishingError,
    PinterestPublishingUnavailable,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'publisher-tests.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class FakePinterestProvider:
    publishing_enabled = True

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.requests: list[PinterestPinPublishRequest] = []

    def publish_pin(self, account, request):
        self.requests.append(request)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return PinterestPinPublishResult(
            external_pin_id=f"remote-{account.id}-{len(self.requests)}",
            published_at=datetime(2026, 9, 29, 12),
        )


def _pin(db, *, source_type="ai"):
    product = Product(title="Publisher test product", url="https://etsy.example.test/item")
    creative = PinCreative(
        product=product,
        creative_type="product_focus",
        title="Publisher test creative",
        description="Publishable local copy.",
        keywords=["test"],
        seo_metadata={"primary_keyword": "test item", "creative_angle": "simple"},
        call_to_action="Explore",
        image_path="https://media.example.test/pin.png",
        source_type=source_type,
        destination_url=product.url,
        generation_key=f"publisher-test-{id(product)}",
    )
    pin = Pin(
        product=product,
        creative=creative,
        title=creative.title,
        description=creative.description,
        image_path=creative.image_path,
        destination_url=creative.destination_url,
        status=PinStatus.SCHEDULED.value,
        scheduled_for=datetime(2026, 9, 30, 9),
    )
    db.add(pin)
    db.commit()
    return pin


def _account(db, name):
    account = PinterestAccount(account_name=name, account_identifier=name, is_active=True)
    db.add(account)
    db.commit()
    return account


def test_published_pin_is_created_only_after_provider_confirmation_and_links_board(db):
    pin = _pin(db)
    account = _account(db, "publisher-business")
    board = PinterestBoard(account=account, board_id="board-ext-1", name="Ideas")
    db.add(board)
    db.commit()
    provider = FakePinterestProvider()

    published = PinterestPublisher(db, provider).publish_pin(pin.id, account.id, board.id)

    assert published.pin_id == pin.id
    assert published.account_id == account.id
    assert published.board_id == board.id
    assert published.external_pin_id == "remote-1-1"
    assert published.metadata_snapshot["primary_keyword"] == "test item"
    assert provider.requests[0].board_external_id == "board-ext-1"
    assert provider.requests[0].image_reference == pin.image_path
    assert pin.status == PinStatus.PUBLISHED.value
    intent = db.scalar(select(PinterestPublishIntent).where(PinterestPublishIntent.pin_id == pin.id))
    assert intent.status == PinterestPublishIntentStatus.PUBLISHED.value
    assert intent.published_pin_id == published.id


def test_repeated_publish_call_returns_existing_record_without_provider_retry(db):
    pin = _pin(db)
    account = _account(db, "idempotent-business")
    provider = FakePinterestProvider()
    publisher = PinterestPublisher(db, provider)

    first = publisher.publish_pin(pin.id, account.id)
    second = publisher.publish_pin(pin.id, account.id)

    assert first.id == second.id
    assert len(provider.requests) == 1
    assert db.scalar(select(PublishedPinterestPin).where(PublishedPinterestPin.pin_id == pin.id)) is not None


def test_confirmed_rejection_preserves_local_scheduled_state_and_retry_reuses_key(db):
    pin = _pin(db)
    account = _account(db, "retry-business")
    provider = FakePinterestProvider([
        PinterestPublishRejected("provider response must not be persisted"),
        PinterestPinPublishResult("remote-after-retry", datetime(2026, 9, 29, 12)),
    ])
    publisher = PinterestPublisher(db, provider)

    with pytest.raises(PinterestPublishRejected):
        publisher.publish_pin(pin.id, account.id)
    db.refresh(pin)
    intent = db.scalar(select(PinterestPublishIntent).where(PinterestPublishIntent.pin_id == pin.id))
    first_key = intent.idempotency_key
    assert pin.status == PinStatus.SCHEDULED.value
    assert intent.status == PinterestPublishIntentStatus.FAILED.value
    assert "provider response" not in intent.error_summary

    published = publisher.publish_pin(pin.id, account.id)
    db.refresh(intent)
    assert published.external_pin_id == "remote-after-retry"
    assert intent.idempotency_key == first_key
    assert intent.status == PinterestPublishIntentStatus.PUBLISHED.value
    assert len(provider.requests) == 2
    assert provider.requests[0].idempotency_key == provider.requests[1].idempotency_key


def test_ambiguous_failure_is_not_resent_after_retry_or_process_restart(db):
    pin = _pin(db)
    account = _account(db, "uncertain-business")
    provider = FakePinterestProvider([TimeoutError("sensitive provider response")])

    with pytest.raises(PinterestPublishOutcomeUnknown):
        PinterestPublisher(db, provider).publish_pin(pin.id, account.id)
    db.refresh(pin)
    intent = db.scalar(select(PinterestPublishIntent).where(PinterestPublishIntent.pin_id == pin.id))
    assert pin.status == PinStatus.SCHEDULED.value
    assert intent.status == PinterestPublishIntentStatus.UNKNOWN.value
    assert "sensitive provider response" not in intent.error_summary

    with pytest.raises(PinterestPublishAlreadyInProgress):
        PinterestPublisher(db, provider).publish_pin(pin.id, account.id)
    assert len(provider.requests) == 1


def test_stale_publishing_claim_prevents_a_second_process_provider_call(db):
    pin = _pin(db)
    account = _account(db, "restart-business")
    intent = PinterestPublishIntent(
        pin=pin,
        account=account,
        account_identifier_snapshot=account.account_identifier,
        status=PinterestPublishIntentStatus.PUBLISHING.value,
    )
    db.add(intent)
    db.commit()
    provider = FakePinterestProvider()

    with pytest.raises(PinterestPublishAlreadyInProgress):
        PinterestPublisher(db, provider).publish_pin(pin.id, account.id)
    assert provider.requests == []


def test_only_one_concurrent_retry_can_claim_a_failed_intent(db):
    from sqlalchemy.orm import Session as SQLAlchemySession

    pin = _pin(db)
    account = _account(db, "concurrent-retry-business")
    intent = PinterestPublishIntent(
        pin=pin,
        account=account,
        account_identifier_snapshot=account.account_identifier,
        status=PinterestPublishIntentStatus.FAILED.value,
    )
    db.add(intent)
    db.commit()

    second_session = SQLAlchemySession(bind=db.get_bind())
    stale_intent = second_session.get(PinterestPublishIntent, intent.id)
    assert stale_intent.status == PinterestPublishIntentStatus.FAILED.value
    first_provider = FakePinterestProvider()
    second_provider = FakePinterestProvider()
    try:
        PinterestPublisher(db, first_provider).publish_pin(pin.id, account.id)
        with pytest.raises(PinterestPublishAlreadyInProgress):
            PinterestPublisher(second_session, second_provider).publish_pin(pin.id, account.id)
        assert len(first_provider.requests) == 1
        assert second_provider.requests == []
    finally:
        second_session.close()


def test_same_local_pin_can_publish_to_another_account_without_cross_account_collision(db):
    pin = _pin(db)
    first_account = _account(db, "publisher-account-one")
    second_account = _account(db, "publisher-account-two")
    provider = FakePinterestProvider()
    publisher = PinterestPublisher(db, provider)

    first = publisher.publish_pin(pin.id, first_account.id)
    second = publisher.publish_pin(pin.id, second_account.id)

    assert first.account_id != second.account_id
    assert db.query(PinterestPublishIntent).filter_by(pin_id=pin.id).count() == 2
    assert db.query(PublishedPinterestPin).filter_by(pin_id=pin.id).count() == 2


def test_board_from_another_account_is_rejected_before_provider_call(db):
    pin = _pin(db)
    account = _account(db, "board-owner")
    other_account = _account(db, "other-board-owner")
    board = PinterestBoard(account=other_account, board_id="foreign-board", name="Foreign")
    db.add(board)
    db.commit()
    provider = FakePinterestProvider()

    with pytest.raises(PinterestPublishingError, match="does not belong"):
        PinterestPublisher(db, provider).publish_pin(pin.id, account.id, board.id)
    assert provider.requests == []
    assert db.query(PinterestPublishIntent).count() == 0


def test_disabled_production_provider_fails_closed_without_creating_intent(db):
    pin = _pin(db)
    account = _account(db, "disabled-publishing")

    with pytest.raises(PinterestPublishingUnavailable):
        PinterestPublisher(db, DisabledPinterestPinPublishingProvider()).publish_pin(pin.id, account.id)
    assert db.query(PinterestPublishIntent).count() == 0
    assert db.query(PublishedPinterestPin).count() == 0
    assert pin.status == PinStatus.SCHEDULED.value


def test_disconnect_keeps_publication_and_intent_idempotency_history(db):
    pin = _pin(db)
    account = _account(db, "disconnect-business")
    provider = FakePinterestProvider()
    published = PinterestPublisher(db, provider).publish_pin(pin.id, account.id)
    published_id = published.id
    intent = db.scalar(select(PinterestPublishIntent).where(PinterestPublishIntent.pin_id == pin.id))
    intent_id = intent.id

    db.delete(account)
    db.commit()
    db.expire_all()

    retained_publication = db.get(PublishedPinterestPin, published_id)
    retained_intent = db.get(PinterestPublishIntent, intent_id)
    assert retained_publication.external_pin_id == published.external_pin_id
    assert retained_publication.account_id is None
    assert retained_intent.account_id is None
    assert retained_intent.account_identifier_snapshot == "disconnect-business"
    assert retained_intent.status == PinterestPublishIntentStatus.PUBLISHED.value


def test_daily_scheduler_only_creates_local_pins_and_never_calls_pinterest_http(db, monkeypatch):
    from datetime import date

    def reject_network(*_args, **_kwargs):
        raise AssertionError("DailyPinScheduler must not make Pinterest HTTP calls")

    monkeypatch.setattr("app.services.pinterest.httpx.get", reject_network)
    monkeypatch.setattr("app.services.pinterest.httpx.post", reject_network)
    product = Product(title="Local queue product", description="No remote operation")
    creative = PinCreative(
        product=product,
        creative_type="product_focus",
        title="Local-only creative",
        description="Prepared locally.",
        keywords=["local"],
        call_to_action="Explore",
        source_type="mockup",
        generation_key="scheduler-publisher-separation-test",
    )
    db.add(creative)
    db.commit()
    result = DailyPinScheduler(db).schedule_daily(date(2036, 2, 3))

    assert len(result.prepared) == 1
    assert result.prepared[0].status == PinStatus.SCHEDULED.value
    assert db.query(PublishedPinterestPin).count() == 0
