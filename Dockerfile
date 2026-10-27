FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG INSTALL_ML_DEPS=0
ARG INSTALL_DEV_DEPS=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements*.txt /tmp/
COPY backend/requirements*.txt /tmp/

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.txt \
    && if [ "$INSTALL_ML_DEPS" = "1" ] && [ -f /tmp/requirements-ml.txt ]; then \
        python -m pip install -r /tmp/requirements-ml.txt; \
    fi \
    && if [ "$INSTALL_DEV_DEPS" = "1" ] && [ -f /tmp/requirements-dev.txt ]; then \
        python -m pip install -r /tmp/requirements-dev.txt; \
    fi

COPY backend/ /app/

RUN useradd --create-home --shell /usr/sbin/nologin django \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R django:django /app
USER django

EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]