# Build both stages from immutable, reviewed image digests. Example build args are
# documented in deploy/airgap/README.md; floating tags are intentionally rejected.
ARG NODE_BUILD_BASE
ARG NGINX_RUNTIME_BASE
FROM ${NODE_BUILD_BASE} AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ARG VITE_API_BASE_URL=/
ARG VITE_WS_BASE_URL=/
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_WS_BASE_URL=${VITE_WS_BASE_URL}
RUN npm run lint && npm run build

FROM ${NGINX_RUNTIME_BASE} AS frontend-runtime
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-builder --chown=101:101 /frontend/dist/ /usr/share/nginx/html/
USER 101:101
EXPOSE 8080
