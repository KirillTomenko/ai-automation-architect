# Python 3.12 slim: FastAPI + uvicorn, системных пакетов не требуется.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Зависимости отдельным слоем: правки кода не инвалидируют кэш сборки.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Непривилегированный пользователь — root в контейнере не нужен.
RUN useradd --create-home appuser

COPY app/ app/
COPY config/ config/
COPY examples/ examples/

USER appuser

EXPOSE 8000

# Один воркер: in-memory состояние (rate-limit, счётчик ретраев) рассчитано
# на один процесс — не масштабировать воркерами без выноса лимитера наружу.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]