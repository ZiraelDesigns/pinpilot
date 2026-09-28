from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.ai_pipeline import dashboard_pipeline_status, set_pipeline_enabled

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineToggleRequest(BaseModel):
    enabled: bool


@router.get("/status")
def status(db: Session = Depends(get_db)):
    result = dashboard_pipeline_status(db)
    db.commit()
    return result


@router.post("/toggle")
def toggle(payload: PipelineToggleRequest, db: Session = Depends(get_db)):
    control = set_pipeline_enabled(db, payload.enabled)
    return {"enabled": control.enabled, **dashboard_pipeline_status(db)}
