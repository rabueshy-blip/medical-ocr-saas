FROM python:3.11-slim

# بلا هذا، Python يخزّن مخرجات stdout بالكامل مؤقتاً (buffered) بدل سطر-بسطر عند
# تشغيله داخل حاوية بلا طرفية حقيقية — أي سجلّ (logging) لم يصل لحجم البَفَر بعد
# يُفقَد نهائياً إن قُتلت العملية فجأة (SIGKILL من OOM killer، لا خروج طبيعي
# يُفرِّغ البَفَر). خلل حقيقي مُشخَّص: سجلّات تشخيص ذاكرة مُضافة حديثاً (لكل صفحة)
# لم تظهر إطلاقاً في سجلّات Render رغم تأكّد تنفيذها فعلياً محلياً — هذا هو السبب.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt \
    && playwright install --with-deps chromium

COPY medical_ocr ./medical_ocr
COPY data ./data

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn medical_ocr.api.app:app --host 0.0.0.0 --port ${PORT}"]
