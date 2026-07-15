# syntax=docker/dockerfile:1

FROM golang:1.22-bookworm AS proxycheck-builder
WORKDIR /src
COPY proxycheck-api/go.mod ./
RUN go mod download
COPY proxycheck-api/ ./
RUN CGO_ENABLED=0 go build -o /out/proxycheck ./cmd/proxycheck
RUN CGO_ENABLED=0 go build -o /out/pool-router ./cmd/pool-router

FROM python:3.12-slim AS runtime

ARG SING_BOX_VERSION=1.13.13
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PPM_HOST=0.0.0.0 \
    PPM_PORT=9100 \
    PPM_PROXY_LISTEN_HOST=0.0.0.0 \
    PPM_CLASH_API_ADDR=127.0.0.1:9090

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY templates ./templates
COPY main.py .
COPY --from=proxycheck-builder /out/proxycheck /app/proxycheck-api/proxycheck
COPY --from=proxycheck-builder /out/pool-router /app/proxycheck-api/pool-router

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) sing_arch="amd64" ;; \
      arm64) sing_arch="arm64" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /app/bin /app/config /app/tmp; \
    curl -fL "https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-${sing_arch}.tar.gz" -o /tmp/sing-box.tar.gz; \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp; \
    find /tmp -type f -name sing-box -exec cp {} /app/bin/sing-box \; -quit; \
    chmod +x /app/bin/sing-box /app/proxycheck-api/proxycheck /app/proxycheck-api/pool-router; \
    rm -rf /tmp/sing-box*

VOLUME ["/app/config", "/app/tmp"]
EXPOSE 9100

CMD ["python", "main.py"]
