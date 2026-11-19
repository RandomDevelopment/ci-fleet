#!/usr/bin/env python3
"""Request GitHub native auto-merge for a verified Dependabot artifact patch."""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode


WORKFLOW = ".github/workflows/build.yml"
BUILD_WORKFLOW_ID = 416363263
BUILD_CHECK = "Build without registering a runner"
GITHUB_ACTIONS_APP_ID = 15368
DEPENDABOT_ID = 49699333
SHA = r"[0-9a-f]{40}"
VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"
PIN = re.compile(rf"( +uses: actions/upload-artifact@)({SHA}) # v({VERSION})")
QUEUE_STATE = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequest {
      headRefOid
      baseRefOid
      baseRefName
      state
      isDraft
      viewerCanEnableAutoMerge
      autoMergeRequest { enabledAt }
      isMergeQueueEnabled
      repository { autoMergeAllowed }
      baseRef {
        branchProtectionRule {
          isAdminEnforced
          requiresStatusChecks
          requiresStrictStatusChecks
          requiredStatusChecks { context app { databaseId } }
        }
      }
    }
  }
}
"""
ENABLE_AUTO_MERGE = """
mutation($input: EnablePullRequestAutoMergeInput!) {
  enablePullRequestAutoMerge(input: $input) {
    pullRequest { autoMergeRequest { enabledAt } }
  }
}
"""


def gh(path, *args, data=None):
    command = ["gh", "api", path, *args]
    if data is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(data).encode() if data is not None else None,
        check=True, stdout=subprocess.PIPE,
    )
    return result.stdout.decode()


def artifact_patch(diff, message):
    """Return the new version and SHA only for a one-line, same-mode pin change."""
    lines = diff.removesuffix("\n").split("\n")
    if (len(lines) < 8 or lines[0] != f"diff --git a/{WORKFLOW} b/{WORKFLOW}"
            or not re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+ 100644", lines[1])
            or lines[2:4] != [f"--- a/{WORKFLOW}", f"+++ b/{WORKFLOW}"]):
        return None
    changes = [line for line in lines[4:] if not line.startswith(" ")]
    if (len(changes) != 3 or not changes[0].startswith("@@ ")
            or not changes[1].startswith("-") or not changes[2].startswith("+")):
        return None
    old = PIN.fullmatch(changes[1][1:])
    new = PIN.fullmatch(changes[2][1:])
    if not old or not new:
        return None
    old_prefix, old_sha, old_version = old.groups()
    new_prefix, new_sha, new_version = new.groups()
    old_parts = tuple(map(int, old_version.split(".")))
    new_parts = tuple(map(int, new_version.split(".")))
    if (old_prefix != new_prefix or old_sha == new_sha
            or old_parts[:2] != new_parts[:2] or new_parts[2] <= old_parts[2]):
        return None

    # Read metadata committed and signed by Dependabot, never the editable PR body.
    # Dependabot labels this workflow action direct:production.
    metadata = re.findall(r"(?ms)^---\nupdated-dependencies:\n(.*?)^\.\.\.$", message)
    if metadata != [
        "- dependency-name: actions/upload-artifact\n"
        f"  dependency-version: {new_version}\n"
        "  dependency-type: direct:production\n"
        "  update-type: version-update:semver-patch\n"
        "  dependency-group: routine-actions\n"
    ]:
        return None
    return new_version, new_sha


def eligible_pull(pull, repo_id, head, branch):
    return (
        pull["state"] == "open" and not pull["draft"]
        and pull["user"]["id"] == DEPENDABOT_ID
        and pull["user"]["type"] == "Bot"
        and pull["head"]["repo"]["id"] == repo_id
        and pull["head"]["ref"] == branch and pull["head"]["sha"] == head
        and pull["base"]["repo"]["id"] == repo_id and pull["base"]["ref"] == "main"
        and pull["commits"] == 1 and pull["changed_files"] == 1
        and pull["additions"] == pull["deletions"] == 1
    )


def build_passed(api, repo, event_run):
    query = urlencode({"event": "pull_request", "head_sha": event_run["head_sha"], "per_page": 1})
    runs = api(f"{repo}/actions/workflows/{BUILD_WORKFLOW_ID}/runs?{query}")["workflow_runs"]
    if not runs:
        return False
    run = runs[0]
    if (run["id"] != event_run["id"] or run["run_attempt"] != event_run["run_attempt"]
            or run["head_sha"] != event_run["head_sha"] or run["workflow_id"] != BUILD_WORKFLOW_ID
            or run["status"] != "completed" or run["conclusion"] != "success"):
        return False
    query = urlencode({"check_name": BUILD_CHECK, "filter": "latest"})
    result = api(f"{repo}/check-suites/{run['check_suite_id']}/check-runs?{query}")
    if result["total_count"] != 1:
        return False
    check, = result["check_runs"]
    return (
        check["name"] == BUILD_CHECK and check["app"]["id"] == GITHUB_ACTIONS_APP_ID
        and check["head_sha"] == event_run["head_sha"]
        and check["status"] == "completed" and check["conclusion"] == "success"
    )


def queue_patch(event, request=gh):
    def api(path, *args, **kwargs):
        return json.loads(request(path, *args, **kwargs))

    repository = event["repository"]
    if repository["full_name"] != "RandomDevelopment/ci-fleet":
        return False
    repo = f"repos/{repository['full_name']}"
    run = event["workflow_run"]
    head, branch = run["head_sha"], run["head_branch"]
    if (run["workflow_id"] != BUILD_WORKFLOW_ID or run["event"] != "pull_request"
            or run["conclusion"] != "success" or run["head_repository"]["id"] != repository["id"]
            or not re.fullmatch(SHA, head)
            or not re.fullmatch(r"dependabot/github_actions/routine-actions-[0-9a-f]+", branch)):
        return False
    query = urlencode({"head": f"{repository['owner']['login']}:{branch}", "base": "main", "state": "open"})
    pulls = api(f"{repo}/pulls?{query}")
    if len(pulls) != 1:
        return False
    path = f"{repo}/pulls/{int(pulls[0]['number'])}"
    pull = api(path)
    if not eligible_pull(pull, repository["id"], head, branch) or pull["auto_merge"] is not None:
        return False

    base = pull["base"]["sha"]
    if not re.fullmatch(SHA, base):
        return False
    compare = f"{repo}/compare/{base}...{head}"
    comparison = api(compare)
    if comparison["total_commits"] != 1 or comparison["merge_base_commit"]["sha"] != base:
        return False
    commit, = comparison["commits"]
    if (commit["sha"] != head or [parent["sha"] for parent in commit["parents"]] != [base]
            or (commit["author"] or {}).get("id") != DEPENDABOT_ID
            or (commit["committer"] or {}).get("login") != "web-flow"
            or commit["commit"]["verification"]["verified"] is not True):
        return False
    diff = request(compare, "--header", "Accept: application/vnd.github.diff")
    patch = artifact_patch(diff, commit["commit"]["message"])
    if not patch:
        return False
    version, pin = patch
    upstream = "repos/actions/upload-artifact"
    tag = f"v{version}"
    release = api(f"{upstream}/releases/tags/{tag}")
    if release["draft"] or release["prerelease"] or release["tag_name"] != tag:
        return False
    if api(f"{upstream}/commits/refs%2Ftags%2F{tag}")["sha"] != pin:
        return False

    if not build_passed(api, repo, run):
        return False
    pull_id = pull["node_id"]
    state = api("graphql", data={"query": QUEUE_STATE, "variables": {"id": pull_id}})["data"]["node"]
    protection = (state["baseRef"] or {}).get("branchProtectionRule") or {}
    required_build = any(
        check["context"] == BUILD_CHECK and (check["app"] or {}).get("databaseId") == GITHUB_ACTIONS_APP_ID
        for check in protection.get("requiredStatusChecks") or []
    )
    if (state["headRefOid"] != head or state["baseRefOid"] != base or state["baseRefName"] != "main"
            or state["state"] != "OPEN" or state["isDraft"]
            or state["autoMergeRequest"] or state["isMergeQueueEnabled"]
            or not state["repository"]["autoMergeAllowed"] or not state["viewerCanEnableAutoMerge"]
            or not all(protection.get(setting) is True for setting in
                       ("isAdminEnforced", "requiresStatusChecks", "requiresStrictStatusChecks"))
            or not required_build):
        return False

    # The CLI's --auto can immediately merge a ready PR. Request only native auto-merge.
    queued = api("graphql", data={
        "query": ENABLE_AUTO_MERGE,
        "variables": {"input": {
            "pullRequestId": pull_id,
            "expectedHeadOid": head,
            "mergeMethod": "SQUASH",
            "commitHeadline": f"ci(deps): bump actions/upload-artifact to {tag} (#{pull['number']})",
        }},
    })["data"]["enablePullRequestAutoMerge"]["pullRequest"]
    return queued["autoMergeRequest"] is not None


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    print("Requested native auto-merge." if queue_patch(event) else "No auto-merge requested.")
