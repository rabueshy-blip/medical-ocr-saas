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

DEFAULT_MODEL = "gemini/gemini-3-flash-preview"


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
    # **مشكلة حقيقية مُشخَّصة:** الافتراضي (4000) كان يُقطَع فعلياً على جداول ممسوحة
    # حقيقية (JSON للتفكير + structured_rows + notes لجدول متعدد الصفوف) — يُفشِل
    # JSONAdapter التحليل، فيُعاد المحاولة (Refine) بحرارة مختلفة، وكل محاولة فاشلة
    # هي استدعاء LM إضافي كامل يستهلك من الحصة اليومية الشحيحة أصلاً (5/دقيقة، 20/يوم
    # على المستوى المجاني) ويُضاعِف زمن الاستخراج الكلي — لوحظ فعلياً 3 محاولات فاشلة
    # متتالية بسبب القطع قبل نجاح واحدة على ملف DEXA حقيقي.
    kwargs = {"max_tokens": 8000, **lm_kwargs}
    lm = dspy.LM(resolved_model, temperature=0.0, **kwargs)
    # **السبب الجذري الفعلي لانهيارات الذاكرة على ملفات كثيرة الصفحات (مُشخَّص فعلياً
    # عبر سجلّات Render، وليس نظرياً):** `configure_lm()` تُستدعى مرة واحدة فقط عند
    # إقلاع الخادم (`api/app.py`)، فـ`lm` هنا كائن وحيد يعيش طوال عمر العملية. كل
    # استدعاء LM حقيقي (`MedicalTableStructurer` لكل جدول ممسوح مكتشَف) يُلحِق نسخة
    # كاملة من الطلب/الاستجابة الخام في `lm.history` (بلا أي سقف إطلاقاً، خلافاً لـ
    # `GLOBAL_HISTORY` الداخلية في dspy نفسها المحدودة بـ10000 عنصر) — تتراكم عبر كل
    # الطلبات منذ إقلاع الخادم، فلا `gc.collect()` ولا `malloc_trim` يُحرّرانها لأنها
    # ليست قمامة فعلاً، بل مرجوعة حيّة من `lm.history`. `disable_history=True` يوقف
    # كلا التسجيلين (المحلي وغير المحدود + العالمي المحدود) من الأساس — لا مكان في
    # الكود يستخدم `dspy.inspect_history()`/`lm.history` فعلياً.
    dspy.settings.configure(lm=lm, disable_history=True)
    return lm
