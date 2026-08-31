# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RELEASE_TAG_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class ReadinessError(RuntimeError):
    """A release-readiness check that should fail the workflow."""


@dataclass(frozen=True)
class ReadinessResult:
    tag: str | None
    ready: bool
    status: str
    detail: str
    age_hours: int | None = None
    failure: str | None = None


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "false", "0", "no"}:
        return False
    if normalized in {"true", "1", "yes"}:
        return True
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit code {exc.returncode}"
        raise ReadinessError(f"git {' '.join(args)} failed: {detail}") from exc
    return result.stdout.strip()


def resolve_release_tag(
    requested_tag: str,
    force_publish: bool,
    default_branch_ref: str,
) -> str:
    if force_publish and not requested_tag:
        raise ReadinessError("A specific tag is required when force_publish is enabled.")
    if requested_tag:
        return requested_tag

    tags = _run_git(
        "tag",
        "--list",
        "--merged",
        default_branch_ref,
        "--sort=-version:refname",
    ).splitlines()
    tag = next((candidate for candidate in tags if RELEASE_TAG_PATTERN.fullmatch(candidate)), "")
    if not tag:
        raise ReadinessError("The default branch has no release tags.")
    return tag


def _fetch_default_branch(default_branch: str) -> str:
    try:
        _run_git("check-ref-format", "--branch", default_branch)
    except ReadinessError as exc:
        raise ReadinessError(
            f"Default branch {default_branch!r} is not a valid branch name."
        ) from exc

    default_branch_ref = f"refs/remotes/origin/{default_branch}"
    try:
        _run_git(
            "fetch",
            "--no-tags",
            "--force",
            "origin",
            f"refs/heads/{default_branch}:{default_branch_ref}",
        )
    except ReadinessError as exc:
        raise ReadinessError(f"Could not fetch default branch {default_branch}.") from exc
    return default_branch_ref


def validate_release_tag(
    tag: str,
    default_branch: str,
    default_branch_ref: str,
) -> None:
    try:
        tag_commit = _run_git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    except ReadinessError as exc:
        raise ReadinessError(f"Release tag {tag} does not exist.") from exc

    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_commit, default_branch_ref],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        raise ReadinessError(
            f"Release tag {tag} is not reachable from default branch {default_branch}."
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ReadinessError(f"Could not validate release tag {tag}: {detail}")


def get_tag_created_at(tag: str) -> int:
    value = _run_git(
        "for-each-ref",
        "--format=%(creatordate:unix)",
        f"refs/tags/{tag}",
    )
    try:
        return int(value)
    except ValueError as exc:
        raise ReadinessError(
            f"Could not determine the creation time for release tag {tag}."
        ) from exc


def _request_json(
    url: str,
    *,
    token: str | None = None,
    timeout: int = 60,
    allow_not_found: bool = False,
) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "aws-deadline-release-readiness-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return None
        raise ReadinessError(f"Request to {url} failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ReadinessError(f"Request to {url} failed: {exc.reason}.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReadinessError(f"Response from {url} is not valid JSON.") from exc


def github_release_exists(
    repository: str,
    tag: str,
    token: str,
    api_url: str = "https://api.github.com",
) -> bool:
    encoded_tag = urllib.parse.quote(tag, safe="")
    url = f"{api_url.rstrip('/')}/repos/{repository}/releases/tags/{encoded_tag}"
    release = _request_json(url, token=token, allow_not_found=True)
    if release is None:
        return False
    if not isinstance(release, dict):
        raise ReadinessError("GitHub returned an invalid release response.")
    if release.get("draft") is True:
        raise ReadinessError(f"GitHub Release {tag} exists as a draft, not a completed release.")
    return True


def inspect_manifest(
    manifest: Any,
    *,
    package: str,
    version: str,
    required_platforms: tuple[str, ...],
) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        raise ReadinessError("The Conda manifest root must be an object.")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise ReadinessError("The Conda manifest has an unsupported schema version.")
    packages = manifest.get("packages")
    if not isinstance(packages, dict):
        raise ReadinessError("The Conda manifest packages field must be an object.")

    versions = packages.get(package)
    if versions is None:
        return False, f"Package {package} is not present."
    if not isinstance(versions, dict):
        raise ReadinessError(f"The Conda manifest entry for {package} must be an object.")

    entry = versions.get(version)
    if entry is None:
        return False, f"Package {package} {version} is not present."
    if not isinstance(entry, dict):
        raise ReadinessError(f"The Conda manifest entry for {package} {version} must be an object.")

    platforms = entry.get("platforms")
    if not isinstance(platforms, list) or not all(isinstance(item, str) for item in platforms):
        raise ReadinessError(
            f"The Conda manifest platforms for {package} {version} must be an array of strings."
        )

    missing = tuple(platform for platform in required_platforms if platform not in platforms)
    if missing:
        return False, f"Missing required platforms: {', '.join(missing)}."
    return True, f"Available for {', '.join(required_platforms)}."


def evaluate_manifest(
    manifest: Any,
    *,
    tag: str,
    tag_created_at: int,
    now: int,
    timeout_seconds: int,
    force_publish: bool,
    package: str,
    required_platforms: tuple[str, ...],
) -> ReadinessResult:
    available, detail = inspect_manifest(
        manifest,
        package=package,
        version=tag,
        required_platforms=required_platforms,
    )
    if available:
        return ReadinessResult(
            tag=tag,
            ready=True,
            status="Ready to complete",
            detail=detail,
        )

    age_seconds = max(0, now - tag_created_at)
    age_hours = age_seconds // 3600
    timed_out = age_seconds >= timeout_seconds

    if timed_out and not force_publish:
        age_days = age_seconds // (24 * 60 * 60)
        timeout_days = timeout_seconds // (24 * 60 * 60)
        failure = (
            f"Release {tag} has waited {age_days} days for Conda, "
            f"reaching the {timeout_days}-day limit."
        )
        status = f"Timed out after {age_days} days"
    elif timed_out:
        failure = None
        status = "Timed out; override approval required"
    elif force_publish:
        failure = None
        status = "Override approval required"
    else:
        failure = None
        status = "Pending"

    return ReadinessResult(
        tag=tag,
        ready=False,
        status=status,
        detail=detail,
        age_hours=age_hours,
        failure=failure,
    )


def _append_outputs(path: str | None, result: ReadinessResult) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"tag={result.tag or ''}\n")
        output.write(f"ready={str(result.ready).lower()}\n")
        output.write(f"status={result.status}\n")


def _append_summary(
    path: str | None,
    result: ReadinessResult,
    *,
    package: str,
) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as summary:
        summary.write("### Conda release check\n\n")
        if result.tag is not None:
            summary.write(f"- Package: `{package}`\n")
            summary.write(f"- Version: `{result.tag}`\n")
        summary.write(f"- Status: {result.status}\n")
        summary.write(f"- Detail: {result.detail}\n")
        if result.age_hours is not None:
            summary.write(f"- Tag age: {result.age_hours} hours\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether a release can be published.")
    parser.add_argument("--requested-tag", default=os.environ.get("REQUESTED_TAG", ""))
    parser.add_argument(
        "--force-publish",
        type=_parse_bool,
        default=os.environ.get("FORCE_PUBLISH", "false"),
    )
    parser.add_argument(
        "--manifest-url",
        default=os.environ.get(
            "MANIFEST_URL",
            "https://downloads.deadlinecloud.amazonaws.com/conda/manifest.json",
        ),
    )
    parser.add_argument("--default-branch", default=os.environ.get("DEFAULT_BRANCH", ""))
    parser.add_argument(
        "--package",
        default=os.environ.get("PACKAGE_NAME", ""),
    )
    parser.add_argument(
        "--required-platforms",
        default=os.environ.get("REQUIRED_PLATFORMS", ""),
    )
    parser.add_argument(
        "--timeout-days",
        type=int,
        default=os.environ.get("RELEASE_TIMEOUT_DAYS", "4"),
    )
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument(
        "--github-api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--step-summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    required_platforms = tuple(
        platform.strip() for platform in args.required_platforms.split(",") if platform.strip()
    )

    try:
        if not args.repository or not args.github_token:
            raise ReadinessError("GITHUB_REPOSITORY and GITHUB_TOKEN are required.")
        if not args.default_branch:
            raise ReadinessError("The repository default branch must be configured.")
        if not args.package:
            raise ReadinessError("A Conda package name must be configured.")
        if not required_platforms:
            raise ReadinessError("At least one required Conda platform must be configured.")
        if args.timeout_days <= 2:
            raise ReadinessError("The release timeout must be greater than two days.")

        default_branch_ref = _fetch_default_branch(args.default_branch)
        tag = resolve_release_tag(
            args.requested_tag,
            args.force_publish,
            default_branch_ref,
        )
        validate_release_tag(tag, args.default_branch, default_branch_ref)

        if github_release_exists(
            args.repository,
            tag,
            args.github_token,
            api_url=args.github_api_url,
        ):
            result = ReadinessResult(
                tag=None,
                ready=False,
                status="Already published",
                detail=f"GitHub Release {tag} already exists; nothing remains to publish.",
            )
        else:
            manifest = _request_json(args.manifest_url)
            result = evaluate_manifest(
                manifest,
                tag=tag,
                tag_created_at=get_tag_created_at(tag),
                now=int(time.time()),
                timeout_seconds=args.timeout_days * 24 * 60 * 60,
                force_publish=args.force_publish,
                package=args.package,
                required_platforms=required_platforms,
            )

        _append_outputs(args.github_output, result)
        _append_summary(args.step_summary, result, package=args.package)
        print(result.detail)
        if result.failure:
            print(f"::error::{result.failure}")
            return 1
        if result.status.startswith("Timed out"):
            print(f"::warning::{result.status}")
        return 0
    except ReadinessError as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
