"""نقطة استخراج المستند الكامل (Document JSON) — الأساس لواجهة المحرر الجديدة
(محرر نصوص + عارض PDF جنباً إلى جنب). لا تمر عبر require_lm_configured كما في
spelling/tables (لا 503 إن غاب المفتاح) — لكن **لم تعد مجانية بالكامل بالضرورة**:
إن احتوت صفحة ممسوحة على جدول مُكتشَف هندسياً (`ingest._detect_scanned_table_regions`)
وكان LM مُهيَّأً فعلياً، يُستدعى `MedicalTableStructurer` تلقائياً لتصحيحه (قرار
مستخدم صريح — كل جدول مكتشف = استدعاء LM واحد)؛ يتدهور بأمان لشبكة خام غير مصحَّحة
إن كان LM غير مُهيَّأ أو فشل الاستدعاء، فلا يفشل الاستخراج نفسه أبداً بسبب ذلك.

Document نفسه (وليس نموذج HTTP منفصل) هو جسم الاستجابة عمداً هنا خلافاً لمبدأ الفصل
المذكور في schemas.py — لأن هذه النقطة غرضها الوحيد هو تعريض ذلك المخطط بالذات
(bbox لكل Block ضروري لميزة ربط الفقرة بموضعها في الـPDF في الواجهة)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from ...ingest import extract_document
from ...schema import Document
from ..rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


@router.post("/extract-document", response_model=Document)
@limiter.limit("20/minute")
async def extract_document_endpoint(request: Request, file: UploadFile = File(...)) -> Document:
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


@router.post("/extract-document-stream")
@limiter.limit("20/minute")
async def extract_document_stream_endpoint(request: Request, file: UploadFile = File(...)) -> StreamingResponse:
    """نسخة Server-Sent Events من `/extract-document` — نفس الاستخراج بالضبط، لكن
    تبثّ حدث تقدّم (`{"type": "progress", "page": N, "total": M}`) بعد كل صفحة
    منجزة، ثم حدثاً أخيراً واحداً (`{"type": "done", "document": {...}}`) يحمل
    المستند الكامل. **السبب:** مستند 30 صفحة ممسوحة (حد الخطة المجانية) قِيس فعلياً
    بحوالي 157 ثانية (استدعاء Vision API حقيقي متسلسل لكل صفحة) — طلب HTTP عادي
    واحد يُبقي المستخدم بلا أي تغذية راجعة طوال هذه المدة، فتبدو الواجهة "عالقة"
    رغم أنها تعمل فعلياً. `extract_document` نفسها تبقى متزامنة (blocking) — تُشغَّل
    هنا في executor thread منفصل، والاستدعاء المرجعي (`on_page_done`) يُمرِّر كل
    تحديث لحلقة الأحداث بأمان عبر `call_soon_threadsafe` (الاستدعاء يصل من thread
    مختلف عن الحلقة نفسها)."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="الملف المرفوع يجب أن يكون بصيغة PDF")

    file_bytes = await file.read()
    original_file_name = file.filename
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_file.write(file_bytes)
            tmp_path = tmp_file.name
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"تعذّرت قراءة الملف كـ PDF صالح: {exc}") from exc

    async def event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_page_done(page: int, total: int) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "progress", "page": page, "total": total})

        def run_extraction() -> None:
            try:
                document = extract_document(tmp_path, file_name=original_file_name, on_page_done=on_page_done)
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "done", "document": document.model_dump(mode="json")}
                )
            except Exception as exc:  # ملف تالف/ليس PDF فعلياً رغم الامتداد، إلخ
                logger.warning("فشل استخراج المستند (بثّ) %s: %s", original_file_name, exc)
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(exc)})

        loop.run_in_executor(None, run_extraction)

        try:
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item["type"] in ("done", "error"):
                    break
        finally:
            os.unlink(tmp_path)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
