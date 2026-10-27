FROM python:3.12.16-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

ARG INSTALL_ML_DEPS=0
ARG INSTALL_DEV_DEPS=0

# Canonical, root-level manifests are copied after .dockerignore has removed
# local secrets, Git history, virtual environments, and generated frontend data.
COPY requirements*.txt /tmp/

RUN python -m pip install --no-cache-dir --upgrade "pip==26.2.2" \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && if [ "$INSTALL_DEV_DEPS" = "1" ]; then \
        test -f /tmp/requirements-dev.txt \
        && python -m pip install --no-cache-dir -r /tmp/requirements-dev.txt; \
    fi \
    && if [ "$INSTALL_ML_DEPS" = "1" ]; then \
        test -f /tmp/requirements-ml.txt \
        && python -m pip install --no-cache-dir -r /tmp/requirements-ml.txt; \
    fi \
    && python -m pip check

COPY backend/ /app/

RUN useradd --create-home --shell /usr/sbin/nologin django \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R django:django /app

USER django

EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
