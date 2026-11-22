FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CURTAILMENT_DB=/app/instance/curtailment.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY curtailment ./curtailment
COPY templates ./templates
COPY static ./static

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/instance \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# 单进程多线程：SQLite 写事务串行化，避免多 worker 写竞争。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "curtailment.app:create_app()"]
