#!/bin/sh
set -eu

workspace_root="${WORKSPACE_ROOT:-/workspaces/session}"
data_dir="${CODE_SERVER_DATA_DIR:-/tmp/code-server}"
extensions_dir="${CODE_SERVER_EXTENSIONS_DIR:-/opt/code-server/extensions}"

mkdir -p "${workspace_root}" "${data_dir}" "${extensions_dir}"

if [ "$(id -u)" = "0" ]; then
  if [ -n "${AGENTCC_CHECKOUT:-}" ]; then
    # Only the controller supplies this fixed, UUID-derived checkout path.
    # Both session users share its group; trust exactly this Git directory.
    case "$AGENTCC_CHECKOUT" in /workspaces/tasks/*/checkout) ;; *) exit 1 ;; esac
    runuser -u coder -- git config --global --add safe.directory "$AGENTCC_CHECKOUT"
  fi
  # Newly attached named volumes are root-owned. Set only direct AgentCC mount
  # roots; the control plane owns any broader migration or repair operation.
  # Change the mode before ownership: the runtime intentionally grants this
  # bootstrap process CAP_CHOWN only, not broader file-ownership capabilities.
  if [ -d /workspaces/session ] && [ -w /workspaces/session ]; then
    chmod 2775 /workspaces/session
    chown coder:workspace /workspaces/session
  fi
  for shared_mount in "${workspace_root}"/shared/*; do
    if [ -d "${shared_mount}" ] && [ -w "${shared_mount}" ]; then
      chmod 2775 "${shared_mount}"
      chown coder:workspace "${shared_mount}"
    fi
  done
  chmod 0700 "${data_dir}"
  chown coder:coder "${data_dir}" "${extensions_dir}"

  # Fixed arguments prevent runtime values from becoming shell source.
  exec runuser -u coder -- env \
    HOME=/home/coder \
    CODE_SERVER_DATA_DIR="${data_dir}" \
    CODE_SERVER_EXTENSIONS_DIR="${extensions_dir}" \
    sh -c 'umask 0002; exec code-server --auth none --disable-telemetry --bind-addr "0.0.0.0:$CODE_SERVER_PORT" --user-data-dir "$CODE_SERVER_DATA_DIR" --extensions-dir "$CODE_SERVER_EXTENSIONS_DIR" "$WORKSPACE_ROOT"'
fi

# Rootless runtimes may start the image directly as `coder`. They must prepare
# mounted-volume ownership before startup.
umask 0002
exec code-server --auth none --disable-telemetry --bind-addr "0.0.0.0:${CODE_SERVER_PORT:-8080}" --user-data-dir "${data_dir}" --extensions-dir "${extensions_dir}" "${workspace_root}"
