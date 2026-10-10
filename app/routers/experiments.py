"""Local CRUD and evaluation routes for manual experiment tracking."""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models import PinterestAccount, SEOABExperiment, SEOABVariant
from app.services.experiments import (
    ExperimentConflictError,
    ExperimentInputError,
    add_variant,
    assign_target,
    create_experiment,
    evaluate_experiment,
    experiment_detail,
    experiment_summaries,
    transition_experiment,
)
from app.security import get_principal, require_admin_csrf, require_admin_read
from app.services.pinterest_publisher import (
    DisabledPinterestPinPublishingProvider,
    PinterestApiPublishingProvider,
    PinterestPublishOutcomeUnknown,
    PinterestPublishingError,
    PinterestPublishingUnavailable,
    PinterestPublisher,
)
from app.services.seo_ab_variations import (
    compare_seo_ab_experiment,
    create_seo_ab_experiment,
    create_seo_ab_variant,
    transition_seo_ab_experiment,
)
from app.services.seo_performance_learning import run_performance_learning


router = APIRouter(prefix="/experiments", tags=["experiments"])
MetricName = Literal[
    "impressions",
    "saves",
    "pin_clicks",
    "outbound_clicks",
    "engagements",
    "engagement_rate",
    "pin_click_rate",
    "outbound_click_rate",
]
ExperimentStatus = Literal["draft"]


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    hypothesis: str | None = None
    status: ExperimentStatus = "draft"
    evaluation_metric: MetricName
    pinterest_account_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class VariantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    creative_type: str | None = Field(default=None, max_length=32)
    source_type: str | None = Field(default=None, max_length=16)
    creative_angle: str | None = Field(default=None, max_length=255)
    primary_keyword: str | None = Field(default=None, max_length=255)
    audience_definition: str | None = None
    configuration: dict = Field(default_factory=dict)


class AssignmentCreate(BaseModel):
    variant_id: int
    creative_id: int | None = None
    published_pin_id: int | None = None
    assignment_reason: str | None = None


class EvaluationCreate(BaseModel):
    period_start: datetime
    period_end: datetime
    notes: str | None = None


class SEOABExperimentCreate(BaseModel):
    source_generation_id: int = Field(gt=0)
    hypothesis: str = Field(min_length=1, max_length=5000)
    hypothesis_source: Literal["human_defined", "system_generated"] = "human_defined"


class SEOABVariantCreate(BaseModel):
    variant_type: Literal["KEYWORD_FOCUS"] = "KEYWORD_FOCUS"
    candidate_keyword: str = Field(min_length=1, max_length=255)


class SEOABStatusUpdate(BaseModel):
    target_status: Literal["READY", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED"]


class SEOABPublishRequest(BaseModel):
    pin_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    board_id: int | None = Field(default=None, gt=0)


SEOABMetricName = Literal[
    "impressions", "saves", "pin_clicks", "outbound_clicks", "engagements",
    "save_rate", "pin_click_rate", "outbound_click_rate", "engagement_rate",
]


class SEOABComparisonRequest(BaseModel):
    period_start: date
    period_end: date
    metric_name: SEOABMetricName = "outbound_clicks"
    account_id: int | None = Field(default=None, gt=0)


class SEOPerformanceLearningRun(BaseModel):
    account_id: int = Field(gt=0)
    window_start: date
    window_end: date


class TransitionRequest(BaseModel):
    target_status: Literal["running", "paused", "completed", "cancelled"]


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    hypothesis: str | None
    status: str
    evaluation_metric: str
    start_at: datetime | None
    end_at: datetime | None
    pinterest_account_id: int | None
    account_identifier_snapshot: str | None
    created_at: datetime
    updated_at: datetime


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    experiment_id: int
    name: str
    description: str | None
    creative_type: str | None
    source_type: str | None
    creative_angle: str | None
    primary_keyword: str | None
    audience_definition: str | None
    configuration: dict
    created_at: datetime


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    experiment_id: int
    variant_id: int
    creative_id: int | None
    published_pin_id: int | None
    assigned_at: datetime
    assignment_reason: str | None
    metadata_snapshot: dict


class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    experiment_id: int
    evaluated_at: datetime
    period_start: datetime
    period_end: datetime
    sample_size: int
    metric_name: str
    notes: str | None
    calculation_metadata: dict
    snapshot_ids: list[int]


def _input_error(exc: ExperimentInputError) -> HTTPException:
    if isinstance(exc, ExperimentConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if "not found" in str(exc).lower():
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _admin_read(request: Request):
    return require_admin_read(request)


def _seo_ab_experiment_data(experiment: SEOABExperiment) -> dict:
    return {
        "id": experiment.id,
        "source_generation_id": experiment.source_generation_id,
        "status": experiment.status,
        "hypothesis": experiment.hypothesis,
        "hypothesis_source": experiment.hypothesis_source,
        "scope": experiment.scope,
        "experiment_version": experiment.experiment_version,
        "variation_version": experiment.variation_version,
        "comparison_version": experiment.comparison_version,
        "created_at": experiment.created_at,
        "updated_at": experiment.updated_at,
        "variants": [
            {
                "id": item.id,
                "variant_key": item.variant_key,
                "variant_name": item.variant_name,
                "variant_type": item.variant_type,
                "status": item.status,
                "quality_status": item.quality_status,
                "quality_score": item.quality_score,
                "output_snapshot": item.output_snapshot,
                "change_set": item.change_set,
                "provenance_snapshot": item.provenance_snapshot,
            }
            for item in experiment.variants
        ],
    }


def get_seo_ab_publisher(db: Session = Depends(get_db)) -> PinterestPublisher:
    """Resolve the existing publisher through the explicit fail-closed flag."""
    provider = (
        PinterestApiPublishingProvider(db)
        if settings.pinterest_publish_enabled
        else DisabledPinterestPinPublishingProvider()
    )
    return PinterestPublisher(db, provider)


@router.post("", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_csrf)])
def create(payload: ExperimentCreate, db: Session = Depends(get_db)):
    try:
        return create_experiment(db, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.post("/{experiment_id}/variants", response_model=VariantOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_csrf)])
def create_variant(experiment_id: int, payload: VariantCreate, db: Session = Depends(get_db)):
    try:
        return add_variant(db, experiment_id, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.patch("/{experiment_id}/status", response_model=ExperimentOut, dependencies=[Depends(require_admin_csrf)])
def update_status(experiment_id: int, payload: TransitionRequest, db: Session = Depends(get_db)):
    try:
        return transition_experiment(db, experiment_id, payload.target_status)
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.post("/{experiment_id}/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_csrf)])
def create_assignment(experiment_id: int, payload: AssignmentCreate, db: Session = Depends(get_db)):
    try:
        return assign_target(db, experiment_id=experiment_id, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.get("", dependencies=[Depends(require_admin_read)])
def list_all(db: Session = Depends(get_db)):
    return {"items": experiment_summaries(db)}


@router.get("/seo-ab")
def list_seo_ab_experiments(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _principal=Depends(_admin_read),
):
    rows = db.scalars(
        select(SEOABExperiment)
        .options(selectinload(SEOABExperiment.variants))
        .order_by(SEOABExperiment.created_at.desc(), SEOABExperiment.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [_seo_ab_experiment_data(item) for item in rows], "limit": limit, "offset": offset}


@router.get("/seo-ab/{experiment_id}")
def get_seo_ab_experiment(
    experiment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _principal=Depends(_admin_read),
):
    experiment = db.scalar(
        select(SEOABExperiment)
        .where(SEOABExperiment.id == experiment_id)
        .options(selectinload(SEOABExperiment.variants), selectinload(SEOABExperiment.comparisons))
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="SEO A/B experiment not found")
    result = _seo_ab_experiment_data(experiment)
    result["comparisons"] = [
        {
            "id": item.id,
            "metric_name": item.metric_name,
            "period_start": item.period_start,
            "period_end": item.period_end,
            "calculated_at": item.calculated_at,
            "comparison_version": item.comparison_version,
            "snapshot_ids": item.snapshot_ids,
            "result": item.result_snapshot,
        }
        for item in sorted(experiment.comparisons, key=lambda row: (row.calculated_at, row.id), reverse=True)
    ]
    return result


@router.get("/{experiment_id}", dependencies=[Depends(require_admin_read)])
def get_detail(experiment_id: int, db: Session = Depends(get_db)):
    experiment = experiment_detail(db, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return {
        "experiment": ExperimentOut.model_validate(experiment),
        "variants": [
            {
                **VariantOut.model_validate(variant).model_dump(),
                "assignments": [AssignmentOut.model_validate(item) for item in variant.assignments],
            }
            for variant in experiment.variants
        ],
        "evaluations": [
            {
                **EvaluationOut.model_validate(item).model_dump(),
                "variants": [
                    {
                        "variant_id": result.variant_id,
                        "variant_name": result.variant.name,
                        "assignment_count": result.assignment_count,
                        "published_pin_count": result.published_pin_count,
                        "sample_size": result.sample_size,
                        "observation_count": result.observation_count,
                        "metric_name": item.metric_name,
                        "metric_value": result.selected_metric_value,
                        "impressions": result.impressions,
                        "saves": result.saves,
                        "pin_clicks": result.pin_clicks,
                        "outbound_clicks": result.outbound_clicks,
                        "engagements": result.engagements,
                        "source_snapshot_ids": result.source_snapshot_ids,
                    }
                    for result in item.variant_results
                ],
            }
            for item in experiment.evaluations
        ],
    }


@router.post("/{experiment_id}/evaluations", dependencies=[Depends(require_admin_csrf)])
def evaluate(experiment_id: int, payload: EvaluationCreate, db: Session = Depends(get_db)):
    try:
        evaluation, variants = evaluate_experiment(db, experiment_id, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc
    return {
        "evaluation": EvaluationOut.model_validate(evaluation),
        "variants": variants,
        "snapshot_ids": evaluation.snapshot_ids,
        "analytics_data_available": evaluation.sample_size > 0,
        "winner_selected": False,
    }


@router.post("/seo-ab", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_csrf)])
def create_seo_ab_draft(payload: SEOABExperimentCreate, db: Session = Depends(get_db)):
    try:
        experiment = create_seo_ab_experiment(
            db,
            payload.source_generation_id,
            hypothesis=payload.hypothesis,
            hypothesis_source=payload.hypothesis_source,
            generate_variants=False,
        )
        db.commit()
        experiment = db.scalar(
            select(SEOABExperiment)
            .where(SEOABExperiment.id == experiment.id)
            .options(selectinload(SEOABExperiment.variants))
        )
        return _seo_ab_experiment_data(experiment)
    except ValueError as exc:
        db.rollback()
        message = str(exc)
        raise HTTPException(status_code=404 if "not found" in message.lower() else 400, detail=message) from exc


@router.post(
    "/seo-ab/{experiment_id}/variants",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_csrf)],
)
def create_seo_ab_variant_route(experiment_id: int, payload: SEOABVariantCreate, db: Session = Depends(get_db)):
    try:
        variant = create_seo_ab_variant(
            db,
            experiment_id,
            variant_type=payload.variant_type,
            candidate_keyword=payload.candidate_keyword,
        )
        db.commit()
        db.refresh(variant)
        return {
            "id": variant.id,
            "experiment_id": variant.experiment_id,
            "variant_key": variant.variant_key,
            "variant_name": variant.variant_name,
            "variant_type": variant.variant_type,
            "status": variant.status,
            "quality_status": variant.quality_status,
            "quality_score": variant.quality_score,
            "output_snapshot": variant.output_snapshot,
            "change_set": variant.change_set,
            "provenance_snapshot": variant.provenance_snapshot,
        }
    except ValueError as exc:
        db.rollback()
        message = str(exc)
        not_found = "not found" in message.lower()
        conflict = "only" in message.lower() or "status" in message.lower()
        raise HTTPException(status_code=404 if not_found else (409 if conflict else 400), detail=message) from exc


@router.patch(
    "/seo-ab/{experiment_id}/status",
    dependencies=[Depends(require_admin_csrf)],
)
def update_seo_ab_status(experiment_id: int, payload: SEOABStatusUpdate, db: Session = Depends(get_db)):
    try:
        experiment = transition_seo_ab_experiment(db, experiment_id, payload.target_status)
        db.commit()
        experiment = db.scalar(
            select(SEOABExperiment)
            .where(SEOABExperiment.id == experiment.id)
            .options(selectinload(SEOABExperiment.variants))
        )
        return _seo_ab_experiment_data(experiment)
    except ValueError as exc:
        db.rollback()
        message = str(exc)
        not_found = "not found" in message.lower()
        raise HTTPException(status_code=404 if not_found else 409, detail=message) from exc


@router.post(
    "/seo-ab/{experiment_id}/variants/{variant_id}/publish",
    dependencies=[Depends(require_admin_csrf)],
)
def publish_seo_ab_variant(
    experiment_id: int,
    variant_id: int,
    payload: SEOABPublishRequest,
    db: Session = Depends(get_db),
    publisher: PinterestPublisher = Depends(get_seo_ab_publisher),
):
    variant = db.scalar(select(SEOABVariant).where(
        SEOABVariant.id == variant_id,
        SEOABVariant.experiment_id == experiment_id,
    ))
    if variant is None:
        raise HTTPException(status_code=404, detail="SEO A/B variant not found for this experiment")
    if variant.quality_status == "FAIL" or variant.status != "READY":
        raise HTTPException(status_code=409, detail="Only a quality-approved READY variant can be selected for publication")
    if variant.experiment.status != "RUNNING":
        raise HTTPException(status_code=409, detail="The experiment must be explicitly RUNNING before publication")
    if not settings.pinterest_publish_enabled:
        raise HTTPException(status_code=423, detail="Pinterest publishing is disabled; no publish intent was created")
    try:
        publication = publisher.publish_pin(
            payload.pin_id,
            payload.account_id,
            payload.board_id,
            seo_ab_variant_id=variant.id,
        )
    except PinterestPublishingUnavailable as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except PinterestPublishOutcomeUnknown as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PinterestPublishingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "publication_id": publication.id,
        "published_pin_id": publication.id,
        "variant_id": variant.id,
        "experiment_id": experiment_id,
        "external_pin_id": publication.external_pin_id,
        "published_at": publication.published_at,
        "attribution_status": "verified",
    }


@router.post(
    "/seo-ab/{experiment_id}/comparisons",
    dependencies=[Depends(require_admin_csrf)],
)
def compare_seo_ab_route(experiment_id: int, payload: SEOABComparisonRequest, db: Session = Depends(get_db)):
    if payload.account_id is not None and db.get(PinterestAccount, payload.account_id) is None:
        raise HTTPException(status_code=404, detail="Pinterest account not found")
    try:
        comparison = compare_seo_ab_experiment(
            db,
            experiment_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            metric_name=payload.metric_name,
            account_id=payload.account_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        message = str(exc)
        raise HTTPException(status_code=404 if "not found" in message.lower() else 400, detail=message) from exc
    return {
        "id": comparison.id,
        "experiment_id": comparison.experiment_id,
        "comparison_version": comparison.comparison_version,
        "metric_name": comparison.metric_name,
        "period_start": comparison.period_start,
        "period_end": comparison.period_end,
        "snapshot_ids": comparison.snapshot_ids,
        "result": comparison.result_snapshot,
    }


@router.post(
    "/seo-ab/learning/run",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_csrf)],
)
def run_seo_ab_learning(payload: SEOPerformanceLearningRun, db: Session = Depends(get_db)):
    account = db.get(PinterestAccount, payload.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Pinterest account not found")
    try:
        learning = run_performance_learning(
            db,
            account_id=account.id,
            window_start=payload.window_start,
            window_end=payload.window_end,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": learning.id,
        "account_id": learning.account_id,
        "account_identifier_snapshot": learning.account_identifier_snapshot,
        "status": learning.status,
        "algorithm_version": learning.algorithm_version,
        "sample_count": learning.sample_count,
        "source_snapshot_ids": learning.source_snapshot_ids,
        "result": learning.result_snapshot,
    }
