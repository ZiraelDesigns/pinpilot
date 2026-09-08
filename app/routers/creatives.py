from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PinCreative, Product
from app.models.core import PinCreativeStatus, PinCreativeType
from app.services.ai_content import AIContentError, AIContentService

router = APIRouter(prefix="/creatives", tags=["creatives"])


class CreativeGenerateRequest(BaseModel):
    product_id: int
    creative_type: PinCreativeType
    desired_count: int = Field(ge=1, le=5)


class CreativeUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1, max_length=12)
    call_to_action: str = Field(min_length=1, max_length=255)


@router.post("/generate")
def generate(payload: CreativeGenerateRequest, db: Session = Depends(get_db)):
    product = db.get(Product, payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")
    try:
        creatives = AIContentService(db).generate(product, payload.creative_type, payload.desired_count)
        return {"created": len(creatives), "message": "Yeni creative oluşturuldu." if creatives else "Bu ürün ve tür için istenen creative sayısı zaten mevcut."}
    except AIContentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{creative_id}")
def update(creative_id: int, payload: CreativeUpdateRequest, db: Session = Depends(get_db)):
    creative = db.get(PinCreative, creative_id)
    if not creative:
        raise HTTPException(status_code=404, detail="Creative bulunamadı.")
    creative.title = payload.title.strip()
    creative.description = payload.description.strip()
    creative.keywords = list(dict.fromkeys(keyword.strip() for keyword in payload.keywords if keyword.strip()))
    creative.call_to_action = payload.call_to_action.strip()
    db.commit()
    return {"id": creative.id, "status": creative.status}


@router.post("/{creative_id}/approve")
def approve(creative_id: int, db: Session = Depends(get_db)):
    creative = db.get(PinCreative, creative_id)
    if not creative:
        raise HTTPException(status_code=404, detail="Creative bulunamadı.")
    creative.status = PinCreativeStatus.APPROVED.value
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/{creative_id}/delete")
def delete(creative_id: int, db: Session = Depends(get_db)):
    creative = db.get(PinCreative, creative_id)
    if not creative:
        raise HTTPException(status_code=404, detail="Creative bulunamadı.")
    db.delete(creative)
    db.commit()
    return RedirectResponse(url="/", status_code=303)
