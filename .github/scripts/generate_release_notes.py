#!/usr/bin/env python3
"""Generate customer-facing release notes from git commits using Bedrock tool use."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

import boto3

MODEL_ID = "us.anthropic.claude-opus-4-6-v1"

TOOL_DEFINITION = {
    "name": "emit_release_notes",
    "description": "Emit structured release note entries. Call this once with ALL entries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["breaking", "features", "bug fixes", "deprecations"],
                        },
                        "description": {
                            "type": "string",
                            "description": "Customer-facing description of the change. Write from the user's perspective.",
                        },
                        "reference": {
                            "type": "string",
                            "description": "PR link like (#1234) or commit hash in backticks like (`abc1234`)",
                        },
                    },
                    "required": ["category", "description", "reference"],
                },
            }
        },
        "required": ["entries"],
    },
}

SYSTEM_PROMPT = """\
You are writing customer-facing release notes for {repo_name}.

{readme_section}

You will receive raw git commit messages and PR descriptions. Your job is to call the emit_release_notes \
tool with structured entries that a customer would find useful.

What to INCLUDE (only changes a user of this package would notice):
- New CLI commands, flags, or options they can use
- New library APIs or behaviors they can rely on
- Bug fixes that affected their workflows
- Breaking changes to CLI behavior, config format, or public API signatures
- Deprecations of public APIs or CLI flags
- Performance improvements they would notice

What to EXCLUDE (never mention these even if the commit prefix suggests otherwise):
- CI/CD pipeline changes, GitHub Actions workflow updates, release process changes
- Test additions, test fixes, test infrastructure
- Internal refactors that don't change user-facing behavior
- Dependency bumps (dependabot, requirements updates)
- Documentation-only changes (docs, README, wiki, migration guides)
- Build infrastructure, installer build process, CodeBuild/CodePipeline changes
- Merge commits, chore commits, code quality/linting changes
- Changes to internal modules that aren't part of the public API
- Telemetry/analytics changes that are invisible to the user

When in doubt, ask: "Would a user of this package notice this change?" If no, skip it.

Writing style:
- Write from the user's perspective — what changed FOR THEM.
- For breaking changes, explain what the user needs to do differently.
- For features, explain what the user can now do.
- For bug fixes, explain what was broken and that it's now fixed.
- Keep descriptions concise — one or two sentences max.
- Use the PR number as reference when available (e.g. "(#1234)"), otherwise use short commit hash.
- Combine related commits into a single entry when they're part of the same feature/fix.
- ALWAYS call the tool. Never respond with plain text."""


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def get_latest_tag() -> str:
    return run_git("describe", "--tags", "--abbrev=0")


def get_readme() -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if os.path.isfile(name):
            with open(name) as f:
                return f.read(1000)
    return ""


def get_commits_since_tag(tag: str) -> list[dict]:
    log = run_git(
        "log", f"{tag}..HEAD",
        "--pretty=format:%H%x00%s%x00%b%x1e",
    )
    if not log:
        return []

    commits = []
    for entry in log.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00", 2)
        if len(parts) < 2:
            continue
        commits.append({
            "hash": parts[0][:7],
            "subject": parts[1],
            "body": parts[2] if len(parts) > 2 else "",
        })
    return commits


def get_pr_descriptions(commits: list[dict]) -> dict[str, str]:
    """Try to fetch PR descriptions via gh CLI for commits that reference PRs."""
    pr_numbers = set()
    for c in commits:
        for m in re.findall(r"#(\d+)", c["subject"]):
            pr_numbers.add(m)

    descriptions = {}
    for pr in pr_numbers:
        result = subprocess.run(
            ["gh", "pr", "view", pr, "--json", "body,title", "-q", ".title + \"\\n\" + .body"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            descriptions[pr] = result.stdout.strip()[:2000]
    return descriptions


def build_input_text(commits: list[dict], pr_descriptions: dict[str, str]) -> str:
    lines = []
    for c in commits:
        pr_match = re.search(r"#(\d+)", c["subject"])
        pr_num = pr_match.group(1) if pr_match else None

        lines.append(f"COMMIT {c['hash']}: {c['subject']}")
        if c["body"]:
            lines.append(f"  Body: {c['body'][:500]}")
        if pr_num and pr_num in pr_descriptions:
            lines.append(f"  PR #{pr_num} description: {pr_descriptions[pr_num][:1000]}")
        lines.append("")

    return "\n".join(lines)


def build_system_prompt(repo_name: str) -> str:
    readme = get_readme()
    readme_section = (
        f"Here is the README for context on what this project does:\n<readme>\n{readme}\n</readme>"
        if readme else ""
    )
    return SYSTEM_PROMPT.format(repo_name=repo_name, readme_section=readme_section)


def invoke_bedrock(input_text: str, region: str, repo_name: str) -> list[dict]:
    client = boto3.client("bedrock-runtime", region_name=region)

    response = client.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": build_system_prompt(repo_name),
            "tools": [TOOL_DEFINITION],
            "tool_choice": {"type": "tool", "name": "emit_release_notes"},
            "messages": [{"role": "user", "content": input_text}],
        }),
    )

    body = json.loads(response["body"].read())

    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_release_notes":
            return block["input"]["entries"]

    raise RuntimeError(f"No tool_use block in response: {json.dumps(body, indent=2)}")


def render_changelog(version: str, entries: list[dict]) -> str:
    """Render entries into the same markdown format as CHANGELOG.md."""
    sections = {
        "breaking": ("BREAKING CHANGES", []),
        "deprecations": ("DEPRECATIONS", []),
        "features": ("Features", []),
        "bug fixes": ("Bug Fixes", []),
    }

    for entry in entries:
        cat = entry["category"]
        if cat in sections:
            sections[cat][1].append(entry)

    lines = [f"## {version} ({date.today().isoformat()})"]

    for key in ["breaking", "deprecations", "features", "bug fixes"]:
        title, items = sections[key]
        if not items:
            continue
        lines.append("")
        lines.append(f"### {title}")
        for item in items:
            ref = f" {item['reference']}" if item.get("reference") else ""
            lines.append(f"* {item['description']}{ref}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate release notes using Bedrock")
    parser.add_argument("version", nargs="?", default="UNRELEASED")
    parser.add_argument("--repo", default=None, help="Repository name (default: inferred from git remote)")
    parser.add_argument("--since", help="Git tag to diff from (default: latest tag)")
    parser.add_argument("--region", default="us-west-2", help="AWS region for Bedrock")
    parser.add_argument("--json", action="store_true", help="Output raw JSON from tool call")
    parser.add_argument("--dry-run", action="store_true", help="Show input to LLM without calling Bedrock")
    args = parser.parse_args()

    if args.repo:
        repo_name = args.repo
    else:
        remote = run_git("remote", "get-url", "origin")
        repo_name = remote.rstrip(".git").rsplit("/", 1)[-1]

    tag = args.since or get_latest_tag()
    print(f"Generating release notes for {repo_name} since {tag}...", file=sys.stderr)

    commits = get_commits_since_tag(tag)
    if not commits:
        print("No commits since last tag.", file=sys.stderr)
        return

    print(f"Found {len(commits)} commits. Fetching PR descriptions...", file=sys.stderr)
    pr_descriptions = get_pr_descriptions(commits)
    print(f"Fetched {len(pr_descriptions)} PR descriptions.", file=sys.stderr)

    input_text = build_input_text(commits, pr_descriptions)

    if args.dry_run:
        print(input_text)
        return

    print("Invoking Bedrock...", file=sys.stderr)
    entries = invoke_bedrock(input_text, args.region, repo_name)

    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        print(render_changelog(args.version, entries))


if __name__ == "__main__":
    main()
