"""
هيكل FastAPI (اليوم الرابع، plan.md): يُغلّف موديولات DSPy (MedicalSpellingCorrector /
MedicalTableStructurer) بنقاط HTTP، تمهيداً لربط الـ pipeline المُحسَّن لاحقاً
بمرحلة Triage/الاستخراج الحقيقية (لم تُبنَ بعد).

لا يفشل بدء تشغيل الخادم عند غياب GEMINI_API_KEY — بدلاً من ذلك تُعطَّل
النقاط التي تحتاج LM (503 برسالة واضحة عبر lm_guard.require_lm_configured)
حتى لا يمنع غياب مفتاح تطوير محلي بقية الخادم (health check، وثائق OpenAPI) من
العمل.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from ..lm_config import configure_lm
from .auth import require_api_key
from .rate_limit import limiter
from .routers import documents, export, spelling, tables
from .schemas import HealthResponse

# **خلل حقيقي مُشخَّص (لا علاقة له بـPYTHONUNBUFFERED رغم تشابه العرض):** بلا
# `basicConfig` هنا، مستوى logger الجذري الافتراضي في Python هو WARNING — أي
# استدعاء `logger.info(...)` في أي مكان بالمشروع (بما فيها سجلّات تشخيص الذاكرة
# لكل صفحة في `ingest.py`) يُسقَط بصمت عند مستوى الإطار نفسه، قبل أن تصل لـstdout
# أصلاً بصرف النظر عن الـbuffering. تحقّق فعلي: حتى بعد `PYTHONUNBUFFERED=1` لم
# تظهر أي سجلّات تشخيص في Render إطلاقاً — السبب هذا، وليس التخزين المؤقت.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        configure_lm()
        app.state.lm_configured = True
    except RuntimeError as exc:
        logger.warning("LM لم يُهيَّأ عند بدء التشغيل: %s", exc)
        app.state.lm_configured = False
    yield


app = FastAPI(title="Medical OCR — Reasoning Pipeline API", version="0.1.0", lifespan=lifespan)

# للتطوير المحلي: يسمح لواجهة Next.js (منفذ 3000 افتراضياً) بمناداة الـAPI عبر المتصفح.
# كلا الاسمين مُدرَجان عمداً (وليس "localhost" فقط): بعض المتصفحات (Safari تحديداً،
# لوحظ فعلياً) تتعثّر مع "localhost" بسبب IPv6/HSTS مخزَّن سابقاً، فيُستخدَم
# "127.0.0.1" بدلاً منه — لكن هذا يغيّر الـOrigin الفعلي الذي يرسله المتصفح، فيُرفَض
# من CORS إن لم يكن مُدرَجاً هنا أيضاً.
# CORS_EXTRA_ORIGINS (اختياري، مفصول بفواصل) يضيف نطاقات إنتاج حقيقية (مثال:
# https://medflow.ai) دون تعديل الكود عند كل نشر.
_extra_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_EXTRA_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", *_extra_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)

# تحديد معدّل الطلبات (slowapi): يعتمد على IP العميل، الحدود الفعلية مضبوطة لكل نقطة
# داخل المُوجِّهات نفسها (انظر rate_limit.py وتوثيق كل نقطة).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# مصادقة مفتاح API: مطلوبة على كل النقاط المكلفة (LM/استخراج/تصدير)، وليس /health —
# انظر auth.py لتفاصيل السلوك عند غياب APP_API_KEY.
_auth_dep = [Depends(require_api_key)]
app.include_router(spelling.router, dependencies=_auth_dep)
app.include_router(tables.router, dependencies=_auth_dep)
app.include_router(documents.router, dependencies=_auth_dep)
app.include_router(export.router, dependencies=_auth_dep)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", lm_configured=bool(app.state.lm_configured))
