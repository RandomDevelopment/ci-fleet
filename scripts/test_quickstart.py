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
    app_setup.index('[[ "$PEM_DEST" =~') : app_setup.index(
        "Use an equivalent privileged SSH workflow"
    )
]
active_guard = '[[ -n "$ACTIVE_PEM" && "$PEM_DEST" == "$ACTIVE_PEM" ]]'
noclobber_line = next(line for line in transfer.splitlines() if "set -C && cat >" in line)
assert '"$PEM_DEST"' in noclobber_line
assert active_guard in transfer
assert transfer.index(active_guard) < transfer.index('ssh "$CONTROLLER"')
for command in ("cat >", "sha256sum --", "stat -c '%U:%G'", "stat -c '%a'"):
    assert any(command in line and "$PEM_DEST" in line for line in transfer.splitlines()), (
        f"transfer does not use configured destination: {command}"
    )

success_branch, failure_branch = transfer.split("else", 1)
for check in (
    'test "$local_sha" = "$remote_sha" &&\n',
    "= 'root:root'\" &&\n",
    "= '600'\"\nthen",
):
    assert check in success_branch, f"download deletion is not gated by: {check}"
    assert success_branch.index(check) < success_branch.index('rm -f -- "$PEM"')
assert 'rm -f -- "$PEM"' not in failure_branch

for use in (
    "exact value of `PEM_DEST`",
    "exact value of `ACTIVE_PEM`",
    'sudo rm -f -- "$ACTIVE_PEM"',
    'sudo rm -f -- "$PEM_DEST"',
):
    assert use in app_setup, f"configured PEM destination contract missing: {use}"
print("documentation_contract=PASS")
