"""نقطة استخراج المستند الكامل (Document JSON) — الأساس لواجهة المحرر الجديدة
(محرر نصوص + عارض PDF جنباً إلى جنب). لا تحتاج LM إطلاقاً (extract_document حرة/مجانية)،
لذا لا تمر عبر require_lm_configured كما في spelling/tables.

Document نفسه (وليس نموذج HTTP منفصل) هو جسم الاستجابة عمداً هنا خلافاً لمبدأ الفصل
المذكور في schemas.py — لأن هذه النقطة غرضها الوحيد هو تعريض ذلك المخطط بالذات
(bbox لكل Block ضروري لميزة ربط الفقرة بموضعها في الـPDF في الواجهة)."""

from __future__ import annotations

import logging
import os
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile

from ...ingest import extract_document
from ...schema import Document

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


@router.post("/extract-document", response_model=Document)
async def extract_document_endpoint(file: UploadFile = File(...)) -> Document:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="الملف المرفوع يجب أن يكون بصيغة PDF")

    file_bytes = await file.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_file.write(file_bytes)
            tmp_path = tmp_file.name

        return extract_document(tmp_path, file_name=file.filename)
    except HTTPException:
        raise
    except Exception as exc:  # ملف تالف/ليس PDF فعلياً رغم الامتداد، إلخ
        logger.warning("فشل استخراج المستند %s: %s", file.filename, exc)
        raise HTTPException(status_code=422, detail=f"تعذّرت قراءة الملف كـ PDF صالح: {exc}") from exc
    finally:
        if tmp_path is not None:
            os.unlink(tmp_path)
