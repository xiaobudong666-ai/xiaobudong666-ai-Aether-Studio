FROM python:3.12-slim

WORKDIR /app

# Install standard dependencies, including FFmpeg and ffprobe
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Verify FFmpeg and ffprobe installations
RUN ffmpeg -version && ffprobe -version

COPY apps/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/worker/app /app/app

EXPOSE 8001

CMD ["python", "-m", "app.main"]
