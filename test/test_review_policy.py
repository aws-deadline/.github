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
auto_paths = ["docs/", "test/", "**/*.md"]
human_paths = ["pyproject.toml", "requirements*.txt", "src/pkg/_session.py"]
auto_max_changed_lines = 300
human_review_authors = ["release-bot"]

[dependabot]
auto_merge_files = ["requirements-testing.txt", "requirements-ui-testing.txt"]
human_review_packages = ["PySide6-essentials"]
trusted_action_owners = ["actions", "aws-actions"]
""",
    path="policy/review.toml",
)

# A config that lets everything in, to show what it cannot.
OPEN_CONFIG = policy.Config.parse(
    'auto_paths = ["**"]\nauto_max_changed_lines = 1000\n[dependabot]\nauto_merge_files = ["**"]',
    path=".github/review-policy.toml",
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
        for key in ("auto_path", "high_risk_paths", "path"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                policy.Config.parse(f'{key} = ["x"]')

    def test_wrong_type_is_rejected(self) -> None:
        for text in (
            'human_paths = "src/"',
            'auto_max_changed_lines = "300"',
            "[dependabot]\nauto_merge_files = [1]",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "wrong type"):
                policy.Config.parse(text)

    def test_unknown_dependabot_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto_merge"):
            policy.Config.parse('[dependabot]\nauto_merge = ["x"]')


class ClassifyTest(TestCase):
    def test_docs_from_member_is_auto(self) -> None:
        self.assertEqual(policy.classify(PR, CONFIG).tier, "auto")

    def test_external_author_is_human(self) -> None:
        c = policy.classify(replace(PR, association="CONTRIBUTOR"), CONFIG)
        self.assertEqual(c.tier, "human")
        self.assertIn("CONTRIBUTOR", c.reasons[0])

    def test_source_change_is_human(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("src/pkg/cli.py", changes=10),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_large_auto_path_change_is_human(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/a.md", changes=301),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_no_files_is_human(self) -> None:
        self.assertEqual(policy.classify(replace(PR, files=()), CONFIG).tier, "human")

    def test_human_path_is_human(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/a.md"), policy.ChangedFile("src/pkg/_session.py")))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.tier, "human")
        self.assertIn("src/pkg/_session.py", c.reasons[0])

    def test_rename_out_of_human_path_is_human(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/session.py", previous_path="src/pkg/_session.py"),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_breaking_title_is_human(self) -> None:
        for title in ("feat!: drop py3.9", "fix(cli)!: rename flag"):
            with self.subTest(title=title):
                self.assertEqual(policy.classify(replace(PR, title=title), CONFIG).tier, "human")

    def test_security_label_is_human(self) -> None:
        self.assertEqual(policy.classify(replace(PR, labels=("security",)), CONFIG).tier, "human")

    def test_human_review_author_is_human_even_for_auto_paths(self) -> None:
        self.assertEqual(policy.classify(replace(PR, author="release-bot"), CONFIG).tier, "human")

    def test_always_human_paths_cannot_be_configured_away(self) -> None:
        # Agent instructions, review guidance, ownership, and CI, wherever they
        # sit, even when the config makes every path an auto path.
        for path in (
            "AGENTS.md",
            "pkg/AGENTS.md",
            "CLAUDE.md",
            "docs/CLAUDE.md",
            "REVIEW.md",
            "CODEOWNERS",
            "docs/CODEOWNERS",
            ".github/CODEOWNERS",
            ".github/workflows/ci.yml",
            ".claude/settings.json",
            "sub/.claude/agents/x.md",
            ".kiro/steering/rules.md",
            ".amazonq/rules/x.md",
            "pkg/.cursor/rules/x.mdc",
            ".cursorrules",
            ".mcp.json",
            "sub/.mcp.json",
            ".github/review-policy.toml",
        ):
            with self.subTest(path=path):
                pr = replace(PR, files=(policy.ChangedFile(path, changes=1),))
                c = policy.classify(pr, OPEN_CONFIG)
                self.assertEqual(c.tier, "human")
                self.assertIn("always needs a human", c.reasons[0])

    def test_config_file_is_always_human_wherever_it_lives(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("policy/review.toml", changes=1),))
        self.assertIn("always needs a human", policy.classify(pr, CONFIG).reasons[0])

    def test_rename_out_of_always_human_path_is_human(self) -> None:
        pr = replace(PR, files=(policy.ChangedFile("docs/notes.md", previous_path="AGENTS.md"),))
        self.assertEqual(policy.classify(pr, OPEN_CONFIG).tier, "human")

    def test_lookalikes_are_not_always_human(self) -> None:
        for path in ("docs/github/x.md", "docs/REVIEW.md.bak", "MY_AGENTS.md", "docs/claude/x.md", "docs/kiro.md"):
            with self.subTest(path=path):
                pr = replace(PR, files=(policy.ChangedFile(path, changes=1),))
                self.assertEqual(policy.classify(pr, OPEN_CONFIG).tier, "auto")


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
        self.assertEqual(c.tier, "auto")

    def test_trusted_action_bump_is_auto(self) -> None:
        pr = dependabot_pr((".github/workflows/ci.yml",), ("actions/checkout", "7.0.1", "semver-minor"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "auto")
        # .github/ is always human, but action bumps are judged by owner.
        self.assertEqual(policy.classify(pr, replace(OPEN_CONFIG, dependabot=CONFIG.dependabot)).tier, "auto")

    def test_untrusted_action_bump_is_human(self) -> None:
        pr = dependabot_pr((".github/workflows/ci.yml",), ("someone/action", "2.1.0", "semver-minor"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_runtime_manifest_is_human(self) -> None:
        pr = dependabot_pr(("pyproject.toml",), ("boto3", "1.40.1", "semver-patch"))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.tier, "human")
        self.assertIn("pyproject.toml", c.reasons[0])

    def test_always_human_file_is_human_whatever_auto_merge_files_says(self) -> None:
        for path in ("CODEOWNERS", ".github/requirements.txt", ".github/review-policy.toml"):
            with self.subTest(path=path):
                pr = dependabot_pr((path,), ("ruff", "1.2.3", "semver-patch"))
                c = policy.classify(pr, OPEN_CONFIG)
                self.assertEqual(c.tier, "human")
                self.assertIn("always needs a human", c.reasons[0])

    def test_repo_human_paths_do_not_override_auto_merge_files(self) -> None:
        # requirements*.txt is a human path for people's PRs; Dependabot's own
        # rules decide its bumps.
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "auto")

    def test_major_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("pytest", "9.0.0", "semver-major"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_zero_x_minor_is_human(self) -> None:
        # The xa11y 0.13 -> 0.15 bump (aws-deadline/deadline-cloud#1367) broke the UI tests.
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.tier, "human")
        self.assertIn("0.x minor", c.reasons[0])

    def test_zero_x_patch_is_auto(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.2", "semver-patch"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "auto")

    def test_human_review_package_is_human(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("PySide6-essentials", "6.10.1", "semver-patch"))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_one_human_dependency_makes_the_group_human(self) -> None:
        pr = dependabot_pr(
            ("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"), ("pytest", "9.0.0", "semver-major")
        )
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_missing_metadata_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",))
        pr = replace(pr, commits=(policy.Commit("dependabot[bot]", "bump", github_signed=True),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_merge_by_someone_else_is_human(self) -> None:
        # A merge can carry arbitrary changes, e.g. "Update branch" in
        # aws-deadline/deadline-cloud#1400.
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        pr = replace(pr, commits=pr.commits + (policy.Commit("larrygao001", "Merge branch 'mainline'"),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_security_label_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        self.assertEqual(policy.classify(replace(pr, labels=("security",)), CONFIG).tier, "human")

    def test_commit_by_someone_else_is_human(self) -> None:
        # Like the xa11y fix-up commits in aws-deadline/deadline-cloud#1367.
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.2", "semver-patch"))
        pr = replace(pr, commits=pr.commits + (policy.Commit("crowecawcaw", "test(ui): retry tree dump"),))
        c = policy.classify(pr, CONFIG)
        self.assertEqual(c.tier, "human")
        self.assertIn("crowecawcaw", c.reasons[0])

    def test_requirement_update_type_is_inferred_from_the_title(self) -> None:
        # aws-deadline/deadline-cloud#1387 carries no update-type.
        for title, version, tier in (
            ("chore(deps): update mypy requirement from ==2.3.* to ==2.4.*", "2.4.0", "auto"),
            ("chore(deps): update mypy requirement from ==2.3.* to ==3.0.*", "3.0.0", "human"),
            ("chore(deps): update ruff requirement from ==0.16.* to ==0.17.*", "0.17.0", "human"),
        ):
            with self.subTest(title=title):
                pr = dependabot_pr(("requirements-testing.txt",), ("mypy", version, ""), subject=title)
                self.assertEqual(policy.classify(pr, CONFIG).tier, tier)

    def test_edited_title_or_mismatched_version_is_not_trusted(self) -> None:
        subject = "chore(deps): update mypy requirement from ==2.* to ==3.*"
        pr = dependabot_pr(("requirements-testing.txt",), ("mypy", "3.0.0", ""), subject=subject)
        edited = replace(pr, title="chore(deps): update mypy requirement from ==2.3.* to ==2.4.*")
        self.assertEqual(policy.classify(edited, CONFIG).tier, "human")
        mismatched = dependabot_pr(
            ("requirements-testing.txt",),
            ("mypy", "3.0.0", ""),
            subject="update mypy requirement from ==2.3.* to ==2.4.*",
        )
        self.assertEqual(policy.classify(mismatched, CONFIG).tier, "human")

    def test_unsigned_dependabot_commit_is_human(self) -> None:
        # The author login comes from the author email, which anyone can set.
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        pr = replace(pr, commits=(replace(pr.commits[0], github_signed=False),))
        self.assertEqual(policy.classify(pr, CONFIG).tier, "human")

    def test_truncated_file_list_is_human(self) -> None:
        pr = dependabot_pr(("requirements-testing.txt",), ("ruff", "1.2.3", "semver-patch"))
        self.assertEqual(policy.classify(replace(pr, files_truncated=True), CONFIG).tier, "human")
        self.assertEqual(policy.classify(replace(PR, files_truncated=True), CONFIG).tier, "human")

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
    AUTO = policy.Classification("auto", ("x",))
    HUMAN = policy.Classification("human", ("x",))
    APPROVED = [review("bob", "APPROVED")]

    def test_auto_needs_clean_ai_review(self) -> None:
        self.assertEqual(
            policy.decide(PR, self.AUTO, [], "success"),
            (True, "Auto tier: Claude review is clean, no human approval needed"),
        )
        self.assertEqual(policy.decide(PR, self.AUTO, [], None), (False, "Auto tier: waiting for the Claude review"))
        self.assertEqual(policy.decide(PR, self.AUTO, [], "failure")[0], False)
        self.assertIn("did not finish, re-run it", policy.decide(PR, self.AUTO, [], "error")[1])

    def test_auto_dependabot_needs_no_ai_review(self) -> None:
        pr = replace(PR, author="dependabot[bot]")
        self.assertEqual(policy.decide(pr, self.AUTO, [], None), (True, "Auto tier: no human approval needed"))

    def test_human_needs_an_approval_on_the_head_and_a_clean_ai_review(self) -> None:
        self.assertEqual(policy.decide(PR, self.HUMAN, [], "success"), (False, "Human tier: needs 1 human approval"))
        self.assertEqual(policy.decide(PR, self.HUMAN, self.APPROVED, "success"), (True, "Human tier: approved by bob"))

    def test_human_approval_alone_is_not_enough(self) -> None:
        for state, reason in (
            (None, "waiting for the Claude review"),
            ("pending", "waiting for the Claude review"),
            ("error", "Claude review did not finish, re-run it"),
            ("failure", "Claude review has open findings"),
        ):
            with self.subTest(state=state):
                self.assertEqual(policy.decide(PR, self.HUMAN, self.APPROVED, state), (False, f"Human tier: {reason}"))
        self.assertEqual(
            policy.decide(PR, self.HUMAN, [], None),
            (False, "Human tier: needs 1 human approval; waiting for the Claude review"),
        )

    def test_human_tier_without_an_ai_review_needs_only_the_approval(self) -> None:
        # The Claude review does not run for bots or for non-members.
        for pr in (
            replace(PR, author="dependabot[bot]"),
            replace(PR, author="release[bot]"),
            replace(PR, association="CONTRIBUTOR"),
        ):
            with self.subTest(author=pr.author, association=pr.association):
                self.assertEqual(policy.decide(pr, self.HUMAN, self.APPROVED, None)[0], True)
                self.assertEqual(policy.decide(pr, self.HUMAN, [], None), (False, "Human tier: needs 1 human approval"))

    def test_auto_tier_never_skips_the_ai_review_for_people(self) -> None:
        # Only Dependabot's bumps merge on CI alone.
        pr = replace(PR, association="CONTRIBUTOR")
        self.assertEqual(policy.decide(pr, self.AUTO, [], None)[0], False)

    def test_stale_bot_and_self_approvals_do_not_count(self) -> None:
        reviews = [
            review("bob", "APPROVED", commit=OLD_SHA),
            review("github-actions", "APPROVED", bot=True),
            review("alice", "APPROVED"),
            review("mallory", "APPROVED", association="NONE"),
        ]
        self.assertEqual(policy.decide(PR, self.HUMAN, reviews, "success")[0], False)

    def test_committer_cannot_approve_their_own_changes(self) -> None:
        pr = replace(PR, commits=(policy.Commit("bob", "fix-up"),))
        self.assertEqual(policy.decide(pr, self.HUMAN, self.APPROVED, "success")[0], False)

    def test_changes_requested_by_another_reviewer_blocks(self) -> None:
        reviews = [review("bob", "APPROVED"), review("carol", "CHANGES_REQUESTED")]
        for tier in (self.AUTO, self.HUMAN):
            with self.subTest(tier=tier.tier):
                met, reason = policy.decide(PR, tier, reviews, "success")
                self.assertEqual((met, reason.split(": ")[1]), (False, "changes requested by carol"))

    def test_later_changes_requested_overrides_approval(self) -> None:
        reviews = [review("bob", "APPROVED"), review("bob", "CHANGES_REQUESTED")]
        self.assertEqual(policy.decide(PR, self.HUMAN, reviews, "success")[0], False)

    def test_later_comment_keeps_approval(self) -> None:
        reviews = [review("bob", "APPROVED"), review("bob", "COMMENTED")]
        self.assertEqual(policy.decide(PR, self.HUMAN, reviews, "success")[0], True)

    def test_descriptions_fit_a_commit_status(self) -> None:
        # status_for truncates, but every fixed message should fit untruncated.
        for tier in (self.AUTO, self.HUMAN):
            for state in (None, "pending", "error", "failure", "success"):
                with self.subTest(tier=tier.tier, state=state):
                    _, reason = policy.decide(PR, tier, [], state)
                    self.assertLessEqual(len(policy.status_for(False, reason, enforce=False)[1]), 139)


class StatusTest(TestCase):
    def test_advisory_always_succeeds(self) -> None:
        self.assertEqual(
            policy.status_for(False, "Human tier: needs 1 human approval", enforce=False),
            ("success", "Advisory, would wait: Human tier: needs 1 human approval"),
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
        pr = replace(PR, labels=("risk:human",))
        calls, _ = self.run_evaluate(pr, enforce=False, auto_merge=False)
        self.assertIn(("-X", "DELETE", "repos/o/r/issues/7/labels/risk:human"), calls)
        self.assertIn(("-X", "POST", "repos/o/r/issues/7/labels"), calls)
        status = next(c for c in calls if c[2] == f"repos/o/r/statuses/{SHA}")
        self.assertIn("state=success", status)
        self.assertIn("description=Advisory, would wait: Auto tier: waiting for the Claude review", status)

    def test_unchanged_status_is_not_reposted(self) -> None:
        current = {"review-policy": ("success", "Advisory, would wait: Auto tier: waiting for the Claude review")}
        calls, _ = self.run_evaluate(PR, enforce=False, auto_merge=False, statuses=current)
        self.assertFalse([c for c in calls if "statuses" in c[2]])

    def test_status_posted_meanwhile_by_another_run_stands(self) -> None:
        seen = ({}, {})
        meanwhile = ({}, {"review-policy": ("pending", "Human tier: changes requested by carol")})
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
        calls, _ = self.run_evaluate(replace(PR, labels=("risk:auto",)), enforce=False, auto_merge=False)
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

    def test_human_tier_is_never_auto_merged(self) -> None:
        pr = dependabot_pr(("requirements-ui-testing.txt",), ("xa11y", "0.15.0", "semver-minor"))
        _, merge = self.run_evaluate(pr, enforce=True, auto_merge=True)
        merge.assert_not_called()

    def test_auto_merge_is_disabled_when_leaving_the_auto_tier(self) -> None:
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
