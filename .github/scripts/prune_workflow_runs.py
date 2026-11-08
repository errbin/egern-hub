"""Keep the active workflow run and its newest predecessor."""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
PAGE_SIZE = 100


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required workflow environment value: {name}")
    return value


def workflow_filename(repository: str, workflow_ref: str) -> str:
    workflow_path, separator, _revision = workflow_ref.partition("@")
    prefix = repository + "/"
    if not separator or not workflow_path.startswith(prefix):
        raise RuntimeError("Unable to identify the current workflow file")

    relative_path = workflow_path[len(prefix):]
    if not relative_path.startswith(".github/workflows/"):
        raise RuntimeError("Current workflow is outside .github/workflows")

    filename = PurePosixPath(relative_path).name
    if not filename.endswith((".yml", ".yaml")):
        raise RuntimeError("Current workflow file has an unsupported extension")
    return filename


def run_order(run: dict) -> tuple[str, int, int]:
    return (
        str(run.get("created_at", "")),
        int(run.get("run_number", 0)),
        int(run.get("id", 0)),
    )


def partition_runs(runs: list[dict], current_run_id: int) -> tuple[dict | None, list[dict], int]:
    """Return the newest predecessor, completed runs to delete, and active runs to protect."""
    previous = [run for run in runs if int(run.get("id", 0)) != current_run_id]
    if not previous:
        return None, [], 0

    newest_previous = max(previous, key=run_order)
    deletable = []
    protected_active = 0
    for run in previous:
        if int(run.get("id", 0)) == int(newest_previous.get("id", 0)):
            continue
        if run.get("status") == "completed":
            deletable.append(run)
        else:
            protected_active += 1
    return newest_previous, deletable, protected_active


class GitHub:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = quote(repository, safe="/")

    def request(self, method: str, path: str) -> dict | None:
        request = Request(
            API_ROOT + path,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "egern-workflow-history-cleanup",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                if method == "DELETE":
                    if response.status != 204:
                        raise RuntimeError("Workflow history deletion returned an unexpected status")
                    return None
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(f"Workflow history cleanup received HTTP {error.code}") from None
        except URLError:
            raise RuntimeError("Workflow history cleanup could not reach GitHub") from None

    def list_runs(self, workflow: str) -> list[dict]:
        workflow = quote(workflow, safe="")
        collected: list[dict] = []
        page = 1
        while True:
            query = urlencode({"per_page": PAGE_SIZE, "page": page})
            result = self.request(
                "GET",
                f"/repos/{self.repository}/actions/workflows/{workflow}/runs?{query}",
            )
            if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
                raise RuntimeError("GitHub returned an invalid workflow run list")
            runs = result["workflow_runs"]
            collected.extend(run for run in runs if isinstance(run, dict))
            if len(runs) < PAGE_SIZE:
                return collected
            page += 1

    def delete_run(self, run_id: int) -> None:
        self.request("DELETE", f"/repos/{self.repository}/actions/runs/{run_id}")


def main() -> int:
    token = required_env("ACTIONS_CLEANUP_TOKEN")
    repository = required_env("GITHUB_REPOSITORY")
    if repository.count("/") != 1:
        raise RuntimeError("GITHUB_REPOSITORY has an invalid format")
    current_run_id = int(required_env("GITHUB_RUN_ID"))
    workflow = workflow_filename(repository, required_env("GITHUB_WORKFLOW_REF"))

    github = GitHub(token, repository)
    runs = github.list_runs(workflow)
    previous, deletable, protected_active = partition_runs(runs, current_run_id)

    deleted = 0
    for run in deletable:
        github.delete_run(int(run["id"]))
        deleted += 1

    retained_previous = 1 if previous else 0
    print(
        "Workflow history cleanup complete: "
        f"current run retained, {retained_previous} previous run retained, "
        f"{deleted} older completed runs deleted, "
        f"{protected_active} other active runs protected."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as error:
        print(f"Workflow history cleanup failed: {error}", file=sys.stderr)
        sys.exit(1)
