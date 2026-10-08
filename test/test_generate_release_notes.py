# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path
from unittest import TestCase, main as unittest_main

# The script imports boto3 at module level; the tests replace the client, so a
# stub module is enough and the suite runs without boto3 installed.
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "generate_release_notes.py"
SPEC = importlib.util.spec_from_file_location("generate_release_notes", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
notes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = notes
SPEC.loader.exec_module(notes)

ENTRY = {"category": "features", "description": "Adds a thing.", "reference": "(#1)"}


def _text(text="Here are the notes..."):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _tool(entries, tool_id="tu_1"):
    return {
        "content": [{"type": "tool_use", "id": tool_id, "name": "emit_release_notes", "input": {"entries": entries}}],
        "stop_reason": "tool_use",
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": io.BytesIO(json.dumps(self.responses.pop(0)).encode())}


class InvokeBedrockTest(TestCase):
    def setUp(self):
        self._orig = (getattr(notes.boto3, "client", None), notes.build_system_prompt)
        notes.build_system_prompt = lambda repo_name: "system"

    def tearDown(self):
        client, notes.build_system_prompt = self._orig
        if client is None:
            del notes.boto3.client
        else:
            notes.boto3.client = client

    def _run(self, *responses):
        self.client = FakeClient(responses)
        notes.boto3.client = lambda *a, **kw: self.client
        return notes.invoke_bedrock("commits", "us-west-2", "repo")

    def test_tool_call_on_first_response(self):
        self.assertEqual(self._run(_tool([ENTRY])), [ENTRY])
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(self.client.requests[0]["tool_choice"], {"type": "auto"})

    def test_empty_entries_are_a_result(self):
        self.assertEqual(self._run(_tool([])), [])

    def test_plain_text_first_response_is_retried(self):
        self.assertEqual(self._run(_text(), _tool([ENTRY])), [ENTRY])
        retry = self.client.requests[1]["messages"]
        self.assertEqual([m["role"] for m in retry], ["user", "assistant", "user"])
        self.assertEqual(retry[1]["content"], _text()["content"])
        self.assertEqual(retry[2]["content"], [{"type": "text", "text": notes.TOOL_REMINDER}])

    def test_malformed_tool_call_is_answered_before_retrying(self):
        self.assertEqual(self._run(_tool("not a list"), _tool([ENTRY], tool_id="tu_2")), [ENTRY])
        reply = self.client.requests[1]["messages"][2]["content"]
        self.assertEqual(reply[0]["type"], "tool_result")
        self.assertEqual(reply[0]["tool_use_id"], "tu_1")

    def test_gives_up_after_max_attempts(self):
        with self.assertRaises(RuntimeError):
            self._run(*[_text()] * notes.MAX_ATTEMPTS)
        self.assertEqual(len(self.client.requests), notes.MAX_ATTEMPTS)

    def test_refusal_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self._run({"content": [], "stop_reason": "refusal"})
        self.assertEqual(len(self.client.requests), 1)


class GetPrDescriptionsTest(TestCase):
    def setUp(self):
        self._orig_run = notes.subprocess.run

    def tearDown(self):
        notes.subprocess.run = self._orig_run

    def _run(self, result):
        notes.subprocess.run = lambda args, **kw: subprocess.CompletedProcess(args, *result)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            descriptions = notes.get_pr_descriptions([{"subject": "feat: a thing (#12)"}])
        return descriptions, stderr.getvalue()

    def test_fetched_description_is_returned(self):
        descriptions, stderr = self._run((0, "Title\nBody", ""))
        self.assertEqual(descriptions, {"12": "Title\nBody"})
        self.assertEqual(stderr, "")

    def test_gh_failure_is_warned_not_silent(self):
        descriptions, stderr = self._run((4, "", "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable.\n"))
        self.assertEqual(descriptions, {})
        self.assertIn("#12", stderr)
        self.assertIn("set the GH_TOKEN environment variable", stderr)


class RepoNameFromRemoteTest(TestCase):
    def setUp(self):
        self._orig = (sys.argv, notes.run_git, notes.get_latest_tag, notes.get_commits_since_tag)
        notes.get_latest_tag = lambda: "1.0.0"
        notes.get_commits_since_tag = lambda tag: []

    def tearDown(self):
        sys.argv, notes.run_git, notes.get_latest_tag, notes.get_commits_since_tag = self._orig

    def _repo_name(self, remote):
        sys.argv = ["generate_release_notes.py"]
        notes.run_git = lambda *args: remote
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            notes.main()
        return stderr.getvalue().split("Generating release notes for ", 1)[1].split(" since ", 1)[0]

    def test_git_suffix_is_removed(self):
        self.assertEqual(self._repo_name("https://github.com/aws-deadline/deadline-cloud.git"), "deadline-cloud")

    def test_names_ending_in_suffix_letters_are_kept(self):
        # rstrip(".git") strips any trailing ".", "g", "i", "t" characters, not the suffix.
        for remote, name in [
            ("https://github.com/OpenJobDescription/openjd-cli.git", "openjd-cli"),
            ("git@github.com:aws-deadline/deadline-cloud-for-unreal-engine-plugin-git", "deadline-cloud-for-unreal-engine-plugin-git"),
            ("https://github.com/aws-deadline/.github", ".github"),
        ]:
            with self.subTest(remote=remote):
                self.assertEqual(self._repo_name(remote), name)


if __name__ == "__main__":
    unittest_main()
