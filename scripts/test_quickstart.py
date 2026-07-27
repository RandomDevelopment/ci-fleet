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
print("quickstart_contract=PASS")
