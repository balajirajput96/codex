#!/usr/bin/env python3
"""Record bounded, GitHub-native Codex maintenance observations safely.

The script is deliberately observational. It reads pull-request, workflow-run,
and commit metadata through the GitHub CLI, then stores a compact, machine-readable
ledger on a dedicated state branch. It never merges pull requests, changes source,
reruns workflows, cancels jobs, or reads/downloads log content.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

MAX_CYCLES = 2400
MAX_TRACKED_FAILURES = 1000
MAX_RECENT_HISTORY = 25
STATE_BRANCH = "maintenance/codex-ci-state"
STATE_PATH = ".github/maintenance/state.json"
CYCLE_PATH_TEMPLATE = ".github/maintenance/cycles/{cycle:04d}.json"
SCHEMA_VERSION = 2
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])")


class GitHubApiError(RuntimeError):
    """An expected GitHub CLI API request failed."""


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cycle_record_path(cycle: int) -> str:
    return CYCLE_PATH_TEMPLATE.format(cycle=cycle)


def run_gh_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    command = ["gh", "api", "--method", method, path]
    stdin = None
    if payload is not None:
        command.extend(["--input", "-"])
        stdin = json.dumps(payload, separators=(",", ":"))

    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    environment.pop("GH_FORCE_TTY", None)
    completed = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=environment,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or f"GitHub CLI exited {completed.returncode}"
        raise GitHubApiError(detail)
    try:
        return json.loads(ANSI_ESCAPE.sub("", completed.stdout))
    except json.JSONDecodeError as error:
        raise GitHubApiError(f"GitHub API returned invalid JSON for {path}") from error


def decode_json_content(response: dict[str, Any], description: str) -> tuple[dict[str, Any], str | None]:
    try:
        encoded = "".join(response["content"].split())
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        document = json.loads(decoded)
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubApiError(f"{description} exists but is not valid UTF-8 JSON") from error
    sha = response.get("sha")
    return document, sha if isinstance(sha, str) else None


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") == 1 and "history" in state:
        legacy_history = state.pop("history")
        if not isinstance(legacy_history, list):
            raise GitHubApiError("legacy maintenance history is not a list")
        completed = state.get("completed_cycles")
        state.update(
            {
                "schema_version": SCHEMA_VERSION,
                "legacy_history_entries": len(legacy_history),
                "latest_cycle_path": cycle_record_path(completed)
                if isinstance(completed, int) and completed
                else None,
                "recent_history": legacy_history[-MAX_RECENT_HISTORY:],
            }
        )
    return state


def read_state(repository: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = run_gh_json(
            f"repos/{repository}/contents/{STATE_PATH}?ref={STATE_BRANCH}"
        )
    except GitHubApiError as error:
        if "404" in str(error) or "Not Found" in str(error):
            return None, None
        raise

    state, state_sha = decode_json_content(response, "maintenance state")
    if not isinstance(state, dict):
        raise GitHubApiError("maintenance state must be a JSON object")
    state = normalize_state(state)
    validate_state(state, repository)
    return state, state_sha


def validate_state(state: Any, repository: str) -> None:
    if not isinstance(state, dict):
        raise GitHubApiError("maintenance state must be a JSON object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise GitHubApiError("maintenance state schema is not supported")
    if state.get("repository") != repository:
        raise GitHubApiError("maintenance state belongs to another repository")
    completed = state.get("completed_cycles")
    if not isinstance(completed, int) or not 0 <= completed <= MAX_CYCLES:
        raise GitHubApiError("maintenance state has an invalid completed_cycles value")
    recent_history = state.get("recent_history")
    if not isinstance(recent_history, list) or len(recent_history) > MAX_RECENT_HISTORY:
        raise GitHubApiError("maintenance state has invalid recent history")
    if completed and not isinstance(state.get("latest_cycle_path"), str):
        raise GitHubApiError("maintenance state is missing its latest cycle path")
    if not isinstance(state.get("known_failure_runs"), dict):
        raise GitHubApiError("maintenance state has invalid known failures")


def new_state(repository: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "max_cycles": MAX_CYCLES,
        "completed_cycles": 0,
        "created_at": utc_now(),
        "updated_at": None,
        "last_result": None,
        "next_recommended_action": "Run the first scheduled maintenance observation.",
        "known_failure_runs": {},
        "latest_cycle_path": None,
        "recent_history": [],
    }


def compact_pull_request(pull_request: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pull_request.get("number"),
        "draft": bool(pull_request.get("draft")),
        "updated_at": pull_request.get("updated_at"),
        "head_sha": pull_request.get("head", {}).get("sha"),
    }


def compact_workflow_run(workflow_run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workflow_run.get("id"),
        "name": workflow_run.get("name"),
        "event": workflow_run.get("event"),
        "status": workflow_run.get("status"),
        "conclusion": workflow_run.get("conclusion"),
        "head_branch": workflow_run.get("head_branch"),
        "head_sha": workflow_run.get("head_sha"),
        "created_at": workflow_run.get("created_at"),
        "updated_at": workflow_run.get("updated_at"),
        "url": workflow_run.get("html_url"),
    }


def collect_snapshot(repository: str) -> dict[str, Any]:
    pull_requests = run_gh_json(f"repos/{repository}/pulls?state=open&per_page=100")
    workflow_response = run_gh_json(
        f"repos/{repository}/actions/runs?per_page=100"
    )
    commits = run_gh_json(f"repos/{repository}/commits?per_page=20")
    if not isinstance(pull_requests, list) or not isinstance(commits, list):
        raise GitHubApiError("GitHub API returned an unexpected repository listing")
    workflow_runs = workflow_response.get("workflow_runs")
    if not isinstance(workflow_runs, list):
        raise GitHubApiError("GitHub API returned an unexpected workflow-run listing")

    return {
        "pull_requests": [compact_pull_request(item) for item in pull_requests],
        "workflow_runs": [compact_workflow_run(item) for item in workflow_runs],
        "head_sha": commits[0].get("sha") if commits else None,
        "workflow_window_limited_to": 100,
    }


def summarize_snapshot(snapshot: dict[str, Any], known_failure_runs: dict[str, Any]) -> dict[str, Any]:
    workflow_runs = snapshot["workflow_runs"]
    active_runs = [
        run
        for run in workflow_runs
        if run.get("status") in {"queued", "in_progress", "waiting", "requested"}
    ]
    failed_runs = [
        run
        for run in workflow_runs
        if run.get("conclusion") in {"failure", "timed_out", "action_required", "startup_failure"}
    ]
    cancelled_runs = [run for run in workflow_runs if run.get("conclusion") == "cancelled"]
    new_failures = [
        run
        for run in failed_runs
        if str(run.get("id")) not in known_failure_runs
    ]
    drafts = [pr for pr in snapshot["pull_requests"] if pr["draft"]]

    status_counts = Counter(str(run.get("status") or "unknown") for run in workflow_runs)
    conclusion_counts = Counter(
        str(run.get("conclusion") or "pending") for run in workflow_runs
    )
    return {
        "main_head_sha": snapshot["head_sha"],
        "open_pull_requests": len(snapshot["pull_requests"]),
        "open_draft_pull_requests": [pr["number"] for pr in drafts],
        "active_workflow_runs": active_runs[:30],
        "failed_workflow_runs": failed_runs[:30],
        "new_failure_runs": new_failures[:30],
        "cancelled_workflow_runs": cancelled_runs[:30],
        "workflow_status_counts": dict(sorted(status_counts.items())),
        "workflow_conclusion_counts": dict(sorted(conclusion_counts.items())),
        "workflow_window_limited_to": snapshot["workflow_window_limited_to"],
    }


def determine_next_action(summary: dict[str, Any]) -> tuple[str, str]:
    if summary["new_failure_runs"]:
        return (
            "review_required",
            "Inspect new failed workflow metadata and collect logs only through a supervised repair task.",
        )
    if summary["active_workflow_runs"]:
        return (
            "monitoring",
            "Wait for active workflows; do not cancel, rerun, merge, or modify source automatically.",
        )
    if summary["cancelled_workflow_runs"]:
        return (
            "review_required",
            "Classify cancelled workflows before requesting any retry; do not assume a source failure.",
        )
    return (
        "healthy_observation",
        "No new failed or active workflow run was observed in the most recent 100-run window.",
    )


def advance_state(
    state: dict[str, Any],
    summary: dict[str, Any],
    *,
    run_id: str | None,
    now: str,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    completed = state["completed_cycles"]
    if completed >= MAX_CYCLES:
        limited = dict(state)
        limited["last_result"] = "cycle_limit_reached"
        limited["next_recommended_action"] = (
            "Do not record another cycle. Inspect the durable ledger and decide whether to extend the system."
        )
        return limited, False, None

    result, next_action = determine_next_action(summary)
    known_failures = dict(state["known_failure_runs"])
    for workflow_run in summary["failed_workflow_runs"]:
        workflow_id = str(workflow_run.get("id"))
        known_failures[workflow_id] = {
            "first_observed_at": now,
            "head_sha": workflow_run.get("head_sha"),
            "conclusion": workflow_run.get("conclusion"),
            "url": workflow_run.get("url"),
        }
    if len(known_failures) > MAX_TRACKED_FAILURES:
        known_failures = dict(list(known_failures.items())[-MAX_TRACKED_FAILURES:])

    cycle = completed + 1
    record = {
        "cycle": cycle,
        "timestamp": now,
        "workflow_run_id": run_id,
        "main_head_sha": summary["main_head_sha"],
        "result": result,
        "open_pull_request_count": summary["open_pull_requests"],
        "open_draft_pull_requests": summary["open_draft_pull_requests"],
        "active_workflow_run_ids": [run.get("id") for run in summary["active_workflow_runs"]],
        "new_failure_run_ids": [run.get("id") for run in summary["new_failure_runs"]],
        "cancelled_workflow_run_ids": [run.get("id") for run in summary["cancelled_workflow_runs"]],
        "next_recommended_action": next_action,
        "workflow_window_limited_to": summary["workflow_window_limited_to"],
    }
    updated = dict(state)
    updated["completed_cycles"] = cycle
    updated["updated_at"] = now
    updated["last_result"] = result
    updated["next_recommended_action"] = next_action
    updated["known_failure_runs"] = known_failures
    updated["latest_cycle_path"] = cycle_record_path(cycle)
    updated["recent_history"] = [*state["recent_history"], record][-MAX_RECENT_HISTORY:]
    return updated, True, record


def default_branch_sha(repository: str) -> str:
    repository_data = run_gh_json(f"repos/{repository}")
    default_branch = repository_data.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise GitHubApiError("repository has no default branch")
    reference = run_gh_json(f"repos/{repository}/git/ref/heads/{default_branch}")
    sha = reference.get("object", {}).get("sha")
    if not isinstance(sha, str) or not sha:
        raise GitHubApiError("default branch has no commit SHA")
    return sha


def ensure_state_branch(repository: str) -> None:
    try:
        run_gh_json(f"repos/{repository}/git/ref/heads/{STATE_BRANCH}")
        return
    except GitHubApiError as error:
        if "404" not in str(error) and "Not Found" not in str(error):
            raise
    run_gh_json(
        f"repos/{repository}/git/refs",
        method="POST",
        payload={
            "ref": f"refs/heads/{STATE_BRANCH}",
            "sha": default_branch_sha(repository),
        },
    )


def write_json_file(
    repository: str,
    path: str,
    document: dict[str, Any],
    message: str,
    previous_sha: str | None = None,
) -> None:
    encoded = base64.b64encode(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).decode("ascii")
    payload: dict[str, Any] = {
        "message": message,
        "content": encoded,
        "branch": STATE_BRANCH,
    }
    if previous_sha:
        payload["sha"] = previous_sha
    run_gh_json(
        f"repos/{repository}/contents/{path}",
        method="PUT",
        payload=payload,
    )


def write_cycle_record(repository: str, record: dict[str, Any]) -> None:
    cycle = record["cycle"]
    if not isinstance(cycle, int):
        raise GitHubApiError("maintenance cycle record has no valid cycle number")
    path = cycle_record_path(cycle)
    try:
        run_gh_json(f"repos/{repository}/contents/{path}?ref={STATE_BRANCH}")
    except GitHubApiError as error:
        if "404" not in str(error) and "Not Found" not in str(error):
            raise
    else:
        raise GitHubApiError(f"maintenance cycle {cycle} already exists; refusing to overwrite it")
    write_json_file(
        repository,
        path,
        record,
        f"chore(maintenance): record cycle {cycle}",
    )


def write_state(repository: str, state: dict[str, Any], previous_sha: str | None) -> None:
    write_json_file(
        repository,
        STATE_PATH,
        state,
        f"chore(maintenance): update state for cycle {state['completed_cycles']}",
        previous_sha,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and summarize metadata without creating a state branch or commit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        print("::error::GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2

    try:
        state, previous_sha = read_state(repository)
        if state is None:
            state = new_state(repository)
        snapshot = collect_snapshot(repository)
        summary = summarize_snapshot(snapshot, state["known_failure_runs"])
        now = utc_now()
        candidate, should_write, record = advance_state(
            state,
            summary,
            run_id=os.environ.get("GITHUB_RUN_ID"),
            now=now,
        )
        output = {
            "dry_run": args.dry_run,
            "state_branch": STATE_BRANCH,
            "completed_cycles_before": state["completed_cycles"],
            "completed_cycles_after": candidate["completed_cycles"],
            "result": candidate["last_result"],
            "next_recommended_action": candidate["next_recommended_action"],
            "new_failure_run_ids": [run.get("id") for run in summary["new_failure_runs"]],
            "active_workflow_run_ids": [run.get("id") for run in summary["active_workflow_runs"]],
        }
        print(json.dumps(output, sort_keys=True))
        if args.dry_run or not should_write:
            return 0
        if record is None:
            raise GitHubApiError("maintenance cycle record was not prepared")
        ensure_state_branch(repository)
        write_cycle_record(repository, record)
        write_state(repository, candidate, previous_sha)
        print(
            f"::notice::Recorded maintenance cycle {candidate['completed_cycles']} on {STATE_BRANCH}."
        )
        return 0
    except GitHubApiError as error:
        print(f"::error::maintenance monitor could not complete safely: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
