"""
Signature الثاني (plan.md القسم 6): هيكلة وتصحيح صفوف/أعمدة الجداول الطبية
الممسوحة ضوئياً (القسم 3-ب-4)، كموديول DSPy مستقل — لا يُدمج مع موديول
التصحيح الإملائي في موديل واحد.

نفس مبدأ منع الهلوسة: لا يُسمح للموديل بحذف/دمج صفوف، ويُفرض ذلك برمجياً عبر
`row_count_preserved` + `dspy.Refine` (إعادة محاولة حتى N مرات حتى يتحقق الشرط)،
وليس مجرد تعليمة نصية. ملاحظة: `dspy.Suggest`/`dspy.Assert` غير متوفرين في إصدار
dspy المُثبَّت هنا (2.6.27)؛ `dspy.Refine` هو البديل المكافئ الحالي.

بوابة إضافية لحماية الرموز الطبية (اليوم السادس): `row_values_grounded` تمنع
تغيير قيمة رقمية واضحة (نتيجة مخبرية/جرعة) ضمن صف لم يُعلَّم UNCERTAIN — لأن
`row_count_preserved` وحدها تتحقق فقط من عدد الصفوف، لا من سلامة قيمها.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import List, Optional

import dspy

from ..numeric_guard import extract_pure_numbers


class MedicalTableStructuring(dspy.Signature):
    """هيكل جدولاً طبياً خاماً (صفوف/خلايا قد تكون غير منتظمة بسبب أخطاء OCR) إلى صفوف وأعمدة متسقة.

    قواعد صارمة:
    - لا تُضِف أو تحذف بيانات طبية (أدوية، جرعات، قيم مخبرية) غير موجودة في raw_rows.
    - إن كان اسم عمود غير واضح استدل عليه من column_hints فقط، ولا تخترع أسماء أعمدة.
    - إن كانت خلية غير مقروءة أو ناقصة ضع قيمتها "UNCERTAIN" بدلاً من تخمين محتواها.
    - حافظ على عدد الصفوف كما هو تماماً؛ ممنوع دمج أو حذف أي صف.
    """

    raw_rows: str = dspy.InputField(
        desc="JSON: الصفوف والخلايا الخام كما استُخرجت من محرك الجداول، قد تحتوي خلايا فارغة أو مُزاحة"
    )
    column_hints: str = dspy.InputField(
        desc="JSON: أسماء أعمدة متوقعة للجدول الطبي (مثال: الدواء، الجرعة، التكرار، الملاحظات)، أو [] إن لم تتوفر"
    )
    structured_rows: str = dspy.OutputField(
        desc="JSON: قائمة صفوف بنفس عدد raw_rows، كل صف dict بمفاتيح أسماء الأعمدة المصححة"
    )
    notes: str = dspy.OutputField(
        desc="JSON: قائمة ملاحظات حول أي خلية غامضة/UNCERTAIN مع رقم الصف والعمود"
    )


def encode_raw_rows(rows: List[List[str]]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def encode_column_hints(column_hints: Optional[List[str]]) -> str:
    return json.dumps(column_hints or [], ensure_ascii=False)


def row_count_preserved(raw_rows: List[List[str]], structured_rows_json: str) -> bool:
    """قيد برمجي: الهيكلة يجب ألا تحذف أو تدمج صفوفاً (القسم 3-ب-4 و6)."""
    try:
        structured = json.loads(structured_rows_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(structured, list) and len(structured) == len(raw_rows)


def structured_row_text(row: object) -> str:
    """يحوّل صفاً مُهيكَلاً (dict بمفاتيح أعمدة أو قيمة أخرى) إلى نص واحد للمقارنة/البحث."""
    return " ".join(str(value) for value in row.values()) if isinstance(row, dict) else str(row)


def row_values_grounded(raw_rows: List[List[str]], structured_rows_json: str) -> bool:
    """بوابة حماية الرموز الطبية (اليوم السادس): تمنع تغيير قيمة رقمية واضحة
    (نتيجة مخبرية/جرعة) ضمن صف لم يُعلَّم UNCERTAIN بالكامل.

    `row_count_preserved` تتحقق فقط من عدد الصفوف، لا من سلامة قيمها — صف قد
    يُعاد ترتيب أعمدته دون حذف/دمج لكن بقيمة رقمية مُستبدَلة بأخرى، وهذا ما
    تكتشفه هذه البوابة. صف فيه أي خلية UNCERTAIN يُستثنى عمداً من هذا الفحص
    لأن فقدان رقمه الأصلي متوقَّع ومقصود في تلك الحالة.
    """
    if not row_count_preserved(raw_rows, structured_rows_json):
        return False
    structured_rows = json.loads(structured_rows_json)
    for raw_row, structured_row in zip(raw_rows, structured_rows):
        structured_text = structured_row_text(structured_row)
        if "UNCERTAIN" in structured_text.upper():
            continue
        raw_numbers = Counter(extract_pure_numbers(" ".join(cell or "" for cell in raw_row)))
        structured_numbers = Counter(extract_pure_numbers(structured_text))
        if any(structured_numbers[number] < count for number, count in raw_numbers.items()):
            return False
    return True


def table_row_count_reward(call_kwargs: dict, prediction: dspy.Prediction) -> float:
    """reward_fn لـ dspy.Refine: 1.0 إن حافظت الهيكلة على عدد الصفوف الأصلي وقيمها الرقمية، وإلا 0.0."""
    try:
        original_rows = json.loads(call_kwargs["raw_rows"])
    except (json.JSONDecodeError, TypeError):
        return 0.0
    return 1.0 if row_values_grounded(original_rows, prediction.structured_rows) else 0.0


class MedicalTableStructurer(dspy.Module):
    """موديول DSPy الذي يغلّف MedicalTableStructuring بترميز JSON وقيد ترسيخ عبر dspy.Refine."""

    def __init__(self, max_attempts: int = 3):
        super().__init__()
        base = dspy.ChainOfThought(MedicalTableStructuring)
        self.structure = dspy.Refine(
            module=base,
            N=max_attempts,
            reward_fn=table_row_count_reward,
            threshold=1.0,
        )

    def forward(self, raw_rows: List[List[str]], column_hints: Optional[List[str]] = None) -> dspy.Prediction:
        return self.structure(
            raw_rows=encode_raw_rows(raw_rows),
            column_hints=encode_column_hints(column_hints),
        )
