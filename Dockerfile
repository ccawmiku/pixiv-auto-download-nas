FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY pixiv_auto_worker.py /app/pixiv_auto_worker.py
COPY pixiv_bookmark_list.py /app/pixiv_bookmark_list.py
COPY config.example.json /app/config.example.json

EXPOSE 8080

CMD ["python", "/app/pixiv_auto_worker.py", "--config", "/config/config.json"]
