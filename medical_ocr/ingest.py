"""
خط الاستخراج الأول من PDF إلى Document (يُستخدم من واجهة Streamlit — plan.md لم يخصّص
له قسماً بعد لأنه بُني تلبيةً لطلب مباشر لواجهة تفاعلية، وليس كجزء من تسلسل الأيام).

محرك OCR الأساسي الحالي للصفحات الممسوحة هو Google Vision API (DOCUMENT_TEXT_DETECTION)
عبر REST مباشرة بمفتاح API (بدون حزمة google-cloud-vision الثقيلة ولا ملف اعتماد خدمة —
نفس فلسفة مفتاح Gemini المباشر في lm_config.py). محرك easyocr المحلي (مجاني، بلا اعتمادية
على بطاقة دفع) لا يزال متاحاً كدالة منفصلة (`_scanned_page_blocks_easyocr`) للتحقق المزدوج
المخطَّط له في القسم 3-ب من plan.md، لكنه غير مستخدَم افتراضياً حالياً.

القرار بين صفحة "رقمية" و"ممسوحة": إن استخرج PyMuPDF نصاً أطول من MIN_DIGITAL_CHARS
حرفاً تُعامَل الصفحة كرقمية (نص PyMuPDF + جداول pdfplumber، بلا أي OCR)، وإلا تُعامَل
كممسوحة وتُمرَّر لـ Google Vision API كصورة (raster) لكامل الصفحة.

ملاحظة نطاق: Vision API لا يكتشف بنية الجداول (صفوف/أعمدة) في الصفحات الممسوحة —
DOCUMENT_TEXT_DETECTION يعطي فقرات (paragraphs) فقط، وليس خلايا جدول. لذا الصفحات
الممسوحة تُستخرَج حالياً كفقرات (Block من نوع paragraph) بغضّ النظر عن وجود جدول
مرئي فيها؛ اكتشاف الجداول الممسوحة يبقى بنداً مفتوحاً (PP-Structure/Table Transformer،
القسم 3-ب-4 من plan.md) — الصفحات الرقمية غير متأثرة (جداولها عبر pdfplumber كما هي).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import List, Optional

import fitz  # PyMuPDF
import pdfplumber
import requests

from .schema import Block, BlockType, BoundingBox, Document, Page, PageSource, SourceEngine

MIN_DIGITAL_CHARS = 20

_VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_VISION_MAX_ATTEMPTS = 3
_VISION_RETRY_BACKOFF_SECONDS = 1.5
_VISION_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)

_easyocr_reader = None


class VisionAPIError(RuntimeError):
    """خطأ في استدعاء Google Vision API (بعد استنفاد كل محاولات إعادة المحاولة، أو خطأ
    غير قابل لإعادة المحاولة مثل مفتاح API مفقود/غير صالح)."""


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        _easyocr_reader = easyocr.Reader(["ar", "en"], gpu=False)
    return _easyocr_reader


def _digital_page_blocks(page: fitz.Page) -> List[Block]:
    blocks = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("blocks"):
        text = text.strip()
        if not text:
            continue
        blocks.append(
            Block(
                block_type=BlockType.PARAGRAPH,
                text=text,
                bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                confidence=1.0,
                source_engine=SourceEngine.PYMUPDF,
            )
        )
    return blocks


def _table_blocks(pdfplumber_page) -> List[Block]:
    blocks = []
    for table in pdfplumber_page.find_tables():
        rows = table.extract()
        if not rows:
            continue
        normalized_rows = [[cell or "" for cell in row] for row in rows]
        x0, y0, x1, y1 = table.bbox
        blocks.append(
            Block(
                block_type=BlockType.TABLE,
                rows=normalized_rows,
                bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                confidence=1.0,
                source_engine=SourceEngine.PDFPLUMBER,
            )
        )
    return blocks


def _scanned_page_blocks_easyocr(page: fitz.Page, dpi: int = 200) -> List[Block]:
    """محرك OCR ثانٍ (محلي، مجاني) للصفحات الممسوحة — غير مستخدَم افتراضياً حالياً
    (انظر توثيق أعلى الملف)، محفوظ للتحقق المزدوج المخطَّط في القسم 3-ب من plan.md."""
    import numpy as np
    from PIL import Image

    pixmap = page.get_pixmap(dpi=dpi)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    results = _get_easyocr_reader().readtext(np.array(image))

    blocks = []
    for box_points, text, confidence in results:
        text = text.strip()
        if not text:
            continue
        xs = [point[0] for point in box_points]
        ys = [point[1] for point in box_points]
        blocks.append(
            Block(
                block_type=BlockType.PARAGRAPH,
                text=text,
                bbox=BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)),
                confidence=float(confidence),
                source_engine=SourceEngine.EASYOCR,
            )
        )
    return blocks


def _get_vision_api_key() -> str:
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if not api_key:
        raise VisionAPIError(
            "GOOGLE_VISION_API_KEY غير موجود في البيئة أو في ملف .env. أنشئ مفتاح API "
            "من Google Cloud Console (يتطلب مشروع GCP مع تفعيل Billing، حتى ضمن الحصة "
            "المجانية) ثم أضِفه إلى .env في جذر المشروع (GOOGLE_VISION_API_KEY=...) قبل "
            "استخراج أي صفحة ممسوحة ضوئياً."
        )
    return api_key


def _call_vision_api(image_bytes: bytes) -> dict:
    """يستدعي Google Vision API عبر REST مباشرة (مفتاح API فقط، بلا حزمة SDK ثقيلة).

    يستخدم عمداً DOCUMENT_TEXT_DETECTION وليس TEXT_DETECTION: الأولى مخصَّصة لمستندات
    كثيفة (تقارير طبية) وتعطي تجميعاً هرمياً page -> block -> paragraph -> word -> symbol
    مع bbox وconfidence لكل مستوى، بخلاف TEXT_DETECTION المُحسَّن لكلمات متفرقة داخل صور
    طبيعية (لافتات، إلخ) بلا هذا التجميع.

    إعادة المحاولة (retry) تُطبَّق فقط على الأعطال التقنية العابرة (أخطاء اتصال/timeout أو
    HTTP 5xx من جهة الخادم) — وليس على أخطاء 4xx (مفتاح غير صالح/طلب سيّئ)، لأن إعادة
    محاولة خطأ مصادقة لن تُصلحه وتُهدر وقتاً/حصة فقط.
    """
    api_key = _get_vision_api_key()
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["ar", "en"]},
            }
        ]
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, _VISION_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                _VISION_ENDPOINT,
                params={"key": api_key},
                json=payload,
                timeout=_VISION_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < _VISION_MAX_ATTEMPTS:
                logger.warning(
                    "فشل اتصال بـ Google Vision API (محاولة %d/%d): %s",
                    attempt,
                    _VISION_MAX_ATTEMPTS,
                    exc,
                )
                time.sleep(_VISION_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise VisionAPIError(
                f"فشل الاتصال بـ Google Vision API بعد {_VISION_MAX_ATTEMPTS} محاولات: {exc}"
            ) from exc

        if response.status_code == 200:
            body = response.json()
            api_error = body.get("responses", [{}])[0].get("error")
            if api_error:
                raise VisionAPIError(
                    f"Google Vision API أعاد خطأ: {api_error.get('message', api_error)}"
                )
            return body["responses"][0]

        if response.status_code >= 500 and attempt < _VISION_MAX_ATTEMPTS:
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            logger.warning(
                "خطأ خادم من Google Vision API (محاولة %d/%d): %s",
                attempt,
                _VISION_MAX_ATTEMPTS,
                last_error,
            )
            time.sleep(_VISION_RETRY_BACKOFF_SECONDS * attempt)
            continue

        # 4xx أو خطأ 5xx بعد استنفاد المحاولات: لا فائدة من إعادة محاولة إضافية.
        raise VisionAPIError(
            f"Google Vision API أعاد HTTP {response.status_code}: {response.text[:300]}"
        )

    raise VisionAPIError(
        f"فشل استدعاء Google Vision API بعد {_VISION_MAX_ATTEMPTS} محاولات: {last_error}"
    )


def _blocks_from_vision_page(vision_page: dict) -> List[Block]:
    """يحوّل صفحة واحدة من fullTextAnnotation.pages[i] إلى Blocks (فقرة لكل paragraph).

    تبسيط متعمد: يفصل بين الكلمات بمسافة واحدة دوماً بدل قراءة detectedBreak لكل رمز
    (type: SPACE/LINE_BREAK/EOL_SURE_SPACE) — كافٍ لعرض النص ولتصحيح DSPy اللاحق، وليس
    الهدف إعادة بناء تنسيق مطابق للصفحة حرفياً."""
    blocks: List[Block] = []
    for block in vision_page.get("blocks", []):
        for paragraph in block.get("paragraphs", []):
            words_text = []
            confidences = []
            for word in paragraph.get("words", []):
                word_text = "".join(s.get("text", "") for s in word.get("symbols", []))
                if not word_text:
                    continue
                words_text.append(word_text)
                if "confidence" in word:
                    confidences.append(word["confidence"])

            text = " ".join(words_text).strip()
            if not text:
                continue

            vertices = paragraph.get("boundingBox", {}).get("vertices", [])
            bbox = None
            if vertices:
                xs = [v.get("x", 0) for v in vertices]
                ys = [v.get("y", 0) for v in vertices]
                bbox = BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))

            if confidences:
                confidence = sum(confidences) / len(confidences)
            else:
                confidence = float(paragraph.get("confidence", block.get("confidence", 0.0)) or 0.0)

            blocks.append(
                Block(
                    block_type=BlockType.PARAGRAPH,
                    text=text,
                    bbox=bbox,
                    confidence=confidence,
                    source_engine=SourceEngine.GOOGLE_VISION,
                )
            )
    return blocks


def _scanned_page_blocks_vision(page: fitz.Page, dpi: int = 200) -> List[Block]:
    """محرك OCR الأساسي الحالي للصفحات الممسوحة: يرستر الصفحة كاملة إلى PNG ثم يمرّرها
    لـ Google Vision API (DOCUMENT_TEXT_DETECTION)."""
    pixmap = page.get_pixmap(dpi=dpi)
    image_bytes = pixmap.tobytes("png")

    vision_response = _call_vision_api(image_bytes)
    full_text_annotation = vision_response.get("fullTextAnnotation")
    if not full_text_annotation or not full_text_annotation.get("pages"):
        return []

    blocks: List[Block] = []
    for vision_page in full_text_annotation["pages"]:
        blocks.extend(_blocks_from_vision_page(vision_page))
    return blocks


def extract_document(pdf_path: str, file_name: Optional[str] = None) -> Document:
    """يفتح ملف PDF كاملاً ويحوّله إلى Document: نص رقمي عبر PyMuPDF + جداول pdfplumber
    للصفحات التي تحتوي طبقة نص، وOCR عبر Google Vision API (raster لكامل الصفحة) للصفحات
    الممسوحة ضوئياً بلا طبقة نص.

    فشل استخراج صفحة ممسوحة واحدة عبر Vision API (بعد استنفاد إعادة المحاولة في
    `_call_vision_api`) لا يوقف استخراج بقية المستند — تُسجَّل الصفحة بـ Block واحد
    يوضّح الفشل بنص عربي صريح (confidence=0.0) بدل رفع استثناء يفقد نتائج كل الصفحات
    الأخرى الناجحة."""
    fitz_doc = fitz.open(pdf_path)
    pages: List[Page] = []

    with pdfplumber.open(pdf_path) as plumber_doc:
        for index in range(fitz_doc.page_count):
            fitz_page = fitz_doc[index]
            digital_text = fitz_page.get_text("text").strip()

            if len(digital_text) >= MIN_DIGITAL_CHARS:
                blocks = _digital_page_blocks(fitz_page) + _table_blocks(plumber_doc.pages[index])
                source = PageSource.DIGITAL
            else:
                source = PageSource.SCANNED
                try:
                    blocks = _scanned_page_blocks_vision(fitz_page)
                except VisionAPIError as exc:
                    logger.warning(
                        "فشل استخراج الصفحة %d عبر Google Vision API: %s", index + 1, exc
                    )
                    blocks = [
                        Block(
                            block_type=BlockType.PARAGRAPH,
                            text=f"[تعذّر استخراج هذه الصفحة تلقائياً — خطأ Google Vision API: {exc}]",
                            confidence=0.0,
                            source_engine=SourceEngine.GOOGLE_VISION,
                        )
                    ]

            pages.append(Page(page_number=index + 1, source=source, blocks=blocks))

    fitz_doc.close()
    return Document(file_name=file_name or pdf_path, pages=pages)
