#!/usr/bin/env python3
import re
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
raw_quickstart = (repo_root / "docs" / "QUICKSTART.md").read_text()
quickstart = " ".join(raw_quickstart.split())

required = (
    "host architecture must be `amd64` or `arm64`",
    "Cancel every queued job",
    "across every repository already authorized for the runner group",
    "the only workflow that can target the shared label",
)
for text in required:
    assert text in quickstart, f"quickstart safety contract missing: {text}"

assert quickstart.index("Cancel every queued job") < quickstart.index("3. Authorize the repository")
assert "PROJECT_PREFIX=" not in raw_quickstart
assert "managed controller managed controller" not in quickstart

app_setup = (repo_root / "docs" / "GITHUB-APP-SETUP.md").read_text()
token_calls = app_setup.count("scripts/github-app-token.sh \\")
redirected_token_calls = re.findall(
    r"scripts/github-app-token\.sh \\\n\s+--env-file [^\n]+ >/dev/null",
    app_setup,
)
assert token_calls == 2, f"expected two documented token-helper calls, found {token_calls}"
assert len(redirected_token_calls) == token_calls, "token-helper stdout must be redirected"
assert app_setup.index("## Key rotation: activate and verify before revocation") < app_setup.index(
    "## Old-key revocation"
)

rotation = re.search(
    r'ACTIVE_PEM="(/etc/ci-fleet/secrets/[^"\n]+)"\n'
    r'PEM_DEST="(/etc/ci-fleet/secrets/[^"\n]+)"',
    app_setup,
)
assert rotation and rotation[1] != rotation[2], "rotation destination must differ from active PEM"

transfer = app_setup[
    app_setup.index("valid_pem_path() {") : app_setup.index(
        "Use an equivalent privileged SSH workflow"
    )
]
active_guard = '[[ -n "$ACTIVE_PEM" && "$PEM_DEST" == "$ACTIVE_PEM" ]]'
assert "valid_pem_path \"$PEM_DEST\" || exit 1" in transfer
assert '[[ -n "$PEM_DIR" ]] || PEM_DIR=/' in transfer
assert "install -d -m 0700" in transfer and "$PEM_DIR" in transfer
assert "^/[A-Za-z0-9._/-]+$" in transfer
assert 'realpath -m -- \\"$PEM_DEST\\"' in transfer
assert active_guard in transfer
assert transfer.index(active_guard) < transfer.index('ssh "$CONTROLLER"')
assert "mktemp --" in transfer and 'cat >\\"\\$tmp\\"' in transfer
hard_link = 'ln -T -- \\"\\$tmp\\" \\"$PEM_DEST\\"'
assert hard_link in transfer
assert transfer.index("mktemp --") < transfer.index('cat >\\"\\$tmp\\"')
assert transfer.index('cat >\\"\\$tmp\\"') < transfer.index(hard_link)
assert "set -C" not in transfer
marker_link = 'ln -T -- \\"\\$tmp\\" \\"$PEM_MARKER\\"'
assert transfer.index(marker_link) < transfer.index(hard_link)
for command in ("sha256sum --", "stat -c '%U:%G'", "stat -c '%a'"):
    assert any(command in line and "$PEM_DEST" in line for line in transfer.splitlines()), (
        f"transfer does not use configured destination: {command}"
    )

verification = transfer[transfer.index('if\nssh "$CONTROLLER"') : transfer.index("then\n  if rm")]
for check in (
    '[[ "$local_sha" =~ ^[0-9a-f]{64}$ ]] &&\n',
    '[[ "$remote_sha" =~ ^[0-9a-f]{64}$ ]] &&\n',
    'test "$local_sha" = "$remote_sha" &&\n',
    "= 'root:root'\" &&\n",
    "= '600'\" &&\n",
):
    assert check in verification, f"download deletion is not gated by: {check}"

delete_download = transfer[
    transfer.index('if rm -f -- "$PEM"; then') : transfer.index(
        "else\n  if ! ssh"
    )
]
delete_success, delete_failure = delete_download.split("else", 1)
assert "unset PEM" in delete_success
assert '"$PEM" >&2' in delete_failure and "exit 1" in delete_failure
assert 'rm -f -- "$PEM"' not in transfer[transfer.index("transfer verification failed") :]
verification_failure = transfer[transfer.index("else\n  if ! ssh") :]
assert 'test \\"$PEM_MARKER\\" -ef \\"$PEM_DEST\\"' in verification_failure
assert 'rm -f -- \\"$PEM_DEST\\" \\"$PEM_MARKER\\"' in verification_failure
assert "remote ownership cleanup failed" in verification_failure
assert "per-transfer hard-link marker proves" in app_setup
assert "pre-existing destinations are preserved" in app_setup
assert "secret-manager-backed destination" in app_setup
assert "authenticated import/version operation" in app_setup
assert "remove only the new inactive version through the manager" in app_setup
assert "sudo readlink -f -- \"$ACTIVE_PEM\"" in app_setup
assert "update `host.env` to that\ncanonical result" in app_setup
assert "rotating, revoking, or directly retiring" in app_setup

for use in (
    "exact value of `PEM_DEST`",
    "exact value of `ACTIVE_PEM`",
):
    assert use in app_setup, f"configured PEM destination contract missing: {use}"

rotation = app_setup[
    app_setup.index("## Key rotation: activate and verify before revocation") :
    app_setup.index("## Old-key revocation")
]
assert "`healthy` for an `active` controller" in rotation
assert "`maintenance`" in rotation and "`drained` or `disabled`" in rotation
assert "If token generation fails, immediately restore" in rotation
assert "run normal\n   reconciliation" in rotation
assert "`RECONCILE CONVERGED`" in rotation
checkpoint_match = '"CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$PEM_DEST"'
assert checkpoint_match in rotation
checkpoint_query = "LATEST_CHECKPOINT=$(sudo find"
checkpoint_destination = "PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE='"
assert rotation.index(checkpoint_destination) < rotation.index(checkpoint_query)
assert rotation.index('valid_pem_path "$PEM_DEST" || exit 1') < rotation.index(
    checkpoint_query
)
assert "! -path '/var/lib/ci-fleet/checkpoints/.checkpoint.staging.*/*'" in rotation
assert "retain the old GitHub key and old PEM" in rotation

revocation = app_setup[
    app_setup.index("## Old-key revocation") : app_setup.index(
        "## Controller retirement and PEM removal"
    )
]
read_destination = "PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE='"
validate_paths = 'for pem in "${OLD_LOCAL_PEMS[@]}" "${OLD_MANAGED_PEMS[@]}"; do'
distinct_paths = '[[ "$pem" != "$PEM_DEST" ]] || exit 1'
remove_old_pem = 'sudo rm -f -- "$pem" || exit 1'
assert 'ACTIVE_PEM="/etc/ci-fleet/secrets/OLD-GITHUB-APP-KEY.pem"' in revocation
assert 'PEM_DEST_BACKING=$(sudo readlink -f -- "$PEM_DEST") || exit 1' in revocation
assert 'backing=$(sudo readlink -f -- "$pem") || exit 1' in revocation
assert '[[ "$backing" != "$PEM_DEST_BACKING" ]] || exit 1' in revocation
assert 'OLD_LOCAL_PEMS=("$backing")' in revocation
assert 'OLD_MANAGED_PEMS=()' in revocation
assert "((active_classifications == 1)) || exit 1" in revocation
old_manager_absent = 'for pem in "${OLD_MANAGED_PEMS[@]}"; do'
assert "^/[A-Za-z0-9._/-]+$" in revocation
assert revocation.index(read_destination) < revocation.index(validate_paths)
assert revocation.index(validate_paths) < revocation.index(distinct_paths)
assert revocation.index(distinct_paths) < revocation.index(old_manager_absent)
assert revocation.index(old_manager_absent) < revocation.index(remove_old_pem)
assert revocation.index(remove_old_pem) < revocation.index(
    "On the GitHub App settings page"
)

retirement = app_setup[app_setup.index("## Controller retirement and PEM removal") :]
validate_paths = 'valid_pem_path "$pem" || exit 1'
uninstall = "scripts/install-worker-controller.sh \\\n        --uninstall &&"
remove_pem = 'sudo rm -f -- "$pem" || return 1'
persist_inventory = 'sudo install -m 0600 /dev/stdin "$PEM_INVENTORY"'
remove_host_env = "sudo rm -f -- /etc/ci-fleet/host.env &&"
remove_inventory = 'sudo rm -f -- "$PEM_INVENTORY"; then'
classify_destination = 'configured_classifications=$((configured_classifications + 1))'
manager_absent = 'sudo test ! -e "$pem" || exit 1'
inventory_distinct = '[[ "$pem" != "$PEM_INVENTORY" ]] || exit 1'
assert 'LOCAL_PEMS=(' in retirement and 'MANAGED_PEMS=(' in retirement
assert 'if sudo test -L "$pem"; then' in retirement
assert 'backing=$(sudo readlink -f -- "$pem") || exit 1' in retirement
assert 'LOCAL_PEMS+=("$backing")' in retirement
assert "^/[A-Za-z0-9._/-]+$" in retirement
assert retirement.index(read_destination) < retirement.index(validate_paths)
assert retirement.index(validate_paths) < retirement.index(inventory_distinct)
assert retirement.index(inventory_distinct) < retirement.index(classify_destination)
assert "((configured_classifications == 1)) || exit 1" in retirement
assert retirement.index(classify_destination) < retirement.index(manager_absent)
assert retirement.index(manager_absent) < retirement.index(persist_inventory)
assert retirement.index(persist_inventory) < retirement.index(uninstall)
assert remove_pem in retirement
assert 'for pem in "${LOCAL_PEMS[@]}"; do' in retirement
assert retirement.index(uninstall) < retirement.index("remove_retired_pems &&")
assert retirement.index("remove_retired_pems &&") < retirement.index(remove_host_env)
assert retirement.index(remove_host_env) < retirement.index(remove_inventory)
retirement_failure = retirement[retirement.index("retirement cleanup failed") :]
assert '"$PEM_INVENTORY" >&2' in retirement_failure and "exit 1" in retirement_failure
print("documentation_contract=PASS")
