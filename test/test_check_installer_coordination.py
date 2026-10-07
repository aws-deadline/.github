# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, main as unittest_main
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_installer_coordination.py"
SPEC = importlib.util.spec_from_file_location("check_installer_coordination", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)
SHA = "a" * 40


def comment(body, *, login="author", kind="User", association="CONTRIBUTOR", id=1, updated="2026-01-01T00:00:00Z"):
    return {
        "id": id, "body": body, "user": {"login": login, "type": kind},
        "author_association": association, "updated_at": updated,
    }


class DetectionTest(TestCase):
    def test_watched_definitions_include_nested_paths(self):
        paths = ["installer/a.xml", "install_builder/a.xml", "installer/components/a.xml"]
        self.assertEqual(checker.affected_paths([{"filename": path} for path in paths]), sorted(paths))

    def test_added_modified_and_deleted_definitions(self):
        for status in ("added", "modified", "removed"):
            with self.subTest(status=status):
                self.assertTrue(checker.affected_paths([{"filename": "installer/a.xml", "status": status}]))

    def test_renames_into_and_out_of_watched_directories(self):
        for old, new in (("installer/a.xml", "docs/a.xml"), ("docs/a.xml", "install_builder/a.xml")):
            with self.subTest(old=old, new=new):
                self.assertTrue(checker.affected_paths([{"filename": new, "previous_filename": old}]))

    def test_payload_and_unrelated_files_do_not_require_confirmation(self):
        files = ["README.md", "src/installer.xml", "installer/logo.png", "install_builder/build_installer.py"]
        self.assertEqual(checker.affected_paths([{"filename": path} for path in files]), [])

    def test_invalid_file_metadata_fails(self):
        for file in ({}, {"filename": None}, {"filename": "README.md", "previous_filename": 7}):
            with self.subTest(file=file), self.assertRaises((KeyError, ValueError)):
                checker.affected_paths([file])


class CommandTest(TestCase):
    def test_all_three_commands_and_whitespace(self):
        for value in ("done", "in-review", "not-needed"):
            with self.subTest(value=value):
                self.assertEqual(checker.parse_command(f" \n/INSTALLER-FOLLOWUP {value.upper()}\n "), value)

    def test_prose_quotes_examples_and_extra_details_are_rejected(self):
        for body in (
            "Already updated.", "/installer-followup planned", "> /installer-followup done",
            "`/installer-followup done`", "```\n/installer-followup done\n```",
            "/installer-followup in-review https://example.invalid/private-tracker",
            "/installer-followup done\n/installer-followup not-needed", None,
        ):
            with self.subTest(body=body):
                self.assertIsNone(checker.parse_command(body))

    def test_pr_author_is_accepted_without_permission_lookup(self):
        with patch.object(checker, "gh") as api:
            self.assertEqual(checker.latest_confirmation("o/r", "author", [comment("/installer-followup done")]), "done")
        api.assert_not_called()

    def test_write_maintain_and_admin_users_are_accepted(self):
        for role in ("write", "maintain", "admin"):
            with self.subTest(role=role), patch.object(checker, "gh", return_value={"permission": role}):
                reply = comment("/installer-followup in-review", login="maintainer", association="MEMBER")
                self.assertEqual(checker.latest_confirmation("o/r", "author", [reply]), "in-review")

    def test_read_only_collaborators_and_outsiders_cannot_confirm(self):
        for association in ("COLLABORATOR", "NONE", "CONTRIBUTOR"):
            with self.subTest(association=association), patch.object(checker, "gh", return_value={"permission": "read"}):
                reply = comment("/installer-followup done", login="reader", association=association)
                self.assertIsNone(checker.latest_confirmation("o/r", "author", [reply]))

    def test_outsider_command_does_not_hide_valid_author_confirmation(self):
        replies = [
            comment("/installer-followup done"),
            comment("/installer-followup not-needed", login="outsider", association="NONE", id=2),
        ]
        with patch.object(checker, "gh") as api:
            self.assertEqual(checker.latest_confirmation("o/r", "author", replies), "done")
        api.assert_not_called()

    def test_bot_commands_are_ignored_even_if_the_bot_is_the_pr_author(self):
        reply = comment("/installer-followup done", login="robot[bot]", kind="Bot")
        self.assertIsNone(checker.latest_confirmation("o/r", "robot[bot]", [reply]))

    def test_latest_edited_valid_command_wins(self):
        replies = [
            comment("/installer-followup done", id=1, updated="2026-01-03T00:00:00Z"),
            comment("/installer-followup in-review", id=2, updated="2026-01-02T00:00:00Z"),
        ]
        self.assertEqual(checker.latest_confirmation("o/r", "author", replies), "done")


class FlowTest(TestCase):
    def setUp(self):
        self.metadata = {
            "state": "open", "head": {"sha": SHA, "repo": {"full_name": "contributor/fork"}},
            "base": {"sha": "c" * 40, "ref": "mainline"}, "changed_files": 1,
            "user": {"login": "author"},
        }
        self.files = [[{"filename": "installer/a.xml"}]]
        self.comments = []
        self.calls = []
        self.statuses = []
        self.fail_files = False
        self.fail_posting = False
        self.change_head = False
        self.metadata_reads = 0

    def api(self, *args, input_json=None):
        self.calls.append((args, deepcopy(input_json)))
        if args[0] == "--paginate":
            endpoint = args[-1]
            if "/files?" in endpoint:
                if self.fail_files:
                    raise subprocess.CalledProcessError(1, ["gh"], stderr="https://example.invalid/private-tracker")
                return deepcopy(self.files)
            return [deepcopy(self.comments)]
        endpoint = args[0]
        if endpoint == "repos/o/r/pulls/1":
            self.metadata_reads += 1
            value = deepcopy(self.metadata)
            if self.change_head and self.metadata_reads > 1:
                value["head"]["sha"] = "b" * 40
            return value
        if "/statuses/" in endpoint:
            self.statuses.append((endpoint, deepcopy(input_json)))
            return {}
        if endpoint == "repos/o/r/issues/1/comments":
            if self.fail_posting:
                raise subprocess.CalledProcessError(1, ["gh"], stderr="Posting failed")
            created = comment(input_json["body"], login=checker.BOT_LOGIN, kind="Bot", id=100)
            self.comments.append(created)
            return deepcopy(created)
        if "/issues/comments/" in endpoint:
            target = next(c for c in self.comments if c["id"] == int(endpoint.rsplit("/", 1)[1]))
            target["body"] = input_json["body"]
            return deepcopy(target)
        if "/permission" in endpoint:
            return {"permission": "write"}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    def run_check(self):
        output = io.StringIO()
        with patch.object(checker, "gh", side_effect=self.api), redirect_stdout(output):
            code = checker.run("o/r", "1", run_url="https://github.com/o/r/actions/runs/1")
        return code, output.getvalue()

    def posted_questions(self):
        return [c for c in self.comments if c["user"]["login"] == checker.BOT_LOGIN]

    def test_changed_definitions_create_question_and_fail_the_pr_head_status(self):
        code, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.posted_questions()), 1)
        self.assertIn("/installer-followup in-review", self.posted_questions()[0]["body"])
        self.assertEqual([s[1]["state"] for s in self.statuses], ["pending", "failure"])
        self.assertTrue(all(s[0].endswith(SHA) for s in self.statuses))
        self.assertTrue(all(s[1]["context"] == "Installer coordination" for s in self.statuses))

    def test_each_confirmation_command_passes_and_updates_the_same_question(self):
        self.run_check()
        for value in ("done", "in-review", "not-needed"):
            with self.subTest(value=value):
                self.comments = [c for c in self.comments if c["id"] == 100]
                self.comments.append(comment(f"/installer-followup {value}"))
                self.assertEqual(self.run_check()[0], 0)
                self.assertEqual(self.statuses[-1][1]["state"], "success")
                self.assertEqual(len(self.posted_questions()), 1)
                self.assertIn(checker.CONFIRMATIONS[value], self.posted_questions()[0]["body"])

    def test_repeated_pushes_do_not_duplicate_or_repatch_unchanged_comment(self):
        self.run_check()
        self.calls = []
        self.run_check()
        writes = [args for args, body in self.calls if body and "/issues/" in args[0]]
        self.assertEqual(writes, [])
        self.assertEqual(len(self.posted_questions()), 1)

    def test_deleting_the_only_confirmation_makes_status_fail_again(self):
        self.comments = [comment("/installer-followup done")]
        self.assertEqual(self.run_check()[0], 0)
        self.comments = [c for c in self.comments if c["id"] != 1]
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.statuses[-1][1]["state"], "failure")
        self.assertIn("Awaiting confirmation", self.posted_questions()[0]["body"])

    def test_editing_the_only_confirmation_into_prose_makes_status_fail_again(self):
        self.comments = [comment("/installer-followup done")]
        self.assertEqual(self.run_check()[0], 0)
        self.comments[0]["body"] = "I might do it later."
        self.assertEqual(self.run_check()[0], 1)

    def test_unrelated_pr_passes_without_posting_a_question(self):
        self.files = [[{"filename": "src/app.py"}]]
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(self.posted_questions(), [])
        self.assertEqual(self.statuses[-1][1]["state"], "success")

    def test_removing_installer_changes_updates_existing_question(self):
        self.run_check()
        self.files = [[{"filename": "README.md"}]]
        self.metadata["head"]["sha"] = "b" * 40
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(len(self.posted_questions()), 1)
        self.assertIn("No installer definitions changed", self.posted_questions()[0]["body"])
        self.assertTrue(self.statuses[-1][0].endswith("b" * 40))

    def test_definition_on_a_later_file_page_is_detected(self):
        self.files = [[{"filename": "README.md"}], [{"filename": "install_builder/a.xml"}]]
        self.metadata["changed_files"] = 2
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(len(self.posted_questions()), 1)

    def test_incomplete_file_list_is_an_error_not_a_pass(self):
        self.metadata["changed_files"] = 2
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.statuses[-1][1]["state"], "error")
        self.assertEqual(self.posted_questions(), [])

    def test_a_head_change_during_evaluation_does_not_receive_old_success(self):
        self.comments = [comment("/installer-followup done")]
        self.change_head = True
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.statuses[-1][1]["state"], "error")
        self.assertTrue(all(endpoint.endswith(SHA) for endpoint, _ in self.statuses))

    def test_api_failure_is_an_error_and_does_not_echo_response_details(self):
        self.fail_files = True
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses[-1][1]["state"], "error")
        self.assertNotIn("example.invalid", output)

    def test_comment_posting_failure_does_not_publish_success(self):
        self.comments = [comment("/installer-followup done")]
        self.fail_posting = True
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.statuses[-1][1]["state"], "error")

    def test_human_marker_cannot_impersonate_the_bot_question(self):
        self.comments = [comment(checker.COMMENT_MARKER, login="other")]
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(len(self.posted_questions()), 1)
        self.assertEqual(self.comments[0]["body"], checker.COMMENT_MARKER)

    def test_private_reply_details_are_not_echoed_into_question_or_summary(self):
        self.comments = [comment("/installer-followup in-review https://example.invalid/private-tracker")]
        with TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                code, output = self.run_check()
            self.assertEqual(code, 1)
            self.assertNotIn("example.invalid", output + summary.read_text() + self.posted_questions()[0]["body"])

    def test_closed_pr_and_other_base_branches_are_skipped(self):
        original = deepcopy(self.metadata)
        for changes in ({"state": "closed"}, {"base": {"sha": "c" * 40, "ref": "other"}}):
            with self.subTest(changes=changes):
                self.metadata = {**deepcopy(original), **changes}
                self.assertEqual(self.run_check()[0], 0)
                self.assertEqual(self.statuses, [])
                self.assertEqual(self.posted_questions(), [])

    def test_malformed_head_sha_fails_cleanly(self):
        self.metadata["head"]["sha"] = 7
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.statuses, [])


class ContextTest(TestCase):
    def test_invalid_repository_and_pr_number_are_rejected_without_api_calls(self):
        for context in ({"REPO": "../r", "PR_NUMBER": "1"}, {"REPO": "o/r", "PR_NUMBER": "0"}):
            with self.subTest(context=context), patch.dict(os.environ, context, clear=True):
                with patch.object(checker, "gh") as api, redirect_stdout(io.StringIO()):
                    self.assertEqual(checker.main(), 1)
                api.assert_not_called()


if __name__ == "__main__":
    unittest_main()
