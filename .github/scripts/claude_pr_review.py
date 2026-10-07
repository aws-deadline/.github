#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Deterministic plumbing for the Claude PR review (reusable_claude_pr_review.yml).

The review agent only reads code and returns its findings as structured output
(claude-code-action's --json-schema); everything that talks to GitHub lives here
so it is predictable, testable, and posts no model output it has not validated.

  prepare  Before the agent runs, in the read-only review job. Reads this
           bot's prior review threads and its summary comment, decides between
           a full and an incremental review, and writes the diffs and prior
           state the agent reads.
  post     After the agent runs, in the separate post job. Validates the
           agent's findings, posts the new findings as one batched review,
           resolves threads the agent judged addressed, then updates the
           summary comment and the commit status.

Each review comment ends with a hidden marker
  <!-- claude-review fp=<path>::<category>::<symbol> sev=<severity> -->
whose fingerprint (fp) is built from stable attributes of the finding, so the
same finding yields the same fp on every revision. The summary comment carries
  <!-- claude-review-summary reviewed=<sha> -->
recording the last head commit that was fully reviewed; the next revision is
reviewed incrementally against it.

A thread the agent judges addressed gets a bot reply giving the reason, ending
in
  <!-- claude-review addressed -->
and is then resolved. GitHub only lets a token with `contents: write` resolve
review threads; without that grant the reply alone marks the thread, which
from then on counts as closed just like a resolved one.

All inputs come from environment variables set by the workflow; see main().
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SEVERITIES = ("blocking", "should-fix", "nit")
SEVERITY_LABELS = {"blocking": "Blocking", "should-fix": "Should fix", "nit": "Nit"}
# Comments posted before severities existed carry no sev; count them as
# should-fix so they keep the status red until someone resolves them.
DEFAULT_SEVERITY = "should-fix"

BOT_LOGINS = {"github-actions", "github-actions[bot]"}
STATUS_CONTEXT = "Claude review"
MAX_BODY_CHARS = 4000
MAX_REASON_CHARS = 300
# How far a mis-numbered anchor may be moved to the nearest commentable line.
MAX_LINE_SNAP = 10

FP_MARKER_RE = re.compile(r"<!-- claude-review fp=(?P<fp>\S+)(?: sev=(?P<sev>[a-z-]+))? -->")
SUMMARY_MARKER_RE = re.compile(r"<!-- claude-review-summary reviewed=(?P<sha>[0-9a-f]{40}|none) -->")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# fp parts are path-like identifiers; anything else (spaces, quotes, "-->")
# would corrupt the marker.
FP_PART_RE = re.compile(r"[^A-Za-z0-9_./+\-]")
ADDRESSED_MARKER = "<!-- claude-review addressed -->"
HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
SIDES = ("RIGHT", "LEFT")

# Per side, each file's commentable line numbers.
DiffLines = dict[str, dict[str, set[int]]]


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def diff_lines(diff_text: str) -> DiffLines:
    """Map each file to the line numbers a review comment can anchor to, per side.

    RIGHT holds the new-side numbers of added and context lines; LEFT holds the
    old-side numbers of removed and context lines: exactly the lines GitHub
    accepts for a review comment on that side of this diff. Files are keyed by
    their new path, or by their old path when deleted (as GitHub does), so a
    finding about removed code -- including a whole deleted file -- has a
    LEFT anchor.
    """
    lines: DiffLines = {side: {} for side in SIDES}
    old_path: str | None = None
    path: str | None = None
    old_line = new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            old_path = path = None
            old_line = new_line = 0
        elif raw.startswith("--- ") and path is None:
            source = raw[4:]
            old_path = source[2:] if source.startswith("a/") else None
        elif raw.startswith("+++ ") and path is None:
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else old_path
            if path is not None:
                for side in SIDES:
                    lines[side].setdefault(path, set())
        elif raw.startswith("@@"):
            m = HUNK_RE.match(raw)
            old_line, new_line = (int(m.group("old")), int(m.group("new"))) if m else (0, 0)
        elif path is not None and (old_line or new_line):
            if raw.startswith("+"):
                lines["RIGHT"][path].add(new_line)
                new_line += 1
            elif raw.startswith("-"):
                lines["LEFT"][path].add(old_line)
                old_line += 1
            elif raw.startswith(" "):
                lines["RIGHT"][path].add(new_line)
                lines["LEFT"][path].add(old_line)
                new_line += 1
                old_line += 1
    for side in SIDES:
        lines[side] = {p: s for p, s in lines[side].items() if s}
    return lines


def make_fp(path: str, category: str, symbol: str) -> str:
    parts = [FP_PART_RE.sub("-", p.strip()) or "unknown" for p in (path, category, symbol)]
    return "::".join(parts)


def parse_fp_marker(body: str) -> tuple[str, str] | None:
    m = FP_MARKER_RE.search(body or "")
    if not m:
        return None
    sev = m.group("sev")
    return m.group("fp"), sev if sev in SEVERITIES else DEFAULT_SEVERITY


@dataclass
class Thread:
    id: str
    fp: str
    severity: str
    path: str
    line: int | None
    # Resolved in GitHub, or marked addressed by this bot.
    is_resolved: bool
    is_outdated: bool
    first_comment_id: int | None
    body: str
    replies: list[dict[str, str]] = field(default_factory=list)
    # Marked addressed but still open in GitHub (marked while the token could
    # not resolve threads); resolved as soon as it can be.
    needs_resolve: bool = False


def parse_threads(nodes: Iterable[dict[str, Any]]) -> list[Thread]:
    """Keep the review threads this bot started, identified by the fp marker."""
    threads = []
    for node in nodes:
        comments = (node.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        # Anyone can open a review thread, and the fp format is predictable:
        # a forged marker would suppress the real finding, skew the open
        # counts, or get a human's thread replied to and resolved.
        if ((comments[0].get("author") or {}).get("login")) not in BOT_LOGINS:
            continue
        parsed = parse_fp_marker(comments[0].get("body", ""))
        if parsed is None:
            continue
        fp, sev = parsed
        github_resolved = bool(node.get("isResolved"))
        addressed = any(
            ((c.get("author") or {}).get("login") in BOT_LOGINS) and ADDRESSED_MARKER in (c.get("body") or "")
            for c in comments[1:]
        )
        threads.append(
            Thread(
                id=node["id"],
                fp=fp,
                severity=sev,
                path=node.get("path") or "",
                line=node.get("line"),
                is_resolved=github_resolved or addressed,
                is_outdated=bool(node.get("isOutdated")),
                first_comment_id=comments[0].get("databaseId"),
                body=FP_MARKER_RE.sub("", comments[0].get("body", "")).strip(),
                replies=[
                    {
                        "author": ((c.get("author") or {}).get("login") or ""),
                        "body": (c.get("body") or "")[:1500],
                    }
                    for c in comments[1:]
                ],
                needs_resolve=addressed and not github_resolved,
            )
        )
    return threads


def suppressed_fps(threads: Iterable[Thread]) -> set[str]:
    """Findings that must not be posted again.

    Everything except outdated-and-unresolved threads: those lost their line,
    so the same finding may be re-anchored on the new code.
    """
    return {t.fp for t in threads if t.is_resolved or not t.is_outdated}


def open_counts(threads: Iterable[Thread]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for t in threads:
        if not t.is_resolved:
            counts[t.severity] += 1
    return counts


def find_summary(comments: Iterable[dict[str, Any]]) -> tuple[int, str | None] | None:
    """Return (comment id, reviewed sha) of this bot's newest summary comment.

    Only comments authored by the Actions bot count. Anyone can write the marker
    into a comment, and trusting a forged `reviewed=<sha>` would let a PR author
    shrink the next incremental review to nothing.
    """
    found = None
    for c in comments:
        if ((c.get("user") or {}).get("login")) not in BOT_LOGINS:
            continue
        m = SUMMARY_MARKER_RE.search(c.get("body") or "")
        if m:
            sha = m.group("sha")
            found = (c["id"], None if sha == "none" else sha)
    return found


@dataclass
class Finding:
    path: str
    line: int
    severity: str
    fp: str
    body: str
    side: str = "RIGHT"


@dataclass
class Resolution:
    fp: str
    reason: str


def parse_agent_output(text: str) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Flatten the agent's structured output into finding and resolve records.

    Returns None when there is no usable output at all (the agent did not
    finish), so the caller can tell "found nothing" from "did not run".
    """
    try:
        data = json.loads(text) if text and text.strip() else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for key, kind in (("findings", "finding"), ("resolutions", "resolve")):
        items = data.get(key) or []
        if not isinstance(items, list):
            errors.append(f"{key} is not a list")
            continue
        for item in items:
            if isinstance(item, dict):
                records.append({**item, "kind": kind})
            else:
                errors.append(f"{key} entry is not an object")
    return records, errors


def snap_line(line: int, valid: set[int]) -> int | None:
    """Nearest commentable line to `line`, if one is within MAX_LINE_SNAP.

    Models sometimes cite a line a few off; a slightly misplaced anchor beats
    dropping the finding.
    """
    if line in valid:
        return line
    best = min(valid, key=lambda v: (abs(v - line), v), default=None)
    return best if best is not None and abs(best - line) <= MAX_LINE_SNAP else None


def select_findings(
    records: Iterable[dict[str, Any]],
    *,
    mode: str,
    pr_lines: DiffLines,
    interdiff_lines: DiffLines,
    suppress: set[str],
    live: set[str] = frozenset(),
) -> tuple[list[Finding], list[Resolution], list[str]]:
    """Validate the agent's output and apply the posting rules.

    - A finding must anchor to a line GitHub will accept on its side of the
      PR diff (snapped to the nearest one when it is slightly off). LEFT
      anchors removed code by its old line number.
    - Its fp must not already be suppressed. Two findings in one run that
      share an fp are distinct issues on the same symbol (the agent does not
      repeat itself within a run), so later ones get a numeric suffix.
      Suffixes skip only `suppress`, the threads seen when the review began,
      so a re-run of the post job names each finding the same way. `live`
      holds the threads on the PR now; a finding whose final fp is among
      them already landed (in an earlier attempt of this job) and is dropped.
    - Incremental reviews post no nits, and post should-fix findings only on
      lines this revision changed; blocking findings may land anywhere in the
      PR diff, since a missed blocker is worth raising late. Old-side line
      numbers of the PR diff and the interdiff count from different commits,
      so a LEFT should-fix only needs this revision to have removed code from
      the same file.
    """
    findings: list[Finding] = []
    resolutions: list[Resolution] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for r in records:
        kind = r.get("kind", "finding")
        if kind == "resolve":
            fp = str(r.get("fp", "")).strip()
            reason = str(r.get("reason", "")).strip()
            if fp and reason:
                resolutions.append(Resolution(fp=fp, reason=reason[:MAX_REASON_CHARS]))
            else:
                dropped.append(f"resolve entry missing fp/reason: {r!r:.120}")
            continue
        path = str(r.get("path", "")).strip()
        severity = str(r.get("severity", "")).strip()
        side = str(r.get("side") or "RIGHT").strip().upper()
        body = str(r.get("body", "")).strip()
        try:
            line = int(r.get("line"))
        except (TypeError, ValueError):
            line = -1
        fp = make_fp(path, str(r.get("category", "")), str(r.get("symbol", "")))
        label = f"{fp} ({path}:{line})"
        if side not in SIDES:
            dropped.append(f"{label}: invalid side {side!r:.20}")
            continue
        snapped = snap_line(line, pr_lines[side].get(path, set()))
        changed = interdiff_lines[side].get(path, set())
        if severity not in SEVERITIES or not body:
            dropped.append(f"{label}: missing/invalid severity or body")
        elif snapped is None:
            dropped.append(f"{label}: line is not part of the PR diff")
        elif fp in suppress:
            dropped.append(f"{label}: already raised")
        elif mode == "incremental" and severity == "nit":
            dropped.append(f"{label}: nit on an incremental review")
        elif (
            mode == "incremental"
            and severity != "blocking"
            and not (changed if side == "LEFT" else {line, snapped} & changed)
        ):
            dropped.append(f"{label}: {severity} on code this revision did not change")
        else:
            if snapped != line:
                print(f"note: {label}: anchored at nearest diff line {snapped}")
                line = snapped
            n = 2
            base = fp
            while fp in seen or fp in suppress:
                fp, n = f"{base}-{n}", n + 1
            seen.add(fp)
            if fp in live:
                dropped.append(f"{fp} ({path}:{line}): already posted")
                continue
            findings.append(Finding(path=path, line=line, severity=severity, fp=fp, body=body, side=side))
    return findings, resolutions, dropped


def defang(text: str) -> str:
    """Neutralize HTML comments in model text before posting it.

    Markers are matched by search, so a marker the PR induced the model to
    write would take precedence over the one we append (relabelling the
    finding's fp or severity), or forge an addressed or summary marker.
    """
    return text.replace("<!--", "&lt;!--")


def render_comment(f: Finding) -> str:
    body = defang(f.body[:MAX_BODY_CHARS])
    return f"**{SEVERITY_LABELS[f.severity]}:** {body}\n\n<!-- claude-review fp={f.fp} sev={f.severity} -->"


def status_for(counts: dict[str, int], agent_ok: bool, unposted: int = 0) -> tuple[str, str]:
    """Commit status state + description. Nits never hold the status red."""
    if not agent_ok:
        return "error", "Review did not finish; push again or re-run to retry"
    if unposted:
        return "error", f"{unposted} finding{'s' if unposted != 1 else ''} could not be posted; re-run to retry"
    must = counts["blocking"] + counts["should-fix"]
    if must == 0:
        nits = f" ({counts['nit']} nit{'s' if counts['nit'] != 1 else ''} open)" if counts["nit"] else ""
        return "success", f"No blocking or should-fix findings open{nits}"
    return "failure", f"Open: {counts['blocking']} blocking, {counts['should-fix']} should-fix, {counts['nit']} nit"


def render_summary(
    *,
    reviewed_sha: str | None,
    head_sha: str,
    mode: str,
    since_sha: str | None,
    agent_ok: bool,
    counts: dict[str, int],
    posted: int,
    resolved: int,
    run_url: str,
    unposted: Iterable[Finding] = (),
) -> str:
    short = head_sha[:7]
    unposted = list(unposted)
    if not agent_ok:
        headline = f"Review of `{short}` did not finish ([run]({run_url})). The next push retries it."
    elif unposted:
        headline = (
            f"Reviewed `{short}`, but {len(unposted)} of {posted + len(unposted)} new findings could not be "
            f"posted ([run]({run_url})); they are listed below. Re-run the workflow or push again to retry."
        )
    elif mode == "incremental" and since_sha:
        headline = f"Reviewed `{short}` (changes since `{since_sha[:7]}`): {posted} new, {resolved} resolved."
    else:
        headline = f"Reviewed `{short}` (full review): {posted} finding{'s' if posted != 1 else ''}."
    must = counts["blocking"] + counts["should-fix"]
    if must:
        state = "❌ Address or reply to the open threads."
    elif agent_ok and not unposted:
        state = "✅ Nothing blocking."
    else:
        state = ""
    unposted_list = "".join(
        f"- **{SEVERITY_LABELS[f.severity]}** `{f.path}:{f.line}`{' (removed code)' if f.side == 'LEFT' else ''}: "
        f"{defang(' '.join(f.body.split()))[:500]}\n"
        for f in unposted
    )
    return (
        "**Claude review** · advisory\n\n"
        f"{headline}\n\n"
        f"Open: **{counts['blocking']} blocking** · {counts['should-fix']} should-fix · {counts['nit']} nit. {state}".rstrip()
        + "\n\n"
        + (f"{unposted_list}\n" if unposted_list else "")
        + "<sub>Fix or reply to each thread; the next revision's review re-checks open threads and resolves "
        "those it agrees are handled. Resolving a thread yourself also closes it. Later revisions review only "
        "what changed.</sub>\n\n"
        f"<!-- claude-review-summary reviewed={reviewed_sha or 'none'} -->"
    )


# ---------------------------------------------------------------------------
# GitHub / git I/O
# ---------------------------------------------------------------------------


def gh(*args: str, input_json: Any = None, check: bool = True) -> Any:
    proc = subprocess.run(
        ["gh", "api", *args] + (["--input", "-"] if input_json is not None else []),
        input=json.dumps(input_json) if input_json is not None else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"gh api {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        print(f"::warning::gh api {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return None
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def git(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


THREADS_QUERY = """
query($owner:String!,$repo:String!,$pr:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path line
          comments(first:20){ nodes{ databaseId body author{ login } } }
        }
      }
    }
  }
}"""


def fetch_threads(repo: str, pr: int) -> list[Thread]:
    owner, name = repo.split("/", 1)
    nodes, cursor = [], None
    while True:
        args = ["graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"repo={name}", "-F", f"pr={pr}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = gh(*args)["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return parse_threads(nodes)
        cursor = page["pageInfo"]["endCursor"]


def fetch_summary(repo: str, pr: int) -> tuple[int, str | None] | None:
    pages = gh("--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments?per_page=100")
    return find_summary(c for page in pages for c in page)


def set_status(repo: str, sha: str, state: str, description: str, run_url: str) -> None:
    # Best effort: the caller must grant `statuses: write`. Without it the
    # summary comment still carries the same signal.
    gh(
        f"repos/{repo}/statuses/{sha}",
        "-f", f"state={state}",
        "-f", f"context={STATUS_CONTEXT}",
        "-f", f"description={description[:140]}",
        "-f", f"target_url={run_url}",
        check=False,
    )


def write_output(**values: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(lines)
    else:
        sys.stdout.write(lines)


@dataclass
class Env:
    repo: str
    pr: int
    head_sha: str
    base_sha: str
    checkout: str
    context_dir: Path
    run_url: str

    @classmethod
    def load(cls) -> "Env":
        e = os.environ
        head, base = e["HEAD_SHA"], e["BASE_SHA"]
        if not (SHA_RE.match(head) and SHA_RE.match(base)):
            raise SystemExit("::error::bad head/base sha")
        return cls(
            repo=e["REPO"],
            pr=int(e["PR_NUMBER"]),
            head_sha=head,
            base_sha=base,
            checkout=e.get("CHECKOUT_DIR", "pr-head"),
            context_dir=Path(e.get("CONTEXT_DIR", "review-context")),
            run_url=e.get("RUN_URL", ""),
        )


def has_commit(checkout: str, sha: str) -> bool:
    return subprocess.run(["git", "-C", checkout, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


def ensure_commit(checkout: str, sha: str) -> bool:
    if has_commit(checkout, sha):
        return True
    # A force-pushed-away commit is not reachable from the checkout's refs, but
    # GitHub still serves it by SHA. Private repos fail here (no credentials are
    # persisted), which just falls back to a full review.
    subprocess.run(["git", "-C", checkout, "fetch", "--quiet", "origin", sha], capture_output=True)
    return has_commit(checkout, sha)


def pr_diff_base(env: Env) -> str:
    # GitHub shows a PR as base...head (from the merge base); match it so line
    # anchors agree with what GitHub accepts.
    mb = git(env.checkout, "merge-base", env.base_sha, env.head_sha, check=False).strip()
    return mb if SHA_RE.match(mb) else env.base_sha


def prepare(env: Env) -> None:
    env.context_dir.mkdir(parents=True, exist_ok=True)

    threads = fetch_threads(env.repo, env.pr)
    summary = fetch_summary(env.repo, env.pr)
    prior = summary[1] if summary else None

    diff_base = pr_diff_base(env)
    pr_diff = git(env.checkout, "diff", f"{diff_base}..{env.head_sha}")
    (env.context_dir / "pr.diff").write_text(pr_diff, encoding="utf-8")
    pr_files = sorted({p for side in diff_lines(pr_diff).values() for p in side})

    mode = "full"
    if prior == env.head_sha:
        mode = "incremental"  # re-run of an already-reviewed commit: nothing new to read
    elif prior and ensure_commit(env.checkout, prior):
        mode = "incremental"
    interdiff = ""
    if mode == "incremental" and pr_files:
        # Restrict to the PR's files so a rebase onto a newer base does not
        # surface unrelated upstream changes as "new in this revision".
        interdiff = git(env.checkout, "diff", f"{prior}..{env.head_sha}", "--", *pr_files, check=False)
    (env.context_dir / "interdiff.diff").write_text(interdiff, encoding="utf-8")

    state = {
        "mode": mode,
        "reviewed_sha": prior if mode == "incremental" else None,
        "suppress": sorted(suppressed_fps(threads)),
        "open_threads": [
            {
                "fp": t.fp,
                "severity": t.severity,
                "path": t.path,
                "line": t.line,
                "outdated": t.is_outdated,
                "body": t.body[:1500],
                "replies": t.replies,
            }
            for t in threads
            if not t.is_resolved
        ],
    }
    (env.context_dir / "prior-review-state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(
        f"mode={mode} prior={prior} diff_base={diff_base} files={len(pr_files)} "
        f"threads={len(threads)} open={len(state['open_threads'])} suppress={len(state['suppress'])}"
    )
    write_output(mode=mode, prior_sha=prior or "", diff_base=diff_base)


def post_review(env: Env, findings: list[Finding]) -> list[Finding]:
    """Post the findings as one review; return the ones that could not be posted."""
    if not findings:
        return []
    comments = [{"path": f.path, "line": f.line, "side": f.side, "body": render_comment(f)} for f in findings]
    if gh(
        f"repos/{env.repo}/pulls/{env.pr}/reviews",
        input_json={"commit_id": env.head_sha, "event": "COMMENT", "comments": comments},
        check=False,
    ) is not None:
        return []
    # One bad anchor rejects the whole batch; fall back to posting singly so the
    # rest still land.
    return [
        f
        for f, c in zip(findings, comments)
        if gh(f"repos/{env.repo}/pulls/{env.pr}/comments", input_json={"commit_id": env.head_sha, **c}, check=False) is None
    ]


RESOLVE_MUTATION = "mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }"


def close_addressed(env: Env, threads: list[Thread], resolutions: list[Resolution]) -> int:
    """Reply with the reason and resolve each thread the agent judged addressed.

    The marked reply is what counts; resolving is best effort and needs
    `contents: write` from the caller. Threads marked on an earlier run but
    still open in GitHub are resolved too. After the first refusal the rest are
    only marked, rather than repeating a call that will fail.
    """
    by_fp: dict[str, list[Thread]] = {}
    for t in threads:
        if not t.is_resolved and t.first_comment_id:
            by_fp.setdefault(t.fp, []).append(t)
    to_resolve = [t for t in threads if t.needs_resolve]
    done = 0
    for r in resolutions:
        for t in by_fp.pop(r.fp, []):
            if gh(
                f"repos/{env.repo}/pulls/{env.pr}/comments/{t.first_comment_id}/replies",
                "-f", f"body=Addressed: {defang(r.reason)}\n\n{ADDRESSED_MARKER}",
                check=False,
            ) is not None:
                done += 1
                to_resolve.append(t)
    for t in to_resolve:
        if gh("graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={t.id}", check=False) is None:
            print(
                "::notice::Could not resolve review threads; grant `contents: write` to let the review "
                "resolve threads it marks addressed."
            )
            break
    return done


def post(env: Env, *, agent_ok: bool, agent_output: str, mode: str, prior_sha: str | None) -> None:
    state_path = env.context_dir / "prior-review-state.json"
    suppress = set(json.loads(state_path.read_text(encoding="utf-8"))["suppress"]) if state_path.exists() else set()
    # What is on the PR now: a re-run of this job after a partial post must not
    # post the findings that did land a second time.
    live = suppressed_fps(fetch_threads(env.repo, env.pr))
    pr_lines = diff_lines((env.context_dir / "pr.diff").read_text(encoding="utf-8"))
    interdiff_path = env.context_dir / "interdiff.diff"
    interdiff_lines = diff_lines(interdiff_path.read_text(encoding="utf-8") if interdiff_path.exists() else "")

    parsed = parse_agent_output(agent_output) if agent_ok else None
    if parsed is None:
        # Nothing usable came back (timeout, error, or malformed output):
        # report the review as unfinished rather than as a clean pass.
        agent_ok = False
        parsed = ([], [])
    records, errors = parsed
    findings, resolutions, dropped = select_findings(
        records, mode=mode, pr_lines=pr_lines, interdiff_lines=interdiff_lines, suppress=suppress, live=live
    )
    for msg in errors + dropped:
        print(f"skipped: {msg}")

    unposted = post_review(env, findings)
    posted = len(findings) - len(unposted)
    threads = fetch_threads(env.repo, env.pr)
    resolved = close_addressed(env, threads, resolutions)
    if resolved:
        threads = fetch_threads(env.repo, env.pr)
    counts = open_counts(threads)

    summary = fetch_summary(env.repo, env.pr)
    # Advance the incremental baseline only when the review completed and all
    # of it landed: a timed-out pass must not mark unread code as reviewed, nor
    # a partial post drop the unposted findings from every later review.
    reviewed = env.head_sha if agent_ok and not unposted else (summary[1] if summary else None)
    body = render_summary(
        reviewed_sha=reviewed,
        head_sha=env.head_sha,
        mode=mode,
        since_sha=prior_sha,
        agent_ok=agent_ok,
        counts=counts,
        posted=posted,
        resolved=resolved,
        run_url=env.run_url,
        unposted=unposted,
    )
    if summary:
        gh(f"repos/{env.repo}/issues/comments/{summary[0]}", "-X", "PATCH", "-f", f"body={body}")
    else:
        gh(f"repos/{env.repo}/issues/{env.pr}/comments", "-f", f"body={body}")

    state, description = status_for(counts, agent_ok, len(unposted))
    set_status(env.repo, env.head_sha, state, description, env.run_url)
    print(f"posted={posted} unposted={len(unposted)} resolved={resolved} open={counts} status={state}")


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] not in ("prepare", "post"):
        raise SystemExit("usage: claude_pr_review.py prepare|post")
    env = Env.load()
    if argv[1] == "prepare":
        prepare(env)
    else:
        mode = os.environ.get("MODE", "full")
        prior = os.environ.get("PRIOR_SHA") or None
        if prior is not None and not SHA_RE.match(prior):
            prior = None
        # The review job hands the agent's outcome and structured output over
        # in its artifact, next to the diffs and prior state.
        outcome_path = env.context_dir / "agent-outcome"
        output_path = env.context_dir / "agent-output.json"
        post(
            env,
            agent_ok=outcome_path.exists() and outcome_path.read_text(encoding="utf-8").strip() == "success",
            agent_output=output_path.read_text(encoding="utf-8") if output_path.exists() else "",
            mode=mode,
            prior_sha=prior,
        )


if __name__ == "__main__":
    main(sys.argv)
