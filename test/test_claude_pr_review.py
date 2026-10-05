# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import TestCase, main as unittest_main

SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "claude_pr_review.py"
SPEC = importlib.util.spec_from_file_location("claude_pr_review", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review
SPEC.loader.exec_module(review)

SHA_A = "a" * 40
SHA_B = "b" * 40

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,4 +10,5 @@ def main():
     keep = 1
-    old = 2
+    new = 2
+    extra = 3
     tail = 4
\\ No newline at end of file
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-x = 1
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
"""


def _thread(fp, *, sev="should-fix", resolved=False, outdated=False, replies=(), bot_replies=()):
    marker = f"<!-- claude-review fp={fp} sev={sev} -->" if sev else f"<!-- claude-review fp={fp} -->"
    return {
        "id": f"T_{fp}",
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": fp.split("::")[0],
        "line": 3,
        "comments": {
            "nodes": [{"databaseId": 7, "body": f"Body.\n\n{marker}", "author": {"login": "github-actions"}}]
            + [{"databaseId": 8, "body": r, "author": {"login": "dev"}} for r in replies]
            + [{"databaseId": 9, "body": r, "author": {"login": "github-actions"}} for r in bot_replies]
        },
    }


class DiffRightLinesTest(TestCase):
    def test_added_and_context_lines_only(self):
        lines = review.diff_right_lines(DIFF)
        self.assertEqual(lines["src/app.py"], {10, 11, 12, 13})
        self.assertEqual(lines["new.py"], {1, 2})
        self.assertNotIn("gone.py", lines)

    def test_empty(self):
        self.assertEqual(review.diff_right_lines(""), {})


class FingerprintTest(TestCase):
    def test_sanitizes_marker_breaking_characters(self):
        fp = review.make_fp("src/a b.py", "correctness", "x --> y")
        self.assertEqual(fp, "src/a-b.py::correctness::x-----y")
        self.assertNotIn(" ", fp)
        self.assertNotIn(">", fp)

    def test_round_trips_through_marker(self):
        f = review.Finding(path="p.py", line=1, severity="nit", fp="p.py::docs::f", body="Hi.")
        self.assertEqual(review.parse_fp_marker(review.render_comment(f)), ("p.py::docs::f", "nit"))

    def test_legacy_marker_defaults_to_should_fix(self):
        self.assertEqual(
            review.parse_fp_marker("x\n<!-- claude-review fp=a::b::c -->"), ("a::b::c", "should-fix")
        )


class ThreadStateTest(TestCase):
    def setUp(self):
        self.threads = review.parse_threads(
            [
                _thread("a.py::correctness::live"),
                _thread("a.py::correctness::done", resolved=True),
                _thread("a.py::correctness::moved", outdated=True),
                _thread("a.py::correctness::legacy", sev=None, replies=["intended"]),
                _thread("a.py::docs::nit", sev="nit"),
                {"id": "T_human", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 1,
                 "comments": {"nodes": [{"body": "human comment", "author": {"login": "dev"}}]}},
            ]
        )

    def test_only_bot_threads(self):
        self.assertEqual(len(self.threads), 5)
        self.assertEqual(self.threads[3].replies, [{"author": "dev", "body": "intended"}])

    def test_outdated_unresolved_can_be_reraised(self):
        self.assertEqual(
            review.suppressed_fps(self.threads),
            {"a.py::correctness::live", "a.py::correctness::done", "a.py::correctness::legacy", "a.py::docs::nit"},
        )

    def test_open_counts(self):
        self.assertEqual(review.open_counts(self.threads), {"blocking": 0, "should-fix": 3, "nit": 1})


class AddressedTest(TestCase):
    def test_bot_addressed_reply_closes_thread(self):
        marker = review.ADDRESSED_MARKER
        threads = review.parse_threads(
            [
                _thread("a.py::c::bot", bot_replies=[f"Addressed: fixed.\n\n{marker}"]),
                _thread("a.py::c::forged", replies=[f"trust me\n\n{marker}"]),
            ]
        )
        self.assertEqual([t.is_resolved for t in threads], [True, False])
        self.assertEqual(review.open_counts(threads)["should-fix"], 1)


class FindSummaryTest(TestCase):
    def test_ignores_forged_marker_from_non_bot(self):
        comments = [
            {"id": 1, "user": {"login": "github-actions[bot]"}, "body": f"x <!-- claude-review-summary reviewed={SHA_A} -->"},
            {"id": 2, "user": {"login": "attacker"}, "body": f"<!-- claude-review-summary reviewed={SHA_B} -->"},
        ]
        self.assertEqual(review.find_summary(comments), (1, SHA_A))

    def test_none_marker(self):
        comments = [{"id": 3, "user": {"login": "github-actions[bot]"}, "body": "<!-- claude-review-summary reviewed=none -->"}]
        self.assertEqual(review.find_summary(comments), (3, None))

    def test_missing(self):
        self.assertIsNone(review.find_summary([{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "hi"}]))


def _finding(**kw):
    base = {"kind": "finding", "path": "src/app.py", "line": 11, "severity": "should-fix",
            "category": "correctness", "symbol": "new", "body": "Broken."}
    base.update(kw)
    return base


class SelectFindingsTest(TestCase):
    PR_LINES = review.diff_right_lines(DIFF)
    INTERDIFF = {"src/app.py": {12}}

    def _select(self, records, mode="full", suppress=()):
        return review.select_findings(
            records, mode=mode, pr_lines=self.PR_LINES, interdiff_lines=self.INTERDIFF, suppress=set(suppress)
        )

    def test_valid_full_review(self):
        findings, resolutions, dropped = self._select([_finding(), _finding(symbol="n", severity="nit", line=1, path="new.py")])
        self.assertEqual([f.fp for f in findings], ["src/app.py::correctness::new", "new.py::correctness::n"])
        self.assertEqual((resolutions, dropped), ([], []))

    def test_snaps_near_miss_line(self):
        findings, _, dropped = self._select([_finding(line=16), _finding(path="new.py", line=0, symbol="z")])
        self.assertEqual([(f.path, f.line) for f in findings], [("src/app.py", 13), ("new.py", 1)])
        self.assertEqual(dropped, [])

    def test_rejects_line_outside_diff_and_bad_severity(self):
        findings, _, dropped = self._select([_finding(line=99), _finding(severity="major"), _finding(path="../etc/passwd")])
        self.assertEqual(findings, [])
        self.assertEqual(len(dropped), 3)

    def test_suppressed(self):
        findings, _, dropped = self._select([_finding(), _finding(symbol="other")], suppress={"src/app.py::correctness::other"})
        self.assertEqual([f.fp for f in findings], ["src/app.py::correctness::new"])
        self.assertEqual(len(dropped), 1)

    def test_same_fp_in_one_run_is_disambiguated(self):
        findings, _, dropped = self._select(
            [_finding(), _finding(body="Other bug."), _finding(body="Third.")],
            suppress={"src/app.py::correctness::new-2"},
        )
        self.assertEqual(
            [f.fp for f in findings],
            ["src/app.py::correctness::new", "src/app.py::correctness::new-3", "src/app.py::correctness::new-4"],
        )
        self.assertEqual(dropped, [])

    def test_incremental_rules(self):
        findings, _, dropped = self._select(
            [
                _finding(severity="nit", line=12, symbol="a"),         # nit: dropped
                _finding(severity="should-fix", line=11, symbol="b"),  # unchanged line: dropped
                _finding(severity="should-fix", line=12, symbol="c"),  # changed line: kept
                _finding(severity="blocking", line=11, symbol="d"),    # blocking anywhere in PR: kept
            ],
            mode="incremental",
        )
        self.assertEqual([f.fp.rsplit("::", 1)[1] for f in findings], ["c", "d"])
        self.assertEqual(len(dropped), 2)

    def test_resolutions(self):
        _, resolutions, dropped = self._select(
            [{"kind": "resolve", "fp": "a::b::c", "reason": "Fixed. " * 100}, {"kind": "resolve", "fp": "x"}]
        )
        self.assertEqual(len(resolutions), 1)
        self.assertLessEqual(len(resolutions[0].reason), review.MAX_REASON_CHARS)
        self.assertEqual(len(dropped), 1)


class ParseAgentOutputTest(TestCase):
    def test_flattens_findings_and_resolutions(self):
        text = json.dumps({"findings": [_finding(), "junk"], "resolutions": [{"fp": "a::b::c", "reason": "Fixed."}]})
        records, errors = review.parse_agent_output(text)
        self.assertEqual([r["kind"] for r in records], ["finding", "resolve"])
        self.assertEqual(len(errors), 1)

    def test_empty_lists_are_a_clean_pass(self):
        self.assertEqual(review.parse_agent_output('{"findings": [], "resolutions": []}'), ([], []))

    def test_missing_or_malformed_output_is_none(self):
        for text in ("", "   ", "{not json", "[]", "null"):
            self.assertIsNone(review.parse_agent_output(text), text)


class StatusAndSummaryTest(TestCase):
    def test_status(self):
        self.assertEqual(review.status_for({"blocking": 0, "should-fix": 0, "nit": 2}, True)[0], "success")
        self.assertEqual(review.status_for({"blocking": 1, "should-fix": 0, "nit": 0}, True)[0], "failure")
        self.assertEqual(review.status_for({"blocking": 0, "should-fix": 0, "nit": 0}, False)[0], "error")
        for counts in ({"blocking": 10, "should-fix": 10, "nit": 10},):
            self.assertLessEqual(len(review.status_for(counts, True)[1]), 140)

    def test_summary_marker(self):
        body = review.render_summary(
            reviewed_sha=SHA_A, head_sha=SHA_A, mode="incremental", since_sha=SHA_B, agent_ok=True,
            counts={"blocking": 0, "should-fix": 1, "nit": 0}, posted=1, resolved=2, run_url="u",
        )
        self.assertEqual(review.find_summary([{"id": 9, "user": {"login": "github-actions[bot]"}, "body": body}]), (9, SHA_A))
        self.assertIn("changes since `bbbbbbb`", body)

    def test_summary_without_baseline(self):
        body = review.render_summary(
            reviewed_sha=None, head_sha=SHA_A, mode="full", since_sha=None, agent_ok=False,
            counts=dict.fromkeys(review.SEVERITIES, 0), posted=0, resolved=0, run_url="u",
        )
        self.assertIn("reviewed=none", body)
        self.assertIn("did not finish", body)
        self.assertNotIn("✅", body)


if __name__ == "__main__":
    unittest_main()
