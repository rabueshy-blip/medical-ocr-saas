"""
حماية الرموز الطبية من الهلوسة (اليوم السادس، plan.md القسم 2): استخراج الأرقام
الصريحة الصرفة من نص، يُستخدم كأساس مشترك لبوابتي الترسيخ الرقمي في:

- `medical_ocr/signatures/spelling.py` (`numeric_tokens_preserved`): يمنع تغيير
  رقم واضح داخل نص مصحَّح.
- `medical_ocr/signatures/tables.py` (`row_values_grounded`): يمنع تغيير قيمة
  رقمية واضحة داخل خلية جدول مُهيكَلة.

مُستخرَج إلى وحدة مشتركة لتفادي ازدواج المنطق بين الاثنين (نفس التعريف الدقيق
لِما يُعتبر "رقماً صريحاً" يجب أن يبقى مصدراً واحداً).
"""

from __future__ import annotations

import re
from typing import List

# رقم صريح صرف (بدون حروف ملتصقة به) — يستثني عمداً رموز التباس OCR مثل "2O"
# أو "5oo" لأن تلك ليست أرقاماً صرفة أصلاً في النص الخام، وتصحيحها إلى رقم
# مقروء هو بالضبط الغرض المقصود من موديولات DSPy وليس هلوسة.
_PURE_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def extract_pure_numbers(text: str) -> List[str]:
    return _PURE_NUMBER_RE.findall(text)
