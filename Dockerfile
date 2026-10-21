FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# This Dockerfile assumes the Django project has a pinned requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

# Run as a non-root user in the container.
RUN useradd --create-home --shell /usr/sbin/nologin django \
    && chown -R django:django /app
USER django
