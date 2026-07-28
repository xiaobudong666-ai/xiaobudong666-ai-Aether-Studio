FROM python:3.12-slim

WORKDIR /app

# Install standard dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY apps/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/worker/app /app/app

EXPOSE 8001

CMD ["python", "-m", "app.main"]
