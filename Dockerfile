FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py ./
COPY readme.md ./
COPY .env.example ./

RUN useradd --create-home --shell /bin/bash bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /app /data

USER bridge

ENTRYPOINT ["python", "/app/sync.py"]
CMD ["--help"]
