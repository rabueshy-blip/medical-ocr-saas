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
import random
import time
from io import BytesIO
from typing import List, Optional, Set

import fitz  # PyMuPDF
import pdfplumber
import requests
from PIL import Image

from .schema import (
    Block,
    BlockType,
    BoundingBox,
    Document,
    ImageAsset,
    Page,
    PageSource,
    SourceEngine,
)

MIN_DIGITAL_CHARS = 20

_VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_VISION_MAX_ATTEMPTS = 4
_VISION_RETRY_BACKOFF_SECONDS = 1.5
_VISION_RETRY_BACKOFF_MAX_SECONDS = 12.0
_VISION_TIMEOUT_SECONDS = 30
_VISION_TIMEOUT_MAX_SECONDS = 90

# الحد الفعلي لحجم الصورة المُرسَلة لـ Vision API — أصغر بكثير من حد الـ API الرسمي
# (~20MB) عمداً: صفحات ممسوحة بدقة DPI عالية تنتج PNG كبيراً يسبب أخطاء "Request size
# exceeds the limit" وانتهاء مهلة الاتصال على شبكات بطيئة قبل الوصول للحد الرسمي أصلاً.
_VISION_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_VISION_MIN_JPEG_QUALITY = 40

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


def _bbox_center_in_any(bbox: tuple, containers: List[tuple]) -> bool:
    """يتحقق إن كانت نقطة مركز bbox تقع داخل أي مستطيل من containers — تُستخدم لاستبعاد
    spans نصية تنتمي فعلياً لخلية جدول مكتشف بدل تكرارها كفقرة منفصلة."""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    for x0, y0, x1, y1 in containers:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return True
    return False


def _digital_page_blocks(page: fitz.Page, exclude_bboxes: Optional[List[tuple]] = None) -> List[Block]:
    exclude_bboxes = exclude_bboxes or []
    blocks = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("blocks"):
        text = text.strip()
        if not text:
            continue
        if _bbox_center_in_any((x0, y0, x1, y1), exclude_bboxes):
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


_DEFAULT_TEXT_TOLERANCE = 5
_MIN_TEXT_TOLERANCE = 1


def _cluster_lines(words: list) -> list:
    """يجمّع كلمات الصفحة (من `extract_words`) في "أسطر" فعلية حسب تراكب مداها الرأسي
    (top/bottom)، بمعزل عن ترتيب استخراج pdfplumber. كل سطر مُرتَّب أفقياً (x0) لأن حساب
    فجوات الأعمدة لاحقاً يحتاج كلمات متجاورة على نفس السطر بترتيب القراءة."""
    lines: list = []
    for word in sorted(words, key=lambda w: w["top"]):
        placed = False
        for line in lines:
            line_top = min(w["top"] for w in line)
            line_bottom = max(w["bottom"] for w in line)
            if min(word["bottom"], line_bottom) - max(word["top"], line_top) > 0:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _min_positive(values: list) -> Optional[float]:
    positive = [v for v in values if v > 0]
    return min(positive) if positive else None


def _dynamic_text_table_settings(pdfplumber_page) -> dict:
    """يحسب text_x_tolerance/text_y_tolerance ديناميكياً بدل قيمة ثابتة، بناءً على أضيق
    فجوة فعلية بين كلمات متجاورة في الصفحة (تجربة تحسينية).

    السبب: `text_x_tolerance` يُمرَّر فعلياً لـ`extract_words` الذي يبنى عليه توليف حدود
    الأعمدة في استراتيجية `"text"` — تفاوت ثابت (5px) أكبر من الفجوة الحقيقية بين عمودين
    متقاربين (شائع في نتائج مخبرية مضغوطة، مثال Result/Unit) يجعل pdfplumber يلتحم نص
    العمودين في "كلمة" واحدة عابرة لحدود الخلية، فيظهر النص في خلية خطأ في Word الناتج.
    الحل: قياس أضيق فجوة أفقية فعلية بين كلمتين متجاورتين على نفس السطر تقريباً، وإن كانت
    أصغر من ضِعف التفاوت الافتراضي نخفّض `text_x_tolerance` إلى نصف تلك الفجوة (بحد أدنى
    1px) بدل الإبقاء على قيمة ثابتة قد تبتلعها. نفس المنطق رأسياً بين الأسطر (`text_y_tolerance`)
    لمنع التحام نص سطرين متقاربين رأسياً في خلية واحدة.

    ملاحظة مهمة: هذا لا علاقة له بمشكلة الصفوف الفارغة الوهمية الموثّقة سابقاً (تلك ناتجة عن
    توليف خطوط وهمية من مواضع الكلمات نفسها، ولا يحلّها ضبط التفاوت — لذلك تبقى مُعالَجة عبر
    فلترة الصفوف الفارغة في `_table_blocks`)؛ هذا تحسين لمشكلة مختلفة: التحام نص عمودين
    متجاورين ضمن نفس الصف."""
    words = pdfplumber_page.extract_words(x_tolerance=1, y_tolerance=1, keep_blank_chars=False)
    x_tol, y_tol = _DEFAULT_TEXT_TOLERANCE, _DEFAULT_TEXT_TOLERANCE
    if not words:
        return {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "text_x_tolerance": x_tol,
            "text_y_tolerance": y_tol,
        }

    lines = _cluster_lines(words)

    min_h_gap = _min_positive(
        b["x0"] - a["x1"] for line in lines for a, b in zip(line, line[1:])
    )
    if min_h_gap is not None and min_h_gap < _DEFAULT_TEXT_TOLERANCE * 2:
        x_tol = max(_MIN_TEXT_TOLERANCE, min_h_gap / 2)

    line_bboxes = sorted(
        ((min(w["top"] for w in line), max(w["bottom"] for w in line)) for line in lines)
    )
    min_v_gap = _min_positive(
        b_top - a_bottom for (_, a_bottom), (b_top, _) in zip(line_bboxes, line_bboxes[1:])
    )
    if min_v_gap is not None and min_v_gap < _DEFAULT_TEXT_TOLERANCE * 2:
        y_tol = max(_MIN_TEXT_TOLERANCE, min_v_gap / 2)

    return {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "text_x_tolerance": x_tol,
        "text_y_tolerance": y_tol,
    }


def _find_tables(pdfplumber_page):
    """يحاول اكتشاف الجداول أولاً بالإعدادات الافتراضية (تعتمد خطوط شبكة مرسومة فعلياً —
    الأدق حين تكون موجودة)، ثم يلجأ لاستراتيجية `"text"` (محاذاة نصية بلا خطوط) فقط إن
    لم يكتشف الإعداد الافتراضي أي جدول — كثير من جداول النتائج المخبرية الطبية ليس لها
    خطوط شبكة مرسومة أصلاً. تطبيق `"text"` مباشرة على جدول له خطوط فعلية يُفسِد النتيجة
    (يُنتج صفوفاً فارغة وهمية من المسافة حول الخطوط)، لذا الترتيب هنا مقصود وليس تبسيطاً.
    عند اللجوء لاستراتيجية `"text"`، التفاوت (`text_x/y_tolerance`) يُحسَب ديناميكياً لكل
    صفحة عبر `_dynamic_text_table_settings` بدل قيمة ثابتة."""
    tables = pdfplumber_page.find_tables()
    if tables:
        return tables
    return pdfplumber_page.find_tables(table_settings=_dynamic_text_table_settings(pdfplumber_page))


def _extract_table_rows(table) -> tuple[List[List[str]], List[List[int]]]:
    """يبني rows (نص كل خلية حقيقية) وcolspans (عدد الأعمدة التي تمتد عبرها) من جدول
    pdfplumber واحد.

    خلية مدمجة في PDF المصدر (بلا خط فاصل بينها وبين ما يجاورها) تظهر في
    `table.rows[i].cells` كموضع واحد ببعد عريض، تتبعه قيمة `None` في كل موضع شبكي
    ابتلعته — تحقّقنا من هذا السلوك عملياً على جدول حقيقي فيه دمج (وليس افتراضاً نظرياً)
    قبل الاعتماد عليه. `table.extract()` يضع نص الخلية المدمجة في موضعها الأول ويترك
    `None` (وليس نصاً فارغاً) في المواضع المُبتلَعة، فنستخدم هذا الفارق (`None` تحديداً)
    للتمييز بين "خلية مدمجة اُبتلعت" و"خلية عادية فارغة فعلاً" (نص فارغ `""`)."""
    extracted_rows = table.extract()
    rows: List[List[str]] = []
    colspans: List[List[int]] = []
    for row_obj, text_row in zip(table.rows, extracted_rows):
        cells = row_obj.cells
        row_texts: List[str] = []
        row_spans: List[int] = []
        i = 0
        n = len(cells)
        while i < n:
            if cells[i] is None:
                i += 1
                continue
            span = 1
            j = i + 1
            while j < n and cells[j] is None:
                span += 1
                j += 1
            row_texts.append(text_row[i] or "")
            row_spans.append(span)
            i = j
        rows.append(row_texts)
        colspans.append(row_spans)
    return rows, colspans


def _table_blocks(pdfplumber_page) -> List[Block]:
    blocks = []
    for table in _find_tables(pdfplumber_page):
        # حارس أمان: عرض الشبكة الفعلي (قبل أي دمج) يجب أن يكون عمودين على الأقل —
        # استراتيجية "text" الاحتياطية تتحمس أحياناً على نص عادي محاذى لليسار وتخترع
        # "جدولاً" بعمود واحد يكرر نفس الفقرات، وعمود واحد لا يمثّل جدولاً قابلاً
        # للترجمة عبر صفوف/أعمدة فعلية.
        grid_width = len(table.rows[0].cells) if table.rows else 0
        if grid_width < 2:
            continue

        rows, colspans = _extract_table_rows(table)
        # صفوف فارغة بالكامل (كل خلاياها "") هي ضوضاء بنيوية بحتة (ناتجة عن توليف خطوط
        # وهمية من مواضع الكلمات في استراتيجية "text") ولا تحمل أي بيانات تُترجَم أبداً،
        # فتُحذف بغضّ النظر عن مصدر الجدول.
        kept = [
            (row, spans)
            for row, spans in zip(rows, colspans)
            if any(cell.strip() for cell in row)
        ]
        if not kept:
            continue
        rows, colspans = [r for r, _ in kept], [s for _, s in kept]

        normalized_rows = [[cell or "" for cell in row] for row in rows]
        x0, y0, x1, y1 = table.bbox
        blocks.append(
            Block(
                block_type=BlockType.TABLE,
                rows=normalized_rows,
                colspans=colspans,
                bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                confidence=1.0,
                source_engine=SourceEngine.PDFPLUMBER,
            )
        )
    return blocks


def _page_images(
    fitz_doc: fitz.Document, fitz_page: fitz.Page, page_number: int, seen_xrefs: Set[int]
) -> List[ImageAsset]:
    """يستخرج الصور المُضمَّنة في صفحة رقمية واحدة (شعارات/رسوم بيانية) لمكتبة الوسائط.

    `seen_xrefs` يُشارَك عبر المستند كله لتفادي تكرار نفس الصورة (شعار ثابت في كل
    صفحة مثلاً) مرات عديدة في الحمولة المُرسَلة للواجهة."""
    images: List[ImageAsset] = []
    for img_index, img in enumerate(fitz_page.get_images(full=True)):
        xref = img[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            extracted = fitz_doc.extract_image(xref)
        except Exception as exc:
            logger.warning("تعذّر استخراج الصورة xref=%d من الصفحة %d: %s", xref, page_number, exc)
            continue
        images.append(
            ImageAsset(
                page_number=page_number,
                index=img_index,
                mime_type=f"image/{extracted['ext']}",
                data_base64=base64.b64encode(extracted["image"]).decode("ascii"),
                width=extracted.get("width", 0),
                height=extracted.get("height", 0),
            )
        )
    return images


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


def _compress_image_to_limit(
    image_bytes: bytes, max_bytes: int = _VISION_MAX_IMAGE_BYTES
) -> bytes:
    """يضغط صورة صفحة ممسوحة تحت حد حجم Vision API قبل إرسالها.

    الترتيب مقصود: يُخفَّض أولاً جودة ترميز JPEG تدريجياً (تُبقي على نفس الدقة/الأبعاد،
    وبالتالي أقل ضرراً على دقة OCR)، ولا تُصغَّر الأبعاد الفعلية للصورة إلا كملاذ أخير
    إن لم تكفِ الجودة وحدها — تصغير الأبعاد يفقد تفاصيل حروف/أرقام دقيقة في مستندات
    طبية كثيفة النص أكثر من تقليل جودة الضغط.
    """
    if len(image_bytes) <= max_bytes:
        return image_bytes

    image = Image.open(BytesIO(image_bytes)).convert("RGB")

    for quality in (85, 70, 55, _VISION_MIN_JPEG_QUALITY):
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        candidate = buffer.getvalue()
        if len(candidate) <= max_bytes:
            logger.info(
                "ضُغطت صورة الصفحة إلى %d بايت (جودة JPEG=%d) للبقاء تحت حد %d بايت",
                len(candidate),
                quality,
                max_bytes,
            )
            return candidate

    scaled = image
    for _ in range(6):
        new_size = (max(1, int(scaled.width * 0.85)), max(1, int(scaled.height * 0.85)))
        scaled = scaled.resize(new_size, Image.LANCZOS)
        buffer = BytesIO()
        scaled.save(buffer, format="JPEG", quality=_VISION_MIN_JPEG_QUALITY, optimize=True)
        candidate = buffer.getvalue()
        if len(candidate) <= max_bytes:
            logger.info(
                "صُغِّرت أبعاد صورة الصفحة إلى %dx%d (%d بايت) للبقاء تحت حد %d بايت",
                new_size[0],
                new_size[1],
                len(candidate),
                max_bytes,
            )
            return candidate

    raise VisionAPIError(
        f"تعذّر ضغط صورة الصفحة تحت حد {max_bytes} بايت حتى بعد تقليل الجودة والأبعاد."
    )


def _next_backoff_seconds(attempt: int) -> float:
    """تصاعد أُسّي (exponential backoff) محدود بحد أقصى، مع jitter عشوائي بسيط (±20%)
    لتفادي أن تُعيد عدة صفحات فاشلة في نفس اللحظة المحاولة معاً بنفس التوقيت بالضبط."""
    base = min(_VISION_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)), _VISION_RETRY_BACKOFF_MAX_SECONDS)
    jitter = base * random.uniform(-0.2, 0.2)
    return max(0.1, base + jitter)


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

    كل من مهلة الاتصال (timeout) والانتظار بين المحاولات (backoff) يزدادان تصاعدياً مع
    كل محاولة فاشلة (مع حد أقصى لكل منهما) بدل قيمة ثابتة — صفحة كبيرة/شبكة بطيئة قد
    تحتاج مهلة أطول من المحاولة الأولى، والزيادة التصاعدية للانتظار (مع jitter عشوائي
    بسيط) تقلّل احتمال اصطدام عدة محاولات متتالية بنفس عطل الخادم العابر.
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
        timeout = min(
            _VISION_TIMEOUT_SECONDS * attempt, _VISION_TIMEOUT_MAX_SECONDS
        )
        try:
            response = requests.post(
                _VISION_ENDPOINT,
                params={"key": api_key},
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < _VISION_MAX_ATTEMPTS:
                kind = "مهلة اتصال" if isinstance(exc, requests.exceptions.Timeout) else "اتصال"
                backoff = _next_backoff_seconds(attempt)
                logger.warning(
                    "فشل %s بـ Google Vision API (محاولة %d/%d، مهلة %ds): %s — إعادة "
                    "المحاولة بعد %.1fs",
                    kind,
                    attempt,
                    _VISION_MAX_ATTEMPTS,
                    timeout,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
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
            backoff = _next_backoff_seconds(attempt)
            logger.warning(
                "خطأ خادم من Google Vision API (محاولة %d/%d): %s — إعادة المحاولة بعد %.1fs",
                attempt,
                _VISION_MAX_ATTEMPTS,
                last_error,
                backoff,
            )
            time.sleep(backoff)
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
    """محرك OCR الأساسي الحالي للصفحات الممسوحة: يرستر الصفحة كاملة إلى PNG، يضغطها تحت
    حد حجم Vision API عند الحاجة (`_compress_image_to_limit`)، ثم يمرّرها لـ Google
    Vision API (DOCUMENT_TEXT_DETECTION)."""
    pixmap = page.get_pixmap(dpi=dpi)
    image_bytes = _compress_image_to_limit(pixmap.tobytes("png"))

    vision_response = _call_vision_api(image_bytes)
    full_text_annotation = vision_response.get("fullTextAnnotation")
    if not full_text_annotation or not full_text_annotation.get("pages"):
        return []

    blocks: List[Block] = []
    for vision_page in full_text_annotation["pages"]:
        blocks.extend(_blocks_from_vision_page(vision_page))
    return blocks


def _normalize_header_text(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _strip_repeated_page_headers(pages: List[Page]) -> None:
    """يحذف الترويسة المكررة (شعار/اسم المستشفى، عنوان التقرير) من بداية كل صفحة بعد
    الأولى إن كانت مطابقة نصياً لبداية الصفحة الأولى — يُبقي أثراً واحداً فقط في
    المستند الناتج بدل تكرارها في كل صفحة، وهو ما يربك برامج CAT عند فتح ملف Word
    المُصدَّر لاحقاً. المقارنة بالترتيب موضعياً (block بـblock) بادئةً من أول الصفحة،
    وتتوقف عند أول اختلاف أو أول جدول (block.text=None) — الجداول لا تُعتبر جزءاً من
    الترويسة أبداً."""
    if len(pages) < 2:
        return

    first_page_texts = [
        _normalize_header_text(b.text) if b.text is not None else None for b in pages[0].blocks
    ]

    for page in pages[1:]:
        match_count = 0
        for block, first_text in zip(page.blocks, first_page_texts):
            if block.text is None or first_text is None:
                break
            if _normalize_header_text(block.text) != first_text:
                break
            match_count += 1
        if match_count:
            page.blocks = page.blocks[match_count:]


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
    images: List[ImageAsset] = []
    seen_image_xrefs: Set[int] = set()

    with pdfplumber.open(pdf_path) as plumber_doc:
        for index in range(fitz_doc.page_count):
            fitz_page = fitz_doc[index]
            digital_text = fitz_page.get_text("text").strip()

            if len(digital_text) >= MIN_DIGITAL_CHARS:
                table_blocks = _table_blocks(plumber_doc.pages[index])
                table_bboxes = [
                    (b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1) for b in table_blocks if b.bbox
                ]
                blocks = _digital_page_blocks(fitz_page, exclude_bboxes=table_bboxes) + table_blocks
                source = PageSource.DIGITAL
                images.extend(_page_images(fitz_doc, fitz_page, index + 1, seen_image_xrefs))
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
    _strip_repeated_page_headers(pages)
    return Document(file_name=file_name or pdf_path, pages=pages, images=images)
