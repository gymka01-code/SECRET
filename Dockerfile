FROM python:3.11-slim

WORKDIR /app

# Зависимости отдельным слоем — кэшируется при сборке
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Запуск ────────────────────────────────────────────────────
# WORKERS — кол-во воркеров (рекомендуется 2 × CPU + 1).
# При высокой нагрузке увеличивай через переменную окружения.
# SQLite с WAL-режимом поддерживает конкурентные чтения,
# но для >500 rps рассмотри переход на PostgreSQL + asyncpg.
ENV WORKERS=4

CMD gunicorn main:app \
      --worker-class uvicorn.workers.UvicornWorker \
      --workers ${WORKERS} \
      --bind 0.0.0.0:${PORT:-8000} \
      --timeout 30 \
      --keep-alive 5 \
      --log-level info
