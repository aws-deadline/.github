# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path
from unittest import TestCase, main as unittest_main, mock

SCRIPT_PATH = (
    Path(__file__).parents[1]
    / ".github"
    / "actions"
    / "check-release-readiness"
    / "check_release_readiness.py"
)
SPEC = importlib.util.spec_from_file_location("check_release_readiness", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_readiness
SPEC.loader.exec_module(release_readiness)

ReadinessError = release_readiness.ReadinessError

MANIFEST = {
    "schemaVersion": 1,
    "generatedAt": "2026-08-27T20:00:00Z",
    "packages": {
        "cinema4d-openjd": {
            "0.12.2": {"platforms": ["linux-64", "win-64"]},
        }
    },
}


def _evaluate(
    manifest=MANIFEST,
    *,
    tag="0.12.2",
    age_seconds=0,
    timeout_seconds=3 * 24 * 60 * 60,
    force_publish=False,
):
    now = 2_000_000_000
    return release_readiness.evaluate_manifest(
        manifest,
        tag=tag,
        tag_created_at=now - age_seconds,
        now=now,
        timeout_seconds=timeout_seconds,
        force_publish=force_publish,
        package="cinema4d-openjd",
        required_platforms=("linux-64", "win-64"),
    )


class TestReleaseReadiness(TestCase):
    def test_manifest_is_ready_when_version_has_all_required_platforms(self):
        result = _evaluate()

        self.assertTrue(result.ready)
        self.assertIsNone(result.failure)
        self.assertEqual(result.status, "Ready to complete")

    def test_missing_version_remains_pending_before_timeout(self):
        result = _evaluate(tag="0.13.0", age_seconds=(3 * 24 * 60 * 60) - 1)

        self.assertFalse(result.ready)
        self.assertIsNone(result.failure)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(result.age_hours, 71)

    def test_missing_platform_fails_at_timeout(self):
        manifest = {
            **MANIFEST,
            "packages": {
                "cinema4d-openjd": {
                    "0.12.2": {"platforms": ["linux-64"]},
                }
            },
        }

        result = _evaluate(manifest, age_seconds=3 * 24 * 60 * 60)

        self.assertFalse(result.ready)
        self.assertIsNotNone(result.failure)
        self.assertEqual(result.status, "Timed out after 3 days")
        self.assertIn("win-64", result.detail)

    def test_force_publish_allows_timed_out_release_to_request_approval(self):
        result = _evaluate(
            tag="0.13.0",
            age_seconds=4 * 24 * 60 * 60,
            force_publish=True,
        )

        self.assertFalse(result.ready)
        self.assertIsNone(result.failure)
        self.assertEqual(result.status, "Timed out; override approval required")

    def test_force_publish_requires_explicit_tag(self):
        with self.assertRaisesRegex(ReadinessError, "specific tag"):
            release_readiness.resolve_release_tag(
                "",
                force_publish=True,
                default_branch_ref="refs/remotes/origin/mainline",
            )

    def test_highest_release_tag_merged_into_default_branch_is_selected(self):
        with mock.patch.object(
            release_readiness,
            "_run_git",
            return_value="nightly\nreadiness-smoke-0.12.2\n0.13.0\n0.12.2",
        ) as run_git:
            self.assertEqual(
                release_readiness.resolve_release_tag(
                    "",
                    force_publish=False,
                    default_branch_ref="refs/remotes/origin/mainline",
                ),
                "0.13.0",
            )
        run_git.assert_called_once_with(
            "tag",
            "--list",
            "--merged",
            "refs/remotes/origin/mainline",
            "--sort=-version:refname",
        )

    def test_default_branch_without_release_tags_fails(self):
        with (
            mock.patch.object(
                release_readiness,
                "_run_git",
                return_value="nightly\nreadiness-smoke-0.12.2",
            ),
            self.assertRaisesRegex(ReadinessError, "no release tags"),
        ):
            release_readiness.resolve_release_tag(
                "",
                force_publish=False,
                default_branch_ref="refs/remotes/origin/mainline",
            )

    def test_default_branch_is_fetched_into_remote_tracking_ref(self):
        with mock.patch.object(
            release_readiness,
            "_run_git",
            side_effect=["", ""],
        ) as run_git:
            default_branch_ref = release_readiness._fetch_default_branch("mainline")

        self.assertEqual(default_branch_ref, "refs/remotes/origin/mainline")
        self.assertEqual(
            run_git.call_args_list,
            [
                mock.call("check-ref-format", "--branch", "mainline"),
                mock.call(
                    "fetch",
                    "--no-tags",
                    "--force",
                    "origin",
                    "refs/heads/mainline:refs/remotes/origin/mainline",
                ),
            ],
        )

    def test_release_tag_is_validated_against_fetched_default_branch(self):
        merge_base = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(
                release_readiness,
                "_run_git",
                return_value="tag-commit",
            ) as run_git,
            mock.patch.object(
                release_readiness.subprocess,
                "run",
                return_value=merge_base,
            ) as run,
        ):
            release_readiness.validate_release_tag(
                "0.12.2",
                "mainline",
                "refs/remotes/origin/mainline",
            )

        run_git.assert_called_once_with(
            "rev-parse",
            "--verify",
            "refs/tags/0.12.2^{commit}",
        )
        run.assert_called_once_with(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                "tag-commit",
                "refs/remotes/origin/mainline",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_release_tag_not_on_default_branch_fails(self):
        merge_base = mock.Mock(returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(
                release_readiness,
                "_run_git",
                return_value="tag-commit",
            ),
            mock.patch.object(
                release_readiness.subprocess,
                "run",
                return_value=merge_base,
            ),
            self.assertRaisesRegex(ReadinessError, "default branch mainline"),
        ):
            release_readiness.validate_release_tag(
                "0.12.2",
                "mainline",
                "refs/remotes/origin/mainline",
            )

    def test_published_github_release_is_complete(self):
        with mock.patch.object(
            release_readiness,
            "_request_json",
            return_value={"draft": False},
        ):
            self.assertTrue(
                release_readiness.github_release_exists(
                    "owner/repo",
                    "0.12.2",
                    "token",
                )
            )

    def test_missing_github_release_is_pending(self):
        with mock.patch.object(release_readiness, "_request_json", return_value=None):
            self.assertFalse(
                release_readiness.github_release_exists(
                    "owner/repo",
                    "0.12.2",
                    "token",
                )
            )

    def test_draft_github_release_fails(self):
        with (
            mock.patch.object(
                release_readiness,
                "_request_json",
                return_value={"draft": True},
            ),
            self.assertRaisesRegex(ReadinessError, "draft"),
        ):
            release_readiness.github_release_exists("owner/repo", "0.12.2", "token")

    def test_invalid_manifest_fails(self):
        cases = [
            ({}, "schema version"),
            ({**MANIFEST, "packages": []}, "packages field"),
            (
                {
                    **MANIFEST,
                    "packages": {
                        "cinema4d-openjd": {
                            "0.12.2": {"platforms": "linux-64"},
                        }
                    },
                },
                "array of strings",
            ),
        ]

        for manifest, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ReadinessError, error):
                    _evaluate(manifest)

    def test_unavailable_manifest_url_fails(self):
        with (
            mock.patch.object(
                release_readiness.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("unavailable"),
            ),
            self.assertRaisesRegex(ReadinessError, "unavailable"),
        ):
            release_readiness._request_json("https://example.invalid/manifest.json")


if __name__ == "__main__":
    unittest_main()
