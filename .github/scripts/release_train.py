#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# The repo side of the release train (reusable_release_train.yml). The train itself runs in AWS and holds no
# GitHub credential; this script asks it what to do and does the GitHub writes with the job's GITHUB_TOKEN.
#
#   python3 release_train.py run        the run step: GITHUB_TOKEN only
#   python3 release_train.py dispatch   the dispatch step: the caller's dispatch token only (see below)
#
# One run:
#   1. Refuse unless the job runs on the repository's default branch (a workflow_run run does: GitHub runs it from
#      the default branch, whichever branch the workflow it follows ran on).
#   2. Mint a GitHub OIDC ID token for the gateway's audience and invoke the gateway (an AWS Lambda function,
#      reached with the credentials the workflow's OIDC role gave this job) with {"phase": "plan"}. The
#      gateway learns the repository, ref and event from the signed token, not from anything sent here.
#   3. Validate every returned action and execute it:
#        push                       fetch the default branch, download the git bundle from its presigned URL,
#                                   check its size and sha256, fetch it, check the tip, that the base is on the
#                                   default branch and that the diff touches no guarded path, then push only
#                                   refs/heads/bot/<name>, with an exact lease on what that branch holds now
#        open_or_update_pr          open the PR from that bot branch into the default branch (or update the open
#                                   one), add the train's labels, wait until GitHub reports the pushed head
#        pr_status                  read a bot PR's state (merge detection)
#        create_tag_and_prerelease  an annotated X.Y.Z tag on a commit reachable from the default branch, and a
#                                   prerelease GitHub Release (rehearsal mode only, with the rehearsal title only; in
#                                   live mode the repository's own release workflow tags and releases)
#        comment                    a comment on a bot PR
#        dispatch_workflow          live mode only: the repository's own Release: Bump (release_bump.yml) on the
#                                   default branch, with GITHUB_TOKEN (a workflow_dispatch in the job's own repository)
#      Live mode's release-notes review of the repo's own release PR (the one its Release: Bump workflow opens on
#      bump/X.Y.Z, titled chore(release): X.Y.Z):
#        find_release_pr            list those open PRs into the default branch (read only)
#        push_release_notes         one commit on top of such a PR's head, from a verified bundle: its parent must
#                                   be the head the train reviewed, it may change CHANGELOG.md only, and it is pushed
#                                   to bump/X.Y.Z with an exact lease on that head (a fast-forward; nothing else)
#        comment_release_pr         a comment on such a PR (the review's change table, or why the notes were kept)
#      Actions of one request run in order; after a failure the rest of that request is skipped.
#   4. Report the results with {"phase": "report"}.
#   5. Wait while the gateway says a train of this repository is busy (a step is running, or a gate this run just
#      answered has not re-run yet): ask again with {"phase": "wait"} every POLL_S seconds and do the actions it
#      hands out, up to the wait limit (wait_minutes). The next handoff of a train this run started or advanced
#      thus reaches this same run, so nothing has to dispatch the repository and no runner sits idle between
#      trains. After a dispatch of Release: Bump in live mode the run also waits (at most RELEASE_PR_WAIT_S) until
#      the release PR it opens is found, so its notes review starts. A run that hits the limit stops; the next
#      event (a merge's push, a dispatch) or the daily schedule picks the train up. Before it ends, a run that
#      waited polls for releases once more (a train that finished may have deferred one).
#   6. Write the downstream repositories the gateway asked to dispatch (fan-out after a release) to a file for the
#      dispatch step.
#
# The dispatch step (a separate step, so only it receives the caller's dispatch token, and it runs nothing but
# this script): for each target, check that the repository is in both the gateway's allowlist and the caller's
# own hard-coded list (dispatch_repos), that the workflow is release_train.yml and the ref that repository's
# default branch, then POST /repos/<owner>/<repo>/actions/workflows/release_train.yml/dispatches and nothing else.
# Without a token it skips with a notice: the downstream repository's daily run picks the release up. It reports
# each outcome with {"phase": "dispatched"}.
#
# Nothing here checks out or executes repository code: the bundle is fetched into a bare repository and only
# inspected with git plumbing. The GITHUB_TOKEN reaches git only through the environment of the one fetch or
# push that needs it, and no token, ID token or presigned URL is ever printed.
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

KINDS = frozenset({"push", "open_or_update_pr", "pr_status", "create_tag_and_prerelease", "comment",
                   "dispatch_workflow", "find_release_pr", "push_release_notes", "comment_release_pr"})
# A bot branch: path components of safe characters, none starting with "." or ending in ".lock".
BOT_BRANCH = re.compile(r"bot/(?!\.)[A-Za-z0-9._+-]+(?<!\.lock)(/(?!\.)[A-Za-z0-9._+-]+(?<!\.lock))*")
RELEASE_TAG = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# The repo's own release PR branch (Release: Bump), and the bundle branch a release-notes commit arrives on.
RELEASE_PR_BRANCH = re.compile(r"bump/([0-9]+\.[0-9]+\.[0-9]+)")
NOTES_BUNDLE_BRANCH = re.compile(r"rt-notes/[0-9]+\.[0-9]+\.[0-9]+")
NOTES_FILES = frozenset({"CHANGELOG.md"})
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ACTION_ID = re.compile(r"[A-Za-z0-9|_-]{1,200}")
REHEARSAL_SUFFIX = "(release-train rehearsal, not an official release)"
# The only labels the train asks for, with how they are created when missing.
LABELS = {"agent-modified": ("d93f0b", "Contains commits written by the release train's fix agent; needs human review")}
DISPATCHABLE = frozenset({"release_bump.yml"})
# Release: Bump's one input (the repositories' release_bump.yml).
BUMP_INPUTS: dict[str, frozenset[str]] = {"force_version_bump": frozenset({"", "patch", "minor", "major"})}
# The fan-out: the one workflow of a downstream repository the dispatch step may start.
FANOUT_WORKFLOW = "release_train.yml"
REPO_NAME = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
BRANCH_NAME = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,100}(?<![./])")
CLAIM_KEY = re.compile(r"DISPATCH#release#[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}#[A-Za-z0-9._:-]{1,100}#"
                       r"[0-9A-Za-z.+-]{1,40}(#[A-Za-z0-9-]{1,80})?")
POLL_S = 20.0
MAX_WAIT_S = 65 * 60.0
RELEASE_PR_WAIT_S = 15 * 60.0
MAX_GATEWAY_FAILURES = 3
DISPATCH_ATTEMPTS = 3
GUARDED_PREFIXES = (".github/",)
GUARDED_FILES = frozenset({".gitmodules"})
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
MAX_TITLE = 256
MAX_BODY = 60000
HEAD_WAIT_S = 60.0


class Refused(Exception):
    """An action this script will not perform; it is reported as failed."""


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


@dataclass
class Context:
    repo: str  # owner/name
    default_branch: str
    token: str
    mode: str
    workdir: Path
    api: Callable[..., Any]
    download: Callable[[str, int], bytes]
    remote_url: str = ""
    sleep: Callable[[float], None] = time.sleep
    pushed: dict[str, str] = field(default_factory=dict)
    _cloned: bool = False

    def __post_init__(self) -> None:
        if not self.remote_url:
            self.remote_url = f"https://github.com/{self.repo}.git"

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def git_dir(self) -> Path:
        return self.workdir / "repo.git"


# -- validation -------------------------------------------------------------------


def _str(action: dict[str, Any], key: str, limit: int = 1000) -> str:
    value: Any = action.get(key)
    if not isinstance(value, str) or len(value) > limit:
        raise Refused(f"{action.get('kind')}: {key} must be a string of at most {limit} characters")
    return value


def _sha(action: dict[str, Any], key: str) -> str:
    value: str = _str(action, key, 40)
    if not SHA.fullmatch(value):
        raise Refused(f"{action.get('kind')}: {key} must be a 40-hex commit id")
    return value


def _bot(action: dict[str, Any]) -> str:
    branch: str = _str(action, "branch", 200)
    if not BOT_BRANCH.fullmatch(branch) or ".." in branch or branch.endswith("."):
        raise Refused(f"{action.get('kind')}: refusing branch {branch!r}: only bot/* branches are written")
    return branch


def _number(action: dict[str, Any]) -> int:
    value: Any = action.get("number")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Refused(f"{action.get('kind')}: number must be a positive integer")
    return value


def validate(action: Any, ctx: Context) -> dict[str, Any]:
    """The action if this script may perform it in this repository; Refused otherwise."""
    if not isinstance(action, dict):
        raise Refused("an action must be an object")
    kind: Any = action.get("kind")
    if kind not in KINDS:
        raise Refused(f"unknown action kind {kind!r}")
    if not isinstance(action.get("id"), str) or not ACTION_ID.fullmatch(action["id"]):
        raise Refused(f"{kind}: bad action id")
    if kind == "push":
        _bot(action)
        _sha(action, "tip")
        _sha(action, "base_sha")
        url: str = _str(action, "bundle_url", 4096)
        host: str = urllib.parse.urlsplit(url).hostname or ""
        if not url.startswith("https://") or not (host.endswith(".amazonaws.com") and ".s3" in f".{host}"):
            raise Refused("push: the bundle URL must be an https S3 URL")
        if not SHA256.fullmatch(_str(action, "bundle_sha256", 64)):
            raise Refused("push: bundle_sha256 must be 64 hex characters")
        size: Any = action.get("bundle_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BUNDLE_BYTES:
            raise Refused(f"push: bundle_bytes must be between 1 and {MAX_BUNDLE_BYTES}")
    elif kind == "open_or_update_pr":
        _bot(action)
        if _str(action, "base", 200) != ctx.default_branch:
            raise Refused(f"open_or_update_pr: base must be the default branch {ctx.default_branch}, "
                          f"not {action['base']!r}")
        title: str = _str(action, "title", MAX_TITLE)
        if not title.strip() or "\n" in title:
            raise Refused("open_or_update_pr: the title must be one non-empty line")
        _str(action, "body", MAX_BODY)
        labels: Any = action.get("labels", [])
        if not isinstance(labels, list) or any(label not in LABELS for label in labels):
            raise Refused(f"open_or_update_pr: labels must be among {sorted(LABELS)}")
    elif kind == "pr_status":
        _number(action)
        _bot(action)
        _sha(action, "tip")
    elif kind == "create_tag_and_prerelease":
        if ctx.mode != "rehearsal":
            raise Refused("create_tag_and_prerelease: only in rehearsal mode (in live mode the repository's own "
                          "release workflow tags and releases)")
        tag: str = _str(action, "tag", 40)
        if not RELEASE_TAG.fullmatch(tag):
            raise Refused(f"create_tag_and_prerelease: refusing tag {tag!r}: only X.Y.Z")
        _sha(action, "sha")
        title = _str(action, "title", MAX_TITLE)
        if ctx.mode == "rehearsal" and not title.endswith(REHEARSAL_SUFFIX):
            raise Refused(f"create_tag_and_prerelease: a rehearsal release must be titled '... {REHEARSAL_SUFFIX}'")
        _str(action, "notes", 125000)
    elif kind == "comment":
        _number(action)
        _str(action, "body", MAX_BODY)
    elif kind in ("push_release_notes", "comment_release_pr"):
        _number(action)
        if not RELEASE_PR_BRANCH.fullmatch(_str(action, "branch", 200)):
            raise Refused(f"{kind}: refusing branch {action['branch']!r}: only a bump/X.Y.Z release PR")
        if kind == "comment_release_pr":
            _str(action, "body", MAX_BODY)
        else:
            _sha(action, "tip")
            _sha(action, "expected_head")
            if not NOTES_BUNDLE_BRANCH.fullmatch(_str(action, "bundle_branch", 200)):
                raise Refused("push_release_notes: the bundle branch must be rt-notes/X.Y.Z")
            url = _str(action, "bundle_url", 4096)
            host = urllib.parse.urlsplit(url).hostname or ""
            if not url.startswith("https://") or not (host.endswith(".amazonaws.com") and ".s3" in f".{host}"):
                raise Refused("push_release_notes: the bundle URL must be an https S3 URL")
            if not SHA256.fullmatch(_str(action, "bundle_sha256", 64)):
                raise Refused("push_release_notes: bundle_sha256 must be 64 hex characters")
            size = action.get("bundle_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BUNDLE_BYTES:
                raise Refused(f"push_release_notes: bundle_bytes must be between 1 and {MAX_BUNDLE_BYTES}")
    elif kind == "dispatch_workflow":
        if _str(action, "workflow", 200) not in DISPATCHABLE:
            raise Refused(f"dispatch_workflow: only {sorted(DISPATCHABLE)} may be dispatched")
        if ctx.mode != "live":
            raise Refused("dispatch_workflow: only in live mode (a rehearsal releases through the train's own PR)")
        if _str(action, "ref", 200) != ctx.default_branch:
            raise Refused(f"dispatch_workflow: only on the default branch {ctx.default_branch}, not {action['ref']!r}")
        inputs: Any = action.get("inputs", {})
        if not isinstance(inputs, dict) or any(k not in BUMP_INPUTS or v not in BUMP_INPUTS[k] for k, v in inputs.items()):
            raise Refused(f"dispatch_workflow: inputs may be only force_version_bump in "
                          f"{sorted(BUMP_INPUTS['force_version_bump'])}")
    return action


def verify_bundle(data: bytes, size: int, sha256: str) -> None:
    if len(data) != size:
        raise Refused(f"push: the bundle has {len(data)} bytes, not the {size} the train wrote")
    actual: str = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise Refused(f"push: bundle sha256 mismatch ({actual[:12]}, expected {sha256[:12]})")


# -- git --------------------------------------------------------------------------


def _git_env(token: str = "") -> dict[str, str]:
    env: dict[str, str] = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    if token:
        basic: str = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def git(git_dir: Path, *args: str, token: str = "", check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", f"--git-dir={git_dir}", *args], capture_output=True, text=True,
                          env=_git_env(token), check=False, timeout=600)
    if check and proc.returncode != 0:
        raise Refused(f"git {args[0]} failed: {proc.stderr.strip()[-400:]}")
    return proc


def guarded_changes(git_dir: Path, base: str, tip: str) -> list[str]:
    """What base..tip changes that a bot branch may not: guarded paths, symlinks and submodules."""
    found: list[str] = []
    fields: list[str] = git(git_dir, "diff", "--raw", "-z", "--no-renames", base, tip).stdout.split("\0")
    for meta, path in zip(fields[0::2], fields[1::2]):
        if not meta.startswith(":"):
            continue
        new_mode: str = meta[1:].split()[1]
        if path in GUARDED_FILES or path.startswith(GUARDED_PREFIXES):
            found.append(f"{path}: guarded path")
        elif new_mode == "120000":
            found.append(f"{path}: symlink")
        elif new_mode == "160000":
            found.append(f"{path}: submodule")
    return found


def ensure_clone(ctx: Context) -> str:
    """A bare repository holding the default branch's full history; returns its tip."""
    tracking: str = f"refs/remotes/origin/{ctx.default_branch}"
    if not ctx._cloned:
        subprocess.run(["git", "init", "--bare", "--quiet", str(ctx.git_dir)], env=_git_env(), check=True,
                       capture_output=True, timeout=60)
        git(ctx.git_dir, "fetch", "--quiet", "--no-tags", ctx.remote_url,
            f"+refs/heads/{ctx.default_branch}:{tracking}", token=ctx.token)
        ctx._cloned = True
    return git(ctx.git_dir, "rev-parse", "--verify", f"{tracking}^{{commit}}").stdout.strip()


def _ancestor(ctx: Context, older: str, newer: str) -> bool:
    return git(ctx.git_dir, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _remote_head(ctx: Context, ref: str) -> str:
    out: str = git(ctx.git_dir, "ls-remote", ctx.remote_url, ref, token=ctx.token).stdout.strip()
    return out.split("\t")[0] if out else ""


# -- actions ----------------------------------------------------------------------


def do_push(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    branch: str = action["branch"]
    tip: str = action["tip"]
    base: str = action["base_sha"]
    default_tip: str = ensure_clone(ctx)
    data: bytes = ctx.download(action["bundle_url"], int(action["bundle_bytes"]))
    verify_bundle(data, int(action["bundle_bytes"]), action["bundle_sha256"])
    bundle: Path = ctx.workdir / f"{tip}.bundle"
    bundle.write_bytes(data)
    git(ctx.git_dir, "bundle", "verify", "--quiet", str(bundle))
    incoming: str = "refs/rt/incoming"
    git(ctx.git_dir, "fetch", "--quiet", "--no-tags", str(bundle), f"+refs/heads/{branch}:{incoming}")
    got: str = git(ctx.git_dir, "rev-parse", "--verify", f"{incoming}^{{commit}}").stdout.strip()
    if got != tip:
        raise Refused(f"push: the bundle's {branch} is {got[:12]}, not the train's tip {tip[:12]}")
    if not _ancestor(ctx, base, tip) or not _ancestor(ctx, base, default_tip):
        raise Refused(f"push: the base {base[:12]} is not below both the tip and {ctx.default_branch}")
    problems: list[str] = guarded_changes(ctx.git_dir, base, tip)
    if problems:
        raise Refused(f"push: refusing {branch}: {'; '.join(problems)}")
    ref: str = f"refs/heads/{branch}"
    remote: str = _remote_head(ctx, ref)
    pushed: bool = remote != tip
    if pushed:
        # Exact lease on what the branch holds now (absent, or an earlier train's push): nothing else is replaced.
        git(ctx.git_dir, "push", "--porcelain", "--no-verify", "--no-follow-tags", f"--force-with-lease={ref}:{remote}",
            ctx.remote_url, f"{tip}:{ref}", token=ctx.token)
    ctx.pushed[branch] = tip
    return {"branch": branch, "sha": tip, "pushed": pushed}


def _pr_out(pr: dict[str, Any]) -> dict[str, Any]:
    merged: bool = bool(pr.get("merged") or pr.get("merged_at"))
    return {"number": int(pr["number"]), "url": str(pr["html_url"]),
            "state": "merged" if merged else str(pr.get("state", "")), "merged": merged,
            "merge_sha": str(pr.get("merge_commit_sha") or "") if merged else "",
            "head_sha": str(pr["head"]["sha"]), "base": str(pr["base"]["ref"]),
            "labels": [str(label["name"]) for label in pr.get("labels", [])]}


def _own_bot_pr(pr: dict[str, Any], ctx: Context) -> None:
    head: dict[str, Any] = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != ctx.repo or not BOT_BRANCH.fullmatch(str(head.get("ref", ""))):
        raise Refused(f"PR #{pr.get('number')} is not a bot/* PR of {ctx.repo}")


def _ensure_label(name: str, ctx: Context) -> None:
    try:
        ctx.api("GET", f"/repos/{ctx.repo}/labels/{urllib.parse.quote(name)}")
    except ApiError as e:
        if e.status != 404:
            raise
        color, description = LABELS[name]
        try:
            ctx.api("POST", f"/repos/{ctx.repo}/labels", {"name": name, "color": color, "description": description})
        except ApiError as again:
            if again.status != 422:  # created meanwhile
                raise


def do_open_or_update_pr(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    branch: str = action["branch"]
    query: str = urllib.parse.urlencode({"head": f"{ctx.owner}:{branch}", "state": "open"})
    found: list[dict[str, Any]] = ctx.api("GET", f"/repos/{ctx.repo}/pulls?{query}")
    fields: dict[str, Any] = {"title": action["title"], "body": action["body"]}
    if found:
        pr: dict[str, Any] = found[0]
        if pr["base"]["ref"] != action["base"]:
            raise Refused(f"open PR #{pr['number']} targets {pr['base']['ref']}, not {action['base']}")
        pr = ctx.api("PATCH", f"/repos/{ctx.repo}/pulls/{pr['number']}", fields)
    else:
        pr = ctx.api("POST", f"/repos/{ctx.repo}/pulls", {**fields, "head": branch, "base": action["base"]})
    _own_bot_pr(pr, ctx)
    labels: list[str] = list(action.get("labels", []))
    if labels:
        for label in labels:
            _ensure_label(label, ctx)
        ctx.api("POST", f"/repos/{ctx.repo}/issues/{pr['number']}/labels", {"labels": labels})
    # GitHub reports a pushed head with a delay; the train records the PR only at the head it pushed.
    want: str = ctx.pushed.get(branch, "")
    waited: float = 0.0
    while True:
        pr = ctx.api("GET", f"/repos/{ctx.repo}/pulls/{pr['number']}")
        if not want or pr["head"]["sha"] == want or waited >= HEAD_WAIT_S:
            return _pr_out(pr)
        ctx.sleep(5.0)
        waited += 5.0


def do_pr_status(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    pr: dict[str, Any] = ctx.api("GET", f"/repos/{ctx.repo}/pulls/{action['number']}")
    _own_bot_pr(pr, ctx)
    if pr["head"]["ref"] != action["branch"]:
        raise Refused(f"PR #{action['number']} is from {pr['head']['ref']}, not {action['branch']}")
    return _pr_out(pr)


def do_create_tag_and_prerelease(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    tag: str = action["tag"]
    sha: str = action["sha"]
    default_tip: str = ensure_clone(ctx)
    if not _ancestor(ctx, sha, default_tip):
        raise Refused(f"create_tag_and_prerelease: {sha[:12]} is not reachable from {ctx.default_branch}")
    try:
        ref: dict[str, Any] | None = ctx.api("GET", f"/repos/{ctx.repo}/git/ref/tags/{tag}")
    except ApiError as e:
        if e.status != 404:
            raise
        ref = None
    if ref is not None:
        target: dict[str, Any] = ref["object"]
        named: str = str(target["sha"])
        if target.get("type") == "tag":
            named = str(ctx.api("GET", f"/repos/{ctx.repo}/git/tags/{named}")["object"]["sha"])
        if named != sha:
            raise Refused(f"create_tag_and_prerelease: tag {tag} already names {named[:12]}, not {sha[:12]}")
    else:
        obj: dict[str, Any] = ctx.api("POST", f"/repos/{ctx.repo}/git/tags", {
            "tag": tag, "message": action["title"], "object": sha, "type": "commit"})
        ctx.api("POST", f"/repos/{ctx.repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})
    try:
        release: dict[str, Any] = ctx.api("GET", f"/repos/{ctx.repo}/releases/tags/{tag}")
        if not release.get("prerelease") or release.get("name") != action["title"]:
            raise Refused(f"create_tag_and_prerelease: release {tag} exists but is not this prerelease")
    except ApiError as e:
        if e.status != 404:
            raise
        release = ctx.api("POST", f"/repos/{ctx.repo}/releases", {
            "tag_name": tag, "name": action["title"], "body": action["notes"], "prerelease": True, "draft": False})
    return {"tag": tag, "sha": sha, "release_url": str(release["html_url"])}


def do_comment(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    _own_bot_pr(ctx.api("GET", f"/repos/{ctx.repo}/pulls/{action['number']}"), ctx)
    made: dict[str, Any] = ctx.api("POST", f"/repos/{ctx.repo}/issues/{action['number']}/comments",
                                   {"body": action["body"]})
    return {"url": str(made.get("html_url", ""))}


def _release_pr(pr: dict[str, Any], ctx: Context) -> str:
    """The version of an open release PR of this repo (bump/X.Y.Z into the default branch); Refused otherwise."""
    head: dict[str, Any] = pr.get("head") or {}
    match = RELEASE_PR_BRANCH.fullmatch(str(head.get("ref", "")))
    if match is None or (head.get("repo") or {}).get("full_name") != ctx.repo or pr.get("state") != "open" \
            or (pr.get("base") or {}).get("ref") != ctx.default_branch \
            or pr.get("title") != f"chore(release): {match.group(1)}":
        raise Refused(f"PR #{pr.get('number')} is not an open bump/X.Y.Z release PR of {ctx.repo}")
    return match.group(1)


def do_find_release_pr(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    query: str = urllib.parse.urlencode({"state": "open", "base": ctx.default_branch, "per_page": "50"})
    found: list[dict[str, Any]] = []
    for pr in ctx.api("GET", f"/repos/{ctx.repo}/pulls?{query}") or []:
        try:
            _release_pr(pr, ctx)
        except Refused:
            continue
        found.append({"number": int(pr["number"]), "head_ref": str(pr["head"]["ref"]),
                      "head_sha": str(pr["head"]["sha"]), "title": str(pr["title"]), "url": str(pr["html_url"])})
    return {"prs": found[:5]}


def do_push_release_notes(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    branch: str = action["branch"]
    head: str = action["expected_head"]
    tip: str = action["tip"]
    pr: dict[str, Any] = ctx.api("GET", f"/repos/{ctx.repo}/pulls/{action['number']}")
    _release_pr(pr, ctx)
    if pr["head"]["ref"] != branch:
        raise Refused(f"PR #{action['number']} is from {pr['head']['ref']}, not {branch}")
    if pr["head"]["sha"] != head:
        if pr["head"]["sha"] == tip:
            return {"branch": branch, "sha": tip, "pushed": False}
        raise Refused(f"push_release_notes: {branch} moved to {pr['head']['sha'][:12]} after the review of {head[:12]}")
    ensure_clone(ctx)
    ref: str = f"refs/heads/{branch}"
    git(ctx.git_dir, "fetch", "--quiet", "--no-tags", ctx.remote_url, f"+{ref}:refs/rt/release-pr", token=ctx.token)
    if git(ctx.git_dir, "rev-parse", "--verify", "refs/rt/release-pr^{commit}").stdout.strip() != head:
        raise Refused(f"push_release_notes: {branch} is not at the reviewed head {head[:12]}")
    data: bytes = ctx.download(action["bundle_url"], int(action["bundle_bytes"]))
    verify_bundle(data, int(action["bundle_bytes"]), action["bundle_sha256"])
    bundle: Path = ctx.workdir / f"{tip}.notes.bundle"
    bundle.write_bytes(data)
    git(ctx.git_dir, "bundle", "verify", "--quiet", str(bundle))
    git(ctx.git_dir, "fetch", "--quiet", "--no-tags", str(bundle), f"+refs/heads/{action['bundle_branch']}:refs/rt/notes")
    got: str = git(ctx.git_dir, "rev-parse", "--verify", "refs/rt/notes^{commit}").stdout.strip()
    if got != tip:
        raise Refused(f"push_release_notes: the bundle holds {got[:12]}, not the train's commit {tip[:12]}")
    parents: list[str] = git(ctx.git_dir, "rev-list", "--parents", "-n", "1", tip).stdout.split()[1:]
    changed: set[str] = set(git(ctx.git_dir, "diff", "--name-only", "-z", head, tip).stdout.split("\0")) - {""}
    if parents != [head] or not changed or changed - NOTES_FILES or guarded_changes(ctx.git_dir, head, tip):
        raise Refused("push_release_notes: refusing anything but one commit on the reviewed head that changes "
                      "CHANGELOG.md only")
    git(ctx.git_dir, "push", "--porcelain", "--no-verify", "--no-follow-tags", f"--force-with-lease={ref}:{head}",
        ctx.remote_url, f"{tip}:{ref}", token=ctx.token)
    return {"branch": branch, "sha": tip, "pushed": True}


def do_comment_release_pr(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    pr: dict[str, Any] = ctx.api("GET", f"/repos/{ctx.repo}/pulls/{action['number']}")
    _release_pr(pr, ctx)
    if pr["head"]["ref"] != action["branch"]:
        raise Refused(f"PR #{action['number']} is from {pr['head']['ref']}, not {action['branch']}")
    made: dict[str, Any] = ctx.api("POST", f"/repos/{ctx.repo}/issues/{action['number']}/comments",
                                   {"body": action["body"]})
    return {"url": str(made.get("html_url", ""))}


def do_dispatch_workflow(action: dict[str, Any], ctx: Context) -> dict[str, Any]:
    # The repository's own Release: Bump, with this job's GITHUB_TOKEN: a workflow_dispatch in the job's own
    # repository is the one event that token may start. It opens the bump/X.Y.Z release PR.
    ctx.api("POST", f"/repos/{ctx.repo}/actions/workflows/{action['workflow']}/dispatches",
            {"ref": action["ref"], "inputs": dict(action.get("inputs", {}))})
    return {"workflow": action["workflow"], "ref": action["ref"], "dispatched": True}


HANDLERS: dict[str, Callable[[dict[str, Any], Context], dict[str, Any]]] = {
    "push": do_push, "open_or_update_pr": do_open_or_update_pr, "pr_status": do_pr_status,
    "create_tag_and_prerelease": do_create_tag_and_prerelease, "comment": do_comment,
    "dispatch_workflow": do_dispatch_workflow, "find_release_pr": do_find_release_pr,
    "push_release_notes": do_push_release_notes, "comment_release_pr": do_comment_release_pr}


def execute(actions: list[Any], ctx: Context) -> list[dict[str, Any]]:
    """Run the actions in order; after a failure, the rest of that action's request is skipped."""
    results: list[dict[str, Any]] = []
    failed: set[str] = set()
    for raw in actions:
        group: str = str(raw.get("id", "")).rsplit("|", 1)[0] if isinstance(raw, dict) else ""
        if group in failed:
            continue
        try:
            action: dict[str, Any] = validate(raw, ctx)
            results.append({"id": action["id"], "ok": True, "outputs": HANDLERS[action["kind"]](action, ctx)})
        except (Refused, ApiError, OSError, subprocess.SubprocessError, KeyError, ValueError) as e:
            failed.add(group)
            results.append({"id": str(raw.get("id", "")) if isinstance(raw, dict) else "", "ok": False,
                            "error": f"{type(e).__name__}: {e}"[:1000]})
    return results


# -- GitHub and AWS ---------------------------------------------------------------


def github_api(token: str, base: str = "https://api.github.com") -> Callable[..., Any]:
    def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(f"{base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "User-Agent": "release-train-workflow"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - the GitHub API
                text: str = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail: str = e.read().decode(errors="replace")[:300]
            raise ApiError(e.code, f"{method} {path.split('?')[0]}: {detail}") from None
        return json.loads(text) if text else None

    return call


def download(url: str, limit: int) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - an https S3 URL (validate())
        data: bytes = resp.read(limit + 1)
    return data


def id_token(audience: str) -> str:
    url: str = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    sep: str = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}audience={urllib.parse.quote(audience)}", headers={
        "Authorization": f"bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - the runner's token service
        value: str = json.loads(resp.read())["value"]
    print(f"::add-mask::{value}")
    return value


class GatewayError(Exception):
    pass


def invoke(function: str, payload: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Invoke the gateway with the AWS CLI; the payload (it holds an ID token) stays in a private file."""
    src: Path = workdir / "gateway-in.json"
    out: Path = workdir / "gateway-out.json"
    fd: int = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        proc = subprocess.run(["aws", "lambda", "invoke", "--function-name", function, "--cli-binary-format",
                               "raw-in-base64-out", "--payload", f"fileb://{src}", str(out)],
                              capture_output=True, text=True, check=False, timeout=120)
    finally:
        src.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise GatewayError(f"aws lambda invoke failed: {proc.stderr.strip()[-500:]}")
    meta: dict[str, Any] = json.loads(proc.stdout or "{}")
    reply: dict[str, Any] = json.loads(out.read_text() or "{}")
    out.unlink(missing_ok=True)
    if meta.get("FunctionError"):
        raise GatewayError(f"the gateway refused: {str(reply.get('errorMessage', 'error'))[:500]}")
    return reply


def _summary(plan: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines: list[str] = ["## Release train", "", f"Mode: `{plan.get('mode', '')}`; node: `{plan.get('node', '')}`", ""]
    if plan.get("started"):
        lines += [f"Started: {', '.join(f'`{s}`' for s in plan['started'])}", ""]
    if plan.get("deferred"):
        lines += [f"Deferred (a train is open): {', '.join(f'`{s}`' for s in plan['deferred'].values())}", ""]
    for note in plan.get("notes", []):
        lines.append(f"- {note}")
    if results:
        lines += ["", "| action | result |", "| --- | --- |"]
        kinds: dict[str, str] = {str(a.get("id")): str(a.get("kind")) for a in plan.get("actions", [])}
        for r in results:
            what: str = "ok" if r["ok"] else f"failed: {r.get('error', '')}"
            lines.append(f"| {kinds.get(r['id'], '?')} `{r['id']}` | {what.replace('|', '/')} |")
    else:
        lines += ["", "Nothing to do."]
    return "\n".join(lines) + "\n"


@dataclass
class Conversation:
    """What one run did: its first plan, every action result, the fan-out it collected and why it stopped."""

    plan: dict[str, Any]
    results: list[dict[str, Any]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    waits: int = 0
    release_pr_seen: bool = False
    ended: str = ""
    failed: bool = False


def _mask_urls(plan: dict[str, Any]) -> None:
    for action in plan.get("actions", []):
        if isinstance(action, dict) and isinstance(action.get("bundle_url"), str):
            print(f"::add-mask::{action['bundle_url']}")


def converse(ctx: Context, call: Callable[[dict[str, Any]], dict[str, Any]], *, wait_s: float, poll_s: float = POLL_S,
             now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
             dispatch_file: Path | None = None) -> Conversation:
    """Plan, act, report, then wait for the next handoff while the gateway says a train here is busy.

    `call` sends one payload to the gateway (the ID token is added by the caller). GatewayError from the first
    plan or a report propagates; during the wait, MAX_GATEWAY_FAILURES failures in a row end it."""
    deadline: float = now() + max(0.0, min(wait_s, MAX_WAIT_S))
    plan: dict[str, Any] = call({"phase": "plan", "mode": ctx.mode})
    conv: Conversation = Conversation(plan=plan)
    after: str = ""
    advanced: list[str] = []
    release_pr_until: float = 0.0
    repolled: bool = False
    failures: int = 0
    while True:
        _mask_urls(plan)
        actions: list[Any] = list(plan.get("actions", []))
        results: list[dict[str, Any]] = execute(actions, ctx)
        conv.results += results
        kinds: dict[str, str] = {str(a.get("id")): str(a.get("kind")) for a in actions if isinstance(a, dict)}
        for r in results:
            if r["ok"] and kinds.get(r["id"]) == "dispatch_workflow" and plan.get("notes_live"):
                release_pr_until = now() + RELEASE_PR_WAIT_S
        moved: bool = False
        if results:
            reply: dict[str, Any] = call({"phase": "report", "results": results})
            conv.replies.append(reply)
            moved = bool(reply.get("advanced"))
            if moved:
                # Only a report that moved a train dates the gates it answered; a pr_status read moves nothing.
                after = str(reply.get("at") or after)
            advanced = list(dict.fromkeys([*advanced, *[str(t) for t in reply.get("advanced", [])]]))
            if reply.get("reviews"):
                conv.release_pr_seen = True
                release_pr_until = 0.0
        advanced = list(dict.fromkeys([*advanced, *[str(t) for t in plan.get("started", [])]]))
        dispatch: dict[str, Any] = dict(plan.get("dispatch") or {})
        conv.allowed = sorted(set(conv.allowed) | {str(r) for r in dispatch.get("allowed", [])})
        known: set[str] = {str(t.get("key")) for t in conv.targets}
        conv.targets += [t for t in dispatch.get("targets", []) if isinstance(t, dict) and str(t.get("key")) not in known]
        # The plan's busy predates this report: a train it just advanced is asked about again before the run ends.
        busy: bool = bool((plan.get("wait") or {}).get("busy")) or moved
        expecting: bool = release_pr_until > now()
        if not busy and not expecting:
            if conv.waits and not repolled:
                # A train that finished while this run waited may have deferred a release: poll once more.
                repolled = True
                plan = call({"phase": "plan", "mode": ctx.mode, "after": after, "advanced": advanced[-50:]})
                continue
            conv.ended = "nothing to wait for"
            break
        if now() + poll_s > deadline:
            conv.ended = f"the wait limit was reached ({(plan.get('wait') or {}).get('reason') or 'release PR'}); " \
                         "the next event or the daily run picks it up"
            break
        sleep(poll_s)
        conv.waits += 1
        try:
            plan = call({"phase": "wait", "mode": ctx.mode, "after": after, "advanced": advanced[-50:]})
            failures = 0
        except GatewayError as e:
            failures += 1
            if failures >= MAX_GATEWAY_FAILURES:
                conv.ended, conv.failed = f"the gateway failed {failures} times while waiting: {e}", True
                break
            plan = {"actions": [], "wait": {"busy": True}, "dispatch": {}}
    if dispatch_file is not None:
        fd: int = os.open(dispatch_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"allowed": conv.allowed, "targets": conv.targets}, f)
    return conv


def run() -> int:
    repo: str = os.environ["GITHUB_REPOSITORY"]
    token: str = os.environ["GITHUB_TOKEN"]
    function: str = os.environ["RT_GATEWAY_FUNCTION"]
    audience: str = os.environ["RT_GATEWAY_AUDIENCE"]
    mode: str = os.environ.get("RT_MODE", "")
    api: Callable[..., Any] = github_api(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    default_branch: str = str(api("GET", f"/repos/{repo}")["default_branch"])
    if os.environ.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        print(f"::error::the release train runs only on the default branch {default_branch}")
        return 1
    try:
        wait_s: float = float(os.environ.get("RT_WAIT_MINUTES") or "60") * 60.0
    except ValueError:
        wait_s = 3600.0
    dispatch_path: str = os.environ.get("RT_DISPATCH_FILE", "")
    with tempfile.TemporaryDirectory(prefix="release-train-", dir=os.environ.get("RUNNER_TEMP")) as tmp:
        workdir: Path = Path(tmp)

        def call(payload: dict[str, Any]) -> dict[str, Any]:
            return invoke(function, {**payload, "id_token": id_token(audience)}, workdir)

        ctx: Context = Context(repo=repo, default_branch=default_branch, token=token, mode=mode, workdir=workdir,
                               api=api, download=download)
        try:
            conv: Conversation = converse(ctx, call, wait_s=wait_s,
                                          dispatch_file=Path(dispatch_path) if dispatch_path else None)
        except GatewayError as e:
            print(f"::error::{e}")
            return 1
    text: str = _summary(conv.plan, conv.results)
    text += f"\nWaited {conv.waits} time(s); {conv.ended}. Downstream dispatches: {len(conv.targets)}.\n"
    print(text)
    for reply in conv.replies:
        print(json.dumps({k: reply.get(k) for k in ("approved", "failed", "ignored")}))
    summary_path: str = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text)
    for r in conv.results:
        if not r["ok"]:
            print(f"::warning::{r['id']}: {r.get('error', '')}")
    if conv.failed:
        print(f"::warning::{conv.ended}")
    return 0


# -- the dispatch step ------------------------------------------------------------


def parse_repos(text: str) -> frozenset[str]:
    """The caller's hard-coded dispatch_repos: owner/repo names separated by commas, spaces or lines."""
    return frozenset(r for r in re.split(r"[\s,]+", text or "") if r)


def _dispatch_one(target: dict[str, Any], token: str, allowed: set[str], caller_repos: frozenset[str],
                  api_for: Callable[[str], Callable[..., Any]], sleep: Callable[[float], None]) -> dict[str, Any]:
    key: str = str(target.get("key", ""))
    repo: str = str(target.get("repo", ""))
    out: dict[str, Any] = {"key": key, "repo": repo, "ok": False, "status": 0, "outcome": "refused", "error": ""}
    ref: str = str(target.get("ref", ""))
    if not CLAIM_KEY.fullmatch(key) or not REPO_NAME.fullmatch(repo) or ".." in repo \
            or target.get("workflow") != FANOUT_WORKFLOW or not BRANCH_NAME.fullmatch(ref):
        out["error"] = f"a dispatch may start only {FANOUT_WORKFLOW} of an owner/repo on a branch"
        return out
    if repo not in allowed or repo not in caller_repos:
        out["error"] = f"{repo} is not in both the gateway's allowlist and this repository's dispatch_repos"
        return out
    if not token:
        out.update(outcome="no-token", error="no dispatch token is configured")
        return out
    api: Callable[..., Any] = api_for(token)
    try:
        default: str = str(api("GET", f"/repos/{repo}")["default_branch"])
    except (ApiError, KeyError, TypeError) as e:
        out.update(outcome="failed", status=getattr(e, "status", 0), error=f"reading {repo}: {type(e).__name__}")
        return out
    if default != ref:
        out["error"] = f"{ref} is not {repo}'s default branch {default}"
        return out
    for attempt in range(1, DISPATCH_ATTEMPTS + 1):
        try:
            api("POST", f"/repos/{repo}/actions/workflows/{FANOUT_WORKFLOW}/dispatches", {"ref": ref})
            out.update(ok=True, status=204, outcome="dispatched")
            return out
        except ApiError as e:
            out.update(outcome="failed", status=e.status, error=f"HTTP {e.status}")
            if e.status < 500 and e.status != 429:
                return out  # a rejected token (401/403) or a 404/422 answers the same every time
        except (OSError, ValueError) as e:
            out.update(outcome="failed", status=0, error=type(e).__name__)
        if attempt < DISPATCH_ATTEMPTS:
            sleep(2.0 * attempt)
    return out


def dispatch(doc: dict[str, Any], token: str, caller_repos: frozenset[str],
             api_for: Callable[[str], Callable[..., Any]], sleep: Callable[[float], None] = time.sleep,
             ) -> list[dict[str, Any]]:
    """Dispatch each fan-out target the run collected; one result per target, never naming the token."""
    allowed: set[str] = {str(r) for r in doc.get("allowed", [])}
    targets: list[Any] = [t for t in doc.get("targets", []) if isinstance(t, dict)][:20]
    return [_dispatch_one(t, token, allowed, caller_repos, api_for, sleep) for t in targets]


def run_dispatch() -> int:
    path: str = os.environ.get("RT_DISPATCH_FILE", "")
    if not path or not os.path.isfile(path):
        print("No downstream dispatches.")
        return 0
    with open(path, encoding="utf-8") as f:
        doc: dict[str, Any] = json.load(f)
    if not doc.get("targets"):
        print("No downstream dispatches.")
        return 0
    token: str = os.environ.get("RT_DISPATCH_TOKEN", "").strip()
    if not token:
        print("::notice::No dispatch token is configured: the downstream repositories are not dispatched; their "
              "daily release-train run picks the release up.")
    base: str = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    results: list[dict[str, Any]] = dispatch(doc, token, parse_repos(os.environ.get("RT_DISPATCH_REPOS", "")),
                                             lambda t: github_api(t, base))
    lines: list[str] = ["## Downstream dispatches", "", "| repository | result |", "| --- | --- |"]
    for r in results:
        lines.append(f"| {r['repo']} | {r['outcome']}{': ' + r['error'] if r['error'] else ''} |")
        if not r["ok"] and r["outcome"] != "no-token":
            print(f"::warning::dispatch of {r['repo']}: {r['outcome']}: {r['error']}")
    text: str = "\n".join(lines) + "\n"
    print(text)
    summary_path: str = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text)
    with tempfile.TemporaryDirectory(prefix="release-train-", dir=os.environ.get("RUNNER_TEMP")) as tmp:
        try:
            invoke(os.environ["RT_GATEWAY_FUNCTION"], {
                "phase": "dispatched", "id_token": id_token(os.environ["RT_GATEWAY_AUDIENCE"]),
                "results": [{k: r[k] for k in ("key", "ok", "status", "outcome", "error")} for r in results]}, Path(tmp))
        except GatewayError as e:
            print(f"::warning::the dispatch results were not recorded: {e}")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["run"]:
        return run()
    if argv[1:] == ["dispatch"]:
        return run_dispatch()
    print("usage: release_train.py run | dispatch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
