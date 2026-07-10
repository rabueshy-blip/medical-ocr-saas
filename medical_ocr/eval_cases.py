"""
حالات اختبار "صعبة" (Day 4، plan.md القسم 6 و8): تُستخدم لتشغيل موديولات DSPy
(MedicalSpellingCorrector / MedicalTableStructurer) ضد LM حقيقي لأول مرة والتحقق
من أن التفكير التسلسلي (CoT) يستخدم السياق فعلاً بدل مجرد تصحيح إملائي سطحي.

هذه ليست اختبارات وحدة (unit tests) — لا تُشغَّل عبر unittest، بل عبر
scripts/run_hard_cases.py لأنها تحتاج LM حقيقياً (تكلفة/زمن استجابة).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class TerminologyCase:
    name: str
    raw_text: str
    note: str


@dataclass(frozen=True)
class TableCase:
    name: str
    raw_rows: List[List[str]]
    column_hints: List[str]
    note: str


TERMINOLOGY_CASES: List[TerminologyCase] = [
    TerminologyCase(
        name="drug_name_ambiguity",
        raw_text=(
            "يُعطى المريض ميتفوبرولين 50 ملغ مرتين يومياً للتحكم في ضغط الدم "
            "وتسارع نبضات القلب"
        ),
        note=(
            "الكلمة المشوَّهة 'ميتفوبرولين' تتطابق تشابهياً (fuzzy) مع اسمين "
            "مختلفين في القاموس: 'ميتفورمين' (لعلاج السكري) و'ميتوبرولول' "
            "(لعلاج ضغط الدم/القلب). السياق (ضغط الدم وتسارع النبض) يرجّح "
            "ميتوبرولول لا ميتفورمين رغم أن الأخير أقرب شكلياً — اختبار حقيقي "
            "لاستخدام التفكير التسلسلي للسياق السريري وليس فقط تشابه الحروف."
        ),
    ),
    TerminologyCase(
        name="ocr_digit_letter_confusion",
        raw_text=(
            "يُنصح بتناول أوموبرازول 2O ملغ مرة يومياً قبل الإفطار بنصف ساعة"
        ),
        note=(
            "'2O' يحتوي حرف O لاتيني بدل الرقم 0 (خطأ OCR شائع). الجرعة الاعتيادية "
            "لأوموبرازول (أوميبرازول) هي 20 ملغ مرة يومياً — اختبار لتصحيح رقمي "
            "مبني على نمط جرعات معروف، دون اختلاق جرعة غير مرتبطة بالنص الخام."
        ),
    ),
]

TABLE_CASES: List[TableCase] = [
    TableCase(
        name="multi_level_lab_header",
        raw_rows=[
            ["الفحص", "", "المدى الطبيعي", ""],
            ["", "القيمة", "الحد الأدنى", "الحد الأقصى"],
            ["Hemoglobin", "13.5", "12", "16"],
            ["Creatinine", "0.9", "0.6", "1.3"],
            ["Glucose", "", "70", "110"],
        ],
        column_hints=["الفحص", "القيمة", "الحد الأدنى", "الحد الأقصى"],
        note=(
            "رأس الجدول مقسوم على سطرين بسبب خلايا مدمجة (merged cells) فكّكها "
            "محرك استخراج الجداول إلى خلايا فارغة. قيمة Glucose غير مقروءة "
            "(خلية فارغة) ويجب أن تصبح 'UNCERTAIN' لا صفراً أو قيمة مخترعة. "
            "عدد الصفوف (5) يجب أن يبقى كما هو تماماً."
        ),
    ),
]
