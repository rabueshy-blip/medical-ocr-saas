"""نماذج طلب/استجابة Pydantic لنقاط FastAPI — منفصلة عمداً عن medical_ocr/schema.py
(الذي يمثّل مخطط Document/Page/Block الموحّد الداخلي، وليس عقد HTTP الخارجي)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SpellingCorrectionRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, description="النص الخام كما استخرجه محرك الـ OCR")


class SpellingCorrectionResponse(BaseModel):
    raw_text: str
    corrected_text: str
    corrections: str = Field(..., description="JSON: قائمة {original, corrected, term_type}")
    uncertain_terms: str = Field(..., description="JSON: قائمة الكلمات غير المؤكدة")
    reasoning: str = Field(..., description="تفكير CoT الذي أدى إلى هذا التصحيح، لغرض التتبع")


class TableStructuringRequest(BaseModel):
    raw_rows: List[List[str]] = Field(..., min_length=1, description="صفوف/خلايا الجدول الخام")
    column_hints: Optional[List[str]] = Field(default=None, description="أسماء أعمدة متوقعة، إن توفرت")


class TableStructuringResponse(BaseModel):
    structured_rows: str = Field(..., description="JSON: قائمة صفوف بنفس عدد raw_rows")
    notes: str = Field(..., description="JSON: ملاحظات حول الخلايا الغامضة/UNCERTAIN")
    reasoning: str = Field(..., description="تفكير CoT الذي أدى إلى هذه الهيكلة، لغرض التتبع")


class HealthResponse(BaseModel):
    status: str
    lm_configured: bool
