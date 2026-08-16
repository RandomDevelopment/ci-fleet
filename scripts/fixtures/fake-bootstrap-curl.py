#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

config = Path(sys.argv[sys.argv.index("--config") + 1]).read_text().splitlines()
values = {}
for line in config:
    if " = " in line:
        key, value = line.split(" = ", 1)
        values.setdefault(key, []).append(value.strip().strip('"'))
if "connect-timeout" not in values or "max-time" not in values:
    raise SystemExit("fake API request lacks transfer bounds")
url = values["url"][0]
endpoint = url.split("?", 1)[0]
method = values.get("request", ["GET"])[0]
output = Path(values["output"][0])
state = Path(os.environ["FAKE_BOOTSTRAP_STATE"])
pem = Path(os.environ["FAKE_BOOTSTRAP_PEM_FILE"]).read_text()
response = None
kind = "unknown"
if "/app-manifests/" in url:
    kind = "manifest-conversion"
    response = {"id": 123, "client_id": "Iv1FixtureClient", "slug": "ci-fleet-example-org-example-ci-01", "pem": pem,
                "client_secret": "fixture-client-secret", "webhook_secret": "fixture-webhook-secret"}
elif endpoint.endswith("/app"):
    kind = "app"
    response = {"id": 123, "client_id": "Iv1FixtureClient", "slug": "ci-fleet-example-org-example-ci-01", "public": False, "events": [], "owner": {"login": "example-org", "type": "Organization"},
                "permissions": {"contents": "read", "metadata": "read", "organization_self_hosted_runners": "write"}}
elif endpoint.endswith("/app/installations"):
    kind = "installations"
    response = [{"id": 456, "repository_selection": "selected", "account": {"login": "example-org", "type": "Organization"}}]
elif endpoint.endswith("/access_tokens"):
    kind = "installation-token"
    response = {"token": "fixture-installation-token-value", "permissions": {"contents": "read", "metadata": "read", "organization_self_hosted_runners": "write"}}
elif "/repos/example-org/example-repo" in url:
    kind = "repository"
    response = {"id": 101, "full_name": "example-org/example-repo", "private": True, "archived": False,
                "owner": {"login": "example-org"}}
elif "/installation/repositories" in url:
    kind = "installation-repositories"
    response = {"total_count": 1, "repositories": [{"id": 201, "full_name": "example-org/config", "private": True, "archived": False}]}
elif endpoint.endswith("/actions/runner-groups") and method == "POST":
    kind = "runner-group-create"
    state.write_text("created\n")
    response = {"id": 789, "name": "example-ci-experimental", "visibility": "selected", "default": False, "allows_public_repositories": False, "restricted_to_workflows": False}
elif endpoint.endswith("/actions/runner-groups"):
    kind = "runner-groups"
    groups = [{"id": 789, "name": "example-ci-experimental", "visibility": "selected", "default": False, "allows_public_repositories": False, "restricted_to_workflows": False}] if state.exists() else []
    response = {"total_count": len(groups), "runner_groups": groups}
elif endpoint.endswith("/actions/runner-groups/789/repositories"):
    kind = "runner-group-repositories"
    response = {"total_count": 1, "repositories": [{"id": 101, "full_name": "example-org/example-repo", "private": True, "archived": False}]}
elif endpoint.endswith("/actions/runner-groups/789") and method == "DELETE":
    kind = "runner-group-delete"
    state.unlink(missing_ok=True)
    response = {}
elif endpoint.endswith("/actions/runner-groups/789"):
    kind = "runner-group"
    response = {"id": 789, "name": "example-ci-experimental", "visibility": "selected", "default": False, "allows_public_repositories": False, "restricted_to_workflows": False}
else:
    raise SystemExit(f"unexpected fake API request: {method} {url}")
if kind == "installations" and os.environ.get("FAKE_BOOTSTRAP_ALL_REPOSITORIES") == "1":
    response[0]["repository_selection"] = "all"
if kind == "app" and os.environ.get("FAKE_BOOTSTRAP_BAD_APP_CLIENT_ID") == "1":
    assert isinstance(response, dict)
    response["client_id"] = "Iv1DifferentClient"
if kind == "installation-token" and os.environ.get("FAKE_BOOTSTRAP_NO_CONTENTS") == "1":
    assert isinstance(response, dict)
    response["permissions"].pop("contents")
if kind in {"runner-group", "runner-groups"} and os.environ.get("FAKE_BOOTSTRAP_PUBLIC_GROUP") == "1":
    (response["runner_groups"][0] if kind == "runner-groups" else response)["allows_public_repositories"] = True
if kind in {"runner-group", "runner-groups"} and os.environ.get("FAKE_BOOTSTRAP_RESTRICTED_GROUP") == "1":
    (response["runner_groups"][0] if kind == "runner-groups" else response)["restricted_to_workflows"] = True
if kind == "runner-group-repositories" and os.environ.get("FAKE_BOOTSTRAP_ARCHIVED_REPOSITORY") == "1":
    assert isinstance(response, dict)
    response["repositories"][0]["archived"] = True
if kind == os.environ.get("FAKE_BOOTSTRAP_TRUNCATE_KIND"):
    response["total_count"] += 1
transient = state.with_suffix(".transient")
if kind == "installations" and os.environ.get("FAKE_BOOTSTRAP_TRANSIENT_INSTALLATIONS") == "1" and not transient.exists():
    transient.write_text("failed-once\n")
    raise SystemExit(22)
with Path(os.environ["FAKE_BOOTSTRAP_LOG"]).open("a") as log:
    log.write(f"{method} {kind}\n")
output.write_text(json.dumps(response))
if kind == "manifest-conversion" and os.environ.get("FAKE_BOOTSTRAP_CONVERSION_NONZERO") == "1":
    raise SystemExit(22)
