"""
تكوين LM فعلي عبر Google AI Studio (Gemini)/litellm (القسم 6 والخطوة 8 من plan.md).
هذه أول نقطة في المشروع تُشغّل موديولات DSPy (المبنية بـ dspy.ChainOfThought) ضد
نموذج حقيقي بدلاً من اختبارات دخان بدون LM.

**تحديث اليوم السابع:** تم التبديل من Anthropic إلى Gemini عبر Google AI Studio
لأن الأخير يوفّر مستوى مجاني (free tier) فعلياً بدون بطاقة دفع، على عكس Anthropic
الذي كان يتطلب مفتاح API مدفوعاً لم يكن متاحاً بعد في هذه البيئة.

مصمّم عمداً بدون أي fallback أو تخمين لمفتاح API: إن غاب المفتاح تُرفع رسالة خطأ
واضحة، لأن هذا حد بيئي (لا صلاحيات إدارية لتثبيت أي شيء) وليس عيباً يُخفى.
"""

from __future__ import annotations

import os
from typing import Optional

import dspy
from dotenv import load_dotenv

DEFAULT_MODEL = "gemini/gemini-2.5-flash"


def configure_lm(model: Optional[str] = None, **lm_kwargs) -> dspy.LM:
    """يحمّل .env إن وُجد، ثم يُعدّ dspy.settings.configure بنموذج Gemini حقيقي.

    يرفع RuntimeError برسالة واضحة إن غاب GEMINI_API_KEY (أو GOOGLE_API_KEY)، بدل
    تمرير الفشل إلى LiteLLM كخطأ شبكة غامض.
    """
    load_dotenv()

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY غير موجود في البيئة أو في ملف .env. "
            "احصل على مفتاح مجاني من https://aistudio.google.com/apikey ثم أضِفه "
            "إلى .env في جذر المشروع (GEMINI_API_KEY=...) قبل تشغيل أي موديول DSPy "
            "يستدعي LM حقيقياً."
        )

    resolved_model = model or os.getenv("MEDICAL_OCR_LM_MODEL", DEFAULT_MODEL)
    lm = dspy.LM(resolved_model, temperature=0.0, **lm_kwargs)
    dspy.settings.configure(lm=lm)
    return lm
