#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Risk-tiered review policy (reusable_review_policy.yml).

Sorts a pull request into a risk tier from facts the GitHub API reports --
changed paths, author, title, labels, and Dependabot's commit metadata -- and
reports, as the "review-policy" commit status, whether the tier's review
requirement is met:

  low     no human approval; the Claude review must be clean (Dependabot
          excepted, since the Claude review does not run for bots)
  medium  one human approval
  high    one human approval; the branch ruleset's code-owner rule, with a
          CODEOWNERS that lists only high-risk paths, picks who

The tier is also applied as a `risk:<tier>` label. Nothing here reads or runs
PR code, and the config is read from the caller's default branch, so a PR
cannot change the rules it is judged by.

In advisory mode the status is always "success" and its description says what
enforcement would decide, so the check can be observed (and registered as a
required check) before it gates anything. In enforce mode it is "success" or
"pending", and Dependabot PRs in the auto lane get auto-merge enabled when the
caller allows it.

All inputs come from environment variables set by the workflow; see main().
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

TIERS = ("low", "medium", "high")
LABEL_PREFIX = "risk:"
STATUS_CONTEXT = "review-policy"
AI_REVIEW_CONTEXT = "Claude review"
DEPENDABOT = "dependabot[bot]"
# Who GITHUB_TOKEN statuses are attributed to.
BOT_LOGINS = {"github-actions", "github-actions[bot]"}
# Conventional-commit breaking change: `feat!:` or `feat(scope)!:`.
BREAKING_RE = re.compile(r"^[a-z]+(\([^)]*\))?!:")
WORKFLOW_DIRS = (".github/workflows/", ".github/actions/")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_DESCRIPTION_CHARS = 140  # GitHub rejects longer commit status descriptions.


@dataclass(frozen=True)
class DependabotConfig:
    # Dependency files whose patch/minor bumps may merge without a human.
    auto_merge_files: tuple[str, ...] = ()
    # Packages that always need a human, e.g. ones that broke builds before.
    human_review_packages: tuple[str, ...] = ()
    # Owners whose GitHub Actions may be bumped without a human.
    trusted_action_owners: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    high_risk_paths: tuple[str, ...] = ()
    low_risk_paths: tuple[str, ...] = ()
    low_risk_exclude: tuple[str, ...] = ()
    low_risk_max_changed_lines: int = 0
    # Authors whose PRs always need a human, e.g. the release bot.
    human_review_authors: tuple[str, ...] = ()
    # author_association values allowed into the low tier.
    trusted_associations: tuple[str, ...] = ("OWNER", "MEMBER", "COLLABORATOR")
    dependabot: DependabotConfig = field(default_factory=DependabotConfig)

    @classmethod
    def parse(cls, text: str) -> Config:
        data = tomllib.loads(text)
        dependabot = data.pop("dependabot", {})
        if not isinstance(dependabot, dict):
            raise ValueError("config key dependabot must be a table")
        known = set(cls.__dataclass_fields__) - {"dependabot"}
        unknown = set(data) - known
        unknown_dependabot = set(dependabot) - set(DependabotConfig.__dataclass_fields__)
        if unknown or unknown_dependabot:
            raise ValueError(f"unknown config keys: {sorted(unknown | unknown_dependabot)}")
        for key, value in [*data.items(), *dependabot.items()]:
            ok = (
                isinstance(value, int) and not isinstance(value, bool)
                if key == "low_risk_max_changed_lines"
                else isinstance(value, list) and all(isinstance(v, str) for v in value)
            )
            if not ok:
                raise ValueError(f"config key {key} has the wrong type: {value!r}")
        return cls(
            **{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()},
            dependabot=DependabotConfig(**{k: tuple(v) for k, v in dependabot.items()}),
        )


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` do not; a trailing `/` matches the whole tree."""
    if pattern.endswith("/"):
        pattern += "**"
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def matching(path: str, patterns: Iterable[str]) -> str | None:
    return next((p for p in patterns if glob_regex(p).match(path)), None)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    changes: int = 0
    previous_path: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        # A rename touches both ends: moving a file out of a high-risk path is
        # as risky as moving one in.
        return (self.path, self.previous_path) if self.previous_path else (self.path,)


@dataclass(frozen=True)
class Commit:
    author: str
    message: str
    # Signed by GitHub, as Dependabot's commits are. The author login comes from
    # the author email, which anyone can set.
    github_signed: bool = False


@dataclass(frozen=True)
class PullRequest:
    number: int
    author: str
    association: str
    title: str
    labels: tuple[str, ...]
    files: tuple[ChangedFile, ...]
    head_sha: str
    commits: tuple[Commit, ...] = ()
    # Login of whoever enabled auto-merge, or None if it is off.
    auto_merge_by: str | None = None
    # The files API lists at most 3000 files.
    files_truncated: bool = False


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    update_type: str  # semver-major | semver-minor | semver-patch, or "" if unknown


def parse_dependabot_metadata(message: str) -> list[Dependency] | None:
    """The `updated-dependencies:` block Dependabot puts in its commit message."""
    block = re.search(r"^updated-dependencies:\n(.*?)^\.\.\.$", message, re.M | re.S)
    if not block:
        return None
    deps: list[Dependency] = []
    current: dict[str, str] = {}
    for line in block.group(1).splitlines():
        m = re.match(r"^(- |  )([a-z-]+): ?(.*)$", line)
        if not m:
            continue
        if m.group(1) == "- " and current:
            deps.append(_dependency(current))
            current = {}
        current[m.group(2)] = m.group(3).strip().strip("'\"")
    if current:
        deps.append(_dependency(current))
    return deps or None


def infer_update_type(old: str, new: str) -> str:
    """semver-major/minor/patch from the first version component that changed, or "" if unknown.

    Only single versions or pins compare safely. A range ("<8" to "<9") can
    admit a new major while its first bound stays put, so it is unknown.
    """
    if any(c in old + new for c in "<,!~|"):
        return ""
    old_parts, new_parts = re.findall(r"\d+", old), re.findall(r"\d+", new)
    if not old_parts or not new_parts or len(old_parts) != len(new_parts):
        return ""
    for i, (a, b) in enumerate(zip(old_parts, new_parts)):
        if int(a) != int(b):
            return ("semver-major", "semver-minor", "semver-patch")[min(i, 2)]
    return "semver-patch"


def _dependency(fields: dict[str, str]) -> Dependency:
    return Dependency(
        name=fields.get("dependency-name", ""),
        version=fields.get("dependency-version", ""),
        update_type=fields.get("update-type", "").removeprefix("version-update:"),
    )


@dataclass(frozen=True)
class Classification:
    tier: str
    reasons: tuple[str, ...]
    # Dependabot PRs only: "auto" may merge without a human, "human" may not.
    lane: str | None = None


def high_risk_reasons(pr: PullRequest, config: Config) -> list[str]:
    high = [
        f"touches high-risk path {path} ({pattern})"
        for f in pr.files
        for path in f.paths
        if (pattern := matching(path, config.high_risk_paths))
    ]
    if pr.files_truncated:
        high.append("too many changed files to classify")
    return high


def classify(pr: PullRequest, config: Config) -> Classification:
    if pr.author == DEPENDABOT:
        if "security" in pr.labels or pr.files_truncated:
            return Classification("high", ("labeled security or too many files",), lane="human")
        c = classify_dependabot(pr, config.dependabot)
        # Auto-lane files sit under high-risk paths too; the lane rules decide
        # those. A bump that needs a human gets the path's tier.
        high = high_risk_reasons(pr, config) if c.lane == "human" else []
        return replace(c, tier="high", reasons=c.reasons + tuple(high)) if high else c

    high = high_risk_reasons(pr, config)
    if BREAKING_RE.match(pr.title):
        high.append("title marks a breaking change")
    if "security" in pr.labels:
        high.append("labeled security")
    if high:
        return Classification("high", tuple(high))
    if pr.author in config.human_review_authors:
        return Classification("medium", (f"PRs by {pr.author} always get a human review",))

    not_low: list[str] = []
    if pr.association not in config.trusted_associations:
        not_low.append(f"author association {pr.association} is not trusted for the low tier")
    for f in pr.files:
        for path in f.paths:
            if not matching(path, config.low_risk_paths):
                not_low.append(f"{path} is not a low-risk path")
            elif matching(path, config.low_risk_exclude):
                not_low.append(f"{path} is excluded from the low tier")
    changed = sum(f.changes for f in pr.files)
    if changed > config.low_risk_max_changed_lines:
        not_low.append(f"{changed} changed lines exceeds the low-tier limit of {config.low_risk_max_changed_lines}")
    if not pr.files:
        not_low.append("no changed files")
    if not_low:
        return Classification("medium", tuple(not_low))
    return Classification("low", ("only low-risk paths, from a trusted author",))


def classify_dependabot(pr: PullRequest, config: DependabotConfig) -> Classification:
    # Any commit by someone else, merges included (a merge can carry arbitrary
    # changes), means a person changed the update, as with the xa11y fix-ups.
    # Dependabot rebases its own PRs, so "@dependabot rebase" keeps the lane.
    edited = [c for c in pr.commits if c.author != DEPENDABOT or not c.github_signed]
    if edited:
        return Classification("medium", (f"has commits by {edited[0].author}, not just Dependabot",), lane="human")
    found = next(
        ((c, d) for c in reversed(pr.commits) if (d := parse_dependabot_metadata(c.message))),
        None,
    )
    if found is None:
        return Classification("medium", ("no Dependabot metadata in its commits",), lane="human")
    commit, deps = found
    if len(deps) == 1 and not deps[0].update_type:
        # Requirement-range updates ("from ==2.3.* to ==2.4.*") carry no
        # update-type; infer it from Dependabot's own subject (the PR title is
        # editable), and only if it agrees with dependency-version.
        m = re.search(r" from (\S+) to (\S+?)(?: \(#\d+\))?$", commit.message.splitlines()[0])
        new_parts = re.findall(r"\d+", m.group(2)) if m else []
        version_parts = re.findall(r"\d+", deps[0].version)
        if m and new_parts and version_parts[: len(new_parts)] == new_parts:
            deps = [replace(deps[0], update_type=infer_update_type(m.group(1), m.group(2)))]
    human: list[str] = []
    is_actions = any(f.path.startswith(WORKFLOW_DIRS) for f in pr.files)
    for f in pr.files:
        if f.path.startswith(WORKFLOW_DIRS):
            continue
        if not matching(f.path, config.auto_merge_files):
            human.append(f"updates {f.path}, which is not an auto-merge file")
    for dep in deps:
        if dep.update_type not in ("semver-minor", "semver-patch"):
            human.append(f"{dep.name} {dep.version} is a {dep.update_type or 'unknown'} update")
        elif dep.update_type == "semver-minor" and dep.version.startswith("0."):
            human.append(f"{dep.name} {dep.version} is a 0.x minor update, which semver allows to break")
        if dep.name in config.human_review_packages:
            human.append(f"{dep.name} always gets a human review")
        if is_actions and "/" in dep.name and dep.name.split("/")[0] not in config.trusted_action_owners:
            human.append(f"{dep.name} is not from a trusted action owner")
    if human:
        return Classification("medium", tuple(human), lane="human")
    names = ", ".join(f"{d.name} {d.version}" for d in deps)
    return Classification("low", (f"patch/minor update of {names}",), lane="auto")


@dataclass(frozen=True)
class Review:
    user: str
    is_bot: bool
    state: str
    commit_id: str
    association: str = "NONE"


def latest_reviews(pr: PullRequest, reviews: Iterable[Review], trusted: Iterable[str]) -> dict[str, Review]:
    """Each trusted human's latest approval or rejection, the PR author's excluded.

    Only members and collaborators count: on a public repo anyone can submit
    a review, and with no approvals required by the ruleset this status is
    the only gate.
    """
    latest: dict[str, Review] = {}
    for r in reviews:
        # COMMENTED reviews do not change an earlier approval or rejection.
        if r.state != "COMMENTED" and not r.is_bot and r.user != pr.author and r.association in trusted:
            latest[r.user] = r
    return latest


def approvers(pr: PullRequest, reviews: Iterable[Review], trusted: Iterable[str]) -> list[str]:
    """Trusted humans whose latest review approves the head commit and who wrote none of its commits."""
    committers = {c.author for c in pr.commits}
    return sorted(
        u
        for u, r in latest_reviews(pr, reviews, trusted).items()
        if r.state == "APPROVED" and r.commit_id == pr.head_sha and u not in committers
    )


def decide(
    pr: PullRequest,
    classification: Classification,
    reviews: Iterable[Review],
    ai_review_state: str | None,
    trusted: Iterable[str] = Config.trusted_associations,
) -> tuple[bool, str]:
    """Whether the tier's requirement is met, and a one-line reason."""
    tier = classification.tier
    reviews = list(reviews)
    blocking = sorted(u for u, r in latest_reviews(pr, reviews, trusted).items() if r.state == "CHANGES_REQUESTED")
    if blocking:
        return False, f"{tier.capitalize()} risk: changes requested by {', '.join(blocking)}"
    if tier == "low":
        if pr.author == DEPENDABOT:
            # The Claude review skips bots; CI is what checks a dependency bump.
            return True, "Low risk: no human approval needed"
        if ai_review_state == "success":
            return True, "Low risk: Claude review is clean, no human approval needed"
        if ai_review_state in (None, "pending"):
            return False, "Low risk: waiting for the Claude review"
        if ai_review_state == "error":
            return False, "Low risk: Claude review did not finish; re-run it"
        return False, "Low risk: Claude review has open findings"
    who = approvers(pr, reviews, trusted)
    if who:
        return True, f"{tier.capitalize()} risk: approved by {', '.join(who)}"
    owner = " (code owner)" if tier == "high" else ""
    return False, f"{tier.capitalize()} risk: needs 1 human approval{owner}"


def status_for(met: bool, reason: str, enforce: bool) -> tuple[str, str]:
    if enforce:
        state, description = ("success" if met else "pending"), reason
    else:
        state, description = "success", f"Advisory, would {'pass' if met else 'wait'}: {reason}"
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1] + "…"
    return state, description


# ── GitHub plumbing ──


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


def fetch_pr(repo: str, number: int) -> PullRequest:
    pr = gh(f"repos/{repo}/pulls/{number}")
    pages = gh("--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files?per_page=100")
    files = tuple(
        ChangedFile(path=f["filename"], changes=f.get("changes", 0), previous_path=f.get("previous_filename"))
        for page in pages
        for f in page
    )
    pages = gh("--paginate", "--slurp", f"repos/{repo}/pulls/{number}/commits?per_page=100")
    commits = tuple(
        Commit(
            author=(c.get("author") or {}).get("login", ""),
            message=c["commit"]["message"],
            github_signed=(c["commit"].get("verification") or {}).get("verified") is True
            and (c.get("committer") or {}).get("login") == "web-flow",
        )
        for page in pages
        for c in page
    )
    return PullRequest(
        number=number,
        author=pr["user"]["login"],
        association=pr["author_association"],
        title=pr["title"],
        labels=tuple(label["name"] for label in pr["labels"]),
        files=files,
        head_sha=pr["head"]["sha"],
        commits=commits,
        auto_merge_by=((pr.get("auto_merge") or {}).get("enabled_by") or {}).get("login"),
        files_truncated=len(files) != pr["changed_files"],
    )


def fetch_reviews(repo: str, number: int) -> list[Review]:
    pages = gh("--paginate", "--slurp", f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    return [
        Review(
            user=r["user"]["login"],
            is_bot=r["user"]["type"] == "Bot",
            state=r["state"],
            commit_id=r["commit_id"],
            association=r.get("author_association", "NONE"),
        )
        for page in pages
        for r in page
        if r.get("user")
    ]


def fetch_statuses(repo: str, sha: str) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Latest (state, description) per context: from this repo's workflows, and from anyone.

    Anyone with write access can post a status under any context name, so a
    "Claude review" from anyone else must not satisfy the low tier. The
    newest from anyone is what GitHub shows, so it decides whether to repost.
    """
    trusted: dict[str, tuple[str, str]] = {}
    newest: dict[str, tuple[str, str]] = {}
    # Newest first; the first of each context is its current state.
    for s in gh(f"repos/{repo}/commits/{sha}/statuses?per_page=100") or []:
        value = (s["state"], s.get("description") or "")
        newest.setdefault(s["context"], value)
        if (s.get("creator") or {}).get("login") in BOT_LOGINS:
            trusted.setdefault(s["context"], value)
    return trusted, newest


def apply_label(repo: str, pr: PullRequest, tier: str) -> None:
    wanted = LABEL_PREFIX + tier
    for label in pr.labels:
        if label.startswith(LABEL_PREFIX) and label != wanted:
            gh("-X", "DELETE", f"repos/{repo}/issues/{pr.number}/labels/{label}", check=False)
    if wanted not in pr.labels:
        gh("-X", "POST", f"repos/{repo}/issues/{pr.number}/labels", input_json={"labels": [wanted]}, check=False)


def set_status(repo: str, sha: str, state: str, description: str, run_url: str) -> None:
    gh(
        "-X", "POST", f"repos/{repo}/statuses/{sha}",
        "-f", f"state={state}",
        "-f", f"context={STATUS_CONTEXT}",
        "-f", f"description={description}",
        "-f", f"target_url={run_url}",
    )  # fmt: skip


def set_auto_merge(repo: str, number: int, enable: bool, head_sha: str = "") -> None:
    # --match-head-commit: a push after arming must not merge unevaluated.
    flag = ["--auto", "--squash", "--match-head-commit", head_sha] if enable else ["--disable-auto"]
    proc = subprocess.run(["gh", "pr", "merge", str(number), "--repo", repo, *flag], capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            f"::warning::could not {'enable' if enable else 'disable'} auto-merge: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def write_summary(pr: PullRequest, c: Classification, state: str, description: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"### Review policy: PR #{pr.number} is `{c.tier}` risk" + (f" (Dependabot lane: {c.lane})" if c.lane else ""),
        "",
        *[f"- {r}" for r in c.reasons],
        "",
        f"Status `{state}`: {description}",
    ]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def evaluate(
    repo: str, number: int, head_sha: str, config: Config, *, enforce: bool, auto_merge: bool, run_url: str
) -> None:
    pr = fetch_pr(repo, number)
    if pr.head_sha != head_sha:
        print(f"::notice::{head_sha} is no longer the head of #{number}; a newer run supersedes this one.")
        return
    classification = classify(pr, config)
    trusted, newest = fetch_statuses(repo, head_sha)
    ai_state = trusted.get(AI_REVIEW_CONTEXT, (None, ""))[0]
    met, reason = decide(pr, classification, fetch_reviews(repo, number), ai_state, config.trusted_associations)
    state, description = status_for(met, reason, enforce)
    apply_label(repo, pr, classification.tier)
    # GitHub keeps at most 1000 statuses per commit and context, and the sweep
    # re-evaluates every open PR every few minutes: post only on change.
    if newest.get(STATUS_CONTEXT) != (state, description):
        # Another run (a sweep and an event can overlap) may have posted from
        # fresher reviews since we read; let its status stand.
        if fetch_statuses(repo, head_sha)[1].get(STATUS_CONTEXT) != newest.get(STATUS_CONTEXT):
            print(f"::notice::#{number}'s status changed while evaluating; leaving it to the newer run.")
        else:
            set_status(repo, head_sha, state, description, run_url)
    wanted = enforce and auto_merge and classification.lane == "auto" and met
    if wanted and pr.auto_merge_by is None:
        set_auto_merge(repo, number, enable=True, head_sha=head_sha)
    elif not wanted and pr.auto_merge_by in BOT_LOGINS:
        # Only undo our own, and in any mode: after a switch to advisory the
        # status always succeeds, so a stale arm would merge unreviewed.
        set_auto_merge(repo, number, enable=False)
    write_summary(pr, classification, state, description)
    print(f"tier={classification.tier} lane={classification.lane} met={met} status={state}")


def sweep(repo: str, config: Config, *, enforce: bool, auto_merge: bool, run_url: str) -> None:
    """Evaluate every open, non-draft PR.

    A fallback for events that never arrive or runs that failed.
    """
    pages = gh("--paginate", "--slurp", f"repos/{repo}/pulls?state=open&per_page=100")
    prs = [p for page in pages for p in page if not p["draft"]]
    # The status is per commit: PRs sharing a head would overwrite each other.
    heads = Counter(p["head"]["sha"] for p in prs)
    failed = 0
    for pr in prs:
        if heads[pr["head"]["sha"]] > 1:
            print(f"::warning::#{pr['number']} shares its head commit with another open PR; not evaluated.")
            continue
        try:
            evaluate(
                repo, pr["number"], pr["head"]["sha"], config, enforce=enforce, auto_merge=auto_merge, run_url=run_url
            )
        except Exception as e:  # One bad PR must not stop the sweep.
            failed += 1
            print(f"::error::#{pr['number']}: {e}")
    if failed:
        raise SystemExit(f"{failed} PR(s) failed to evaluate")


def main() -> None:
    repo = os.environ["REPO"]
    number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    if number and (not number.isdigit() or not SHA_RE.match(head_sha)):
        raise SystemExit("bad PR_NUMBER or HEAD_SHA")
    config = Config.parse(Path(os.environ["CONFIG_FILE"]).read_text(encoding="utf-8"))
    mode = os.environ.get("MODE", "advisory")
    if mode not in ("advisory", "enforce"):
        raise SystemExit(f"bad MODE {mode!r}")
    options = dict(
        enforce=mode == "enforce",
        auto_merge=os.environ.get("AUTO_MERGE") == "true",
        run_url=os.environ.get("RUN_URL", ""),
    )
    if number:
        evaluate(repo, int(number), head_sha, config, **options)
    else:
        sweep(repo, config, **options)


if __name__ == "__main__":
    main()
