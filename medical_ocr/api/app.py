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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..lm_config import configure_lm
from .routers import documents, export, spelling, tables
from .schemas import HealthResponse

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

# للتطوير المحلي فقط: يسمح لواجهة Next.js (منفذ 3000 افتراضياً) بمناداة الـAPI عبر المتصفح.
# كلا الاسمين مُدرَجان عمداً (وليس "localhost" فقط): بعض المتصفحات (Safari تحديداً،
# لوحظ فعلياً) تتعثّر مع "localhost" بسبب IPv6/HSTS مخزَّن سابقاً، فيُستخدَم
# "127.0.0.1" بدلاً منه — لكن هذا يغيّر الـOrigin الفعلي الذي يرسله المتصفح، فيُرفَض
# من CORS إن لم يكن مُدرَجاً هنا أيضاً.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(spelling.router)
app.include_router(tables.router)
app.include_router(documents.router)
app.include_router(export.router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", lm_configured=bool(app.state.lm_configured))
