from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.ai_pipeline import dashboard_pipeline_status, set_pipeline_enabled
from app.security import require_admin_csrf, require_admin_read

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineToggleRequest(BaseModel):
    enabled: bool


@router.get("/status", dependencies=[Depends(require_admin_read)])
def status(db: Session = Depends(get_db)):
    return dashboard_pipeline_status(db)


@router.post("/toggle", dependencies=[Depends(require_admin_csrf)])
def toggle(payload: PipelineToggleRequest, db: Session = Depends(get_db)):
    control = set_pipeline_enabled(db, payload.enabled)
    return {"enabled": control.enabled, **dashboard_pipeline_status(db)}
