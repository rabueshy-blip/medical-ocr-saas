"""محدِّد معدّل الطلبات المشترك (slowapi) — نسخة وحيدة (singleton) يستوردها app.py
(لتسجيل الـmiddleware/exception handler) والمُوجِّهات (لتزيين كل نقطة بحدها الخاص)،
لتفادي استيراد دائري بين app.py والمُوجِّهات.

المفتاح الافتراضي هو عنوان IP للعميل (get_remote_address) — كافٍ لمنع طرف مجهول
واحد من استنزاف حصة Gemini/Vision المجانية أو إغراق الخادم، دون حاجة لحساب مستخدم."""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
