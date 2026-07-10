from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import get_table_structurer
from ..schemas import TableStructuringRequest, TableStructuringResponse
from ..lm_guard import require_lm_configured
from ...signatures.tables import MedicalTableStructurer

router = APIRouter(tags=["tables"])


@router.post("/structure-table", response_model=TableStructuringResponse)
def structure_table(
    payload: TableStructuringRequest,
    request: Request,
    structurer: MedicalTableStructurer = Depends(get_table_structurer),
) -> TableStructuringResponse:
    require_lm_configured(request)
    try:
        prediction = structurer(raw_rows=payload.raw_rows, column_hints=payload.column_hints)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"فشل استدعاء LM: {exc}") from exc

    return TableStructuringResponse(
        structured_rows=prediction.structured_rows,
        notes=prediction.notes,
        reasoning=prediction.reasoning,
    )
