"""Local CRUD and evaluation routes for manual experiment tracking."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
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


@router.post("", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED)
def create(payload: ExperimentCreate, db: Session = Depends(get_db)):
    try:
        return create_experiment(db, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.post("/{experiment_id}/variants", response_model=VariantOut, status_code=status.HTTP_201_CREATED)
def create_variant(experiment_id: int, payload: VariantCreate, db: Session = Depends(get_db)):
    try:
        return add_variant(db, experiment_id, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.patch("/{experiment_id}/status", response_model=ExperimentOut)
def update_status(experiment_id: int, payload: TransitionRequest, db: Session = Depends(get_db)):
    try:
        return transition_experiment(db, experiment_id, payload.target_status)
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.post("/{experiment_id}/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def create_assignment(experiment_id: int, payload: AssignmentCreate, db: Session = Depends(get_db)):
    try:
        return assign_target(db, experiment_id=experiment_id, **payload.model_dump())
    except ExperimentInputError as exc:
        raise _input_error(exc) from exc


@router.get("")
def list_all(db: Session = Depends(get_db)):
    return {"items": experiment_summaries(db)}


@router.get("/{experiment_id}")
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


@router.post("/{experiment_id}/evaluations")
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
