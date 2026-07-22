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

ملاحظة نطاق (محدَّثة): Vision API (DOCUMENT_TEXT_DETECTION) لا يعطي كياناً اسمه "جدول"
أصلاً — فقط نص + bbox في كل مستوى (page/block/paragraph/word/symbol)، بخلاف Google
Document AI الذي لديه مُحلِّل جداول مخصَّص (لم يُعتمَد هنا، يتطلب تفعيل API/processor
منفصل). بدلاً من ذلك، بنية الجدول (صفوف/أعمدة) في الصفحات الممسوحة تُكتشَف هندسياً من
bbox كل كلمة (`_vision_word_boxes` + `_detect_scanned_table_regions`، نفس أسلوب
`_dynamic_text_table_settings` للصفحات الرقمية لكن من كلمات OCR بدل pdfplumber)، ثم
تُصحَّح تلقائياً عبر `MedicalTableStructurer` (القسم 3-ب-4 من plan.md — كان بلا مستدعٍ
حتى الآن) قبل أن تصير Block(TABLE) حقيقياً. الصفحات الرقمية غير متأثرة (جداولها عبر
pdfplumber كما هي).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import statistics
import time
from functools import lru_cache
from io import BytesIO
from typing import List, Optional, Set

import cv2
import dspy
import fitz  # PyMuPDF
import numpy as np
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
from .signatures.tables import MedicalTableStructurer

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

# اكتشاف جداول الصفحات الممسوحة هندسياً (`_detect_scanned_table_regions`): أي تسلسل
# من أسطر متتالية له هذا الحد الأدنى من الأسطر/الخلايا يُعتبر "منطقة جدول" مرشَّحة.
# **جُرِّب رفعه إلى 3 لاستبعاد أزواج حقول الترويسة/التذييل الوهمية (NAME/FILE NO،
# DATE/REF) — لكن هذا فعلياً استبعد أيضاً جدولاً سريرياً حقيقياً قصيراً (صفّان فقط:
# "Overview of measurement result" بقيم Neck/Total الحقيقية)، مُشتِّتاً أرقامه إلى
# 12 فقرة منفصلة — نفس المشكلة الأصلية بالضبط.** الحل الأدق: الإبقاء على 2، مع
# فلتر محتوى (`_looks_like_non_clinical_metadata`) يستبعد حسب كثافة الخلايا
# الرقمية بدل حد أدنى عام لعدد الصفوف — انظر توثيقها.
_SCANNED_TABLE_MIN_ROWS = 2
_SCANNED_TABLE_MIN_COLS = 2
# أصغر نسبة خلايا "رقمية صرفة" (بعد إزالة الترقيم الشائع) في منطقة كي تُعتبر
# جدول بيانات سريري حقيقي — انظر `_looks_like_non_clinical_metadata`.
_MIN_NUMERIC_CELL_FRACTION = 0.5
# أكبر تباين نسبي (coefficient of variation) مقبول في عدد خلايا الصفوف داخل منطقة
# واحدة — انظر `_has_inconsistent_row_lengths`. جداول سريرية حقيقية مُختبَرة فعلياً
# (Lumbar/Femur) كانت ≈0.1-0.14، بينما نص متناثر حول رسم بياني ≈0.6-0.7.
_MAX_ROW_LENGTH_VARIATION = 0.3

# اكتشاف جداول بخطوط شبكة مرسومة فعلياً (`_detect_ruled_table_regions`) — استمارات/
# جداول معلومات مريض حدودها مرسومة، خلافاً لجداول القياس بلا حدود المُكتشَفة هندسياً
# من تباعد النص فقط. قرار مستخدم صريح: أي جدول بخطوط مرسومة فعلياً يُعتبر جدولاً
# دوماً بصرف النظر عن محتواه (لا يخضع لفلاتر `_looks_like_non_clinical_metadata`/
# `_has_inconsistent_row_lengths` المخصَّصة للاكتشاف الهندسي من النص فقط).
_RULED_TABLE_MIN_LINE_PIXELS = 800
# قطاعات خط رأسي/أفقي أطول من هذه النسبة من ارتفاع/عرض الصفحة تُستبعَد بصفتها ظل/
# حافة صفحة مصوَّرة (لوحظ فعلياً: خطان رأسيان بطول الصفحة كاملاً من حافتَي الصورة
# المصوَّرة، وليسا جزءاً من أي جدول) — جدول حقيقي نادراً ما يمتد طوله/عرضه لهذه الدرجة.
_RULED_TABLE_MAX_VERTICAL_SPAN_RATIO = 0.5
_RULED_TABLE_MAX_HORIZONTAL_SPAN_RATIO = 0.85
# هامش حواف الصورة يُستبعَد كلياً قبل اكتشاف الخطوط (نفس سبب أعلاه: ظل/انحناء تصوير
# شائع عند حواف الصفحة المصوَّرة، جدول حقيقي لا يلامس حافة الصفحة المطلقة عملياً).
_RULED_TABLE_EDGE_MARGIN_RATIO = 0.02
# قطاع خط طويل جداً (يتجاوز نسبة الامتداد أعلاه) يُستبعَد فقط إن كان **أيضاً** قريباً
# من حافة الصورة المطلقة ضمن هذه النسبة — طول وحده لا يكفي (جدول حقيقي طويل، مثال
# استمارة فيها خلية شكوى مريض ضخمة، قد يشغل معظم ارتفاع الصفحة أيضاً لكنه لا يلامس
# الحافة المطلقة عملياً، يبقى هامش أبيض حوله دوماً).
_RULED_TABLE_EDGE_ARTIFACT_MARGIN_RATIO = 0.15
# أصغر نسبة تغطية (طول الخط ÷ امتداد المحور الكلي) تُعتبر خط شبكة حقيقياً داخل منطقة
# جدول — 0.15 وليس أعلى: جدول حقيقي (استمارة إحالة) فيه خلية واحدة ضخمة مدمجة (شكوى
# المريض، نص سريري طويل) يجعل الخط الرأسي الفاصل الحقيقي بين عمودي "تسمية/قيمة" لا
# يغطي إلا ~26% من الارتفاع الكلي للمنطقة (يقتصر على الصفوف القصيرة أعلى/أسفل الخلية
# الضخمة) — عتبة أعلى (كانت 0.3 مبدئياً) تفوّت هذا الخط الحقيقي تماماً.
_RULED_TABLE_LINE_COVERAGE_RATIO = 0.15
# أصغر قفزة نسبية بين فجوتين متتاليتين (مُرتَّبتين، بعد تجاهل ما دون
# `_SCANNED_TABLE_MIN_GAP_FOR_RATIO_CHECK`) تُعتبر انفصالاً حقيقياً بين "تباعد كلمات
# عادي" و"فجوة عمود جدول" — انظر `_find_gap_threshold`.
# **درس مهم (غير متعلق بأي منطق برمجي هنا):** Vision API لا يُرجع نفس bounding
# boxes بالضبط لنفس صورة الصفحة (نفس bytes بالضبط، تحقّق عبر sha256) عبر استدعاءات
# منفصلة على فترات زمنية متباعدة (دقائق) رغم استقرارها ضمن دفعة استدعاءات متقاربة
# (ثوانٍ) — لوحظ فعلياً: نفس صفحة أُعيد اختبارها فشل اكتشاف جدولها (أفضل قفزة
# 1.725 فقط) بعد أن نجحت سابقاً بنفس اليوم. 1.6 (بدل 1.9) هامش أمان إضافي ضد هذا
# التذبذب الخارجي الذي لا نملك تحكماً به، وليس نتيجة قياس حاسم لحد فاصل "صحيح"
# جديد — القيمة الفعلية للحد الفاصل بين نثر/عمود تبقى بحدود مئات البكسل عادةً
# (هامش كبير)، فخفض العتبة النسبية بمقدار كهذا لا يزيد خطر إيجابيات كاذبة عملياً.
_SCANNED_TABLE_MIN_JUMP_RATIO = 1.6
# فجوات أصغر من هذا (بالبكسل) تُستبعَد من فحص القفزة النسبية — عند هذا المقياس
# الضئيل تهيمن ضجة تقريب bbox (1px مقابل 2px نسبتها 2x رغم كونه فرقاً تافهاً)، لا
# تباعد حقيقي مقصود.
_SCANNED_TABLE_MIN_GAP_FOR_RATIO_CHECK = 5.0
# انقطاع تسلسل الجدول رأسياً: فجوة بين سطرين متتاليين أكبر من هذا المعامل × التباعد
# الرأسي المعتاد بين أسطر الصفحة (median) تعني على الأرجح جدولاً مختلفاً/قسماً جديداً
# بمسافة بيضاء واضحة، وليس صفاً تالياً لنفس الجدول — انظر `_detect_scanned_table_regions`.
_SCANNED_TABLE_MAX_VERTICAL_GAP_RATIO = 2.5

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
    """يجمّع كلمات الصفحة (من `extract_words`) في "أسطر" فعلية حسب قرب مركزها
    الرأسي من متوسط مراكز السطر (وليس تراكب bbox تراكمي)، بمعزل عن ترتيب الاستخراج.
    كل سطر مُرتَّب أفقياً (x0) لأن حساب فجوات الأعمدة لاحقاً يحتاج كلمات متجاورة
    على نفس السطر بترتيب القراءة.

    **درس من خلل حقيقي:** النسخة الأولى قارنت تراكب bbox تراكمي (`min(word["bottom"],
    line_bottom) - max(...) > 0`، حيث `line_bottom` يتوسّع مع كل كلمة تُضاف). هذا
    يسبب "انزلاقاً تسلسلياً" (transitive drift): جدول DEXA حقيقي بصفوف متقاربة
    رأسياً جداً (فجوة ~50px، ارتفاع كلمة ~55px) — كلمات الصف الأول تدريجياً تُوسِّع
    حدود "السطر" حتى تلامس الصف التالي فعلياً، فيلتحم صفّان مختلفان تماماً
    (Neck وTotal في جدول "Analysis of Femur") في سطر واحد مشوَّش الترتيب. المقارنة
    بمتوسط المراكز (وليس الحدود القصوى المتراكمة) لا تنجرف بنفس الطريقة لأنها
    مُقيَّدة بالبيانات الفعلية، وليست دالة أحادية الاتجاه للتوسّع."""
    lines: List[dict] = []  # كل عنصر: {"centers": [...], "words": [...]}
    for word in sorted(words, key=lambda w: w["top"]):
        center = (word["top"] + word["bottom"]) / 2
        height = word["bottom"] - word["top"]
        placed = False
        for line in lines:
            reference = sum(line["centers"]) / len(line["centers"])
            if abs(center - reference) <= height * 0.6:
                line["centers"].append(center)
                line["words"].append(word)
                placed = True
                break
        if not placed:
            lines.append({"centers": [center], "words": [word]})
    result = [line["words"] for line in lines]
    for line in result:
        line.sort(key=lambda w: w["x0"])
    return result


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
    """يستخرج الصور المُضمَّنة في صفحة رقمية واحدة (شعارات/رسوم بيانية) لمكتبة الوسائط
    ولإدراج Placeholder تلقائي في مكانها الأصلي (ميزة استخراج الأصول).

    `seen_xrefs` يُشارَك عبر المستند كله لتفادي تكرار نفس الصورة (شعار ثابت في كل
    صفحة مثلاً) مرات عديدة في الحمولة المُرسَلة للواجهة. تُحوَّل كل صورة إلى PNG (بلا
    فقدان جودة، بصرف النظر عن الترميز الأصلي JPEG/etc) عبر Pillow — طلب المستخدم صراحةً
    ملف PNG مستقل بدقة عالية، والدقة الأصلية للصورة المُضمَّنة في PDF هي أعلى دقة متاحة
    أصلاً (لا رفع دقة اصطناعي ممكن أو مفيد هنا). `image_id` يُترَك فارغاً هنا ويُملأ لاحقاً
    في `extract_document` (يحتاج عدّاداً عبر المستند كله وليس عبر الصفحة فقط)."""
    images: List[ImageAsset] = []
    for img_index, img in enumerate(fitz_page.get_images(full=True)):
        xref = img[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            extracted = fitz_doc.extract_image(xref)
            png_buffer = BytesIO()
            Image.open(BytesIO(extracted["image"])).convert("RGB").save(png_buffer, format="PNG")
            png_bytes = png_buffer.getvalue()
        except Exception as exc:
            logger.warning("تعذّر استخراج الصورة xref=%d من الصفحة %d: %s", xref, page_number, exc)
            continue

        rects = fitz_page.get_image_rects(xref)
        bbox = None
        if rects:
            r = rects[0]
            bbox = BoundingBox(x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1)

        images.append(
            ImageAsset(
                page_number=page_number,
                index=img_index,
                mime_type="image/png",
                data_base64=base64.b64encode(png_bytes).decode("ascii"),
                width=extracted.get("width", 0),
                height=extracted.get("height", 0),
                bbox=bbox,
            )
        )
    return images


def _sort_blocks_by_position(blocks: List[Block]) -> List[Block]:
    """يرتّب Blocks حسب الموضع الرأسي الحقيقي (bbox.y0) بدل ترتيب الاستخراج — ضروري
    كلما جُمعت Blocks من مصادر مختلفة (فقرات + جداول + placeholders) لا تصل بترتيب
    قراءة صحيح من تلقاء نفسها. Blocks بلا bbox (نادرة، مثال بلوك خطأ Vision API) تبقى
    بترتيبها النسبي الأصلي عبر مفتاح ترتيب مستقر (fallback إلى ما لا نهاية + الفهرس
    الأصلي)."""
    ordered = sorted(
        enumerate(blocks),
        key=lambda pair: (pair[1].bbox.y0 if pair[1].bbox else float("inf"), pair[0]),
    )
    return [block for _, block in ordered]


def _insert_image_placeholders(blocks: List[Block], page_images: List[ImageAsset]) -> List[Block]:
    """يدمج Blocks الصفحة (فقرات/جداول) مع Block نصي Placeholder واحد لكل صورة مكتشفة
    في نفس الصفحة، ثم يعيد ترتيبها بالموضع الحقيقي (كانت الجداول سابقاً تُلحَق دوماً
    بعد كل الفقرات بصرف النظر عن موضعها الفعلي في الصفحة — هذا الترتيب ضروري هنا كي
    يظهر الـplaceholder في موقعه الصحيح بين الفقرات، ويُصحّح كأثر جانبي مفيد ترتيب
    الجداول أيضاً)."""
    placeholders = [
        Block(
            block_type=BlockType.PARAGRAPH,
            text=f"[Insert {image.image_id} here]",
            bbox=image.bbox,
            confidence=1.0,
            source_engine=SourceEngine.PYMUPDF,
        )
        for image in page_images
        if image.bbox is not None
    ]
    return _sort_blocks_by_position(blocks + placeholders)


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


def _vision_word_boxes(vision_page: dict) -> List[dict]:
    """يستخرج كل word مع bbox (بالبكسل، بنفس نظام إحداثيات raster الصفحة المُرسَلة
    لـVision) من fullTextAnnotation — مستوى تفصيل أعمق من `_blocks_from_vision_page`
    (فقرات فقط). ضروري لاكتشاف بنية جدول هندسياً (صفوف/أعمدة) بنفس فكرة
    `_dynamic_text_table_settings` للصفحات الرقمية (تجميع كلمات حسب الموضع)، لكن هنا
    من كلمات OCR بدل pdfplumber — Vision لا يعطي كياناً اسمه "جدول" أصلاً (انظر توثيق
    نطاق أعلى الملف)، فقط نص + bbox في كل مستوى (page/block/paragraph/word/symbol).

    `slope` (جديد): Vision يعطي رباعياً (quadrilateral) فعلياً دوّاراً لكل كلمة، وليس
    مربعاً محاذياً للمحاور — نحسب ميل حافته العلوية (أعلى-يسار → أعلى-يمين) ونحتفظ
    به بدل تجاهله عبر min/max وحدها، لأنه يكشف دوران/ميل الصفحة الفعلي (صفحة
    مصوَّرة بالهاتف وليست ممسوحة مسطَّحة) — ضروري لـ`_estimate_page_skew_slope`."""
    words: List[dict] = []
    for block in vision_page.get("blocks", []):
        for paragraph in block.get("paragraphs", []):
            for word in paragraph.get("words", []):
                text = "".join(s.get("text", "") for s in word.get("symbols", []))
                if not text.strip():
                    continue
                vertices = word.get("boundingBox", {}).get("vertices", [])
                if len(vertices) < 2:
                    continue
                xs = [v.get("x", 0) for v in vertices]
                ys = [v.get("y", 0) for v in vertices]
                top_left, top_right = vertices[0], vertices[1]
                dx = top_right.get("x", 0) - top_left.get("x", 0)
                slope = (top_right.get("y", 0) - top_left.get("y", 0)) / dx if dx else 0.0
                words.append(
                    {"text": text, "x0": min(xs), "x1": max(xs), "top": min(ys), "bottom": max(ys), "slope": slope}
                )
    return words


def _estimate_page_skew_slope(words: List[dict]) -> float:
    """يقدّر ميل انحراف/دوران الصفحة الفعلي (radians تقريباً، tan الزاوية) عبر
    الوسيط (median، مقاوم للقيم الشاذة) لميل الحافة العلوية لكل كلمة عرضها ≥40px
    (كلمات أقصر جداً ضجة قياس بحتة لا تعكس دوران الصفحة).

    **درس من اختبار حقيقي:** صفحة مصوَّرة (لا ممسوحة مسطَّحة) بزاوية ميل ~2.5°
    فعلية جعلت قيمة واحدة ضمن نفس الصف المطبوع (Neck) تنحرف رأسياً ~122px بين
    أقصى اليسار وأقصى اليمين — أكبر من التباعد الفعلي بين صفوف متجاورة (~53px)،
    فيكسر `_cluster_lines` (المُقارِن بالموضع الرأسي الخام) تماماً بلا تصحيح: يلتحم
    جزء من صف بجزء من الصف التالي. يُقيَّد الميل المُقدَّر بـ±0.2 (~±11°) لأن انحرافاً
    أكبر يعني على الأرجح تخطيطاً حقيقياً متعدد الأعمدة وليس ميل تصوير، فلا يُصحَّح حينها."""
    slopes = [w["slope"] for w in words if "slope" in w and w["x1"] - w["x0"] >= 40]
    if not slopes:
        return 0.0
    median_slope = statistics.median(slopes)
    return median_slope if abs(median_slope) <= 0.2 else 0.0


def _split_line_into_cells(line: List[dict], gap_threshold: float) -> List[str]:
    """يقسّم كلمات سطر واحد (مُرتَّبة x0) إلى خلايا عند أول فجوة أفقية أكبر من
    gap_threshold — نفس الفكرة التي يتّبعها قارئ بشري لجدول مطبوع بلا خطوط فاصلة:
    عمود جديد يبدأ حيث تتباعد الكلمات فجأة أكثر من التباعد العادي بين كلمتين بنفس
    الخلية."""
    cells = [line[0]["text"]]
    for prev, curr in zip(line, line[1:]):
        gap = curr["x0"] - prev["x1"]
        if gap > gap_threshold:
            cells.append(curr["text"])
        else:
            cells[-1] = f"{cells[-1]} {curr['text']}"
    return cells


def _find_gap_threshold(gaps: List[float]) -> float:
    """يحدّد عتبة الفصل بين "تباعد عادي بين كلمتين" و"فجوة عمود جدول حقيقية" عبر
    البحث عن *أول* قفزة نسبية (ratio) ≥ `_SCANNED_TABLE_MIN_JUMP_RATIO` بين قيمتين
    متتاليتين في قائمة الفجوات مُرتَّبة تصاعدياً (بدءاً من الأصغر) — وليس أكبر قفزة
    في القائمة كلها، ولا وسيطاً، ولا أضيق قيمة على مستوى الصفحة.

    **دروس من اختبارَين حقيقيَّين متتاليَين (وليسا افتراضيَّين):**
    1. "أضيق فجوة × معامل ثابت" فشل: Vision يفصل الترقيم الملتصق ("Patient" عن ":")
       بفجوة شبه صفرية، فتُصبح العتبة صغيرة جداً وتُصنَّف كل نثر عادي كجدول.
    2. "أكبر قفزة نسبية في كامل القائمة" فشل بدوره على مستند حقيقي أعقد (تقرير طبي
       فيه عدة "جداول"/أزواج حقول متفاوتة الاتساع: حقول ترويسة ~1000px+، جدول
       بيانات رئيسي ~400-900px، جدول مرجعي ثانٍ ~1700px+) — القفزة الأكبر فعلياً قد
       تقع بين مجموعتين من الفجوات الكبيرة نفسها (لا علاقة لها بالحد الحقيقي بين
       "عادي" و"كبير")، فلا يُقسَّم أي سطر إطلاقاً. الحل: أول قفزة تتجاوز العتبة
       بدءاً من الأسفل تكفي — لا يهم أن تتفاوت الفجوات "الكبيرة" فيما بينها (400 أو
       1700، كلاهما "كبير" بما يكفي)، المهم فقط تمييزها عن "الصغيرة" أسفل القائمة.

    فجوات أصغر من `_SCANNED_TABLE_MIN_GAP_FOR_RATIO_CHECK` تُستبعَد من الفحص (ضجة
    تقريب bbox، ليست تباعداً مقصوداً — بدونه، قفزة تافهة مثل 1px→2px "تضاعفت" رياضياً
    فتُطلق عتبة صغيرة جداً خطأً). إن لم تتحقق أي قفزة كافية تُعاد `inf` فلا ينقسم
    أي سطر (على الأرجح لا يوجد جدول أصلاً)."""
    positive = sorted(g for g in gaps if g >= _SCANNED_TABLE_MIN_GAP_FOR_RATIO_CHECK)
    if len(positive) < 2:
        return float("inf")

    for i in range(len(positive) - 1):
        ratio = positive[i + 1] / max(positive[i], 1.0)
        if ratio >= _SCANNED_TABLE_MIN_JUMP_RATIO:
            return (positive[i] + positive[i + 1]) / 2

    return float("inf")


def _merge_split_header_row(rows: List[List[str]]) -> List[List[str]]:
    """يدمج أول سطرين في منطقة جدول إن كان الثاني استمراراً لترويسة السطر الأول
    بعدد خلايا أقل — مثال حقيقي (تقرير DEXA): "Site Region BMD Young Adult Age
    Matched" يتبعه مباشرة "(gm/cm2) T-score Z-score" على سطر مطبوع منفصل، وهما
    فعلياً ترويسة واحدة مقسّمة على سطرين، وليس صفّي بيانات مستقلَّين.

    الدمج مُحاذًى لليمين (آخر عمود في السطر الثاني ↔ آخر عمود في الأول) لأن أعمدة
    المعرِّفات الأولى (Site/Region) عادة لا تحمل سطر وحدات/تسمية فرعية، بخلاف أعمدة
    القيم الأخيرة (BMD/T-Score/Z-Score) — نمط شائع في تقارير DEXA/المختبر. **بدون
    هذا الدمج**، `MedicalTableStructurer` يُضطَر لملء "UNCERTAIN" في خلايا السطر
    الثاني الأولى (لا بيانات حقيقية مقابلة لها)، فيظهر صف ترويسة إضافي مربك في
    الجدول النهائي رغم أن كل قيمة رقمية فعلية محفوظة سليمة — لوحظ هذا حرفياً في
    اختبار ضد ملف مريض حقيقي (تقرير DEXA)، والمستخدم وصفه بأن "بعض الأرقام تظهر
    وبعضها لا".

    يُطبَّق فقط على أول سطرين في المنطقة عمداً (وليس أي زوج سطرين متتاليين) — دمج
    صفّي بيانات حقيقيَّين لمجرد نقص خلية عرضي في أحدهما كان سيُفسِد الجدول بدل
    إصلاحه."""
    if len(rows) < 2:
        return rows
    first, second = rows[0], rows[1]
    if not second or not (0 < len(second) < len(first)):
        return rows
    pad = len(first) - len(second)
    merged_first = list(first)
    for i, value in enumerate(second):
        idx = pad + i
        merged_first[idx] = f"{merged_first[idx]} {value}".strip() if merged_first[idx] else value
    return [merged_first] + rows[2:]


_NUMERIC_CELL_STRIP_CHARS = "()%.,-/: \t"


def _is_clean_numeric_cell(cell: str) -> bool:
    """خلية "رقمية صرفة": بعد إزالة الترقيم الشائع حول الأرقام الطبية (%()."-/:
    والمسافات)، الباقي أرقام فقط بلا أي حرف. "0.870" أو "-1.5(82%)" رقمية صرفة؛
    "158.0 cm" أو "98.5 kg" **ليست** كذلك (وحدة القياس حرف ملتصق بالرقم نفسه)."""
    stripped = "".join(ch for ch in cell if ch not in _NUMERIC_CELL_STRIP_CHARS)
    return bool(stripped) and stripped.isdigit()


def _looks_like_non_clinical_metadata(rows: List[List[str]]) -> bool:
    """يتحقق إن كانت منطقة معلومات مريض/ترويسة مبعثرة (Name/Birthdate/Weight/
    Height/Gender/Ethnicity...) بدل جدول بيانات سريري حقيقي (BMD/T-Score/Z-Score) —
    عبر كثافة الخلايا "الرقمية الصرفة" (`_is_clean_numeric_cell`) في المنطقة كلها،
    وليس عدد الصفوف.

    **درس من اختبارات حقيقية متتالية على نفس ملف المريض:** أول محاولة استخدمت حداً
    أدنى لعدد الصفوف (`_SCANNED_TABLE_MIN_ROWS=3`) لاستبعاد أزواج NAME/FILE-NO —
    استبعدت أيضاً جدولاً سريرياً حقيقياً قصيراً (Overview: Neck/Total). المحاولة
    الثانية استهدفت حرفياً ":" داخل الخلايا (صفّين فقط) — نجحت مع NAME/FILE-NO لكن
    فشلت مع شبكة معلومات مريض أعقد (Birthdate/Weight/Gender/Ethnicity، 3×3 خلية)
    لأن OCR فقد حرف ":" نفسه أثناء التقسيم الهندسي، فلم تبقَ أي خلية تحويه حرفياً.

    **الفارق الفعلي الأعمق:** صف جدول بيانات حقيقي (Neck/L1/Spine...) خليته الأولى
    تسمية قصيرة والباقي قيم رقمية صرفة (≈75-85% من كل الخلايا رقمية) — بينما صفوف
    معلومات المريض تخلط تسميات نصية بقيم ذات وحدات ملتصقة حرفياً ("158.0 cm"،
    "98.5 kg") فتنخفض نسبة الخلايا الرقمية الصرفة كثيراً (~29% في الاختبار الفعلي).
    هذا المعيار عام (يعمل بصرف النظر عن عدد الصفوف) ويلتقط كلا النمطين معاً.

    **خلل حقيقي اكتُشف واقعياً بعد التطبيق الأول:** جدول BMD الحقيقي (3 صفوف بعد
    دمج الترويسة: صف ترويسة + Spine + Femur فقط) كان يُستبعَد خطأً — صف الترويسة
    المدموج نفسه ("Site"، "Region"، "BMD ( gm / cm2 )"...) نصّي بالكامل بطبيعته
    (كل ترويسة جدول كذلك)، فيُخفِّض متوسط الكثافة الرقمية للجدول القصير كله دون
    نصف (5 خلايا ترويسة نصية أمام 6 خلايا بيانات رقمية فقط = 40%). **الحل:** تُستبعَد
    أول صف (الترويسة المُفترَضة) من حساب الكثافة دوماً — لا يُعاقَب جدول قصير
    لمجرد امتلاكه ترويسة نصية طبيعية كما يجب."""
    data_rows = rows[1:] if len(rows) > 1 else rows
    all_cells = [cell for row in data_rows for cell in row if cell]
    if not all_cells:
        return False
    numeric_fraction = sum(1 for cell in all_cells if _is_clean_numeric_cell(cell)) / len(all_cells)
    return numeric_fraction < _MIN_NUMERIC_CELL_FRACTION


def _has_inconsistent_row_lengths(rows: List[List[str]]) -> bool:
    """يتحقق إن كانت أطوال صفوف المنطقة (عدد الخلايا) غير متّسقة نسبياً — إشارة على
    نص متناثر حول رسم بياني (تسميات محاور رقمية، مفاتيح دلالية مثل
    Normal/Osteopenia/Osteoporosis) بدل جدول بيانات حقيقي.

    **لماذا هذا معيار منفصل عن كثافة الخلايا الرقمية:** بعض تسميات الرسم البياني
    (محور الأعمار "20 30 40...100") رقمية بحتة أصلاً، فلا تُستبعَد بفحص الكثافة
    الرقمية وحده — لكن عدد خلايا أسطرها يتذبذب بشدة (2، 3، 5، 2، 10 في اختبار حقيقي
    فعلي، تباين نسبي ≈0.6-0.7)، بخلاف جدول بيانات حقيقي (Lumbar/Femur) حيث عدد
    الخلايا شبه ثابت لكل صف (تباين نسبي ≈0.1-0.14 في نفس الاختبار)."""
    lengths = [len(row) for row in rows]
    if len(lengths) < 2:
        return False
    mean_length = sum(lengths) / len(lengths)
    if mean_length == 0:
        return False
    variance = sum((length - mean_length) ** 2 for length in lengths) / len(lengths)
    coefficient_of_variation = (variance**0.5) / mean_length
    return coefficient_of_variation > _MAX_ROW_LENGTH_VARIATION


def _append_table_region(
    regions: List[dict], lines: List[List[dict]], line_cells: List[List[str]], start: int, end: int
) -> None:
    """يُضيف منطقة جدول من `lines[start:end]` إن كانت ≥`_SCANNED_TABLE_MIN_ROWS` سطراً
    وليست معلومات مريض/ترويسة مبعثرة (`_looks_like_non_clinical_metadata`) ولا نصاً
    متناثراً حول رسم بياني (`_has_inconsistent_row_lengths`) — مُستخرَجة كدالة
    مستقلة لأن `_detect_scanned_table_regions` يحتاج استدعاءها من أكثر من مكان في
    حلقة بناء التسلسلات (كسر التسلسل بسبب سطر غير جدولي، أو بسبب فجوة رأسية كبيرة،
    ونهاية الصفحة)."""
    if end - start < _SCANNED_TABLE_MIN_ROWS:
        return
    region_lines = lines[start:end]
    rows = _merge_split_header_row(line_cells[start:end])
    if _looks_like_non_clinical_metadata(rows) or _has_inconsistent_row_lengths(rows):
        return
    regions.append(
        {
            "rows": rows,
            "bbox": BoundingBox(
                x0=min(w["x0"] for ln in region_lines for w in ln),
                y0=min(w["top"] for ln in region_lines for w in ln),
                x1=max(w["x1"] for ln in region_lines for w in ln),
                y1=max(w["bottom"] for ln in region_lines for w in ln),
            ),
        }
    )


def _detect_scanned_table_regions(words: List[dict]) -> List[dict]:
    """يكتشف مناطق شبيهة بجداول ضمن كلمات صفحة ممسوحة (OCR) عبر التجميع الهندسي
    البحت فقط — Vision (DOCUMENT_TEXT_DETECTION) لا يعطي أي كيان "جدول" جاهز، فقط
    نص + bbox (انظر توثيق نطاق أعلى الملف وتوثيق `_vision_word_boxes`).

    الخطوات: تجميع الكلمات في أسطر حسب تداخل الموضع الرأسي (`_cluster_lines`، نفس
    الدالة المستخدَمة للصفحات الرقمية)، ثم تقسيم كل سطر لخلايا عند فجوات أفقية أكبر
    من عتبة `_find_gap_threshold` (انظر توثيقها للسبب).

    أي تسلسل من ≥`_SCANNED_TABLE_MIN_ROWS` سطر متتالٍ له ≥`_SCANNED_TABLE_MIN_COLS`
    خلية في كل سطر يُعتبر "منطقة جدول" مرشَّحة — **بشرط استمرار رأسي** أيضاً: إن
    كانت الفجوة الرأسية بين سطرين متتاليين أكبر من `_SCANNED_TABLE_MAX_VERTICAL_GAP_RATIO`
    ضِعف التباعد المعتاد بين أسطر الصفحة (median)، يُقطَع التسلسل ويبدأ آخر جديد —
    **درس من اختبار حقيقي:** تقرير طبي فيه جدول بيانات رئيسي (BMD/T-Score/Z-Score)
    يتبعه مباشرة (بلا سطر نثر فاصل بينهما) جدول مرجعي مختلف تماماً (معايير WHO
    التشخيصية) بمسافة بيضاء واضحة بينهما فقط — بدون هذا الشرط يلتحم الجدولان في
    "منطقة" واحدة ضخمة غير صحيحة.

    **تصحيح الميل قبل التجميع (`_estimate_page_skew_slope`):** صفحة مصوَّرة (لا
    ممسوحة مسطَّحة) قد تكون مائلة بزاوية ثابتة عبر الصفحة كلها — بدون تصحيح، قيم
    متباعدة أفقياً ضمن نفس الصف المطبوع تنحرف رأسياً أكثر من التباعد الفعلي بين
    صفوف مختلفة، فيلتحم جزء من صف بجزء من التالي عند التجميع. التصحيح هنا **محلي
    لقرار التجميع فقط** (نسخ مؤقتة بـtop/bottom مُصحَّحين تُستخدَم لتحديد أي الكلمات
    تنتمي لنفس السطر)، ثم تُستبدَل فوراً بالكلمات الأصلية غير المُعدَّلة لكل ما
    يلي (حساب bbox المنطقة، فجوات الأعمدة) كي يبقى الموضع المُبلَّغ مطابقاً للصفحة
    الفعلية."""
    if not words:
        return []

    skew_slope = _estimate_page_skew_slope(words)
    if skew_slope:
        clustering_input = [
            {**w, "top": w["top"] - skew_slope * w["x0"], "bottom": w["bottom"] - skew_slope * w["x0"], "_original": w}
            for w in words
        ]
        lines = [[w["_original"] for w in line] for line in _cluster_lines(clustering_input)]
    else:
        lines = _cluster_lines(words)

    lines = [sorted(line, key=lambda w: w["x0"]) for line in lines]
    lines.sort(key=lambda line: min(w["top"] for w in line))

    horizontal_gaps = [curr["x0"] - prev["x1"] for line in lines for prev, curr in zip(line, line[1:])]
    gap_threshold = _find_gap_threshold(horizontal_gaps)
    line_cells = [_split_line_into_cells(line, gap_threshold) for line in lines]

    line_tops = [min(w["top"] for w in line) for line in lines]
    line_bottoms = [max(w["bottom"] for w in line) for line in lines]
    vertical_gaps = [
        line_tops[i] - line_bottoms[i - 1] for i in range(1, len(lines)) if line_tops[i] > line_bottoms[i - 1]
    ]
    median_vertical_gap = statistics.median(vertical_gaps) if vertical_gaps else 0.0
    max_allowed_vertical_gap = (
        median_vertical_gap * _SCANNED_TABLE_MAX_VERTICAL_GAP_RATIO if median_vertical_gap > 0 else float("inf")
    )

    regions: List[dict] = []
    run_start = None
    for index, cells in enumerate(line_cells):
        is_tabular = len(cells) >= _SCANNED_TABLE_MIN_COLS
        continues_run = run_start is not None and (line_tops[index] - line_bottoms[index - 1]) <= max_allowed_vertical_gap
        if is_tabular and run_start is not None and continues_run:
            continue
        if run_start is not None:
            _append_table_region(regions, lines, line_cells, run_start, index)
            run_start = None
        if is_tabular:
            run_start = index
    if run_start is not None:
        _append_table_region(regions, lines, line_cells, run_start, len(line_cells))

    return regions


@lru_cache(maxsize=1)
def _get_table_structurer() -> MedicalTableStructurer:
    return MedicalTableStructurer()


def _structure_scanned_table_rows(raw_rows: List[List[str]]) -> Optional[List[List[str]]]:
    """يستدعي MedicalTableStructurer (موديول DSPy موجود أصلاً لهذا الغرض بالضبط، القسم
    3-ب-4 — كان بلا أي مستدعٍ لمُدخَل جدول ممسوح حتى الآن) لتصحيح/تسمية أعمدة جدول
    ممسوح خام (Test/Result/Unit/Range، Region/BMD/T-Score/Z-Score، إلخ) قبل بناء جدول
    Word حقيقي منه.

    **قرار مستخدم صريح:** يعمل تلقائياً لكل جدول مُكتشَف أثناء `extract_document` (وليس
    إجراءً يدوياً لكل جدول) — على عكس بقية `extract_document` المجانية عمداً؛ كل جدول
    مُكتشَف = استدعاء LM واحد فعلي. يتدهور بأمان (يُعيد None) إن كان LM غير مُهيَّأ أصلاً
    (`dspy.settings.lm is None`، شائع في تطوير محلي بلا GEMINI_API_KEY) أو فشل الاستدعاء
    لأي سبب (شبكة/حصة/بوابة الترسيخ لم تتحقق) — فشل تصحيح جدول واحد لا يجب أن يوقف
    استخراج بقية المستند، فيبقى الجدول بشبكته الخام غير المصحَّحة بدل رفع استثناء.

    header hints: السطر الأول من raw_rows نفسه (وليس قائمة أسماء أعمدة طبية جاهزة
    مُفترَضة سلفاً) — يطابق نمط الاستخدام الموثَّق فعلياً في `tests/test_signatures.py`
    (raw_rows تتضمّن سطر الترويسة كصف عادي، والنموذج قد يصحّح نصه أيضاً)، ويبقي الكود
    عاماً بدل افتراض مفردات ثابتة (Test/Result مقابل Region/BMD) لا تنطبق على كل تقرير."""
    if dspy.settings.lm is None:
        return None

    try:
        header_hints = raw_rows[0] if raw_rows else []
        prediction = _get_table_structurer()(raw_rows=raw_rows, column_hints=header_hints)
        structured = json.loads(prediction.structured_rows)
    except Exception as exc:
        logger.warning("فشل تصحيح جدول ممسوح عبر LM، استُخدمت الشبكة الخام بدلاً منه: %s", exc)
        return None

    if not isinstance(structured, list) or len(structured) != len(raw_rows):
        logger.warning("استجابة LM لتصحيح الجدول غير متسقة (عدد صفوف مختلف)، استُخدمت الشبكة الخام")
        return None
    if not all(isinstance(row, dict) for row in structured):
        logger.warning("استجابة LM لتصحيح الجدول بشكل غير متوقَّع (ليست list[dict])، استُخدمت الشبكة الخام")
        return None

    columns: List[str] = []
    for row in structured:
        for key in row.keys():
            if key not in columns:
                columns.append(key)

    return [[str(row.get(col, "")) for col in columns] for row in structured]


def _drop_oversized_line_components(mask: "np.ndarray", max_span_ratio: float, axis: str) -> "np.ndarray":
    """يستبعد قطاعات خط (مكوّنات متصلة) أطول/أعرض من `max_span_ratio` من ارتفاع/عرض
    الصفحة كلها **و** قريبة من حافة الصورة المطلقة — ظل أو حافة صفحة مصوَّرة (وليست
    ممسوحة مسطَّحة) تُنتج خطوطاً طويلة جداً ملتصقة بحافة الصورة، بخلاف حدود جدول حقيقي
    طويل (قد يشغل معظم ارتفاع الصفحة أيضاً، لوحظ في استمارة حقيقية بخلية شكوى مريض
    ضخمة) لكنه لا يلامس الحافة المطلقة عملياً — يبقى هامش أبيض حوله دوماً.

    **درس من اختبار حقيقي:** الشرطان معاً ضروريان؛ الطول وحده كان يستبعد خطوطاً
    رأسية حقيقية لجدول طويل (تجربة اصطناعية بجدول يشغل 64% من ارتفاع صورة اختبار)،
    والقرب من الحافة وحده كان سيُبقي ظلال تصوير قصيرة نسبياً. `axis="v"` يقيس
    الارتفاع/الموضع الأفقي (لخطوط رأسية)، `axis="h"` يقيس العرض/الموضع الرأسي
    (لخطوط أفقية)."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    mask_height, mask_width = mask.shape
    span_dim, span_stat = (mask_height, cv2.CC_STAT_HEIGHT) if axis == "v" else (mask_width, cv2.CC_STAT_WIDTH)
    edge_dim, pos_stat, size_stat = (
        (mask_width, cv2.CC_STAT_LEFT, cv2.CC_STAT_WIDTH)
        if axis == "v"
        else (mask_height, cv2.CC_STAT_TOP, cv2.CC_STAT_HEIGHT)
    )
    span_limit = span_dim * max_span_ratio
    edge_margin = edge_dim * _RULED_TABLE_EDGE_ARTIFACT_MARGIN_RATIO

    cleaned = np.zeros_like(mask)
    for i in range(1, count):
        is_too_long = stats[i, span_stat] > span_limit
        pos_start = stats[i, pos_stat]
        pos_end = pos_start + stats[i, size_stat]
        is_near_edge = pos_start <= edge_margin or pos_end >= edge_dim - edge_margin
        if not (is_too_long and is_near_edge):
            cleaned[labels == i] = 255
    return cleaned


def _ruled_line_masks(image_bytes: bytes) -> Optional[tuple]:
    """يستخرج قناعَي الخطوط الأفقية/الرأسية المرسومة فعلياً في صورة صفحة ممسوحة عبر
    عمليات مورفولوجية (erode بعنصر بنيوي واسع أفقياً/رأسياً يعزل الخطوط الطويلة فقط،
    ثم dilate لاستعادة سمكها) — يكتشف استمارات/جداول معلومات حدودها مرسومة فعلياً،
    خلافاً لجداول القياس بلا حدود (`_detect_scanned_table_regions` الهندسي من النص).

    هامش حواف الصورة يُصفَّر أولاً، وقطاعات الخط الطويلة جداً (ظل/انحناء تصوير عند
    حواف الصفحة، وليست جزءاً من أي جدول — لوحظ فعلياً: خطان رأسيان بطول الصفحة كاملاً
    من حافتَي صورة مصوَّرة بالهاتف) تُستبعَد عبر `_drop_oversized_line_components`.
    يُعيد None إن تعذّر فك ترميز الصورة (تدهور آمن، لا يوقف الاستخراج)."""
    try:
        gray = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None
    if gray is None:
        return None

    height, width = gray.shape
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)

    margin = int(min(height, width) * _RULED_TABLE_EDGE_MARGIN_RATIO)
    if margin > 0:
        binary[:margin, :] = 0
        binary[-margin:, :] = 0
        binary[:, :margin] = 0
        binary[:, -margin:] = 0

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 30, 1), 1))
    h_lines = cv2.dilate(cv2.erode(binary, h_kernel, iterations=1), h_kernel, iterations=2)
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 30, 1)))
    v_lines = cv2.dilate(cv2.erode(binary, v_kernel, iterations=1), v_kernel, iterations=2)

    h_lines = _drop_oversized_line_components(h_lines, _RULED_TABLE_MAX_HORIZONTAL_SPAN_RATIO, axis="h")
    v_lines = _drop_oversized_line_components(v_lines, _RULED_TABLE_MAX_VERTICAL_SPAN_RATIO, axis="v")
    return h_lines, v_lines


def _drop_overlapping_boxes(candidates: List[tuple]) -> List[tuple]:
    """عند تداخل صندوقين مرشَّحين، يُبقي الأكثر كثافة خطوط شبكة حقيقية (بكسل خط لكل
    وحدة مساحة) ويُسقط الآخر بالكامل.

    **درس من خلل حقيقي:** جُرِّب أولاً *دمج* الصندوقين المتداخلين في اتحادهما (شعار/
    ختم مُزخرَف دائري التصق بحافة جدول حقيقي مجاور، منحنياته سُجِّلت خطأً كقطاع خط) —
    لكن الاتحاد الأكبر خفَّض كثافة الخطوط الرأسية داخله دون عتبة الكشف (لأن مساحة
    الشعار الفارغة انضمّت لحساب الكثافة)، ففشل استخراج حدود الأعمدة كلياً رغم وجود
    جدول حقيقي واضح تماماً ضمن نفس المنطقة. إسقاط الصندوق الأضعف كثافة (الشعار) بدل
    دمجه يحلّ هذا تماماً لأن الجدول الحقيقي يبقى بحدوده الأصلية الدقيقة.

    `candidates`: قائمة (bbox, density) — density = مجموع بكسلات الخط / مساحة الصندوق."""
    ordered = sorted(candidates, key=lambda item: item[1], reverse=True)
    kept: List[tuple] = []
    for (x0, y0, x1, y1), _density in ordered:
        overlaps_kept = any(x0 < kx1 and kx0 < x1 and y0 < ky1 and ky0 < y1 for kx0, ky0, kx1, ky1 in kept)
        if not overlaps_kept:
            kept.append((x0, y0, x1, y1))
    return kept


def _line_group_centers(mask_roi: "np.ndarray", axis: int, extent: int, origin: int) -> List[int]:
    """يجد مراكز مجموعات البكسل المضاءة (خطوط شبكة) عبر محور واحد ضمن منطقة جدول —
    `axis=1` يجمع كل صف (خطوط أفقية، حدود صفوف)، `axis=0` يجمع كل عمود (خطوط رأسية،
    حدود أعمدة). خطوط متجاورة (سماكة الخط نفسه عدة بكسل) تُجمَّع في مجموعة واحدة.

    `origin`: يُضاف لكل موضع مُكتشَف لتحويله من إحداثي نسبي داخل `mask_roi` (المقطوعة
    من الصورة الكاملة) إلى إحداثي مطلق على الصفحة — **خلل حقيقي وُجد ويُصحَّح هنا:**
    نسخة أولى أعادت المواضع النسبية مباشرة دون هذا التعويض، فاختلطت بحدود مطلقة
    (`y0`/`y1` أو `x0`/`x1`) في نفس قائمة الحدود، منتجة شبكة صفوف/أعمدة عشوائية
    تماماً لا تطابق الجدول الحقيقي إطلاقاً."""
    sums = mask_roi.sum(axis=axis)
    threshold = 255 * extent * _RULED_TABLE_LINE_COVERAGE_RATIO
    positions = [i for i, s in enumerate(sums) if s > threshold]
    groups: List[List[int]] = []
    for pos in positions:
        if groups and pos - groups[-1][-1] <= 5:
            groups[-1].append(pos)
        else:
            groups.append([pos])
    return [origin + int(sum(g) / len(g)) for g in groups]


def _assign_words_to_grid(words: List[dict], row_bounds: List[int], col_bounds: List[int]) -> List[List[str]]:
    """يعيّن كل كلمة (مركزها) لخليتها في شبكة الجدول (بين حدّي صف وحدّي عمود متتاليين)
    مباشرة من الهندسة المرسومة الفعلية — أدق من التقسيم عند فجوات النص (يستخدمه
    الاكتشاف الهندسي بلا حدود) لأنه يعتمد على خطوط حقيقية مرسومة، لا تقديراً."""
    n_rows, n_cols = len(row_bounds) - 1, len(col_bounds) - 1
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for word in words:
        center_x = (word["x0"] + word["x1"]) / 2
        center_y = (word["top"] + word["bottom"]) / 2
        row_idx = next((i for i in range(n_rows) if row_bounds[i] <= center_y < row_bounds[i + 1]), None)
        col_idx = next((i for i in range(n_cols) if col_bounds[i] <= center_x < col_bounds[i + 1]), None)
        if row_idx is None or col_idx is None:
            continue
        cell = grid[row_idx][col_idx]
        grid[row_idx][col_idx] = f"{cell} {word['text']}".strip() if cell else word["text"]
    return grid


def _detect_ruled_table_regions(image_bytes: bytes, words: List[dict]) -> List[dict]:
    """يكتشف جداول بخطوط شبكة مرسومة فعلياً (استمارات/جداول معلومات مريض حدودها
    مرسومة على الصفحة الممسوحة نفسها — مثال: حقول Patient ID/Name/DOB في استمارة
    أشعة/تحويل) عبر معالجة صورة (OpenCV)، خلافاً لـ`_detect_scanned_table_regions`
    الذي يكتشف جداول القياس بلا حدود هندسياً من تباعد النص فقط.

    **قرار مستخدم صريح:** أي جدول بخطوط مرسومة فعلياً يُعتبر جدولاً دوماً بصرف النظر
    عن محتواه — لا يخضع لفلاتر `_looks_like_non_clinical_metadata`/
    `_has_inconsistent_row_lengths` (تلك مخصَّصة للاكتشاف الهندسي من النص بلا حدود،
    حيث لا يوجد دليل بصري مباشر على وجود جدول أصلاً). خطوط الشبكة الحقيقية إشارة
    أقوى وأوثق من أي تحليل محتوى."""
    masks = _ruled_line_masks(image_bytes)
    if masks is None:
        return []
    h_lines, v_lines = masks

    grid_mask = cv2.bitwise_or(h_lines, v_lines)
    merge_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    merged_mask = cv2.dilate(grid_mask, merge_kernel, iterations=2)
    contours, _ = cv2.findContours(merged_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        h_count = cv2.countNonZero(h_lines[y : y + h, x : x + w])
        v_count = cv2.countNonZero(v_lines[y : y + h, x : x + w])
        if h_count >= _RULED_TABLE_MIN_LINE_PIXELS and v_count >= _RULED_TABLE_MIN_LINE_PIXELS:
            density = (h_count + v_count) / (w * h)
            candidates.append(((x, y, x + w, y + h), density))

    regions = []
    for x0, y0, x1, y1 in _drop_overlapping_boxes(candidates):
        row_bounds = [y0] + _line_group_centers(h_lines[y0:y1, x0:x1], axis=1, extent=x1 - x0, origin=y0) + [y1]
        col_bounds = [x0] + _line_group_centers(v_lines[y0:y1, x0:x1], axis=0, extent=y1 - y0, origin=x0) + [x1]
        if len(row_bounds) < 3 or len(col_bounds) < 3:
            continue  # أقل من صفّين/عمودين فعليّين بين الحدود — ليس شبكة جدول حقيقية

        region_words = [w for w in words if x0 <= (w["x0"] + w["x1"]) / 2 <= x1 and y0 <= (w["top"] + w["bottom"]) / 2 <= y1]
        rows = _assign_words_to_grid(region_words, row_bounds, col_bounds)
        if not any(cell for row in rows for cell in row):
            continue
        regions.append({"rows": rows, "bbox": BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)})

    return regions


def _scanned_table_blocks(image_bytes: bytes, words: List[dict]) -> List[Block]:
    """يبني Block(TABLE) لكل منطقة جدول مُكتشَفة، من مصدرين مستقلَّين:

    1. جداول بخطوط شبكة مرسومة فعلياً (`_detect_ruled_table_regions`) — تُعتبر
       جدولاً دوماً بصرف النظر عن محتواها (لا فلاتر محتوى)، ولا تُصحَّح عبر LM
       (بنيتها معروفة بدقة من الخطوط الحقيقية نفسها).
    2. جداول بلا حدود مُكتشَفة هندسياً من تباعد النص (`_detect_scanned_table_regions`)
       على الكلمات المتبقية فقط (خارج مناطق الجداول المرسومة، لتفادي ازدواج) — بعد
       محاولة تصحيحها عبر LM (`_structure_scanned_table_rows`).

    `raw_rows` تبقى دوماً الشبكة الخام قبل أي تصحيح (أثر تدقيق/audit trail)."""
    blocks: List[Block] = []

    ruled_regions = _detect_ruled_table_regions(image_bytes, words)
    ruled_bboxes = []
    for region in ruled_regions:
        bbox = region["bbox"]
        ruled_bboxes.append((bbox.x0, bbox.y0, bbox.x1, bbox.y1))
        blocks.append(
            Block(
                block_type=BlockType.TABLE,
                rows=region["rows"],
                raw_rows=region["rows"],
                bbox=bbox,
                confidence=0.7,
                source_engine=SourceEngine.GOOGLE_VISION,
            )
        )

    remaining_words = [
        w for w in words if not _bbox_center_in_any((w["x0"], w["top"], w["x1"], w["bottom"]), ruled_bboxes)
    ]
    for region in _detect_scanned_table_regions(remaining_words):
        raw_rows = region["rows"]
        structured_rows = _structure_scanned_table_rows(raw_rows)
        if structured_rows is not None:
            rows, confidence, source_engine = structured_rows, 0.85, SourceEngine.LLM_CORRECTED
        else:
            rows, confidence, source_engine = raw_rows, 0.5, SourceEngine.GOOGLE_VISION

        blocks.append(
            Block(
                block_type=BlockType.TABLE,
                rows=rows,
                raw_rows=raw_rows,
                bbox=region["bbox"],
                confidence=confidence,
                source_engine=source_engine,
            )
        )
    return blocks


def _blocks_from_vision_page(vision_page: dict, exclude_bboxes: Optional[List[tuple]] = None) -> List[Block]:
    """يحوّل صفحة واحدة من fullTextAnnotation.pages[i] إلى Blocks (فقرة لكل paragraph).

    تبسيط متعمد: يفصل بين الكلمات بمسافة واحدة دوماً بدل قراءة detectedBreak لكل رمز
    (type: SPACE/LINE_BREAK/EOL_SURE_SPACE) — كافٍ لعرض النص ولتصحيح DSPy اللاحق، وليس
    الهدف إعادة بناء تنسيق مطابق للصفحة حرفياً.

    `exclude_bboxes` (جديد): فقرات مركزها داخل منطقة جدول مُكتشَفة (`_scanned_table_blocks`)
    تُستبعَد كي لا يتكرر نصها هنا وكـBlock(TABLE) منفصل معاً — بنفس مبدأ استبعاد نص
    خلايا الجدول عن الفقرات الرقمية في `_digital_page_blocks`."""
    exclude_bboxes = exclude_bboxes or []
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

            if bbox is not None and _bbox_center_in_any((bbox.x0, bbox.y0, bbox.x1, bbox.y1), exclude_bboxes):
                continue

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
    Vision API (DOCUMENT_TEXT_DETECTION).

    اكتشاف الجداول (جديد): يُستخرَج bbox كل كلمة (`_vision_word_boxes`) وتُكتشَف مناطق
    شبيهة بجداول من مصدرين (`_scanned_table_blocks`): خطوط شبكة مرسومة فعلياً (أولوية،
    عبر معالجة صورة)، ثم تباعد نصي هندسي بلا حدود على الباقي — Vision نفسه لا يعطي
    كيان جدول جاهزاً. كل منطقة مُكتشَفة تصير Block(TABLE) واحداً، والفقرات التي تقع
    داخل حدودها تُستبعَد من الفقرات العادية لتفادي تكرار النص، ثم يُعاد ترتيب الجميع
    بالموضع الحقيقي (`_sort_blocks_by_position`) بدل إلحاق الجداول دوماً في النهاية."""
    pixmap = page.get_pixmap(dpi=dpi)
    image_bytes = _compress_image_to_limit(pixmap.tobytes("png"))

    vision_response = _call_vision_api(image_bytes)
    full_text_annotation = vision_response.get("fullTextAnnotation")
    if not full_text_annotation or not full_text_annotation.get("pages"):
        return []

    blocks: List[Block] = []
    for vision_page in full_text_annotation["pages"]:
        table_blocks = _scanned_table_blocks(image_bytes, _vision_word_boxes(vision_page))
        table_bboxes = [(b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1) for b in table_blocks if b.bbox]
        paragraph_blocks = _blocks_from_vision_page(vision_page, exclude_bboxes=table_bboxes)
        blocks.extend(_sort_blocks_by_position(paragraph_blocks + table_blocks))
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

                page_images = _page_images(fitz_doc, fitz_page, index + 1, seen_image_xrefs)
                for image in page_images:
                    images.append(image)
                    image.image_id = f"Image_{len(images):02d}"
                blocks = _insert_image_placeholders(blocks, page_images)
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
