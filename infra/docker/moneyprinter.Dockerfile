FROM python:3.11-slim-bullseye

ARG MONEYPRINTER_COMMIT=475f21147f0808f5ffe3f58af9ab794b28a4da2c

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        imagemagick \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /MoneyPrinterTurbo

RUN git init . \
    && git remote add origin https://github.com/harry0703/MoneyPrinterTurbo.git \
    && git fetch --depth 1 origin "${MONEYPRINTER_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${MONEYPRINTER_COMMIT}" \
    && rm -rf .git

RUN pip install --no-cache-dir --retries 3 --timeout 60 -r requirements.txt

ENV PYTHONPATH=/MoneyPrinterTurbo

EXPOSE 8080

CMD ["python3", "main.py"]
