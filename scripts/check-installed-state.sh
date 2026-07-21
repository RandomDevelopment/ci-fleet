#!/usr/bin/env bash
set -Eeuo pipefail

state_file=${CI_FLEET_INSTALL_STATE_FILE:-/var/lib/ci-fleet/install-state.json}
installer=${CI_FLEET_INSTALLER:-/opt/ci-fleet/manager/current/scripts/install-worker-controller.sh}

[[ -f "$state_file" ]] || { echo "ERROR: installed desired-state record is missing: $state_file" >&2; exit 2; }
[[ $(stat -c %u "$state_file") == 0 && $(stat -c %a "$state_file") == 600 ]] || { echo "ERROR: install state must be owned by root with mode 0600: $state_file" >&2; exit 2; }
command -v python3 >/dev/null || { echo 'ERROR: python3 is required' >&2; exit 2; }
[[ -x "$installer" ]] || { echo "ERROR: installer is unavailable: $installer" >&2; exit 2; }

if ! state_values=$(python3 - "$state_file" <<'PY'
import json
import sys

try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
    values = [state[name] for name in ("controller", "config_repository", "config_ref")]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(2)
print("\n".join(values))
PY
); then
  echo "ERROR: installed desired-state record is invalid: $state_file" >&2
  exit 2
fi
mapfile -t values <<<"$state_values"
[[ ${#values[@]} == 3 ]] || { echo "ERROR: installed desired-state record is incomplete: $state_file" >&2; exit 2; }
controller=${values[0]}
config_repository=${values[1]}
config_ref=${values[2]}

exec "$installer" --check \
  --config-repo "$config_repository" \
  --ref "$config_ref" \
  --controller "$controller"
