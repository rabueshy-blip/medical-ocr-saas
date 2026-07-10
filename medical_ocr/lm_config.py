"""
تكوين LM فعلي عبر anthropic/litellm (القسم 6 والخطوة 8 من plan.md، البند الذي كان
"لم يبدأ بعد" حتى نهاية اليوم الثالث). هذه أول نقطة في المشروع تُشغّل موديولات
DSPy (المبنية بـ dspy.ChainOfThought) ضد نموذج حقيقي بدلاً من اختبارات دخان بدون LM.

مصمّم عمداً بدون أي fallback أو تخمين لمفتاح API: إن غاب المفتاح تُرفع رسالة خطأ
واضحة، لأن هذا حد بيئي (لا صلاحيات إدارية لتثبيت أي شيء) وليس عيباً يُخفى.
"""

from __future__ import annotations

import os
from typing import Optional

import dspy
from dotenv import load_dotenv

DEFAULT_MODEL = "anthropic/claude-sonnet-5"


def configure_lm(model: Optional[str] = None, **lm_kwargs) -> dspy.LM:
    """يحمّل .env إن وُجد، ثم يُعدّ dspy.settings.configure بنموذج Claude حقيقي.

    يرفع RuntimeError برسالة واضحة إن غاب ANTHROPIC_API_KEY، بدل تمرير الفشل
    إلى LiteLLM كخطأ شبكة غامض.
    """
    load_dotenv()

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY غير موجود في البيئة أو في ملف .env. "
            "أضِفه إلى .env في جذر المشروع (ANTHROPIC_API_KEY=sk-ant-...) "
            "قبل تشغيل أي موديول DSPy يستدعي LM حقيقياً."
        )

    resolved_model = model or os.getenv("MEDICAL_OCR_LM_MODEL", DEFAULT_MODEL)
    lm = dspy.LM(resolved_model, temperature=0.0, **lm_kwargs)
    dspy.settings.configure(lm=lm)
    return lm
