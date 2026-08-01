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
from ...ingest_pptx import extract_pptx_document
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


@router.post("/extract-pptx", response_model=Document)
@limiter.limit("20/minute")
async def extract_pptx_endpoint(request: Request, file: UploadFile = File(...)) -> Document:
    """يستخرج Document من ملف PowerPoint حديث (.pptx فقط، Office Open XML) — انظر
    توثيق `ingest_pptx.py` لسبب استبعاد .ppt القديم عمداً (يحتاج LibreOffice/أداة
    تحويل نظام غير متاحة في بيئة النشر الحالية). استخراج بنيوي مباشر (لا OCR/LM)،
    فلا حاجة لنسخة streaming بتقدّم مرحلي كما في `/extract-document-stream`."""
    if not (file.filename or "").lower().endswith(".pptx"):
        raise HTTPException(status_code=422, detail="الملف المرفوع يجب أن يكون بصيغة .pptx (PowerPoint حديث)")

    file_bytes = await file.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tmp_file:
            tmp_file.write(file_bytes)
            tmp_path = tmp_file.name

        return extract_pptx_document(tmp_path, file_name=file.filename)
    except HTTPException:
        raise
    except Exception as exc:  # ملف تالف/ليس pptx صالحاً رغم الامتداد، إلخ
        logger.warning("فشل استخراج عرض PowerPoint %s: %s", file.filename, exc)
        raise HTTPException(status_code=422, detail=f"تعذّرت قراءة الملف كـ PowerPoint صالح: {exc}") from exc
    finally:
        if tmp_path is not None:
            os.unlink(tmp_path)


@router.post("/extract-document-stream")
@limiter.limit("20/minute")
async def extract_document_stream_endpoint(request: Request, file: UploadFile = File(...)) -> StreamingResponse:
    """نسخة Server-Sent Events من `/extract-document` — نفس الاستخراج بالضبط، لكن
    تبثّ حدث تقدّم (`{"type": "progress", "page": N, "total": M}`) **وحدث بيانات كامل
    لكل صفحة فور جهوزيتها** (`{"type": "page", "page": {...}, "images": [...]}`)، ثم
    حدثاً أخيراً `{"type": "done"}` بلا أي حمولة (كل المحتوى وصل فعلاً عبر أحداث
    "page" السابقة).

    **بثّ حقيقي للبيانات، وليس تقدّماً رقمياً فقط (تصحيح مهم):** النسخة السابقة كانت
    تبثّ فقط رقم الصفحة/الإجمالي أثناء المعالجة، ثم تُرسل **المستند الكامل** (كل
    النصوص + كل الصور base64) دفعة واحدة في حدث "done" الأخير — يعني الخادم كان يبقي
    كل صفحات/صور المستند مُجمَّعة في الذاكرة طوال الطلب رغم البثّ الظاهري، فذروة
    الذاكرة تتناسب مع حجم المستند **كاملاً** بصرف النظر عن كونه streaming. رُصِد فعلياً
    عبر Render أن ملفاً ~17MB كثيف الصور يُسقط الخادم بتجاوز حد الذاكرة (512MB على
    الخطة المجانية) رغم تحسين ذاكرة الصفحة الواحدة (`_flatten_to_white_rgb`/تقليل فك
    الترميز المكرر). الحل: `extract_document(..., on_page_ready=..., keep_full_result=False)`
    يبثّ كل صفحة فور اكتمالها **ولا يُبقيها** في قوائم الخادم الداخلية — فذروة الذاكرة
    تصير بحجم صفحة واحدة تقريباً بدل المستند كاملاً، بصرف النظر عن عدد الصفحات/الصور
    الكلي. لا تغيير إطلاقاً في منطق OCR/الاستخراج نفسه — فقط توقيت إرسال/الاحتفاظ
    بالبيانات.

    `extract_document` نفسها تبقى متزامنة (blocking) — تُشغَّل هنا في executor thread
    منفصل، والاستدعاءان المرجعيان (`on_page_done`/`on_page_ready`) يُمرِّران كل تحديث
    لحلقة الأحداث بأمان عبر `call_soon_threadsafe` (الاستدعاء يصل من thread مختلف عن
    الحلقة نفسها)."""
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

        def on_page_ready(page, page_images) -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "type": "page",
                    "page": page.model_dump(mode="json"),
                    "images": [image.model_dump(mode="json") for image in page_images],
                },
            )

        def run_extraction() -> None:
            try:
                extract_document(
                    tmp_path,
                    file_name=original_file_name,
                    on_page_done=on_page_done,
                    on_page_ready=on_page_ready,
                    keep_full_result=False,
                )
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "done"})
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
