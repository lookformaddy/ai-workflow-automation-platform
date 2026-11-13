FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY data/ ./data/

ENV PYTHONPATH=/app/src
ENV AWAP_STORE_PATH=/app/data/store.json
ENV AWAP_AUDIT_PATH=/app/data/audit.jsonl

EXPOSE 8000

CMD ["uvicorn", "awap.service:app", "--host", "0.0.0.0", "--port", "8000"]
