from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import get_spelling_corrector
from ..schemas import SpellingCorrectionRequest, SpellingCorrectionResponse
from ..lm_guard import require_lm_configured
from ...signatures.spelling import MedicalSpellingCorrector

router = APIRouter(tags=["spelling"])


@router.post("/correct-spelling", response_model=SpellingCorrectionResponse)
def correct_spelling(
    payload: SpellingCorrectionRequest,
    request: Request,
    corrector: MedicalSpellingCorrector = Depends(get_spelling_corrector),
) -> SpellingCorrectionResponse:
    require_lm_configured(request)
    try:
        prediction = corrector(raw_text=payload.raw_text)
    except Exception as exc:  # خطأ فعلي من استدعاء LM (شبكة/حصة/نموذج) — يُعاد كـ 502 بدل تسريبه كـ 500 غامض
        raise HTTPException(status_code=502, detail=f"فشل استدعاء LM: {exc}") from exc

    return SpellingCorrectionResponse(
        raw_text=payload.raw_text,
        corrected_text=prediction.corrected_text,
        corrections=prediction.corrections,
        uncertain_terms=prediction.uncertain_terms,
        reasoning=prediction.reasoning,
    )
