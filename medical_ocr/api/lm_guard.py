"""حارس مشترك: يمنع نقاط الـ API من محاولة استدعاء LM غير مُهيَّأ (لا يوجد مفتاح
API)، ويعيد 503 برسالة واضحة بدل تسريب خطأ LiteLLM/شبكة غامض إلى المستخدم."""

from __future__ import annotations

from fastapi import HTTPException, Request


def require_lm_configured(request: Request) -> None:
    if not request.app.state.lm_configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "LM غير مُهيَّأ: لا يوجد GEMINI_API_KEY في بيئة الخادم. "
                "أضِفه إلى .env وأعد تشغيل الخادم."
            ),
        )
