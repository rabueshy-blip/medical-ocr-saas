"""
اليوم الخامس (plan.md): دوال قياس (metrics) موحّدة تُقارن مخرجات موديولات DSPy
بالمخرجات المرجعية في Gold Dataset (medical_ocr/gold_dataset.py). تُستخدم في
مكانين لتفادي التكرار:

- scripts/evaluate_gold.py: قياس الدقة عبر dspy.Evaluate.
- scripts/optimize_modules.py: دالة الهدف (metric) لمُحسِّن dspy.BootstrapFewShot.

المبدأ: القيد البرمجي لمنع الهلوسة (is_correction_grounded / row_count_preserved،
القسم 2 من plan.md) بوابة صارمة أولاً (0.0 عند الفشل)، ثم درجة جزئية بحسب مطابقة
المصطلحات/الخلايا المتوقعة عبر fuzzy matching — وليس مطابقة نصية حرفية، لأن صياغة
الموديل قد تختلف شكلياً عن الصياغة المرجعية دون أن تكون خاطئة.
"""

from __future__ import annotations

import json
from typing import Any

import dspy
from rapidfuzz import fuzz

from .signatures.spelling import is_correction_grounded
from .signatures.tables import row_count_preserved

FUZZY_CONTAINS_THRESHOLD = 80.0


def _fuzzy_contains(haystack: str, needle: str, threshold: float = FUZZY_CONTAINS_THRESHOLD) -> bool:
    """يتحقق أن `needle` موجودة تقريباً ضمن `haystack` (مطابقة جزئية متسامحة مع اختلافات طفيفة)."""
    if not needle:
        return True
    if not haystack:
        return False
    return fuzz.partial_ratio(needle, haystack) >= threshold


def terminology_metric(example: dspy.Example, prediction: dspy.Prediction, trace: Any = None) -> float:
    """يقيس مخرجات MedicalSpellingCorrector مقابل GoldTerminologyExample واحدة.

    بوابة الترسيخ (grounding) والمصطلحات الممنوعة تُفشل الدرجة كاملة (0.0) — أي
    تصحيح غير مرتكز على raw_text أو يستبدل مصطلحاً بآخر ممنوع صراحة (مثل حسم
    التباس دوائي بالاتجاه الخاطئ) يُعامل كخطأ جسيم لا فرق جزئي.
    """
    corrected_text = getattr(prediction, "corrected_text", "") or ""
    uncertain_terms_raw = getattr(prediction, "uncertain_terms", "") or ""

    if not is_correction_grounded(example.raw_text, corrected_text):
        return 0.0

    if any(_fuzzy_contains(corrected_text, term) for term in example.forbidden_terms):
        return 0.0

    expected_terms = example.expected_correction_terms
    term_score = (
        sum(_fuzzy_contains(corrected_text, term) for term in expected_terms) / len(expected_terms)
        if expected_terms
        else 1.0
    )

    expected_uncertain = example.expected_uncertain_terms
    uncertain_score = (
        sum(_fuzzy_contains(uncertain_terms_raw, term) for term in expected_uncertain) / len(expected_uncertain)
        if expected_uncertain
        else 1.0
    )

    return round(0.5 * term_score + 0.5 * uncertain_score, 4)


def table_metric(example: dspy.Example, prediction: dspy.Prediction, trace: Any = None) -> float:
    """يقيس مخرجات MedicalTableStructurer مقابل GoldTableExample واحدة.

    يبحث عن القيم المتوقعة لكل صف كنص حر ضمن قيم ذلك الصف المُهيكَل (بغضّ النظر
    عن مفتاح العمود الذي اختاره الموديل)، لأن أسماء الأعمدة الفعلية في
    structured_rows تُستنتَج من الموديل نفسه ولا يمكن افتراض مطابقتها الحرفية.
    """
    structured_rows_json = getattr(prediction, "structured_rows", "") or ""

    if not row_count_preserved(example.raw_rows, structured_rows_json):
        return 0.0

    try:
        structured_rows = json.loads(structured_rows_json)
    except (json.JSONDecodeError, TypeError):
        return 0.0

    scores = []

    for row_index_str, expected_values in example.expected_row_values.items():
        row_index = int(row_index_str)
        if row_index >= len(structured_rows) or not expected_values:
            continue
        row = structured_rows[row_index]
        row_text = " ".join(str(v) for v in row.values()) if isinstance(row, dict) else str(row)
        hits = sum(_fuzzy_contains(row_text, value) for value in expected_values)
        scores.append(hits / len(expected_values))

    for row_index in example.expected_uncertain_row_indices:
        if row_index >= len(structured_rows):
            scores.append(0.0)
            continue
        row = structured_rows[row_index]
        row_text = " ".join(str(v) for v in row.values()) if isinstance(row, dict) else str(row)
        scores.append(1.0 if "UNCERTAIN" in row_text.upper() else 0.0)

    return round(sum(scores) / len(scores), 4) if scores else 1.0
