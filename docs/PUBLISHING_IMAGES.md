# Publishing AgentCC images

AgentCC's public Docker Hub namespace is `nievric`. The repository source is
licensed under Apache-2.0; see the root `LICENSE` and `NOTICE` files before
publishing a release.

## Image names and tags

Publish these repositories:

| Image | Docker Hub repository |
| --- | --- |
| API | `nievric/agentcc-api` |
| Web UI | `nievric/agentcc-web` |
| Common session base | `nievric/agentcc-session-base` |
| Codex harness | `nievric/agentcc-session-codex` |
| Hermes harness | `nievric/agentcc-session-hermes` |
| Claude Code harness | `nievric/agentcc-session-claude-code` |
| Kilo Code harness | `nievric/agentcc-session-kilo-code` |

Build an immutable release tag such as `0.1.0` first. After it passes the
release checks, add the compatibility tag (`0.1`) and, for a stable release,
`latest`. Never publish a development image under `latest` or `dev`.

All images in a release must use the same AgentCC version and git revision.
The image labels record both values as OCI metadata.

## Release procedure

Run these commands from the repository root after logging into Docker Hub.
They build locally by default; replace `--load` with `--push` only after the
checks below pass.

```sh
export VERSION=0.1.0
export REVISION="$(git rev-parse HEAD)"
export PLATFORM=linux/amd64

docker buildx build --platform "$PLATFORM" --load \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-session-base:$VERSION" \
  --file images/base/Dockerfile images/base

docker buildx build --platform "$PLATFORM" --load \
  --build-arg BASE_IMAGE="nievric/agentcc-session-base:$VERSION" \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-session-codex:$VERSION" \
  --file images/harness-codex/Dockerfile images/harness-codex

docker buildx build --platform "$PLATFORM" --load \
  --build-arg BASE_IMAGE="nievric/agentcc-session-base:$VERSION" \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-session-hermes:$VERSION" \
  --file images/harness-hermes/Dockerfile images/harness-hermes

docker buildx build --platform "$PLATFORM" --load \
  --build-arg BASE_IMAGE="nievric/agentcc-session-base:$VERSION" \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-session-claude-code:$VERSION" \
  --file images/harness-claude-code/Dockerfile images/harness-claude-code

docker buildx build --platform "$PLATFORM" --load \
  --build-arg BASE_IMAGE="nievric/agentcc-session-base:$VERSION" \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-session-kilo-code:$VERSION" \
  --file images/harness-kilocode/Dockerfile .

docker buildx build --platform "$PLATFORM" --load \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-api:$VERSION" \
  --file backend/Dockerfile backend

docker buildx build --platform "$PLATFORM" --load \
  --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" \
  --tag "nievric/agentcc-web:$VERSION" \
  --file frontend/Dockerfile frontend
```

The current Kilo Code image installs an x64 CLI package, so release it as
`linux/amd64` until every harness and all upstream dependencies have been
tested for another architecture. Do not publish a multi-platform manifest
until that verification is complete.

## Required release checks

1. Build from a clean checkout and confirm the root, API, frontend, base, and
   Codex build contexts exclude local credentials and runtime data.
2. Re-resolve and review each upstream digest in the Dockerfiles as part of a
   release refresh. The 0.1.0 Dockerfiles already pin the reviewed base-image
   indexes and verify the Hermes release commit.
3. Inspect image history and configuration for credentials; do not use build
   arguments for API keys, vault keys, or workspace data.
4. Produce and retain an SBOM and vulnerability-scan result for each exact
   image digest. Review the licenses and attribution obligations shown by the
   SBOM; `NOTICE` is not a substitute for the third-party notices required by
   the released components. Docker Scout can export an SPDX report from a
   local candidate before publishing:

   ```sh
   docker scout sbom "local://nievric/agentcc-session-codex:$VERSION" \
     --format spdx \
     --output "agentcc-session-codex-$VERSION.spdx.json"
   ```

   Re-run this command against the final pushed digest and retain that report
   as the release artifact; a baseline SBOM from a development tag is not a
   substitute for the final image's SBOM.
5. Smoke-test the API, web UI, and every session harness from their release
   tags. In particular, do not publish code-server directly with a host port:
   it intentionally uses `--auth none` and must remain behind AgentCC's
   authenticated private-network gateway.
6. Inspect the release labels, for example:

   ```sh
   docker image inspect "nievric/agentcc-session-codex:$VERSION" \
     --format '{{json .Config.Labels}}'
   ```

After all checks pass, rebuild with `--push` (or push the verified local tags),
then create the `0.1` and `latest` tags from the verified immutable digest.
