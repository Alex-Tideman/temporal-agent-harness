"""GitHub publication and CI reads with exact branch/commit reconciliation."""

import os
from pathlib import Path
from urllib.parse import quote

import httpx

from . import remote_repositories


def repository(project: dict) -> str:
    return remote_repositories.canonical_url(project["remote_url"]).removeprefix(
        "https://github.com/"
    )


def request(project: dict, method: str, path: str, **kwargs):
    token = os.environ.get("SDLC_GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "Set SDLC_GITHUB_TOKEN with repository contents and pull request write access to publish"
        )
    try:
        response = httpx.request(
            method,
            "https://api.github.com/repos/" + repository(project) + path,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            follow_redirects=False,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        raise ValueError(
            f"GitHub returned HTTP {error.response.status_code}. Check repository access and retry."
        ) from None
    except httpx.HTTPError:
        raise ValueError(
            "GitHub is unreachable. Retry to reconcile publication."
        ) from None


def pull_requests(project: dict, item: dict):
    return request(
        project,
        "GET",
        "/pulls",
        params={
            "head": repository(project).split("/")[0] + ":" + item["branch"],
            "base": item["target_branch"],
            "state": "all",
            "per_page": 100,
        },
    )


def reconcile(project: dict, item: dict) -> dict | None:
    matches = pull_requests(project, item)
    if not matches:
        return None
    result = matches[0]
    commit = item["publication"]["commit"]
    if (
        result["head"]["sha"] != commit
        or result["base"]["ref"] != item["target_branch"]
    ):
        raise ValueError("Pull request revision differs from the reviewed publication")
    return {
        "number": result["number"],
        "url": result["html_url"],
        "state": result["state"],
        "commit": commit,
    }


def publish(project: dict, item: dict, folder: Path) -> dict:
    commit = item["publication"]["commit"]
    branch = item["branch"]
    url = remote_repositories.canonical_url(project["remote_url"]) + ".git"
    # Existing refs are read before any mutation, including after a lost reply.
    refs = remote_repositories.git(
        folder, "ls-remote", "--heads", "--", url, "refs/heads/" + branch
    )
    if refs:
        if refs.split()[0] != commit:
            raise ValueError(
                "The publication branch contains another revision. It was not overwritten."
            )
    else:
        # An empty expected ref is compare-and-swap creation; never overwrite
        # an existing branch, including a race after ls-remote.
        remote_repositories.git(
            folder,
            "push",
            "--force-with-lease=refs/heads/" + branch + ":",
            "--",
            url,
            commit + ":refs/heads/" + branch,
        )
    matches = pull_requests(project, item)
    if matches:
        result = matches[0]
    else:
        sources = "\n".join(
            f"- {t['title']} · {t['task_id']} · accepted revision {t['revision']}"
            for t in item["inputs"]
        )
        result = request(
            project,
            "POST",
            "/pulls",
            json={
                "title": f"Integrate {len(item['tasks'])} reviewed tasks",
                "head": branch,
                "base": item["target_branch"],
                "body": f"Combined changes verified and accepted in boltzmann.\n\n{sources}\n\nIntegration revision: {item['publication']['revision']}\nTarget commit: {item['target_commit']}\nApproved by: {item['publication']['approved_by']['name']}",
                "draft": False,
            },
        )
    if (
        result["head"]["sha"] != commit
        or result["base"]["ref"] != item["target_branch"]
    ):
        raise ValueError("Pull request revision differs from the reviewed publication")
    return {
        "number": result["number"],
        "url": result["html_url"],
        "state": result["state"],
        "commit": commit,
    }


def feedback(project: dict, item: dict) -> dict:
    publication = item.get("publication")
    if not publication:
        raise ValueError("Publish the integration before checking GitHub CI")
    commit = publication["commit"]
    status = request(
        project, "GET", f"/commits/{commit}/status", params={"per_page": 100}
    )
    checks = request(
        project, "GET", f"/commits/{commit}/check-runs", params={"per_page": 100}
    )
    pulls = pull_requests(project, item)
    pull = pulls[0] if pulls else None
    base = request(
        project, "GET", "/git/ref/heads/" + quote(item["target_branch"], safe="")
    )
    current = bool(pull and pull["head"]["sha"] == commit)
    states = [status["state"]] if status["total_count"] else []
    for check in checks["check_runs"]:
        states.append(
            "pending"
            if check["status"] != "completed"
            else "success"
            if check.get("conclusion") in {"success", "neutral", "skipped"}
            else "failure"
        )
    overall = (
        "failure"
        if any(s in {"error", "failure"} for s in states)
        else "pending"
        if "pending" in states
        else "success"
        if states
        else "not_reported"
    )
    return {
        "commit": commit,
        "state": overall if current else "stale",
        "checks": [
            {
                "name": c["name"],
                "status": c["status"],
                "conclusion": c.get("conclusion"),
            }
            for c in checks["check_runs"]
        ],
        "truncated": checks["total_count"] > 100 or status["total_count"] > 100,
        "pull_request_state": pull["state"] if pull else "missing",
        "head_current": current,
        "target_current": base["object"]["sha"] == item["target_commit"],
    }
