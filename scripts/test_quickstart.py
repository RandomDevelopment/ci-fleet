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
print("documentation_contract=PASS")
