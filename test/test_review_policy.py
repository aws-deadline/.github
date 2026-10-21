# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, main as unittest_main, mock

SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "review_policy.py"
SPEC = importlib.util.spec_from_file_location("review_policy", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)

SHA = "a" * 40
OLD_SHA = "b" * 40

CONFIG = policy.Config.parse(
    """
high_risk_paths = [".github/", "pyproject.toml", "requirements*.txt", "src/pkg/_session.py"]
low_risk_paths = ["docs/", "test/", "**/*.md"]
low_risk_exclude = ["AGENTS.md", "REVIEW.md"]
low_risk_max_changed_lines = 300
human_review_authors = ["release-bot"]

[dependabot]
auto_merge_files = ["requirements-testing.txt", "requirements-ui-testing.txt"]
human_review_packages = ["PySide6-essentials"]
trusted_action_owners = ["actions", "aws-actions"]
"""
)

PR = policy.PullRequest(
    number=7,
    author="alice",
    association="MEMBER",
    title="docs: fix a typo",
    labels=(),
    files=(policy.ChangedFile("docs/guide.md", changes=4),),
    head_sha=SHA,
)


def dependabot_pr(
    files: tuple[str, ...], *deps: tuple[str, str, str], subject: str = "chore(deps): bump things"
) -> policy.PullRequest:
    meta = "".join(
        f"- dependency-name: {name}\n  dependency-version: {version}\n"
        f"  dependency-type: direct:production\n  update-type: version-update:{update}\n"
        for name, version, update in deps
    )
    message = f"{subject}\n\nBumps things.\n\n---\nupdated-dependencies:\n{meta}...\n\nSigned-off-by: dependabot[bot]"
    return replace(
        PR,
        author="dependabot[bot]",
        association="CONTRIBUTOR",
        title="chore(deps): bump things",
        files=tuple(policy.ChangedFile(f, changes=2) for f in files),
        commits=(policy.Commit("dependabot[bot]", message, github_signed=True),),
    )


class GlobTest(TestCase):
    def test_patterns(self) -> None:
        cases = [
            ("docs/", "docs/a/b.md", True),
            ("docs/", "docsx/a.md", False),
            ("**/*.md", "README.md", True),
            ("**/*.md", "a/b/c.md", True),
            ("requirements*.txt", "requirements-testing.txt", True),
            ("requirements*.txt", "sub/requirements.txt", False),
            ("src/*.py", "src/a/b.py", False),
            ("src/**", "src/a/b.py", True),
            ("a?.py", "ab.py", True),
            ("a.py", "aXpy", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(policy.glob_regex(pattern).match(path)), expected)


class ConfigTest(TestCase):
    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "high_risk_path"):
            policy.Config.parse('high_risk_path = ["x"]')

    def test_wrong_type_is_rejected(self) -> None:
        for text in (
            'high_risk_paths = ".github/"',
            'low_risk_max_changed_lines = "300"',
            "[dependabot]\nauto_merge_files = [1]",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "wrong type"):
                policy.Config.parse(text)

    def test_unknown_dependabot_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto_merge"):
            policy.Config.parse('[dependabot]\nauto_merge = ["x"]')


class ClassifyTest(TestCase):
    def test_docs_from_member_is_low(self) -> None:
        self.assertEqual(policy.classify(PR, CONFIG).tier, "low")

    def test_external_author_is_not_low(self) -> None:
        c = policy.classify(replace(PR, association="CONTRIBUTOR"), CONFIG)
        self.assertEqual(c.tier, "medium")
        self.assertIn("CONTRIBUTOR", c.reasons[0])

    def test_source_change_is_medium(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("src/pkg/cli.py", changes=10),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "medium")

    def test_excluded_low_path_is_medium(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("REVIEW.md", changes=1),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "medium")

    def test_large_low_path_change_is_medium(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/a.md", changes=301),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "medium")

    def test_no_files_is_medium(self) -> None:
        self.assertEqual(policy.classify(replace(PR, files=()), CONFIG).tier, "medium")

    def test_high_risk_path_is_high(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/a.md"), policy.ChangedFile("src/pkg/_session.py")))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.tier, "high")
        self.assertIn("src/pkg/_session.py", c.reasons[0])

    def test_rename_out_of_high_risk_path_is_high(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/session.py", previous_path="src/pkg/_session.py"),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "high")

    def test_breaking_title_is_high(self) -> None:
        for title in ("feat!: drop py3.9", "fix(cli)!: rename flag"):
            with self.subTest(title=title):
                self.assertEqual(policy.classify(replace(PR, title=title), CONFIG).tier, "high")

    def test_security_label_is_high(self) -> None:
        self.assertEqual(policy.classify(replace(PR, labels=("security",)), CONFIG).tier, "high")

    def test_human_review_author_is_medium_even_for_low_paths(self) -> None:
        c = policy.classify(replace(PR, author="release-bot"), CONFIG)
        self.assertEqual(c.tier, "medium")


class DependabotTest(TestCase):
    def test_metadata_is_parsed(self) -> None:
        pr = dependabot_pr(
            ("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"), ("mypy", "2.1.0", "semver-minor")
        )
        self.assertEqual(
            policy.parse_dependabot_metadata(pr.commits[0].message),
            [policy.Dependency("ruff", "1.2.3", "semver-patch"), policy.Dependency("mypy", "2.1.0", "semver-minor")],
        )

    def test_real_actions_group_commit_is_parsed(self) -> None:
        # From aws-deadline/deadline-cloud#1399.
        message = (
            "chore(deps): bump actions/upload-artifact (#1399)\n\n---\nupdated-dependencies:\n"
            "- dependency-name: actions/upload-artifact\n  dependency-version: 7.0.2\n"
            "  dependency-type: direct:production\n  update-type: version-update:semver-patch\n"
            "  dependency-group: github-actions-minor-patch\n...\n"
        )
        self.assertEqual(
            policy.parse_dependabot_metadata(message),
            [policy.Dependency("actions/upload-artifact", "7.0.2", "semver-patch")],
        )

    def test_patch_of_test_dependency_is_auto(self) -> None:
        c = policy.classify(dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch")), CONFIG)
        self.assertEqual((c.tier, c.lane), ("low", "auto"))

    def test_trusted_action_bump_is_auto(self) -> None:
        pr = dependabot_pr((".github/workflows/ci.yml",), ("actions/checkout", "7.0.1", "semver-minor"))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "auto")

    def test_untrusted_action_bump_is_human(self) -> None:
        pr = dependabot_pr((".github/workflows/ci.yml",), ("someone/action", "2.1.0", "semver-minor"))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_runtime_manifest_is_human(self) -> None:
        pr = dependabot_pr(("pyproject.toml",), ("boto3", "1.40.1", "semver-patch"))
        c = policy.classify(pr, CONFIG)
        # pyproject.toml is a high-risk path, so the bump takes its tier.
        self.assertEqual((c.tier, c.lane), ("high", "human"))
        self.assertIn("pyproject.toml", c.reasons[0])

    def test_major_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("pytest", "9.0.0", "semver-major"))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_zero_x_minor_is_human(self) -> None:
        # The xa11y 0.13 -> 0.15 bump (aws-deadline/deadline-cloud#1367) broke the UI tests.
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.lane, "human")
        self.assertIn("0.x minor", c.reasons[0])

    def test_zero_x_patch_is_auto(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.2", "semver-patch"))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "auto")

    def test_human_review_package_is_human(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("PySide6-essentials", "6.10.1", "semver-patch"))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_one_human_dependency_makes_the_group_human(self) -> None:
        pr = dependabot_pr(
            ("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"), ("pytest", "9.0.0", "semver-major")
        )
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_missing_metadata_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",))
        pr = replace(pr, commits=(policy.Commit("dependabot[bot]", "bump", github_signed=True),))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_merge_by_someone_else_is_human(self) -> None:
        # A merge can carry arbitrary changes, e.g. "Update branch" in
        # aws-deadline/deadline-cloud#1400.
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        pr = replace(pr, commits=pr.commits + (policy.Commit("larrygao001", "Merge branch 'mainline'"),))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_security_label_is_high(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        c = policy.classify(replace(pr, labels=("security",)), CONFIG)
        self.assertEqual((c.tier, c.lane), ("high", "human"))

    def test_commit_by_someone_else_is_human(self) -> None:
        # Like the xa11y fix-up commits in aws-deadline/deadline-cloud#1367.
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.2", "semver-patch"))
        pr = replace(pr, commits=pr.commits + (policy.Commit("crowecawcaw", "test(ui): retry tree dump"),))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.lane, "human")
        self.assertIn("crowecawcaw", c.reasons[0])

    def test_requirement_update_type_is_inferred_from_the_title(self) -> None:
        # aws-deadline/deadline-cloud#1387 carries no update-type.
        for title, version, lane in (
            ("chore(deps): update mypy requirement from ==2.3.* to ==2.4.*", "2.4.0", "auto"),
            ("chore(deps): update mypy requirement from ==2.3.* to ==3.0.*", "3.0.0", "human"),
            ("chore(deps): update ruff requirement from ==0.16.* to ==0.17.*", "0.17.0", "human"),
        ):
            with self.subTest(title=title):
                pr = dependabot_pr(("requirements-testing.txt",), ("mypy", version, ""), subject=title)
                self.assertEqual(policy.classify(pr, CONFIG).lane, lane)

    def test_edited_title_or_mismatched_version_is_not_trusted(self) -> None:
        subject = "chore(deps): update mypy requirement from ==2.* to ==3.*"
        pr = dependabot_pr(("requirements-testing.txt",), ("mypy", "3.0.0", ""), subject=subject)
        edited = replace(pr, title="chore(deps): update mypy requirement from ==2.3.* to ==2.4.*")
        self.assertEqual(policy.classify(edited, CONFIG).lane, "human")
        mismatched = dependabot_pr(
            ("requirements-testing.txt",),
            ("mypy", "3.0.0", ""),
            subject="update mypy requirement from ==2.3.* to ==2.4.*",
        )
        self.assertEqual(policy.classify(mismatched, CONFIG).lane, "human")

    def test_unsigned_dependabot_commit_is_human(self) -> None:
        # The author login comes from the author email, which anyone can set.
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        pr = replace(pr, commits=(replace(pr.commits[0], github_signed=False),))
        self.assertEqual(policy.classify(pr, CONFIG).lane, "human")

    def test_truncated_file_list_is_high(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        self.assertEqual(policy.classify(replace(pr, files_truncated=True), CONFIG).tier, "high")
        self.assertEqual(policy.classify(replace(PR, files_truncated=True), CONFIG).tier, "high")

    def test_infer_update_type(self) -> None:
        cases = [
            ("1.2.3", "2.0.0", "semver-major"),
            ("==2.3.*", "==2.4.*", "semver-minor"),
            (">=0.12.0", ">=0.13.0", "semver-minor"),
            ("1.2.3", "1.2.4", "semver-patch"),
            ("1.2", "1.2.0.1", ""),
            ("latest", "1.0", ""),
            # Ranges can admit a new major while the first bound stays put.
            (">=7.0,<8.0", ">=7.0,<9.0", ""),
            ("==2.3.*", ">=2.3,<4", ""),
        ]
        for old, new, expected in cases:
            with self.subTest(old=old, new=new):
                self.assertEqual(policy.infer_update_type(old, new), expected)


def review(user: str, state: str, commit: str = SHA, bot: bool = False, association: str = "MEMBER") -> policy.Review:
    return policy.Review(user=user, is_bot=bot, state=state, commit_id=commit, association=association)


class DecideTest(TestCase):
    MEDIUM = policy.Classification("medium", ("x",))
    HIGH = policy.Classification("high", ("x",))
    LOW = policy.Classification("low", ("x",))

    def test_low_needs_clean_ai_review(self) -> None:
        self.assertEqual(policy.decide(PR, self.LOW, [], "success")[0], True)
        self.assertEqual(policy.decide(PR, self.LOW, [], None), (False, "Low risk: waiting for the Claude review"))
        self.assertEqual(policy.decide(PR, self.LOW, [], "failure")[0], False)
        self.assertIn("did not finish", policy.decide(PR, self.LOW, [], "error")[1])

    def test_low_dependabot_needs_no_ai_review(self) -> None:
        pr = replace(PR, author="dependabot[bot]")
        self.assertEqual(policy.decide(pr, self.LOW, [], None)[0], True)

    def test_medium_needs_an_approval_on_the_head(self) -> None:
        self.assertEqual(policy.decide(PR, self.MEDIUM, [], "success"), (False, "Medium risk: needs 1 human approval"))
        self.assertEqual(policy.decide(PR, self.MEDIUM, [review("bob", "APPROVED")], None)[0], True)

    def test_stale_bot_and_self_approvals_do_not_count(self) -> None:
        reviews = [
            review("bob", "APPROVED", commit=OLD_SHA),
            review("github-actions", "APPROVED", bot=True),
            review("alice", "APPROVED"),
            review("mallory", "APPROVED", association="NONE"),
        ]
        self.assertEqual(policy.decide(PR, self.MEDIUM, reviews, None)[0], False)

    def test_committer_cannot_approve_their_own_changes(self) -> None:
        pr = replace(PR, commits=(policy.Commit("bob", "fix-up"),))
        self.assertEqual(policy.decide(pr, self.MEDIUM, [review("bob", "APPROVED")], None)[0], False)

    def test_changes_requested_by_another_reviewer_blocks(self) -> None:
        reviews = [review("bob", "APPROVED"), review("carol", "CHANGES_REQUESTED")]
        for tier in (self.LOW, self.MEDIUM):
            with self.subTest(tier=tier.tier):
                met, reason = policy.decide(PR, tier, reviews, "success")
                self.assertEqual((met, reason.split(": ")[1]), (False, "changes requested by carol"))

    def test_later_changes_requested_overrides_approval(self) -> None:
        reviews = [review("bob", "APPROVED"), review("bob", "CHANGES_REQUESTED")]
        self.assertEqual(policy.decide(PR, self.MEDIUM, reviews, None)[0], False)

    def test_later_comment_keeps_approval(self) -> None:
        reviews = [review("bob", "APPROVED"), review("bob", "COMMENTED")]
        self.assertEqual(policy.decide(PR, self.MEDIUM, reviews, None)[0], True)

    def test_high_names_the_code_owner(self) -> None:
        self.assertEqual(
            policy.decide(PR, self.HIGH, [], None), (False, "High risk: needs 1 human approval (code owner)")
        )


class StatusTest(TestCase):
    def test_advisory_always_succeeds(self) -> None:
        self.assertEqual(
            policy.status_for(False, "Medium risk: needs 1 human approval", enforce=False),
            ("success", "Advisory, would wait: Medium risk: needs 1 human approval"),
        )

    def test_enforce_is_pending_until_met(self) -> None:
        self.assertEqual(policy.status_for(False, "r", enforce=True), ("pending", "r"))
        self.assertEqual(policy.status_for(True, "r", enforce=True), ("success", "r"))

    def test_description_is_truncated(self) -> None:
        _, description = policy.status_for(True, "x" * 500, enforce=True)
        self.assertEqual(len(description), policy.MAX_DESCRIPTION_CHARS)


class EvaluateTest(TestCase):
    def run_evaluate(self, pr: policy.PullRequest, *, enforce: bool, auto_merge: bool, statuses: dict | None = None):
        calls: list[tuple[str, ...]] = []

        def fake_gh(*args: str, input_json=None, check=True):
            calls.append(args)

        with (
            mock.patch.object(policy, "fetch_pr", return_value=pr),
            mock.patch.object(policy, "fetch_reviews", return_value=[]),
            mock.patch.object(policy, "fetch_statuses", return_value=(statuses or {}, statuses or {})),
            mock.patch.object(policy, "gh", fake_gh),
            mock.patch.object(policy, "set_auto_merge") as merge,
            mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": ""}),
        ):
            policy.evaluate("o/r", pr.number, SHA, CONFIG, enforce=enforce, auto_merge=auto_merge, run_url="u")
        return calls, merge

    def test_labels_and_sets_status(self) -> None:
        pr = replace(PR, labels=("risk:high",))
        calls, _ = self.run_evaluate(pr, enforce=False, auto_merge=False)
        self.assertIn(("-X", "DELETE", "repos/o/r/issues/7/labels/risk:high"), calls)
        self.assertIn(("-X", "POST", "repos/o/r/issues/7/labels"), calls)
        status = next(c for c in calls if c[2] == f"repos/o/r/statuses/{SHA}")
        self.assertIn("state=success", status)
        self.assertIn("description=Advisory, would wait: Low risk: waiting for the Claude review", status)

    def test_unchanged_status_is_not_reposted(self) -> None:
        current = {"review-policy": ("success", "Advisory, would wait: Low risk: waiting for the Claude review")}
        calls, _ = self.run_evaluate(PR, enforce=False, auto_merge=False, statuses=current)
        self.assertFalse([c for c in calls if "statuses" in c[2]])

    def test_status_posted_meanwhile_by_another_run_stands(self) -> None:
        seen = ({}, {})
        meanwhile = ({}, {"review-policy": ("pending", "Medium risk: changes requested by carol")})
        calls: list[tuple[str, ...]] = []
        with (
            mock.patch.object(policy, "fetch_pr", return_value=PR),
            mock.patch.object(policy, "fetch_reviews", return_value=[]),
            mock.patch.object(policy, "fetch_statuses", side_effect=[seen, meanwhile]),
            mock.patch.object(policy, "gh", lambda *a, **k: calls.append(a)),
            mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": ""}),
        ):
            policy.evaluate("o/r", 7, SHA, CONFIG, enforce=False, auto_merge=False, run_url="u")
        self.assertFalse([c for c in calls if "statuses" in c[2]])

    def test_existing_label_is_left_alone(self) -> None:
        calls, _ = self.run_evaluate(replace(PR, labels=("risk:low",)), enforce=False, auto_merge=False)
        self.assertFalse([c for c in calls if "labels" in c[2]])

    def test_superseded_head_does_nothing(self) -> None:
        calls, merge = self.run_evaluate(replace(PR, head_sha=OLD_SHA), enforce=True, auto_merge=True)
        self.assertEqual(calls, [])
        merge.assert_not_called()

    def test_auto_merge_only_when_enforced_and_allowed(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        for enforce, allowed, expected in ((False, True, False), (True, False, False), (True, True, True)):
            with self.subTest(enforce=enforce, allowed=allowed):
                _, merge = self.run_evaluate(pr, enforce=enforce, auto_merge=allowed)
                self.assertEqual(merge.call_args_list == [mock.call("o/r", 7, enable=True, head_sha=SHA)], expected)

    def test_human_lane_is_never_auto_merged(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        _, merge = self.run_evaluate(pr, enforce=True, auto_merge=True)
        merge.assert_not_called()

    def test_auto_merge_is_disabled_when_leaving_the_auto_lane(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        _, merge = self.run_evaluate(replace(pr, auto_merge_by="github-actions[bot]"), enforce=True, auto_merge=True)
        merge.assert_called_once_with("o/r", 7, enable=False)

    def test_auto_merge_armed_by_a_person_is_left_alone(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        _, merge = self.run_evaluate(replace(pr, auto_merge_by="bob"), enforce=True, auto_merge=True)
        merge.assert_not_called()


class FetchStatusesTest(TestCase):
    def test_only_statuses_from_actions_count(self) -> None:
        statuses = [
            {"context": "Claude review", "state": "success", "creator": {"login": "mallory"}},
            {"context": "Claude review", "state": "failure", "creator": {"login": "github-actions[bot]"}},
            {"context": "Claude review", "state": "pending", "creator": {"login": "github-actions[bot]"}},
        ]
        with mock.patch.object(policy, "gh", return_value=statuses):
            trusted, newest = policy.fetch_statuses("o/r", SHA)
        self.assertEqual((trusted["Claude review"][0], newest["Claude review"][0]), ("failure", "success"))


class SweepTest(TestCase):
    PULLS = [
        [
            {"number": 1, "draft": False, "head": {"sha": SHA}},
            {"number": 2, "draft": True, "head": {"sha": SHA}},
            {"number": 3, "draft": False, "head": {"sha": OLD_SHA}},
        ]
    ]

    def test_evaluates_open_non_draft_prs(self) -> None:
        with (
            mock.patch.object(policy, "gh", return_value=self.PULLS),
            mock.patch.object(policy, "evaluate") as evaluate,
        ):
            policy.sweep("o/r", CONFIG, enforce=False, auto_merge=False, run_url="u")
        self.assertEqual([c.args[1:3] for c in evaluate.call_args_list], [(1, SHA), (3, OLD_SHA)])

    def test_prs_sharing_a_head_are_skipped(self) -> None:
        pulls = [
            [{"number": 1, "draft": False, "head": {"sha": SHA}}, {"number": 2, "draft": False, "head": {"sha": SHA}}]
        ]
        with mock.patch.object(policy, "gh", return_value=pulls), mock.patch.object(policy, "evaluate") as evaluate:
            policy.sweep("o/r", CONFIG, enforce=False, auto_merge=False, run_url="u")
        evaluate.assert_not_called()

    def test_one_failure_does_not_stop_the_sweep(self) -> None:
        with (
            mock.patch.object(policy, "gh", return_value=self.PULLS),
            mock.patch.object(policy, "evaluate", side_effect=[RuntimeError("boom"), None]) as evaluate,
            self.assertRaisesRegex(SystemExit, "1 PR"),
        ):
            policy.sweep("o/r", CONFIG, enforce=False, auto_merge=False, run_url="u")
        self.assertEqual(evaluate.call_count, 2)


if __name__ == "__main__":
    unittest_main()
