# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Ask for shared-installer coordination using fixed public PR comments."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


STATUS_CONTEXT = "Installer coordination"
COMMENT_MARKER = "<!-- installer-coordination -->"
BOT_LOGIN = "github-actions[bot]"
COMMAND = re.compile(r"/installer-followup[ \t]+(done|in-review|not-needed)", re.IGNORECASE)
CONFIRMATIONS = {
    "done": "The corresponding update is complete.",
    "in-review": "A corresponding change is under review.",
    "not-needed": "No corresponding update is needed.",
}
AWAITING = "Confirm shared installer follow-up using a PR comment."
DATA_ERROR = "Installer coordination could not be checked. Re-run the workflow."
QUESTION = """This PR changes installer definitions. Please confirm any necessary follow-up for the shared Deadline Cloud submitter installer.

The PR author or a maintainer can reply in a new PR comment with one command:

- `/installer-followup done` — the corresponding update is complete.
- `/installer-followup in-review` — a corresponding change is under review.
- `/installer-followup not-needed` — no corresponding update is needed.

No non-public implementation details, review identifiers, or tracking links are needed."""


def parse_command(body: Any) -> str | None:
    match = COMMAND.fullmatch(body.strip()) if isinstance(body, str) else None
    return match[1].lower() if match else None


def affected_paths(files: Iterable[dict[str, Any]]) -> list[str]:
    """Watch added/deleted XML definitions and both sides of renames."""
    paths: set[str] = set()
    for file in files:
        filename, previous = file["filename"], file.get("previous_filename")
        if not isinstance(filename, str) or (previous is not None and not isinstance(previous, str)):
            raise ValueError("Invalid file metadata")
        for path in (filename, previous):
            if path and path.startswith(("installer/", "install_builder/")) and path.endswith(".xml"):
                paths.add(path)
    return sorted(paths)


def gh(*args: str, input_json: Any = None) -> Any:
    command = ["gh", "api", *args]
    if input_json is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(input_json) if input_json is not None else None,
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def read_pages(endpoint: str) -> list[dict[str, Any]]:
    pages = gh("--paginate", "--slurp", endpoint)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Incomplete page data")
    records = [record for page in pages for record in page]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Invalid page records")
    return records


def latest_confirmation(repo: str, author: str, comments: list[dict[str, Any]]) -> str | None:
    """Accept only the PR author or a user with current write/maintain/admin access."""
    permissions: dict[str, bool] = {}
    for comment in sorted(comments, key=lambda c: (c.get("updated_at", ""), c["id"]), reverse=True):
        command = parse_command(comment.get("body"))
        user = comment.get("user") or {}
        login = user.get("login", "")
        if command is None or user.get("type") != "User" or not login:
            continue
        if login == author:
            return command
        # Association is server-provided. Skip outsiders without making a
        # permissions request that could prevent a valid author reply being read.
        if comment.get("author_association") not in ("OWNER", "MEMBER", "COLLABORATOR"):
            continue
        if login not in permissions:
            role = gh(f"repos/{repo}/collaborators/{login}/permission")
            permissions[login] = role["permission"] in ("write", "maintain", "admin")
        if permissions[login]:
            return command
    return None


def bot_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            comment for comment in comments
            if (comment.get("user") or {}).get("login") == BOT_LOGIN
            and (comment.get("user") or {}).get("type") == "Bot"
            and COMMENT_MARKER in (comment.get("body") or "")
        ),
        None,
    )


def render_comment(*, required: bool, confirmation: str | None) -> str:
    if not required:
        content = "No installer definitions changed; no shared installer confirmation is required."
    else:
        state = CONFIRMATIONS[confirmation] if confirmation else "Awaiting confirmation."
        content = QUESTION + f"\n\n**Status:** {state}"
    return f"### Shared installer coordination\n\n{content}\n\n{COMMENT_MARKER}"


def update_comment(repo: str, pr: str, existing: dict[str, Any] | None, body: str) -> None:
    if existing:
        if existing["body"] != body:
            gh(f"repos/{repo}/issues/comments/{existing['id']}", "-X", "PATCH", input_json={"body": body})
    else:
        gh(f"repos/{repo}/issues/{pr}/comments", "-X", "POST", input_json={"body": body})


def set_status(repo: str, sha: str, state: str, description: str, run_url: str) -> None:
    payload = {"state": state, "context": STATUS_CONTEXT, "description": description}
    if run_url:
        payload["target_url"] = run_url
    gh(f"repos/{repo}/statuses/{sha}", "-X", "POST", input_json=payload)


def assert_current(before: dict[str, Any], current: dict[str, Any], file_count: int) -> None:
    if (
        current["state"] != "open"
        or current["head"]["sha"] != before["head"]["sha"]
        or current["base"]["sha"] != before["base"]["sha"]
        or current["base"]["ref"] != before["base"]["ref"]
        or current["changed_files"] != file_count
    ):
        raise ValueError("PR revision changed or file data was incomplete")


def report(message: str, *, failed: bool = False) -> int:
    print(("::error::" if failed else "") + message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(f"### Shared installer coordination\n\n{message}\n", encoding="utf-8")
    return int(failed)


def run(repo: str, pr: str, *, base_branch: str = "mainline", run_url: str = "") -> int:
    endpoint = f"repos/{repo}/pulls/{pr}"
    sha = ""
    try:
        metadata = gh(endpoint)
        if metadata["state"] != "open" or metadata["base"]["ref"] != base_branch:
            return report("No open PR targeting the configured branch needs checking.")
        sha = metadata["head"]["sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid PR head SHA")
        # Comment events run on the default branch. Publish a dedicated status
        # on the PR's actual head, so both PR events and replies update one gate.
        set_status(repo, sha, "pending", "Checking shared installer coordination.", run_url)
        files = read_pages(f"{endpoint}/files?per_page=100")
        required = bool(affected_paths(files))
        comments = read_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100")
        confirmation = latest_confirmation(repo, metadata["user"]["login"], comments) if required else None
        assert_current(metadata, gh(endpoint), len(files))
        existing = bot_comment(comments)
        if required or existing:
            update_comment(repo, pr, existing, render_comment(required=required, confirmation=confirmation))
        # A push during comment posting must not receive a result for older code.
        assert_current(metadata, gh(endpoint), len(files))
        if not required:
            state, message = "success", "No installer definitions changed; no confirmation is required."
        elif confirmation:
            state, message = "success", CONFIRMATIONS[confirmation]
        else:
            state, message = "failure", AWAITING
        set_status(repo, sha, state, message, run_url)
        return report(message, failed=state == "failure")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Do not echo command bodies, API responses, or stderr into public output.
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
            try:
                set_status(repo, sha, "error", DATA_ERROR, run_url)
            except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError):
                pass
        return report(DATA_ERROR, failed=True)


def main() -> int:
    repo, pr = os.environ.get("REPO", ""), os.environ.get("PR_NUMBER", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[1-9][0-9]*", pr):
        return report("Missing or invalid pull request context.", failed=True)
    return run(
        repo, pr, base_branch=os.environ.get("BASE_BRANCH", "mainline"),
        run_url=os.environ.get("RUN_URL", ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
