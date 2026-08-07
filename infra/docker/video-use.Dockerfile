FROM python:3.12-slim

ARG VIDEO_USE_COMMIT=92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN git init /opt/video-use \
    && git -C /opt/video-use remote add origin https://github.com/browser-use/video-use.git \
    && git -C /opt/video-use fetch --depth 1 origin "${VIDEO_USE_COMMIT}" \
    && git -C /opt/video-use checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/video-use rev-parse HEAD)" = "${VIDEO_USE_COMMIT}" \
    && rm -rf /opt/video-use/.git

WORKDIR /service

COPY apps/video_use/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e /opt/video-use

COPY apps/video_use/app /service/app
COPY apps/video_use/ci_smoke.py /service/ci_smoke.py

RUN useradd --create-home --uid 10001 aether \
    && mkdir -p /media \
    && chown -R aether:aether /media /service

USER aether

ENV VIDEO_USE_MEDIA_ROOT=/media \
    VIDEO_USE_UPSTREAM_ROOT=/opt/video-use \
    VIDEO_USE_WORKERS=2

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
