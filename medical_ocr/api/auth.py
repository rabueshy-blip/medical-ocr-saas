"""حارس مصادقة بسيط: يتطلب رأس `X-API-Key` مطابقاً لـ `APP_API_KEY` في بيئة الخادم
قبل السماح بالوصول لنقاط مكلفة (استخراج/تصدير/تصحيح LM) — الهدف حماية حصة
Gemini/Vision المجانية ومنع مجهولين من استخدام الخادم مجاناً، وليس نظام حسابات
مستخدمين كامل (مفتاح واحد مشترك بين الواجهة والباك-إند يكفي حالياً).

يتّبع نفس فلسفة `lm_guard.require_lm_configured`: غياب الإعداد لا يُسقِط الخادم،
بل يُبقي النقاط مفتوحة مع تحذير واضح في السجلّ (وضع تطوير محلي بلا احتكاك) —
لكن **يجب** ضبط `APP_API_KEY` في بيئة الإنتاج (Render) وإلا بقيت النقاط مفتوحة فعلياً."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_warned_unconfigured = False


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    configured_key = os.environ.get("APP_API_KEY")

    if not configured_key:
        global _warned_unconfigured
        if not _warned_unconfigured:
            logger.warning(
                "APP_API_KEY غير مُعرَّف في بيئة الخادم — نقاط API الحساسة مفتوحة "
                "بلا مصادقة (مقبول للتطوير المحلي فقط). اضبطه في بيئة الإنتاج."
            )
            _warned_unconfigured = True
        return

    if x_api_key != configured_key:
        raise HTTPException(
            status_code=401,
            detail="مفتاح API مفقود أو غير صحيح (أرسِله عبر رأس X-API-Key)",
        )
