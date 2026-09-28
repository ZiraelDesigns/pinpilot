"""Local experiment and variant tracking over existing PinPilot records."""

from __future__ import annotations

from datetime import datetime
import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AnalyticsSnapshot,
    Experiment,
    ExperimentAssignment,
    ExperimentEvaluation,
    ExperimentEvaluationResult,
    ExperimentVariant,
    Pin,
    PinCreative,
    Product,
    PinterestAccount,
    PublishedPinterestPin,
)


PIN_METRICS = frozenset({
    "impressions",
    "saves",
    "pin_clicks",
    "outbound_clicks",
    "engagements",
    "engagement_rate",
    "pin_click_rate",
    "outbound_click_rate",
})
EXPERIMENT_TRANSITIONS = {
    "draft": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"running", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class ExperimentInputError(ValueError):
    """Invalid experiment, variant, assignment, or evaluation input."""


class ExperimentConflictError(ExperimentInputError):
    """The requested target already exists in this experiment."""


_SENSITIVE_CONFIGURATION_KEY = re.compile(r"(?:api[_-]?key|secret|token|credential|password)", re.I)


def _validate_configuration(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_CONFIGURATION_KEY.search(str(key)):
                raise ExperimentInputError("Variant configuration cannot contain credential fields")
            _validate_configuration(item)
    elif isinstance(value, list):
        for item in value:
            _validate_configuration(item)


def create_experiment(db: Session, *, name: str, evaluation_metric: str, **fields) -> Experiment:
    if evaluation_metric not in PIN_METRICS:
        raise ExperimentInputError("Unsupported evaluation metric")
    if fields.get("start_at") and fields.get("end_at") and fields["end_at"] <= fields["start_at"]:
        raise ExperimentInputError("Experiment end_at must be after start_at")
    if fields.get("status", "draft") != "draft":
        raise ExperimentInputError("Experiments must be created in draft status")
    account_id = fields.get("pinterest_account_id")
    if account_id is not None:
        account = db.get(PinterestAccount, account_id)
        if account is None or not account.account_identifier:
            raise ExperimentInputError("Pinterest account not found or has no stable identifier")
        fields["account_identifier_snapshot"] = account.account_identifier
    experiment = Experiment(name=name, evaluation_metric=evaluation_metric, **fields)
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


def transition_experiment(db: Session, experiment_id: int, target_status: str) -> Experiment:
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise ExperimentInputError("Experiment not found")
    if target_status not in EXPERIMENT_TRANSITIONS.get(experiment.status, frozenset()):
        raise ExperimentInputError(f"Invalid experiment status transition: {experiment.status} -> {target_status}")
    if target_status == "running":
        variant_count = db.scalar(
            select(func.count(ExperimentVariant.id)).where(ExperimentVariant.experiment_id == experiment.id)
        ) or 0
        if variant_count < 2:
            raise ExperimentInputError("At least two variants are required before an experiment can run")
    experiment.status = target_status
    db.commit()
    db.refresh(experiment)
    return experiment


def add_variant(db: Session, experiment_id: int, **fields) -> ExperimentVariant:
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise ExperimentInputError("Experiment not found")
    if experiment.status != "draft":
        raise ExperimentInputError("Variants can only be added while an experiment is draft")
    _validate_configuration(fields.get("configuration") or {})
    variant = ExperimentVariant(experiment=experiment, **fields)
    db.add(variant)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ExperimentInputError("Variant name already exists in this experiment") from exc
    db.refresh(variant)
    return variant


def assign_target(
    db: Session,
    *,
    experiment_id: int,
    variant_id: int,
    creative_id: int | None = None,
    published_pin_id: int | None = None,
    assignment_reason: str | None = None,
) -> ExperimentAssignment:
    experiment = db.get(Experiment, experiment_id)
    variant = db.get(ExperimentVariant, variant_id)
    if experiment is None:
        raise ExperimentInputError("Experiment not found")
    if experiment.status not in {"draft", "running"}:
        raise ExperimentInputError("Assignments can only be changed while an experiment is draft or running")
    if variant is None or variant.experiment_id != experiment_id:
        raise ExperimentInputError("Variant does not belong to this experiment")
    if (creative_id is None) == (published_pin_id is None):
        raise ExperimentInputError("Exactly one creative or published Pin is required")

    creative = db.get(PinCreative, creative_id) if creative_id is not None else None
    published_pin = db.get(PublishedPinterestPin, published_pin_id) if published_pin_id is not None else None
    if creative_id is not None and creative is None:
        raise ExperimentInputError("Creative not found")
    if published_pin_id is not None and published_pin is None:
        raise ExperimentInputError("Published Pin not found")
    if published_pin is not None:
        linked_creative_id = None
        if published_pin.pin_id is not None:
            linked_creative_id = db.scalar(select(Pin.creative_id).where(Pin.id == published_pin.pin_id))
        if linked_creative_id is not None and db.scalar(
            select(ExperimentAssignment.id).where(
                ExperimentAssignment.experiment_id == experiment_id,
                ExperimentAssignment.creative_id == linked_creative_id,
            ).limit(1)
        ) is not None:
            raise ExperimentConflictError("This Pin's creative is already assigned in this experiment")
    elif creative is not None:
        linked_published = select(PublishedPinterestPin.id).join(
            Pin, PublishedPinterestPin.pin_id == Pin.id
        ).where(Pin.creative_id == creative.id)
        if db.scalar(
            select(ExperimentAssignment.id).where(
                ExperimentAssignment.experiment_id == experiment_id,
                ExperimentAssignment.published_pin_id.in_(linked_published),
            ).limit(1)
        ) is not None:
            raise ExperimentConflictError("This creative already has a published Pin assignment in this experiment")
    if published_pin is not None:
        _bind_or_validate_account(experiment, published_pin)
    snapshot = ExperimentAssignment.capture_metadata(creative=creative, published_pin=published_pin)
    if creative is not None:
        snapshot["product_id"] = creative.product_id
        snapshot["product_title"] = creative.product.title if creative.product else None
    elif published_pin is not None:
        local_pin = published_pin.pin
        product = (local_pin.product if local_pin else None)
        if product is None and published_pin.metadata_snapshot.get("product_id"):
            product = db.get(Product, published_pin.metadata_snapshot["product_id"])
        snapshot["product_id"] = product.id if product else published_pin.metadata_snapshot.get("product_id")
        snapshot["product_title"] = product.title if product else None
    assignment = ExperimentAssignment(
        experiment_id=experiment.id,
        variant_id=variant.id,
        creative_id=creative.id if creative is not None else None,
        published_pin_id=published_pin.id if published_pin is not None else None,
        assignment_reason=assignment_reason,
        metadata_snapshot=snapshot,
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ExperimentConflictError("This target is already assigned in this experiment") from exc
    db.refresh(assignment)
    return assignment


def _bind_or_validate_account(experiment: Experiment, publication: PublishedPinterestPin) -> None:
    identifier = publication.account_identifier_snapshot
    if identifier is None and publication.account is not None:
        identifier = publication.account.account_identifier
    if not identifier:
        raise ExperimentInputError("Published Pin has no stable Pinterest account identity")
    if experiment.account_identifier_snapshot is None:
        experiment.pinterest_account_id = publication.account_id
        experiment.account_identifier_snapshot = identifier
        return
    if identifier != experiment.account_identifier_snapshot:
        raise ExperimentInputError("Published Pin belongs to a different Pinterest account scope")
    if (
        experiment.pinterest_account_id is not None
        and publication.account_id is not None
        and experiment.pinterest_account_id != publication.account_id
    ):
        raise ExperimentInputError("Published Pin belongs to a different Pinterest account scope")


def _assignment_publications(db: Session, experiment_id: int):
    """Resolve assigned creatives only through their exact local Pin publication links."""
    return db.execute(
        select(
            ExperimentAssignment.id.label("assignment_id"),
            ExperimentAssignment.variant_id.label("variant_id"),
            ExperimentAssignment.creative_id.label("creative_id"),
            ExperimentAssignment.published_pin_id.label("direct_published_pin_id"),
            PublishedPinterestPin.id.label("published_pin_id"),
            PublishedPinterestPin.account_id.label("account_id"),
            func.coalesce(PublishedPinterestPin.account_identifier_snapshot, PinterestAccount.account_identifier).label("account_identifier"),
        )
        .select_from(ExperimentAssignment)
        .outerjoin(
            Pin,
            and_(
                ExperimentAssignment.creative_id.is_not(None),
                Pin.creative_id == ExperimentAssignment.creative_id,
            ),
        )
        .outerjoin(
            PublishedPinterestPin,
            or_(
                PublishedPinterestPin.id == ExperimentAssignment.published_pin_id,
                and_(
                    ExperimentAssignment.creative_id.is_not(None),
                    PublishedPinterestPin.pin_id == Pin.id,
                ),
            ),
        )
        .outerjoin(PinterestAccount, PinterestAccount.id == PublishedPinterestPin.account_id)
        .where(ExperimentAssignment.experiment_id == experiment_id)
    ).mappings().all()


def _evaluation_snapshot_rows(db: Session, period_start: datetime, period_end: datetime, publication_ids: list[int]):
    start_date = period_start.date()
    end_date = period_end.date()
    metric_day = AnalyticsSnapshot.metric_date
    conditions = [
        AnalyticsSnapshot.published_pin_id.in_(publication_ids or [-1]),
        or_(
            and_(
                metric_day.is_not(None), metric_day >= start_date, metric_day <= end_date,
                or_(
                    and_(AnalyticsSnapshot.period_start.is_(None), AnalyticsSnapshot.period_end.is_(None)),
                    and_(AnalyticsSnapshot.period_start >= period_start, AnalyticsSnapshot.period_end <= period_end),
                ),
            ),
            and_(
                metric_day.is_(None),
                AnalyticsSnapshot.period_start.is_not(None),
                AnalyticsSnapshot.period_start >= period_start,
                AnalyticsSnapshot.period_end <= period_end,
            ),
            and_(
                metric_day.is_(None),
                AnalyticsSnapshot.period_start.is_(None),
                func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) >= period_start,
                func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) <= period_end,
            ),
        ),
    ]
    effective_date = func.coalesce(
        AnalyticsSnapshot.metric_date,
        func.date(func.coalesce(AnalyticsSnapshot.period_end, AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at)),
    )
    rank = func.row_number().over(
        partition_by=AnalyticsSnapshot.published_pin_id,
        order_by=(
            effective_date.desc(),
            AnalyticsSnapshot.period_start.desc().nullslast(),
            AnalyticsSnapshot.period_end.asc().nullslast(),
            AnalyticsSnapshot.fetched_at.desc().nullslast(),
            AnalyticsSnapshot.recorded_at.desc(),
            AnalyticsSnapshot.id.desc(),
        ),
    ).label("row_number")
    return select(
        AnalyticsSnapshot.id.label("snapshot_id"),
        AnalyticsSnapshot.published_pin_id.label("published_pin_id"),
        AnalyticsSnapshot.impressions.label("impressions"),
        AnalyticsSnapshot.saves.label("saves"),
        AnalyticsSnapshot.pin_clicks.label("pin_clicks"),
        AnalyticsSnapshot.outbound_clicks.label("outbound_clicks"),
        AnalyticsSnapshot.engagements.label("engagements"),
        AnalyticsSnapshot.engagement_rate.label("engagement_rate"),
        AnalyticsSnapshot.pin_click_rate.label("pin_click_rate"),
        AnalyticsSnapshot.outbound_click_rate.label("outbound_click_rate"),
        rank,
    ).where(*conditions).subquery("experiment_snapshot_rows")


def evaluate_experiment(
    db: Session,
    experiment_id: int,
    *,
    period_start: datetime,
    period_end: datetime,
    notes: str | None = None,
) -> tuple[ExperimentEvaluation, list[dict]]:
    if period_start >= period_end:
        raise ExperimentInputError("Evaluation period must have a positive duration")
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise ExperimentInputError("Experiment not found")
    metric = experiment.evaluation_metric
    if metric not in PIN_METRICS:
        raise ExperimentInputError("Unsupported evaluation metric")
    assignment_publications = _assignment_publications(db, experiment_id)
    # Resolve and validate a single stable account scope before aggregation.
    identities = {}
    for row in assignment_publications:
        if row["published_pin_id"] is not None:
            identity = row["account_identifier"]
            if not identity:
                raise ExperimentInputError("Published Pin has no stable Pinterest account identity for evaluation")
            identities[identity] = row["account_id"]
    if experiment.account_identifier_snapshot:
        if any(key != experiment.account_identifier_snapshot for key in identities):
            raise ExperimentInputError("Evaluation cannot combine Pinterest accounts; remove assignments outside the bound account")
    elif len(identities) > 1:
        raise ExperimentInputError("Evaluation cannot combine Pinterest accounts; bind the experiment to one account")
    elif identities:
        identifier, account_id = next(iter(identities.items()))
        experiment.account_identifier_snapshot = identifier
        experiment.pinterest_account_id = account_id
        db.flush()
    allowed_identity = experiment.account_identifier_snapshot
    assigned_publications_by_variant: dict[int, set[int]] = {}
    for row in assignment_publications:
        if row["published_pin_id"] is None:
            continue
        identity = row["account_identifier"]
        if allowed_identity and identity != allowed_identity:
            raise ExperimentInputError("Evaluation cannot combine Pinterest accounts; remove assignments outside the bound account")
        assigned_publications_by_variant.setdefault(row["variant_id"], set()).add(row["published_pin_id"])
    publication_ids = sorted({pid for pids in assigned_publications_by_variant.values() for pid in pids})
    snapshots = _evaluation_snapshot_rows(db, period_start, period_end, publication_ids)
    value_column = snapshots.c[metric]
    rate_metric = metric.endswith("_rate")
    data = db.execute(
        select(
            ExperimentVariant.id.label("variant_id"),
            ExperimentVariant.name.label("variant_name"),
            func.count(func.distinct(ExperimentAssignment.id)).label("assignment_count"),
        )
        .select_from(ExperimentVariant)
        .outerjoin(
            ExperimentAssignment,
            and_(
                ExperimentAssignment.variant_id == ExperimentVariant.id,
                ExperimentAssignment.experiment_id == ExperimentVariant.experiment_id,
            ),
        )
        .where(ExperimentVariant.experiment_id == experiment_id)
        .group_by(ExperimentVariant.id, ExperimentVariant.name)
        .order_by(ExperimentVariant.id)
    ).mappings().all()
    variants = []
    all_snapshot_ids: set[int] = set()
    for base in data:
        row = dict(base)
        pub_ids = assigned_publications_by_variant.get(row["variant_id"], set())
        selected = db.execute(
            select(snapshots).where(snapshots.c.row_number == 1, snapshots.c.published_pin_id.in_(pub_ids or [-1]))
        ).mappings().all()
        selected = [dict(item) for item in selected]
        source_ids = [item["snapshot_id"] for item in selected]
        all_snapshot_ids.update(source_ids)
        values = [item[metric] for item in selected if item[metric] is not None]
        row.update({
            "published_pin_count": len(pub_ids),
            "observation_count": len(selected),
            "sample_size": len(values),
            "metric_value": (sum(values) / len(values) if rate_metric else sum(values)) if values else None,
            "impressions_denominator": _sum_non_null(selected, "impressions"),
            "impressions": _sum_non_null(selected, "impressions"),
            "saves": _sum_non_null(selected, "saves"),
            "pin_clicks": _sum_non_null(selected, "pin_clicks"),
            "outbound_clicks": _sum_non_null(selected, "outbound_clicks"),
            "engagements": _sum_non_null(selected, "engagements"),
            "engagement_rate": _avg_non_null(selected, "engagement_rate"),
            "pin_click_rate": _avg_non_null(selected, "pin_click_rate"),
            "outbound_click_rate": _avg_non_null(selected, "outbound_click_rate"),
            "snapshot_ids": source_ids,
        })
        variants.append(row)
    observed_sample_size = sum(row["sample_size"] for row in variants)
    evaluation = ExperimentEvaluation(
        experiment_id=experiment_id,
        period_start=period_start,
        period_end=period_end,
        sample_size=observed_sample_size,
        metric_name=metric,
        notes=notes,
        calculation_metadata={
            "snapshot_source": "analytics_snapshots linked to published_pinterest_pins",
            "selection": "one latest eligible reporting observation per published Pin; overlapping periods are never summed",
            "observation_unit": "one published Pinterest Pin",
            "null_metrics_excluded_from_metric_sample_size": True,
            "rate_aggregation": "arithmetic mean of available snapshot rates" if rate_metric else None,
            "metric_value_persisted": True,
            "winner_selection": False,
            "account_identifier": experiment.account_identifier_snapshot,
            "sample_size_definition": "selected published-Pin observations with non-null evaluation metric",
        },
        snapshot_ids=sorted(all_snapshot_ids),
    )
    db.add(evaluation)
    db.flush()
    for row in variants:
        db.add(ExperimentEvaluationResult(
            evaluation_id=evaluation.id,
            variant_id=row["variant_id"],
            assignment_count=row["assignment_count"],
            published_pin_count=row["published_pin_count"],
            observation_count=row["observation_count"],
            sample_size=row["sample_size"],
            impressions=row["impressions"], saves=row["saves"], pin_clicks=row["pin_clicks"],
            outbound_clicks=row["outbound_clicks"], engagements=row["engagements"],
            engagement_rate=row["engagement_rate"], pin_click_rate=row["pin_click_rate"],
            outbound_click_rate=row["outbound_click_rate"],
            impressions_denominator=row["impressions_denominator"],
            selected_metric_value=row["metric_value"], source_snapshot_ids=row["snapshot_ids"],
        ))
    db.commit()
    db.refresh(evaluation)
    variants = [
        {**row, "metric_value": row["metric_value"], "source_snapshot_ids": row["snapshot_ids"]}
        for row in variants
    ]
    return evaluation, variants


def _sum_non_null(rows, key):
    values = [row[key] for row in rows if row[key] is not None]
    return sum(values) if values else None


def _avg_non_null(rows, key):
    values = [row[key] for row in rows if row[key] is not None]
    return sum(values) / len(values) if values else None


def experiment_detail(db: Session, experiment_id: int) -> Experiment | None:
    return db.scalar(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .options(
            selectinload(Experiment.variants).selectinload(ExperimentVariant.assignments),
            selectinload(Experiment.evaluations).selectinload(ExperimentEvaluation.variant_results).selectinload(ExperimentEvaluationResult.variant),
        )
    )


def experiment_summaries(db: Session) -> list[dict]:
    """Use the most recently persisted evaluation result, never today's live snapshots."""
    experiments = db.scalars(select(Experiment).options(
        selectinload(Experiment.variants).selectinload(ExperimentVariant.assignments),
        selectinload(Experiment.evaluations).selectinload(ExperimentEvaluation.variant_results).selectinload(ExperimentEvaluationResult.variant),
    ).order_by(Experiment.created_at.desc(), Experiment.id.desc())).all()
    assignment_links = {}
    for link in db.execute(
        select(ExperimentAssignment.experiment_id, ExperimentAssignment.variant_id,
               PublishedPinterestPin.id.label("published_pin_id"))
        .select_from(ExperimentAssignment)
        .outerjoin(Pin, and_(ExperimentAssignment.creative_id.is_not(None), Pin.creative_id == ExperimentAssignment.creative_id))
        .outerjoin(PublishedPinterestPin, or_(
            PublishedPinterestPin.id == ExperimentAssignment.published_pin_id,
            and_(ExperimentAssignment.creative_id.is_not(None), PublishedPinterestPin.pin_id == Pin.id),
        ))
    ).all():
        assignment_links.setdefault((link.experiment_id, link.variant_id), set())
        if link.published_pin_id is not None:
            assignment_links[(link.experiment_id, link.variant_id)].add(link.published_pin_id)
    summaries = []
    for experiment in experiments:
        latest = max(experiment.evaluations, key=lambda item: (item.evaluated_at, item.id), default=None)
        frozen = {item.variant_id: item for item in latest.variant_results} if latest else {}
        variant_rows = []
        for variant in experiment.variants:
            result = frozen.get(variant.id)
            assignment_count = len(variant.assignments)
            pub_count = result.published_pin_count if result else len(assignment_links.get((experiment.id, variant.id), set()))
            variant_rows.append({
                "variant_id": variant.id, "variant_name": variant.name,
                "assignment_count": result.assignment_count if result else assignment_count,
                "published_pin_count": pub_count,
                "sample_size": result.sample_size if result else 0,
                "observation_count": result.observation_count if result else 0,
                "metric_name": latest.metric_name if latest else experiment.evaluation_metric,
                "metric_value": result.selected_metric_value if result else None,
                "impressions": result.impressions if result else None,
                "saves": result.saves if result else None,
                "pin_clicks": result.pin_clicks if result else None,
                "outbound_clicks": result.outbound_clicks if result else None,
                "analytics_available": bool(result and result.observation_count),
            })
        published_count = sum(row["published_pin_count"] for row in variant_rows)
        observed_count = sum(row["observation_count"] for row in variant_rows)
        summaries.append({
            "id": experiment.id, "name": experiment.name, "status": experiment.status,
            "start_at": experiment.start_at, "end_at": experiment.end_at,
            "evaluation_metric": experiment.evaluation_metric,
            "variant_count": len(experiment.variants),
            "assignment_count": sum(row["assignment_count"] for row in variant_rows),
            "published_sample_count": published_count,
            "observed_sample_count": observed_count,
            "analytics_available": observed_count > 0,
            "evaluation_period_start": latest.period_start if latest else None,
            "evaluation_period_end": latest.period_end if latest else None,
            "variant_results": variant_rows,
        })
    return summaries
