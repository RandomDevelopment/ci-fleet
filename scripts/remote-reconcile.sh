#!/usr/bin/env bash
# Remote Git-authored configuration reconciliation for ci-fleet controllers.
#
# Fetches the private desired-state repository using a short-lived GitHub App
# installation token, resolves the default-branch HEAD to an immutable commit,
# validates the configuration, checks for drift, and reconciles if needed.
#
# Usage:
#   remote-reconcile.sh [--check-only] [--no-op]
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
installer=$repo_root/scripts/install-worker-controller.sh
state_file=${CI_FLEET_REMOTE_STATE_FILE:-/var/lib/ci-fleet/install-state.json}
rendered_env=${CI_FLEET_RENDERED_ENV:-/etc/ci-fleet/ci-fleet.env}
host_env=${CI_FLEET_HOST_ENV:-/etc/ci-fleet/host.env}
token_script=$script_dir/github-app-token.sh
reconcile_state_dir=${CI_FLEET_RECONCILE_STATE_DIR:-/var/lib/ci-fleet/reconcile}
reconcile_state_file=$reconcile_state_dir/state.json
lkg_dir=${CI_FLEET_LKG_DIR:-/var/lib/ci-fleet/last-known-good}
temp_dir=$(mktemp -d) || exit 2
# shellcheck disable=SC2317 # cleanup_temp is invoked indirectly via trap
cleanup_temp() { rm -rf "$temp_dir"; }
trap cleanup_temp EXIT

mode=reconcile  # reconcile or check-only
no_op=false
max_attempts=${CI_FLEET_RECONCILE_MAX_ATTEMPTS:-3}

usage() {
  cat >&2 <<'EOF'
usage:
  remote-reconcile.sh [--check-only] [--no-op]

  Fetches the desired-state repository at the current default-branch HEAD,
  validates it, and reconciles the controller if a newer commit is available.

  --check-only  Validate and report without reconciling.
  --no-op       Log what would be done without side effects.
EOF
}

while (($#)); do
  case "$1" in
    --check-only) mode=check-only ;;
    --no-op) no_op=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

note() { printf 'RECONCILE %s\n' "$*"; }
die() {
  printf 'RECONCILE_ERROR: %s\n' "$*" >&2
  save_reconcile_state 'failed' "${2:-}" "${3:-}" "${4:-}" "${5:-$*}"
  exit 2
}

require_commands() {
  local cmd
  for cmd in git python3 openssl curl cmp flock; do
    command -v "$cmd" >/dev/null || die "$cmd is required"
  done
}

# --- State persistence ---

save_reconcile_state() {
  local status=${1:-} desired_commit=${2:-} applied_commit=${3:-} health=${4:-} message=${5:-}
  install -d -m 0700 "$reconcile_state_dir"
  python3 - "$reconcile_state_file" "$status" "$desired_commit" "$applied_commit" "$health" "$message" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile

path = sys.argv[1]
state = {
    "status": sys.argv[2],
    "desired_commit": sys.argv[3] or "",
    "applied_commit": sys.argv[4] or "",
    "health": sys.argv[5] or "",
    "message": sys.argv[6] or "",
    "checked_at": int(__import__("time").time()),
}
fd, tmp = tempfile.mkstemp(prefix=".reconcile-state.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except:
    os.unlink(tmp, missing_ok=True)
    raise
PY
}

load_installed_state() {
  installed_config_repo=
  installed_config_ref=
  installed_controller=
  if [[ ! -f "$state_file" ]]; then
    return 1
  fi
  local values
  if ! values=$(python3 - "$state_file" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("config_repository", "config_ref", "controller"):
    print(state[key])
PY
  ); then
    return 1
  fi
  mapfile -t vals <<<"$values"
  [[ ${#vals[@]} == 3 ]] || return 1
  installed_config_repo=${vals[0]}
  installed_config_ref=${vals[1]}
  installed_controller=${vals[2]}
}

# --- GitHub App token ---

generate_token() {
  local env_file=$1
  "$token_script" --env-file "$env_file" 2>"$temp_dir/token_err" || {
    local err
    err=$(<"$temp_dir/token_err")
    [[ -n "$err" ]] || err="unknown error"
    printf '%s' "$err" >"$temp_dir/last_token_err"
    return 1
  }
}

# --- Remote fetch ---

fetch_remote_config() {
  local repo=$1 token=$2
  local fetch_dir=$temp_dir/config-repo
  mkdir -p "$fetch_dir"
  git init -q "$fetch_dir"
  # Use auth_url with embedded token for authenticated fetch
  local auth_url="https://x-access-token:${token}@github.com/${repo}.git"
  GIT_TERMINAL_PROMPT=0 git -C "$fetch_dir" fetch -q --filter=blob:none --depth=1 origin HEAD 2>"$temp_dir/fetch_err" || {
    local err
    # Retry with auth_url if plain fetch failed (private repo needs auth)
    GIT_TERMINAL_PROMPT=0 git -C "$fetch_dir" fetch -q --filter=blob:none --depth=1 "$auth_url" HEAD 2>"$temp_dir/fetch_err" || {
      local err
      err=$(<"$temp_dir/fetch_err")
      [[ -n "$err" ]] || err="fetch failed"
      printf '%s' "$err" >"$temp_dir/last_fetch_err"
      return 1
    }
  }
  local resolved
  resolved=$(git -C "$fetch_dir" rev-parse 'FETCH_HEAD^{commit}') || die "cannot resolve FETCH_HEAD"
  printf '%s' "$resolved"
}

validate_config() {
  local checkout_dir=$1 commit=$2
  git -C "$checkout_dir" show "$commit:fleet.json" >"$temp_dir/fleet.json" 2>/dev/null || return 1
  git -C "$checkout_dir" ls-tree -rz --name-only "$commit" >"$temp_dir/tree-paths" 2>/dev/null || return 1

  # Validate using the installer's validation chain
  # Also run the template validator with --strict + --tree-paths (like installer does)
  python3 "$repo_root/scripts/desired_state.py" validate --config "$temp_dir/fleet.json" 2>"$temp_dir/validate_err" || {
    local err
    err=$(<"$temp_dir/validate_err")
    [[ -n "$err" ]] || err="validation failed"
    log_json "ERROR" "validation" "config validation failed"
    return 1
  }
  python3 "$repo_root/templates/config-repository/scripts/validate.py" \
    --config "$temp_dir/fleet.json" --strict --tree-paths "$temp_dir/tree-paths" 2>"$temp_dir/strict_err" || {
    local err
    err=$(<"$temp_dir/strict_err")
    [[ -n "$err" ]] || err="strict validation failed"
    log_json "ERROR" "validation" "strict validation rejected"
    return 1
  }

  # Secret scan
  python3 "$repo_root/scripts/scan_committed_secrets.py" \
    --repository "$checkout_dir" --commit "$commit" 2>"$temp_dir/scan_err" || {
    local err
    err=$(<"$temp_dir/scan_err")
    [[ -n "$err" ]] || err="secret scan failed"
    log_json "ERROR" "secrets" "secret scan rejected"
    return 1
  }

  return 0
}

# Restore LKG by re-applying the known-good config via the installer
# with the durable config_repo identity, not a temp checkout path.
apply_lkg() {
  note "ROLLING_BACK_TO_LKG"
  if [[ "$no_op" == true ]]; then
    note "NO_OP would restore last-known-good"
    return
  fi

  local lkg_ref lkg_repo lkg_controller
  mapfile -t lkg_vals <<<"$(python3 - "$lkg_dir/metadata.json" <<'PY' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("config_ref", "config_repository", "controller"):
    print(d.get(k, ""))
PY
)"
  [[ ${#lkg_vals[@]} == 3 && -n "${lkg_vals[0]}" ]] || { log_json "ERROR" "rollback" "LKG metadata incomplete"; return 1; }
  lkg_ref=${lkg_vals[0]}
  lkg_repo=${lkg_vals[1]}
  lkg_controller=${lkg_vals[2]}

  # Re-apply the LKG ref via the installer
  # Create a checkout containing the LKG ref (may differ from fetched HEAD)
  local lkg_pinned=$temp_dir/lkg-pinned
  mkdir -p "$lkg_pinned"
  git init -q "$lkg_pinned"
  # Use the reconciliation token for authenticated fetch
  local lkg_token
  lkg_token=$(cat "$temp_dir/reconcile-token" 2>/dev/null || echo "")
  if [[ -n "$lkg_token" ]]; then
    GIT_TERMINAL_PROMPT=0 git -C "$lkg_pinned" fetch -q --depth=1 \
      "https://x-access-token:${lkg_token}@github.com/${lkg_repo}.git" "$lkg_ref" 2>"$temp_dir/lkg_fetch_err" || {
      log_json "ERROR" "rollback" "LKG fetch failed"
      return 1
    }
  else
    GIT_TERMINAL_PROMPT=0 git -C "$lkg_pinned" fetch -q --depth=1 origin "$lkg_ref" 2>"$temp_dir/lkg_fetch_err" || {
      log_json "ERROR" "rollback" "LKG fetch failed"
      return 1
    }
  fi
  git -C "$lkg_pinned" checkout -q FETCH_HEAD

  CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --upgrade \
    --config-repo "$lkg_pinned" \
    --config-identity "$lkg_repo" \
    --ref "$lkg_ref" \
    --controller "$lkg_controller" 2>"$temp_dir/rollback_err" && {
    log_json "WARN" "rollback" "restored last-known-good"
    return 0
  }
  local err
  err=$(<"$temp_dir/rollback_err")
  log_json "ERROR" "rollback" "rollback failed: ${err}"
  return 1
}

save_lkg() {
  local commit=$1
  install -d -m 0700 "$lkg_dir"
  python3 - "$lkg_dir/metadata.json" "$installed_config_repo" "$commit" "$installed_controller" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile

path = sys.argv[1]
meta = {
    "config_repository": sys.argv[2],
    "config_ref": sys.argv[3],
    "controller": sys.argv[4],
    "saved_at": int(__import__("time").time()),
}
fd, tmp = tempfile.mkstemp(prefix=".lkg-meta.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except:
    os.unlink(tmp, missing_ok=True)
    raise
PY
}

# --- Logging (sanitized, no secrets) ---
# message passed via argv, never interpolated into Python source

log_json() {
  local level=$1 component=$2 message=$3
  python3 - "$level" "$component" "$message" "$installed_controller" <<'PY' 2>/dev/null || true
import json, sys, time
level, component, message, controller = sys.argv[1:]
print(json.dumps({
    'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'level': level,
    'component': component,
    'message': message,
    'controller': controller,
}))
PY
}

# --- Health (with rendered env) ---

run_health_check() {
  local output=$1
  (
    set -a
    # shellcheck disable=SC1090
    [[ ! -f "$rendered_env" ]] || . "$rendered_env"
    set +a
    python3 "$repo_root/scripts/health.py" local --output "$output" 2>/dev/null
  ) || true
  python3 -c "import json; print(json.load(open('$output'))['status'])" 2>/dev/null || echo "unknown"
}

# --- Main ---

require_commands

# Serialize with installer mutations — share the installer's lock
lock_file=${CI_FLEET_INSTALLER_LOCK:-/run/ci-fleet-installer.lock}
install -d -m 0755 "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock -n 9 || die "another reconcile or installer is already running"

# Load installed state
load_installed_state || die "no installed state found at $state_file"
note "INSTALLED controller=${installed_controller} config_repo=${installed_config_repo} config_ref=${installed_config_ref}"

# Prefer host.env for token generation; fall back to rendered_env
token_env=
if [[ -f "$host_env" ]]; then
  token_env=$host_env
elif [[ -f "$rendered_env" ]]; then
  token_env=$rendered_env
else
  die "no host environment file found for token generation"
fi

attempt=0
while ((attempt < max_attempts)); do
  attempt=$((attempt + 1))

  # Generate token — retry on transient failure
  note "GENERATING_TOKEN attempt=${attempt}"
  token=$(generate_token "$token_env") || {
    err=$(cat "$temp_dir/last_token_err" 2>/dev/null || echo "unknown")
    note "TOKEN_FAILED attempt=${attempt} error=${err}"
    ((attempt < max_attempts)) && { sleep 5; continue; }
    die "token generation exhausted after ${max_attempts} attempts"
  }
  printf '%s' "$token" >"$temp_dir/reconcile-token"

  # Fetch remote config
  note "FETCHING_CONFIG repo=${installed_config_repo}"
  desired_commit=$(fetch_remote_config "$installed_config_repo" "$token") || {
    note "FETCH_FAILED attempt=${attempt}"
    ((attempt < max_attempts)) && { sleep 5; continue; }
    die "fetch exhausted after ${max_attempts} attempts"
  }
  break
done

note "RESOLVED desired_ref=${desired_commit}"

# Compare with installed
if [[ "$desired_commit" == "$installed_config_ref" ]]; then
  # Same commit — just verify convergence
  note "NO_CHANGE desired=${desired_commit}"
  if [[ "$no_op" == true ]]; then
    note "NO_OP would run drift check"
    save_reconcile_state 'converged' "$desired_commit" "$installed_config_ref" 'healthy' 'no change, converged'
    exit 0
  fi

  # Run drift check using the fetched local checkout
  local_pinned=$temp_dir/config-repo
  if [[ -d "$local_pinned/.git" ]]; then
    if CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --check \
      --config-repo "$local_pinned" \
      --config-identity "$installed_config_repo" \
      --ref "$installed_config_ref" \
      --controller "$installed_controller" 2>"$temp_dir/drift_err"; then
      note "CONVERGED controller=${installed_controller} config_ref=${installed_config_ref}"
      save_reconcile_state 'converged' "$desired_commit" "$installed_config_ref" 'healthy' 'no change, converged'
      exit 0
    fi
  fi
  # Same commit + drift is still drift: check-only reports it, normal mode repairs it.
  if [[ "$mode" == check-only ]]; then
    save_reconcile_state 'drift' "$desired_commit" "$installed_config_ref" 'drift' 'internal drift detected'
    exit 3
  fi
  note "DRIFT falling through to reconcile"
fi

# New commit or drift — validate and reconcile
fetch_dir=$temp_dir/config-repo
if ! validate_config "$fetch_dir" "$desired_commit"; then
  log_json "ERROR" "validation" "desired config commit rejected by validation"
  note "INVALID_CONFIG commit=${desired_commit}"
  save_reconcile_state 'invalid' "$desired_commit" "$installed_config_ref" 'healthy' "config rejected at ${desired_commit}"
  if [[ "$installed_config_ref" != "$desired_commit" ]]; then
    save_lkg "$installed_config_ref"
  fi
  exit 3
fi

note "VALIDATED commit=${desired_commit}"

if [[ "$mode" == check-only ]]; then
  note "CHECK_ONLY would reconcile to ${desired_commit}"
  save_reconcile_state 'pending' "$desired_commit" "$installed_config_ref" 'healthy' "would reconcile to ${desired_commit}"
  exit 0
fi

if [[ "$no_op" == true ]]; then
  note "NO_OP would reconcile to ${desired_commit}"
  save_reconcile_state 'pending' "$desired_commit" "$installed_config_ref" 'healthy' "would reconcile to ${desired_commit}"
  exit 0
fi

# Save LKG before reconciling
save_lkg "$installed_config_ref"

# Create a pinned local checkout for the installer.
# Keep the durable repository identity while the installer reads the fetched checkout.
pinned_dir=$temp_dir/pinned-config
cp -a "$fetch_dir" "$pinned_dir"
git -C "$pinned_dir" checkout -q "$desired_commit"

# Reconcile
note "RECONCILING controller=${installed_controller} config_ref=${desired_commit}"
if CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --upgrade \
  --config-repo "$pinned_dir" \
  --config-identity "$installed_config_repo" \
  --ref "$desired_commit" \
  --controller "$installed_controller" 2>"$temp_dir/upgrade_err"; then
  note "RECONCILED controller=${installed_controller} config_ref=${desired_commit}"

  # Save new LKG
  save_lkg "$desired_commit"

  # Run health check
  health_status=$(run_health_check "$temp_dir/health.json")

  save_reconcile_state 'converged' "$desired_commit" "$desired_commit" "$health_status" "reconciled to ${desired_commit}"
  note "RECONCILE_OK controller=${installed_controller} desired=${desired_commit} applied=${desired_commit} health=${health_status}"
  exit 0
else
  upg_err=$(<"$temp_dir/upgrade_err")
  note "RECONCILE_FAILED error=${upg_err:-unknown}"

  # Rollback to LKG — reinstalls a checkpoint of this attempt was already created,
  # or safely restores LKG config directly via the installer
  apply_lkg || die "rollback to last-known-good also failed"
  health_status=$(run_health_check "$temp_dir/health.json")
  save_reconcile_state 'rolled_back' "$desired_commit" "$installed_config_ref" "$health_status" "reconciled failed, rolled back to ${installed_config_ref}"
  note "ROLLBACK_OK controller=${installed_controller} restored=${installed_config_ref}"
  exit 3
fi
