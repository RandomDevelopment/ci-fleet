#!/usr/bin/env bash
set -Eeuo pipefail

state_file=${CI_FLEET_INSTALL_STATE_FILE:-/var/lib/ci-fleet/install-state.json}
installer=${CI_FLEET_INSTALLER:-/opt/ci-fleet/manager/current/scripts/install-worker-controller.sh}

[[ -f "$state_file" ]] || { echo "ERROR: installed desired-state record is missing: $state_file" >&2; exit 2; }
command -v jq >/dev/null || { echo 'ERROR: jq is required' >&2; exit 2; }
[[ -x "$installer" ]] || { echo "ERROR: installer is unavailable: $installer" >&2; exit 2; }

controller=$(jq -er .controller "$state_file")
config_repository=$(jq -er .config_repository "$state_file")
config_ref=$(jq -er .config_ref "$state_file")

exec "$installer" --check \
  --config-repo "$config_repository" \
  --ref "$config_ref" \
  --controller "$controller"
