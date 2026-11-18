# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from unittest import TestCase, main as unittest_main, skipUnless

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/reusable_claude_pr_review.yml"
SHA = "a" * 40
BASE_SHA = "b" * 40
BASE_REPO = "aws-deadline/deadline-cloud-for-comfyui"
HEAD_REPO = "EngineLab-Ltd/deadline-cloud-for-comfyui"


def _find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Windows' bash.exe may launch WSL. Use Git Bash, which runs Windows tools.
    git = shutil.which("git")
    if git:
        bash = Path(git).parent.parent / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    return shutil.which("gitbash")


BASH = _find_bash()


def _pr(number=16, *, sha=SHA, state="open", head_repo=HEAD_REPO, base_repo=BASE_REPO):
    return {
        "number": number,
        "state": state,
        "head": {"sha": sha, "repo": {"full_name": head_repo}},
        "base": {"sha": BASE_SHA, "repo": {"full_name": base_repo}},
    }


@skipUnless(
    BASH and shutil.which("jq"),
    "workflow resolution needs Git Bash on Windows, bash elsewhere, and jq",
)
class PRResolutionTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute the workflow's actual resolver, with only GitHub's response mocked.
        step = WORKFLOW.read_text().split("        id: meta\n", 1)[1]
        cls.script = textwrap.dedent(
            re.search(r"        run: \|\n((?:          [^\n]*\n|\n)+)", step).group(1)
        )

    def _resolve(self, candidates, **overrides):
        gh_mock = """
            gh() {
              [[ "$#" == 4 && "$1" == api && "$3" == --jq ]] || return 1
              [[ "$2" == "repos/$EXPECTED_HEAD_REPO/commits/$HEAD_SHA/pulls" ]] || return 1
              printf '%s' "$PR_CANDIDATES" | jq "$4"
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.touch()
            env = {
                **os.environ,
                "REPO": BASE_REPO,
                "HEAD_SHA": SHA,
                "EXPECTED_HEAD_REPO": HEAD_REPO,
                "ACTOR": "contributor",
                "DEBOUNCE_SECONDS": "0",
                "GITHUB_OUTPUT": output.name,
                "PR_CANDIDATES": json.dumps(candidates),
                **overrides,
            }
            result = subprocess.run(
                [BASH, "-c", textwrap.dedent(gh_mock) + self.script],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return result, outputs

    def test_resolves_current_head_among_stacked_prs(self):
        candidates = [_pr(17, sha="c" * 40), _pr(), _pr(18, sha="d" * 40)]
        result, outputs = self._resolve(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            outputs,
            {"resolved": "true", "pr_number": "16", "head_sha": SHA, "base_sha": BASE_SHA},
        )

    def test_repository_and_state_guards_exclude_other_matches(self):
        candidates = [
            _pr(17, head_repo="other/fork"),
            _pr(18, base_repo="other/base"),
            _pr(19, state="closed"),
            _pr(),
        ]
        result, outputs = self._resolve(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs["pr_number"], "16")
        self.assertEqual(outputs["resolved"], "true")

    def test_skips_absent_superseded_and_ambiguous_heads(self):
        cases = {
            "absent": [],
            "superseded": [_pr(sha="c" * 40)],
            "ambiguous": [_pr(), _pr(17)],
        }
        for name, candidates in cases.items():
            with self.subTest(name=name):
                result, outputs = self._resolve(candidates)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs, {"resolved": "false"})

    def test_skips_bot_actor(self):
        result, outputs = self._resolve([_pr()], ACTOR="dependabot[bot]")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs, {"resolved": "false"})

    def test_rejects_invalid_metadata(self):
        bad_base = _pr()
        bad_base["base"]["sha"] = "invalid"
        cases = [
            ([_pr()], {"HEAD_SHA": "invalid"}, "bad head_sha"),
            ([_pr(number="invalid")], {}, "bad pr_number"),
            ([bad_base], {}, "bad base_sha"),
        ]
        for candidates, overrides, error in cases:
            with self.subTest(candidates=candidates, overrides=overrides):
                result, outputs = self._resolve(candidates, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stdout + result.stderr)
                self.assertNotIn("resolved", outputs)


if __name__ == "__main__":
    unittest_main()
