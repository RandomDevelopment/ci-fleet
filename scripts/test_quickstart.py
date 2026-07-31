#!/usr/bin/env python3
from pathlib import Path

raw_quickstart = (Path(__file__).resolve().parents[1] / "docs" / "QUICKSTART.md").read_text()
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

raw_app_setup = (Path(__file__).resolve().parents[1] / "docs" / "GITHUB-APP-SETUP.md").read_text()
app_setup = " ".join(raw_app_setup.split())
app_safety_contract = (
    "sudo ./scripts/github-app-token.sh",
    "sudo /opt/ci-fleet/manager/current/scripts/github-app-token.sh",
    "Every verification invocation must redirect stdout to `/dev/null`",
    "/etc/ci-fleet/secrets/github-app.next.pem",
    "controlled management workstation",
    "Before any key bytes arrive",
    "Delete the workstation copy immediately after",
    "before token, reconciliation, health, or convergence checks",
    "new-key activation, reconciliation, health, or convergence",
    "Revoke every key for this controller in GitHub",
    "disable or remove the controller declaration",
    "Remove the controller's PEM and its local GitHub App identity state",
    "operators must not improvise removal from Markdown examples",
    "issue #27",
)
for text in app_safety_contract:
    assert text in app_setup, f"GitHub App safety contract missing: {text}"
assert raw_app_setup.count("--env-file /etc/ci-fleet/host.env >/dev/null") == 2
assert "LOCAL_PEMS=" not in raw_app_setup
assert "declare -a" not in raw_app_setup
print("quickstart_contract=PASS")
