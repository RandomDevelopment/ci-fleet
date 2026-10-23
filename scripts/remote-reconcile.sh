#!/usr/bin/env bash
# Remote Git-authored configuration reconciliation for ci-fleet controllers.
#
# Fetches the private desired-state repository using a short-lived GitHub App
# installation token, resolves the default-branch HEAD to an immutable commit,
# validates the configuration, checks for drift, and reconciles if needed.
#
# Usage:
#   remote-reconcile.sh [--check-only] [--installed-ref] [--no-op]
set -Eeuo pipefail
set +x
export PYTHONDONTWRITEBYTECODE=1

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
installer=$repo_root/scripts/install-worker-controller.sh
state_file=${CI_FLEET_REMOTE_STATE_FILE:-/var/lib/ci-fleet/install-state.json}
rendered_env=${CI_FLEET_RENDERED_ENV:-/etc/ci-fleet/ci-fleet.env}
host_env=${CI_FLEET_HOST_ENV:-/etc/ci-fleet/host.env}
config_review_root=${CI_FLEET_ROOT_PREFIX:-}
[[ -z "$config_review_root" || ${CI_FLEET_TESTING:-0} == 1 ]] || { echo 'ERROR: root prefix is test-only' >&2; exit 2; }
config_review_policy=$config_review_root/etc/ci-fleet/config-review-policy.json
config_review_required=$config_review_root/etc/ci-fleet/config-review-required
token_script=$script_dir/github-app-token.sh
reconcile_state_dir=${CI_FLEET_RECONCILE_STATE_DIR:-/var/lib/ci-fleet/reconcile}
reconcile_state_file=$reconcile_state_dir/state.json
lkg_dir=${CI_FLEET_LKG_DIR:-/var/lib/ci-fleet/last-known-good}

mode=reconcile  # reconcile or check-only
no_op=false
installed_ref=false
explicit_ref=
max_attempts=${CI_FLEET_RECONCILE_MAX_ATTEMPTS:-3}
# ponytail: one-hour queue cap; align with any future installer transaction timeout.
lock_wait_seconds=3600

usage() {
  cat >&2 <<'EOF'
usage:
  remote-reconcile.sh [--check-only] [--installed-ref] [--no-op]

  Fetches the desired-state repository at the current default-branch HEAD,
  validates it, and reconciles the controller if a newer commit is available.

  --ref SHA     Mutating opt-in drained recovery at one full lowercase 40-hex
                commit; unchanged controller/fleet policy and runtime engine only.
                Incompatible with --check-only, --installed-ref and --no-op.
  --check-only  Validate and report without reconciling.
  --installed-ref Check the locked installation's commit (check-only only).
  --no-op       Log what would be done without side effects.
EOF
}

while (($#)); do
  case "$1" in
    --ref)
      [[ $# -ge 2 && -z "$explicit_ref" && "$2" =~ ^[0-9a-f]{40}$ ]] || { echo 'ERROR: --ref requires one full lowercase commit SHA' >&2; exit 2; }
      explicit_ref=$2; shift ;;
    --check-only) mode=check-only ;;
    --installed-ref) installed_ref=true ;;
    --no-op) no_op=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done
[[ -z "$explicit_ref" || ( "$installed_ref" == false && "$no_op" == false && "$mode" == reconcile ) ]] || { echo 'ERROR: --ref is incompatible with --installed-ref, --check-only and --no-op' >&2; exit 2; }
[[ "$installed_ref" == false || "$mode" == check-only ]] || { echo 'ERROR: --installed-ref requires --check-only' >&2; exit 2; }

temp_dir=$(mktemp -d) || exit 2
transaction_result=$temp_dir/transaction-result.json
: >"$transaction_result"
chmod 0600 "$transaction_result"
exec 7>"$transaction_result"
# shellcheck disable=SC2317 # cleanup_temp is invoked indirectly via trap
cleanup_temp() { rm -rf "$temp_dir"; }
trap cleanup_temp EXIT

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
  local status=${1:-} desired_commit=${2:-} applied_commit=${3:-} health=${4:-} message=${5:-} mark_success=${6:-false}
  local transaction_phase=${7:-} failure_reason=${8:-}
  install -d -m 0700 "$reconcile_state_dir"
  python3 - "$reconcile_state_file" "$status" "$desired_commit" "$applied_commit" "$health" "$message" "$mark_success" "$transaction_phase" "$failure_reason" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile

path = sys.argv[1]
now = int(__import__("time").time())
try:
    previous = json.load(open(path, encoding="utf-8"))
    if not isinstance(previous, dict):
        raise ValueError("reconciliation state must be an object")
    last_success_at = previous.get("last_success_at")
    if not isinstance(last_success_at, int) or last_success_at < 0:
        last_success_at = None
except (OSError, ValueError, TypeError):
    last_success_at = None
if sys.argv[7] == "true":
    last_success_at = now
state = {
    "status": sys.argv[2],
    "desired_commit": sys.argv[3] or "",
    "applied_commit": sys.argv[4] or "",
    "health": sys.argv[5] or "",
    "message": sys.argv[6] or "",
    "checked_at": now,
    "last_success_at": last_success_at,
    "transaction_phase": sys.argv[8],
    "failure_reason": sys.argv[9],
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
  if [[ ! -f "$state_file" ]]; then
    return 1
  fi
  local values
  if ! values=$(python3 - "$state_file" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("config_repository", "config_ref", "controller", "controller_state"):
    print(state[key])
PY
  ); then
    return 1
  fi
  mapfile -t vals <<<"$values"
  [[ ${#vals[@]} == 4 ]] || return 1
  installed_config_repo=${vals[0]}
  installed_config_ref=${vals[1]}
  installed_controller=${vals[2]}
  installed_controller_state=${vals[3]}
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
  local repo=$1 token=$2 ref=${3:-HEAD}
  local fetch_dir=$temp_dir/config-repo
  mkdir -p "$fetch_dir"
  git init -q "$fetch_dir"
  # Use auth_url with embedded token for authenticated fetch
  local auth_url="https://x-access-token:${token}@github.com/${repo}.git"
  GIT_TERMINAL_PROMPT=0 git -C "$fetch_dir" fetch -q --filter=blob:none --depth=2 origin "$ref" 2>"$temp_dir/fetch_err" || {
    local err
    # Retry with auth_url if plain fetch failed (private repo needs auth)
    GIT_TERMINAL_PROMPT=0 git -C "$fetch_dir" fetch -q --filter=blob:none --depth=2 "$auth_url" "$ref" 2>"$temp_dir/fetch_err" || {
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
  local args=(--config "$temp_dir/fleet.json" --strict --tree-paths "$temp_dir/tree-paths")
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
  if git -C "$checkout_dir" show "$commit:engine-rollout-evidence.json" >"$temp_dir/rollout-evidence.json" 2>/dev/null; then
    args+=(--rollout-evidence "$temp_dir/rollout-evidence.json")
  fi
  if git -C "$checkout_dir" show "$commit^:fleet.json" >"$temp_dir/previous-fleet.json" 2>/dev/null \
    && python3 -c 'import json, sys; current, previous=(json.load(open(path)) for path in sys.argv[1:]); sys.exit(0 if isinstance(current, dict) and isinstance(previous, dict) and ("managed_images" not in current or "managed_images" not in previous) else 1)' "$temp_dir/fleet.json" "$temp_dir/previous-fleet.json"; then
    args+=(--previous-config "$temp_dir/previous-fleet.json")
    if git -C "$checkout_dir" show "$commit^:engine-rollout-evidence.json" >"$temp_dir/previous-evidence.json" 2>/dev/null; then
      args+=(--previous-rollout-evidence "$temp_dir/previous-evidence.json")
    fi
  fi
  python3 "$repo_root/templates/config-repository/scripts/validate.py" "${args[@]}" 2>"$temp_dir/strict_err" || {
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

  local lkg_ref lkg_repo lkg_controller lkg_status=0 outcome
  local rollback_metadata=$lkg_dir/metadata.json
  [[ -z "$explicit_ref" ]] || rollback_metadata=$temp_dir/recovery-lkg.json
  mapfile -t lkg_vals <<<"$(python3 - "$rollback_metadata" <<'PY' 2>/dev/null || true
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

  exec 7>"$transaction_result"
  CI_FLEET_TRANSACTION_RESULT_FD=7 CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --upgrade \
    --restore-last-known-good \
    --config-repo "$lkg_pinned" \
    --config-identity "$lkg_repo" \
    --ref "$lkg_ref" \
    --controller "$lkg_controller" 2>"$temp_dir/rollback_err" || lkg_status=$?
  outcome=$(transaction_outcome)
  if ((lkg_status == 0)) && [[ "$outcome" == applied ]]; then
    log_json "WARN" "rollback" "restored last-known-good"
    return 0
  fi
  local err
  err=$(<"$temp_dir/rollback_err")
  log_json "ERROR" "rollback" "rollback failed: ${err}"
  return 1
}

save_lkg() {
  local commit=$1 replace=${2:-false}
  install -d -m 0700 "$lkg_dir" || return 1
  python3 - "$lkg_dir/metadata.json" "$installed_config_repo" "$commit" "$installed_controller" "$replace" <<'PY' 2>/dev/null
import json, os, stat, sys, tempfile

path = sys.argv[1]
replace = sys.argv[5] == "true"
meta = {
    "config_repository": sys.argv[2],
    "config_ref": sys.argv[3],
    "controller": sys.argv[4],
    "saved_at": int(__import__("time").time()),
}
try:
    metadata = os.lstat(path)
    current = json.load(open(path, encoding="utf-8"))
except (OSError, TypeError, ValueError):
    metadata = None
    current = None
valid = (
    metadata is not None
    and stat.S_ISREG(metadata.st_mode)
    and not stat.S_ISLNK(metadata.st_mode)
    and metadata.st_uid == os.getuid()
    and stat.S_IMODE(metadata.st_mode) == 0o600
    and isinstance(current, dict)
    and set(current) == set(meta)
    and all(isinstance(current[key], str) and current[key] for key in ("config_repository", "config_ref", "controller"))
    and type(current["saved_at"]) is int
    and current["saved_at"] >= 0
)
if valid and (
    not replace
    or all(current[key] == meta[key] for key in ("config_repository", "config_ref", "controller"))
):
    raise SystemExit(0)
fd, tmp = tempfile.mkstemp(prefix=".lkg-meta.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with open(path, encoding="utf-8") as handle:
        if json.load(handle) != meta:
            raise OSError("LKG persistence readback mismatch")
except:
    os.unlink(tmp, missing_ok=True)
    raise
PY
}

lkg_readback_matches() {
  python3 -c '
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if [value.get(key) for key in ("config_repository", "config_ref", "controller")] != sys.argv[2:]:
    raise SystemExit(1)
' "$lkg_dir/metadata.json" "$installed_config_repo" "$1" "$installed_controller"
}

transaction_outcome() {
  python3 - "$transaction_result" "${1:-outcome}" <<'PY' 2>/dev/null || printf 'unknown\n'
import json, sys
def unique_fields(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate result field")
    return value

raw = open(sys.argv[1], encoding="utf-8").read(4097)
if len(raw) > 4096:
    raise SystemExit(1)
lines = raw.splitlines()
if len(lines) != 1:
    raise SystemExit(1)
value = json.loads(lines[0], object_pairs_hook=unique_fields)
if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
    raise SystemExit(1)
if value.get("outcome") == "rejected":
    if (set(value) != {"schema_version", "outcome", "phase", "reason"}
            or value["phase"] != "pre_transaction"
            or value["reason"] not in {"docker_disk_capacity", "installer_precondition"}):
        raise SystemExit(1)
elif set(value) != {"schema_version", "outcome"} or value["outcome"] not in {"applied", "rollback_verified", "rollback_unverified"}:
    raise SystemExit(1)
print(value.get(sys.argv[2], "unknown"))
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
  local output=$1 probe_status=0
  # Every invocation must produce its own report, including rollback probes.
  if ! rm -f -- "$output"; then
    printf 'unknown\n'
    return
  fi
  (
    set -a
    # shellcheck disable=SC1090
    [[ ! -f "$rendered_env" ]] || . "$rendered_env"
    set +a
    export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1
    python3 "$repo_root/scripts/health.py" local --output "$output" >/dev/null 2>&1
  ) || probe_status=$?
  # health.py uses 0 for healthy/maintenance, 1 for warning, 2 for
  # unhealthy. A nonzero warning is valid only with its fresh warning report.
  python3 - "$output" "$probe_status" <<'PY' 2>/dev/null || printf 'unknown\n'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
status = value["status"]
expected = {"healthy": 0, "maintenance": 0, "warning": 1, "unhealthy": 2}[status]
if int(sys.argv[2]) != expected:
    raise SystemExit(1)
print(status)
PY
}

verify_explicit_rollback_health() {
  if [[ -n "$explicit_ref" ]]; then
    [[ ${1:-unknown} == maintenance || ${1:-unknown} == warning ]] || return 1
    load_installed_state || return 1
    [[ "$installed_config_ref" == "$restored_config_ref" && "$installed_controller_state" == drained ]] || return 1
    python3 "$script_dir/explicit_recovery.py" "$temp_dir/recovery-baseline.json" "$temp_dir/recovery-baseline.json" \
      "$state_file" "$config_review_root/opt/ci-fleet/current" "$temp_dir/nonexistent-lkg" || return 1
  fi
  [[ -z "$explicit_ref" ]] || python3 - "$temp_dir/health.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("desired_state") != "drained" or value.get("status") not in {"maintenance", "warning"}:
    raise SystemExit(1)
PY
}

# --- Main ---

require_commands

# Serialize with installer mutations — share the installer's lock
lock_file=${CI_FLEET_INSTALLER_LOCK:-/run/ci-fleet-installer.lock}
install -d -m 0755 "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock -w "$lock_wait_seconds" 9 || die "timed out waiting for another reconcile or installer"

# Load installed state
load_installed_state || die "no installed state found at $state_file"
[[ "$installed_config_ref" =~ ^[0-9a-f]{40}$ ]] || die "installed config ref is not a full lowercase commit SHA"
fetch_ref=HEAD
if [[ -n "$explicit_ref" ]]; then
  [[ "$installed_controller_state" == drained ]] || die 'explicit recovery requires a drained installation'
  fetch_ref=$explicit_ref
fi
[[ "$installed_ref" == false ]] || fetch_ref=$installed_config_ref
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
  desired_commit=$(fetch_remote_config "$installed_config_repo" "$token" "$fetch_ref") || {
    note "FETCH_FAILED attempt=${attempt}"
    ((attempt < max_attempts)) && { sleep 5; continue; }
    die "fetch exhausted after ${max_attempts} attempts"
  }
  break
done

note "RESOLVED desired_ref=${desired_commit}"
if [[ -n "$explicit_ref" ]]; then
  [[ "$desired_commit" == "$explicit_ref" ]] || die 'explicit ref fetch identity mismatch'
  git -C "$temp_dir/config-repo" show "$desired_commit:fleet.json" >"$temp_dir/recovery-candidate.json" || die 'candidate unavailable'
  fetch_remote_config "$installed_config_repo" "$token" "$installed_config_ref" >/dev/null || die 'drained baseline unavailable'
  git -C "$temp_dir/config-repo" show "$installed_config_ref:fleet.json" >"$temp_dir/recovery-baseline.json" || die 'baseline unavailable'
  python3 "$script_dir/explicit_recovery.py" "$temp_dir/recovery-candidate.json" "$temp_dir/recovery-baseline.json" \
    "$state_file" "$config_review_root/opt/ci-fleet/current" "$lkg_dir/metadata.json" || die 'explicit recovery policy rejected'
fi

if [[ -e "$config_review_policy" || -L "$config_review_policy" || -e "$config_review_required" || -L "$config_review_required" ]]; then
  desired_tree=$(git -C "$temp_dir/config-repo" rev-parse "$desired_commit^{tree}") || die 'configuration tree is unavailable'
  if ! printf '%s' "$token" | python3 "$script_dir/verify_config_approval.py" --policy "$config_review_policy" \
    --repository "$installed_config_repo" --commit "$desired_commit" --tree "$desired_tree" --installed-state "$state_file"; then
    save_reconcile_state 'invalid' "$desired_commit" "$installed_config_ref" 'unknown' 'configuration approval rejected'
    exit 3
  fi
fi

# Compare with installed
if [[ "$desired_commit" == "$installed_config_ref" && -z "$explicit_ref" ]]; then
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
      save_reconcile_state 'converged' "$desired_commit" "$installed_config_ref" 'healthy' 'no change, converged' true
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

# Preserve an existing accepted LKG; bootstrap it only when absent or invalid.
save_lkg "$installed_config_ref" || die 'cannot persist baseline LKG'
if [[ -n "$explicit_ref" ]]; then
  lkg_readback_matches "$installed_config_ref" || die 'drained baseline LKG readback rejected'
  cp "$lkg_dir/metadata.json" "$temp_dir/recovery-lkg.json" || die 'cannot retain verified recovery baseline'
fi
restored_config_ref=$(python3 - "$lkg_dir/metadata.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
ref = value.get("config_ref")
if not isinstance(ref, str) or not ref:
    raise SystemExit(1)
print(ref)
PY
) || die 'LKG metadata incomplete'

# Create a pinned local checkout for the installer.
# Keep the durable repository identity while the installer reads the fetched checkout.
pinned_dir=$temp_dir/pinned-config
cp -a "$fetch_dir" "$pinned_dir"
git -C "$pinned_dir" checkout -q "$desired_commit"

# Reconcile
note "RECONCILING controller=${installed_controller} config_ref=${desired_commit}"
save_reconcile_state 'reconciling' "$desired_commit" "$installed_config_ref" 'unknown' "reconciling to ${desired_commit}"
upgrade_status=0
if CI_FLEET_TRANSACTION_RESULT_FD=7 CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --upgrade \
  --config-repo "$pinned_dir" \
  --config-identity "$installed_config_repo" \
  --ref "$desired_commit" \
  --controller "$installed_controller" 2>"$temp_dir/upgrade_err"; then
  note "RECONCILED controller=${installed_controller} config_ref=${desired_commit}"
  [[ -z "$explicit_ref" || $(transaction_outcome) == applied ]] || die 'explicit recovery apply outcome unverified'

  # Run health check
  save_reconcile_state 'converged' "$desired_commit" "$desired_commit" 'unknown' "reconciled to ${desired_commit}; checking health"
  load_installed_state || installed_controller_state=unknown
  if [[ -n "$explicit_ref" ]]; then
    [[ "$installed_config_ref" == "$desired_commit" && "$installed_controller_state" == drained ]] || die 'explicit recovery installed state readback rejected'
    python3 "$script_dir/explicit_recovery.py" "$temp_dir/recovery-candidate.json" "$temp_dir/recovery-candidate.json" \
      "$state_file" "$config_review_root/opt/ci-fleet/current" "$temp_dir/nonexistent-lkg" || die 'explicit recovery runtime readback rejected'
  fi
  health_status=$(run_health_check "$temp_dir/health.json")
  health_desired_state=$(python3 - "$temp_dir/health.json" <<'PY' 2>/dev/null || echo unknown
import json, sys
desired_state = json.load(open(sys.argv[1], encoding="utf-8"))["desired_state"]
if desired_state not in {"active", "drained", "disabled"}:
    raise ValueError("unknown desired state")
print(desired_state)
PY
  )

  case "$installed_controller_state:$health_desired_state:$health_status" in
    active:active:healthy|active:active:warning|drained:drained:maintenance|drained:drained:warning|disabled:disabled:maintenance|disabled:disabled:warning) ;;
    *)
      save_reconcile_state 'failed' "$desired_commit" "$desired_commit" "$health_status" "reconciled to ${desired_commit}; final health rejected"
      apply_lkg || die "final health was ${health_status}; rollback to last-known-good also failed"
      rollback_health_status=$(run_health_check "$temp_dir/health.json")
      verify_explicit_rollback_health "$rollback_health_status" || die 'drained rollback health unverified'
      save_reconcile_state 'rolled_back' "$desired_commit" "$restored_config_ref" "$rollback_health_status" "final health ${health_status} for ${health_desired_state} rejected for applied state ${installed_controller_state}; rolled back to ${restored_config_ref}"
      note "ROLLBACK_OK controller=${installed_controller} restored=${restored_config_ref}"
      exit 3
      ;;
  esac

  if ! { save_lkg "$desired_commit" true && lkg_readback_matches "$desired_commit"; }; then
    save_reconcile_state 'failed' "$desired_commit" "$desired_commit" "$health_status" 'candidate LKG persistence failed'
    if [[ -n "$explicit_ref" ]]; then
      # Candidate promotion may already have replaced the live record before a
      # readback failure. Restore the retained, verified record atomically before
      # the installer consults it for approval AND image-free checkpoint lookup.
      python3 - "$temp_dir/recovery-lkg.json" "$lkg_dir/metadata.json" <<'PY' || die 'baseline metadata restoration failed'
import os, sys, tempfile
source, target = sys.argv[1:]
original = open(source, "rb").read()
fd, temporary = tempfile.mkstemp(prefix=".lkg-restore.", dir=os.path.dirname(target))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    if open(target, "rb").read() != original:
        raise OSError("baseline restoration readback mismatch")
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
      lkg_readback_matches "$restored_config_ref" || die 'baseline metadata restoration readback rejected'
      apply_lkg || die 'LKG persistence failed; drained baseline rollback failed'
      rollback_health_status=$(run_health_check "$temp_dir/health.json")
      verify_explicit_rollback_health "$rollback_health_status" || die 'drained rollback health unverified'
      [[ "$rollback_health_status" == maintenance || "$rollback_health_status" == warning ]] || die 'LKG persistence failed; rollback health unverified'
      save_reconcile_state 'rolled_back' "$desired_commit" "$restored_config_ref" "$rollback_health_status" 'LKG persistence failed; restored drained baseline'
    fi
    exit 3
  fi
  save_reconcile_state 'converged' "$desired_commit" "$desired_commit" "$health_status" "reconciled to ${desired_commit}" true
  note "RECONCILE_OK controller=${installed_controller} desired=${desired_commit} applied=${desired_commit} health=${health_status}"
  exit 0
else
  upgrade_status=$?
  upg_err=$(<"$temp_dir/upgrade_err")
  note "RECONCILE_FAILED error=${upg_err:-unknown}"
  outcome=$(transaction_outcome)
  if [[ "$upgrade_status" == 2 && "$outcome" == rejected ]]; then
    rejection_reason=$(transaction_outcome reason)
    save_reconcile_state 'failed' "$desired_commit" "$installed_config_ref" 'unknown' "reconcile rejected before transaction: ${rejection_reason}" false pre_transaction "$rejection_reason"
    note "REJECTED phase=pre_transaction reason=${rejection_reason}"
    exit 3
  fi
  [[ "$outcome" != rejected ]] || outcome=unknown
  if [[ "$outcome" == applied ]]; then
    save_reconcile_state 'failed' "$desired_commit" "$desired_commit" 'unknown' "reconcile failed after apply: ${upg_err:-unknown}"
    exit 3
  fi
  if [[ "$upgrade_status" == 20 && "$outcome" == rollback_verified ]]; then
    health_status=$(run_health_check "$temp_dir/health.json")
    verify_explicit_rollback_health "$health_status" || die 'installer drained rollback readback unverified'
    save_reconcile_state 'rolled_back' "$desired_commit" "$installed_config_ref" "$health_status" "reconcile failed; installer verified rollback to ${installed_config_ref}"
    note "ROLLBACK_OK controller=${installed_controller} restored=${installed_config_ref}"
    exit 3
  fi
  save_reconcile_state 'failed' "$desired_commit" "$installed_config_ref" 'unknown' "reconcile failed; installer rollback ${outcome}"
  note "ROLLBACK_UNVERIFIED outcome=${outcome}"
  exit 3
fi
