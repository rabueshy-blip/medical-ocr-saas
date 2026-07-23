FROM python:3.11-slim

WORKDIR /app

COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt \
    && playwright install --with-deps chromium

COPY medical_ocr ./medical_ocr

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn medical_ocr.api.app:app --host 0.0.0.0 --port ${PORT}"]
