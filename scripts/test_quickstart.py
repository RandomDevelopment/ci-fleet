#!/usr/bin/env python3
from pathlib import Path

raw_quickstart = (Path(__file__).resolve().parents[1] / "docs" / "QUICKSTART.md").read_text()
quickstart = " ".join(raw_quickstart.split())

required = (
    "host architecture must be `amd64` or `arm64`",
    "Cancel every queued job",
    "REPOSITORY=OWNER/REPOSITORY",
    "RUN_ATTEMPT=<dispatched-run-attempt>",
    'PROJECT_PREFIX="ci-${REPO_COMPONENT}-${RUN_ID}-${RUN_ATTEMPT}-"',
    '--filter "name=^/${PROJECT_PREFIX}"',
    '--filter "name=^${PROJECT_PREFIX}"',
)
for text in required:
    assert text in quickstart, f"quickstart safety contract missing: {text}"

assert quickstart.index("Cancel every queued job") < quickstart.index("3. Authorize the repository")
assert "managed controller managed controller" not in quickstart
print("quickstart_contract=PASS")
