# Disposable nonproduction G72 PostgreSQL image ONLY. Never use for release.
# Mirrors the API runtime with the CI-only PostgreSQL driver; main API image
# and dependency policy are intentionally unchanged.
FROM python:3.12-slim
WORKDIR /app
COPY apps/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt 'psycopg[binary]==3.3.6'
COPY apps/api/app /app/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
