ARG VERSION=dev
ARG REVISION=unknown

# Pinned index digest for node:22-bookworm-slim, resolved for the 0.1.0 release.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Pinned index digest for nginx:1.27-alpine, resolved for the 0.1.0 release.
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

ARG VERSION
ARG REVISION

LABEL org.opencontainers.image.title="AgentCC web" \
      org.opencontainers.image.description="AgentCC browser command center" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
