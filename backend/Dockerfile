ARG VERSION=dev
ARG REVISION=unknown

# Pinned index digest for python:3.13-slim, resolved for the 0.1.0 release.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

ARG VERSION
ARG REVISION

LABEL org.opencontainers.image.title="AgentCC API" \
      org.opencontainers.image.description="AgentCC local control-plane REST API" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 9000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
