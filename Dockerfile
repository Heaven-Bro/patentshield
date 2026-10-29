# Pin the complete multi-platform manifest by digest. Update this value only
# through the normal image-update review and vulnerability-scan process.
ARG PYTHON_BASE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${PYTHON_BASE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS builder
WORKDIR /build
ARG INSTALL_ML_DEPS=0
ARG INSTALL_DEV_DEPS=0

# Compilers and headers exist only in the build stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
        pkg-config \
        libxml2-dev \
        libxmlsec1-dev \
        libxmlsec1-openssl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
COPY requirements-ml.txt /tmp/requirements-ml.txt
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip install -r /tmp/requirements.txt \
    && if [ "$INSTALL_ML_DEPS" = "1" ]; then \
        /opt/venv/bin/python -m pip install -r /tmp/requirements-ml.txt; \
    fi \
    && if [ "$INSTALL_DEV_DEPS" = "1" ]; then \
        /opt/venv/bin/python -m pip install -r /tmp/requirements-dev.txt; \
    fi

FROM base AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

# Runtime contains libraries required by psycopg/xmlsec, but no compiler,
# development headers, package metadata, or build-essential toolchain.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        libpq5 \
        libxml2 \
        libxmlsec1 \
        libxmlsec1-openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin django \
    && install -d -o django -g django /app/media /app/staticfiles

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=django:django backend/ /app/

USER django
EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
