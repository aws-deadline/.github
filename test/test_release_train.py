# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase, main as unittest_main, mock

SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "release_train.py"
SPEC = importlib.util.spec_from_file_location("release_train", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
rt = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rt
SPEC.loader.exec_module(rt)

REPO = "example-owner/example-repo"
BRANCH = "bot/sessions-openjd-model-0.15.0"
URL = "https://example-bucket.s3.us-west-2.amazonaws.com/trains/t/handoff/sessions/pr/x.bundle?X-Amz-Signature=s"
ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "PATH": os.environ["PATH"], "HOME": os.devnull}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


class FakeApi:
    """The GitHub REST calls the script makes, against an in-memory repository."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.prs: dict[int, dict[str, Any]] = {}
        self.labels: set[str] = set()
        self.refs: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}

    def pr(self, number: int, branch: str = BRANCH, head: str = "a" * 40, **fields: Any) -> dict[str, Any]:
        self.prs[number] = {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}", "state": "open",
                            "merged": False, "merge_commit_sha": None, "labels": [],
                            "head": {"ref": branch, "sha": head, "repo": {"full_name": REPO}},
                            "base": {"ref": "rt-ci"}, **fields}
        return self.prs[number]

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        prefix = f"/repos/{REPO}"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        if method == "GET" and rest.startswith("/pulls?"):
            return [p for p in self.prs.values() if p["state"] == "open" and p["head"]["ref"] == BRANCH]
        if method == "POST" and rest == "/pulls":
            return self.pr(len(self.prs) + 20, body["head"])
        if rest.startswith("/pulls/"):
            number = int(rest.split("/")[2])
            if method == "PATCH":
                self.prs[number].update(body)
            return self.prs[number]
        if rest.startswith("/labels"):
            if method == "GET":
                if rest.split("/")[-1] not in self.labels:
                    raise rt.ApiError(404, "Not Found")
                return {}
            self.labels.add(body["name"])
            return {}
        if method == "POST" and rest.startswith("/issues/") and rest.endswith("/comments"):
            return {"html_url": f"https://github.com/{REPO}/pull/{rest.split('/')[2]}#comment"}
        if method == "POST" and rest.startswith("/actions/workflows/") and rest.endswith("/dispatches"):
            return None
        if rest.startswith("/issues/") and rest.endswith("/labels"):
            self.prs[int(rest.split("/")[2])]["labels"] += [{"name": n} for n in body["labels"]]
            return []
        if rest.startswith("/git/ref/tags/"):
            if rest not in self.refs:
                raise rt.ApiError(404, "Not Found")
            return self.refs[rest]
        if rest == "/git/tags":
            return {"sha": "e" * 40}
        if rest.startswith("/git/tags/"):
            return {"object": {"sha": self.tag_target}}
        if rest == "/git/refs":
            self.refs[f"/git/ref/tags/{body['ref'].removeprefix('refs/tags/')}"] = {
                "object": {"type": "tag", "sha": body["sha"]}}
            return {}
        if rest.startswith("/releases/tags/"):
            tag = rest.split("/")[-1]
            if tag not in self.releases:
                raise rt.ApiError(404, "Not Found")
            return self.releases[tag]
        if rest == "/releases":
            self.releases[body["tag_name"]] = {"html_url": f"https://github.com/{REPO}/releases/tag/{body['tag_name']}",
                                               "name": body["name"], "prerelease": body["prerelease"]}
            return self.releases[body["tag_name"]]
        raise AssertionError(f"unexpected {method} {path}")


class Repos(TestCase):
    """A local stand-in for the GitHub repository (rt-ci with history) and a train clone with a bot branch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "rt-ci", str(self.remote)], check=True, env=ENV)
        self.work = self.tmp / "work"
        subprocess.run(["git", "init", "-q", "-b", "rt-ci", str(self.work)], check=True, env=ENV)
        (self.work / "pyproject.toml").write_text('dependencies = ["openjd-model >= 0.14,< 0.15"]\n')
        (self.work / ".github").mkdir()
        (self.work / ".github" / "ci.yml").write_text("on: push\n")
        sh(self.work, "add", ".")
        sh(self.work, "commit", "-qm", "base")
        self.base = sh(self.work, "rev-parse", "HEAD")
        sh(self.work, "push", "-q", str(self.remote), "rt-ci")
        sh(self.work, "switch", "-qc", BRANCH)
        (self.work / "pyproject.toml").write_text('dependencies = ["openjd-model >= 0.15,< 0.16"]\n')
        sh(self.work, "commit", "-qam", "feat!: Require openjd-model 0.15")
        self.api = FakeApi()
        self.downloads: dict[str, bytes] = {}
        self.ctx = rt.Context(repo=REPO, default_branch="rt-ci", token="", mode="rehearsal",
                              workdir=self.tmp / "job", api=self.api,
                              download=lambda url, limit: self.downloads[url], remote_url=str(self.remote),
                              sleep=lambda s: None)
        self.ctx.workdir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def bundle(self) -> dict[str, Any]:
        tip = sh(self.work, "rev-parse", BRANCH)
        out = self.tmp / f"{tip}.src.bundle"
        sh(self.work, "bundle", "create", "-q", str(out), f"refs/heads/{BRANCH}", f"^{self.base}")
        data = out.read_bytes()
        self.downloads[URL] = data
        return {"kind": "push", "id": "t|sessions|pr|0123456789abcdef|0", "branch": BRANCH, "tip": tip,
                "base_sha": self.base, "bundle_url": URL, "bundle_sha256": hashlib.sha256(data).hexdigest(),
                "bundle_bytes": len(data)}

    def remote_branch(self) -> str:
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "-q",
                               f"refs/heads/{BRANCH}"], capture_output=True, text=True, env=ENV).stdout.strip()


class TestValidate(TestCase):
    def setUp(self) -> None:
        self.ctx = rt.Context(repo=REPO, default_branch="rt-ci", token="", mode="rehearsal", workdir=Path("/nonexistent"),
                              api=FakeApi(), download=lambda url, limit: b"")

    def push(self, **fields: Any) -> dict[str, Any]:
        return {"kind": "push", "id": "a|b|pr|0123456789abcdef|0", "branch": BRANCH, "tip": "a" * 40,
                "base_sha": "b" * 40, "bundle_url": URL, "bundle_sha256": "c" * 64, "bundle_bytes": 10, **fields}

    def test_only_bot_branches_are_pushed(self) -> None:
        self.assertEqual(rt.validate(self.push(), self.ctx)["branch"], BRANCH)
        for branch in ("rt-ci", "mainline", "refs/heads/bot/x", "bot/../mainline", "bot/.hidden", "bot/x.lock",
                       "bot/", "bots/x", "bot/x y"):
            with self.subTest(branch=branch), self.assertRaisesRegex(rt.Refused, "only bot/\\* branches"):
                rt.validate(self.push(branch=branch), self.ctx)

    def test_a_bundle_url_must_be_https_s3(self) -> None:
        for url in ("http://example-bucket.s3.amazonaws.com/x", "https://example.com/x.bundle",
                    "https://s3.amazonaws.com.example.com/x", "file:///tmp/x.bundle"):
            with self.subTest(url=url), self.assertRaisesRegex(rt.Refused, "https S3 URL"):
                rt.validate(self.push(bundle_url=url), self.ctx)

    def test_shas_and_sizes_are_checked(self) -> None:
        with self.assertRaisesRegex(rt.Refused, "tip must be a 40-hex"):
            rt.validate(self.push(tip="main"), self.ctx)
        with self.assertRaisesRegex(rt.Refused, "bundle_sha256 must be 64 hex"):
            rt.validate(self.push(bundle_sha256="x"), self.ctx)
        with self.assertRaisesRegex(rt.Refused, "bundle_bytes must be between"):
            rt.validate(self.push(bundle_bytes=rt.MAX_BUNDLE_BYTES + 1), self.ctx)

    def test_a_pr_targets_the_default_branch_with_known_labels_only(self) -> None:
        pr = {"kind": "open_or_update_pr", "id": "a|b|pr|0123456789abcdef|1", "branch": BRANCH, "base": "rt-ci",
              "title": "feat!: x", "body": "b", "labels": ["agent-modified"]}
        self.assertEqual(rt.validate(pr, self.ctx)["base"], "rt-ci")
        with self.assertRaisesRegex(rt.Refused, "base must be the default branch rt-ci"):
            rt.validate({**pr, "base": "mainline"}, self.ctx)
        with self.assertRaisesRegex(rt.Refused, "labels must be among"):
            rt.validate({**pr, "labels": ["approved"]}, self.ctx)
        self.assertEqual(rt.validate({**pr, "labels": ["agent-modified", "needs-human"]}, self.ctx)["labels"],
                         ["agent-modified", "needs-human"])
        with self.assertRaisesRegex(rt.Refused, "one non-empty line"):
            rt.validate({**pr, "title": "a\nb"}, self.ctx)

    def test_tags_are_x_y_z_and_rehearsal_releases_say_so(self) -> None:
        tag = {"kind": "create_tag_and_prerelease", "id": "a|b|publish|0123456789abcdef|0", "tag": "0.15.0",
               "sha": "a" * 40, "title": f"0.15.0 {rt.REHEARSAL_SUFFIX}", "notes": "n"}
        self.assertEqual(rt.validate(tag, self.ctx)["tag"], "0.15.0")
        for bad in ("v0.15.0", "0.15", "0.15.0rc1", "latest"):
            with self.subTest(tag=bad), self.assertRaisesRegex(rt.Refused, "only X.Y.Z"):
                rt.validate({**tag, "tag": bad}, self.ctx)
        with self.assertRaisesRegex(rt.Refused, "rehearsal release must be titled"):
            rt.validate({**tag, "title": "0.15.0"}, self.ctx)

    def test_live_mode_never_creates_a_tag_or_release(self) -> None:
        # In live mode the repository's own release workflow tags and releases; a rehearsal tag would collide.
        tag = {"kind": "create_tag_and_prerelease", "id": "a|b|publish|0123456789abcdef|0", "tag": "0.15.0",
               "sha": "a" * 40, "title": f"0.15.0 {rt.REHEARSAL_SUFFIX}", "notes": "n"}
        for mode in ("live", "", "Rehearsal"):
            self.ctx.mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(rt.Refused, "only in rehearsal mode"):
                rt.validate(tag, self.ctx)

    def test_only_release_bump_is_dispatched_on_the_default_branch_in_live_mode(self) -> None:
        bump = {"kind": "dispatch_workflow", "id": "a|b|release_pr|0123456789abcdef|0", "workflow": "release_bump.yml",
                "ref": "rt-ci", "inputs": {}}
        self.ctx.mode = "live"
        self.assertEqual(rt.validate(bump, self.ctx)["workflow"], "release_bump.yml")
        self.assertEqual(rt.validate({**bump, "inputs": {"force_version_bump": "minor"}}, self.ctx)["ref"], "rt-ci")
        with self.assertRaisesRegex(rt.Refused, "may be dispatched"):
            rt.validate({**bump, "workflow": "release_publish.yml"}, self.ctx)
        with self.assertRaisesRegex(rt.Refused, "only on the default branch rt-ci"):
            rt.validate({**bump, "ref": "release"}, self.ctx)
        for inputs in ({"force_version_bump": "huge"}, {"other": "x"}, "minor"):
            with self.subTest(inputs=inputs), self.assertRaisesRegex(rt.Refused, "inputs"):
                rt.validate({**bump, "inputs": inputs}, self.ctx)
        self.ctx.mode = "rehearsal"
        with self.assertRaisesRegex(rt.Refused, "only in live mode"):
            rt.validate(bump, self.ctx)

    def test_unknown_kinds_ids_and_workflows_are_refused(self) -> None:
        with self.assertRaisesRegex(rt.Refused, "unknown action kind"):
            rt.validate({"kind": "merge", "id": "x"}, self.ctx)
        with self.assertRaisesRegex(rt.Refused, "bad action id"):
            rt.validate(self.push(id="x;rm -rf /"), self.ctx)
        self.ctx.mode = "live"
        with self.assertRaisesRegex(rt.Refused, "may be dispatched"):
            rt.validate({"kind": "dispatch_workflow", "id": "x", "workflow": "release_publish.yml", "ref": "rt-ci",
                         "inputs": {}}, self.ctx)


class TestVerifyBundle(TestCase):
    def test_a_matching_bundle_passes(self) -> None:
        rt.verify_bundle(b"bundle", 6, hashlib.sha256(b"bundle").hexdigest())

    def test_a_changed_byte_is_a_sha256_mismatch(self) -> None:
        with self.assertRaisesRegex(rt.Refused, "bundle sha256 mismatch"):
            rt.verify_bundle(b"bundlf", 6, hashlib.sha256(b"bundle").hexdigest())

    def test_a_truncated_or_padded_bundle_is_refused(self) -> None:
        digest = hashlib.sha256(b"bundle").hexdigest()
        for data in (b"bundl", b"bundle!"):
            with self.subTest(data=data), self.assertRaisesRegex(rt.Refused, "not the 6 the train wrote"):
                rt.verify_bundle(data, 6, digest)


class TestGuardedChanges(Repos):
    def commit(self, *paths: str, link: str = "") -> str:
        for path in paths:
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            (self.work / path).write_text("x\n")
        if link:
            os.symlink("pyproject.toml", self.work / link)
        sh(self.work, "add", "-A")
        sh(self.work, "commit", "-qm", "change")
        return sh(self.work, "rev-parse", "HEAD")

    def test_an_ordinary_change_passes(self) -> None:
        self.assertEqual(rt.guarded_changes(self.work / ".git", self.base, sh(self.work, "rev-parse", "HEAD")), [])

    def test_guarded_paths_symlinks_and_submodules_are_found(self) -> None:
        tip = self.commit(".github/workflows/release.yml", ".gitmodules", link="link")
        sh(self.work, "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendored")
        sh(self.work, "commit", "-qm", "submodule")
        tip = sh(self.work, "rev-parse", "HEAD")
        self.assertEqual(sorted(rt.guarded_changes(self.work / ".git", self.base, tip)), [
            ".github/workflows/release.yml: guarded path", ".gitmodules: guarded path", "link: symlink",
            "vendored: submodule"])

    def test_editing_an_existing_workflow_is_found(self) -> None:
        (self.work / ".github" / "ci.yml").write_text("on: pull_request_target\n")
        sh(self.work, "commit", "-qam", "ci")
        self.assertEqual(rt.guarded_changes(self.work / ".git", self.base, sh(self.work, "rev-parse", "HEAD")),
                         [".github/ci.yml: guarded path"])


class TestPush(Repos):
    def test_a_verified_bundle_is_pushed_to_its_bot_branch(self) -> None:
        action = self.bundle()
        out = rt.do_push(action, self.ctx)
        self.assertEqual(out, {"branch": BRANCH, "sha": action["tip"], "pushed": True})
        self.assertEqual(self.remote_branch(), action["tip"])
        # Again: the branch already holds the tip, nothing is pushed.
        self.assertFalse(rt.do_push(action, self.ctx)["pushed"])

    def test_a_tampered_bundle_is_refused_before_anything_is_pushed(self) -> None:
        action = self.bundle()
        self.downloads[URL] = self.downloads[URL][:-1] + b"\0"
        with self.assertRaisesRegex(rt.Refused, "bundle sha256 mismatch"):
            rt.do_push(action, self.ctx)
        self.assertEqual(self.remote_branch(), "")

    def test_a_bundle_whose_tip_is_not_the_trains_is_refused(self) -> None:
        action = self.bundle()
        with self.assertRaisesRegex(rt.Refused, "not the train's tip"):
            rt.do_push({**action, "tip": "f" * 40}, self.ctx)
        self.assertEqual(self.remote_branch(), "")

    def test_a_branch_touching_a_workflow_is_refused(self) -> None:
        (self.work / ".github" / "ci.yml").write_text("on: pull_request_target\n")
        sh(self.work, "commit", "-qam", "ci")
        with self.assertRaisesRegex(rt.Refused, ".github/ci.yml: guarded path"):
            rt.do_push(self.bundle(), self.ctx)
        self.assertEqual(self.remote_branch(), "")

    def test_a_base_that_is_not_on_the_default_branch_is_refused(self) -> None:
        first = sh(self.work, "rev-parse", BRANCH)
        (self.work / "more.txt").write_text("more\n")
        sh(self.work, "add", "more.txt")
        sh(self.work, "commit", "-qm", "fix: more")
        # The claimed base is a commit of the bot branch itself, so the guard would only see the last commit.
        with self.assertRaisesRegex(rt.Refused, "not below both the tip and rt-ci"):
            rt.do_push({**self.bundle(), "base_sha": first}, self.ctx)
        self.assertEqual(self.remote_branch(), "")

    def test_a_bundle_needing_commits_the_default_branch_lacks_is_refused(self) -> None:
        action = self.bundle()
        empty = self.tmp / "empty.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "rt-ci", str(empty)], check=True, env=ENV)
        sh(self.work, "switch", "-q", "--orphan", "other")
        (self.work / "x").write_text("x\n")
        sh(self.work, "add", "x")
        sh(self.work, "commit", "-qm", "unrelated")
        sh(self.work, "push", "-q", str(empty), "HEAD:rt-ci")
        self.ctx.remote_url = str(empty)
        with self.assertRaisesRegex(rt.Refused, "git bundle failed"):
            rt.do_push(action, self.ctx)

    def test_a_branch_moved_by_someone_else_is_replaced_only_through_the_lease(self) -> None:
        action = self.bundle()
        rt.do_push(action, self.ctx)
        # An earlier train's push sits there now: the lease names it, so the new tip replaces it.
        (self.work / "more.txt").write_text("more\n")
        sh(self.work, "add", "more.txt")
        sh(self.work, "commit", "-qm", "fix: more")
        second = self.bundle()
        self.assertTrue(rt.do_push(second, self.ctx)["pushed"])
        self.assertEqual(self.remote_branch(), second["tip"])


class TestPullRequests(Repos):
    def test_open_then_update_and_label(self) -> None:
        self.ctx.pushed[BRANCH] = "a" * 40
        action = {"kind": "open_or_update_pr", "id": "t|s|pr|0123456789abcdef|1", "branch": BRANCH, "base": "rt-ci",
                  "title": "feat!: Require openjd-model 0.15", "body": "b", "labels": ["agent-modified"]}
        out = rt.do_open_or_update_pr(action, self.ctx)
        self.assertEqual((out["number"], out["state"], out["base"], out["head_sha"], out["labels"]),
                         (20, "open", "rt-ci", "a" * 40, ["agent-modified"]))
        self.assertIn("agent-modified", self.api.labels)
        created = [c for c in self.api.calls if c[:2] == ("POST", f"/repos/{REPO}/pulls")]
        self.assertEqual(created[0][2], {"title": action["title"], "body": "b", "head": BRANCH, "base": "rt-ci"})
        rt.do_open_or_update_pr({**action, "body": "b2", "labels": []}, self.ctx)
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(self.api.prs[20]["body"], "b2")

    def test_a_missing_needs_human_label_is_created_and_added(self) -> None:
        self.ctx.pushed[BRANCH] = "a" * 40
        action = {"kind": "open_or_update_pr", "id": "t|s|pr|0123456789abcdef|1", "branch": BRANCH, "base": "rt-ci",
                  "title": "feat!: Require openjd-model 0.15", "body": "b", "labels": ["needs-human"]}
        out = rt.do_open_or_update_pr(action, self.ctx)
        self.assertEqual(out["labels"], ["needs-human"])
        created = [c[2] for c in self.api.calls if c[:2] == ("POST", f"/repos/{REPO}/labels")]
        self.assertEqual([c["name"] for c in created], ["needs-human"])
        self.assertRegex(created[0]["color"], "^[0-9a-f]{6}$")

    def test_an_open_pr_into_another_base_is_refused(self) -> None:
        self.api.pr(7, base={"ref": "mainline"})
        with self.assertRaisesRegex(rt.Refused, "targets mainline, not rt-ci"):
            rt.do_open_or_update_pr({"kind": "open_or_update_pr", "id": "x", "branch": BRANCH, "base": "rt-ci",
                                     "title": "t", "body": "b", "labels": []}, self.ctx)

    def test_pr_status_reports_a_merge(self) -> None:
        self.api.pr(20, state="closed", merged=True, merge_commit_sha="c" * 40, labels=[{"name": "agent-modified"}])
        out = rt.do_pr_status({"kind": "pr_status", "id": "x", "number": 20, "branch": BRANCH, "tip": "a" * 40},
                              self.ctx)
        self.assertEqual(out, {"number": 20, "url": f"https://github.com/{REPO}/pull/20", "state": "merged",
                               "merged": True, "merge_sha": "c" * 40, "head_sha": "a" * 40, "base": "rt-ci",
                               "labels": ["agent-modified"]})

    def test_pr_status_refuses_a_pr_that_is_not_a_bot_pr_of_this_repo(self) -> None:
        self.api.pr(5, branch="feature/x")
        with self.assertRaisesRegex(rt.Refused, "not a bot/\\* PR"):
            rt.do_pr_status({"kind": "pr_status", "id": "x", "number": 5, "branch": BRANCH, "tip": "a" * 40}, self.ctx)
        self.api.pr(6, head="a" * 40)
        self.api.prs[6]["head"]["repo"] = {"full_name": "someone/fork"}
        with self.assertRaisesRegex(rt.Refused, "not a bot/\\* PR"):
            rt.do_pr_status({"kind": "pr_status", "id": "x", "number": 6, "branch": BRANCH, "tip": "a" * 40}, self.ctx)


class TestTags(Repos):
    def action(self, sha: str) -> dict[str, Any]:
        return {"kind": "create_tag_and_prerelease", "id": "t|s|publish|0123456789abcdef|0", "tag": "0.15.0",
                "sha": sha, "title": f"0.15.0 {rt.REHEARSAL_SUFFIX}", "notes": "notes"}

    def test_a_tag_and_prerelease_on_the_default_branch(self) -> None:
        out = rt.do_create_tag_and_prerelease(self.action(self.base), self.ctx)
        self.assertEqual(out, {"tag": "0.15.0", "sha": self.base,
                               "release_url": f"https://github.com/{REPO}/releases/tag/0.15.0"})
        tag_call = next(c for c in self.api.calls if c[1] == f"/repos/{REPO}/git/tags")
        self.assertEqual(tag_call[2]["object"], self.base)
        self.assertTrue(self.api.releases["0.15.0"]["prerelease"])
        # Again: the tag (annotated, at this commit) and the release are already there.
        self.api.tag_target = self.base
        self.assertEqual(rt.do_create_tag_and_prerelease(self.action(self.base), self.ctx)["sha"], self.base)
        self.assertEqual(len([c for c in self.api.calls if c[1] == f"/repos/{REPO}/releases"]), 1)

    def test_a_commit_off_the_default_branch_is_not_tagged(self) -> None:
        tip = sh(self.work, "rev-parse", BRANCH)
        self.bundle()
        rt.do_push(self.bundle(), self.ctx)  # the commit exists on GitHub, but only on the bot branch
        with self.assertRaisesRegex(rt.Refused, "not reachable from rt-ci"):
            rt.do_create_tag_and_prerelease(self.action(tip), self.ctx)
        self.assertNotIn("0.15.0", self.api.releases)

    def test_a_tag_at_another_commit_is_refused(self) -> None:
        self.api.refs["/git/ref/tags/0.15.0"] = {"object": {"type": "commit", "sha": "f" * 40}}
        with self.assertRaisesRegex(rt.Refused, "already names ffffffffffff"):
            rt.do_create_tag_and_prerelease(self.action(self.base), self.ctx)


class TestExecute(Repos):
    def test_a_failed_push_skips_the_rest_of_its_request_only(self) -> None:
        push = {**self.bundle(), "bundle_sha256": "0" * 64}
        pr = {"kind": "open_or_update_pr", "id": "t|sessions|pr|0123456789abcdef|1", "branch": BRANCH,
              "base": "rt-ci", "title": "t", "body": "b", "labels": []}
        other = {"kind": "comment", "id": "u|cli|pr|0123456789abcdef|0", "number": 7, "body": "hello"}
        self.api.pr(7)
        results = rt.execute([push, pr, other], self.ctx)
        self.assertEqual([(r["id"], r["ok"]) for r in results], [(push["id"], False), (other["id"], True)])
        self.assertIn("bundle sha256 mismatch", results[0]["error"])
        self.assertEqual(list(self.api.prs), [7])

    def test_an_invalid_action_is_reported_not_raised(self) -> None:
        results = rt.execute([{"kind": "merge", "id": "t|s|pr|0123456789abcdef|0"}], self.ctx)
        self.assertEqual(results[0]["ok"], False)
        self.assertIn("unknown action kind", results[0]["error"])



class TestReleasePrNotes(Repos):
    """Live mode's release-notes review of the repo's own bump/X.Y.Z release PR."""

    NOTES_URL = "https://example-bucket.s3.us-west-2.amazonaws.com/trains/t/handoff/sessions/notes_review/x.bundle?s=1"

    def setUp(self) -> None:
        super().setUp()
        # The repo's Release: Bump workflow: bump/0.15.1 with a chore(release) commit, pushed and opened as a PR.
        sh(self.work, "switch", "-q", "rt-ci")
        sh(self.work, "switch", "-qc", "bump/0.15.1")
        (self.work / "CHANGELOG.md").write_text("## 0.15.1\n\n* generated\n")
        sh(self.work, "add", "CHANGELOG.md")
        sh(self.work, "commit", "-qm", "chore(release): 0.15.1")
        self.head = sh(self.work, "rev-parse", "HEAD")
        sh(self.work, "push", "-q", str(self.remote), "bump/0.15.1")
        self.api.pr(31, "bump/0.15.1", self.head, title="chore(release): 0.15.1")

    def notes(self, *files: str) -> dict[str, Any]:
        sh(self.work, "switch", "-q", "-C", "rt-notes/0.15.1", self.head)
        for name in files or ("CHANGELOG.md",):
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text("## 0.15.1\n\n* reviewed\n")
            sh(self.work, "add", name)
        sh(self.work, "commit", "-qm", "chore(release): review the 0.15.1 release notes")
        tip = sh(self.work, "rev-parse", "HEAD")
        out = self.tmp / "notes.bundle"
        sh(self.work, "bundle", "create", "-q", str(out), "refs/heads/rt-notes/0.15.1", f"^{self.head}")
        data = out.read_bytes()
        self.downloads[self.NOTES_URL] = data
        return {"kind": "push_release_notes", "id": "t|sessions|notes_review|0123456789abcdef|0",
                "branch": "bump/0.15.1", "number": 31, "expected_head": self.head, "tip": tip,
                "bundle_branch": "rt-notes/0.15.1", "bundle_url": self.NOTES_URL,
                "bundle_sha256": hashlib.sha256(data).hexdigest(), "bundle_bytes": len(data)}

    def remote_bump(self) -> str:
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/bump/0.15.1"],
                              capture_output=True, text=True, env=ENV).stdout.strip()

    def test_find_lists_only_open_release_prs_of_this_repo(self) -> None:
        self.api.pr(32, "bump/0.15.2", "b" * 40, title="chore: something else")
        self.api.pr(33, BRANCH, "c" * 40, title="chore(release): 0.15.3")
        self.api.pr(34, "bump/0.15.4", "d" * 40, title="chore(release): 0.15.4")
        self.api.prs[34]["head"]["repo"] = {"full_name": "someone/fork"}
        original = self.api.__call__

        def api(method: str, path: str, body: Any = None) -> Any:
            if method == "GET" and "/pulls?" in path:
                self.assertIn("state=open", path)
                self.assertIn("base=rt-ci", path)
                return list(self.api.prs.values())
            return original(method, path, body)

        self.ctx.api = api
        out = rt.do_find_release_pr({"kind": "find_release_pr", "id": "scan|sessions|0a1b2c3d"}, self.ctx)
        self.assertEqual([p["number"] for p in out["prs"]], [31])
        self.assertEqual(out["prs"][0]["head_sha"], self.head)

    def test_a_reviewed_notes_commit_is_pushed_as_a_fast_forward(self) -> None:
        action = self.notes()
        out = rt.execute([action], self.ctx)
        self.assertEqual(out, [{"id": action["id"], "ok": True, "outputs": {"branch": "bump/0.15.1",
                                                                             "sha": action["tip"], "pushed": True}}])
        self.assertEqual(self.remote_bump(), action["tip"])

    def test_a_notes_commit_changing_another_file_is_refused(self) -> None:
        action = self.notes("CHANGELOG.md", "src/x.py")
        with self.assertRaisesRegex(rt.Refused, "CHANGELOG.md only"):
            rt.do_push_release_notes(action, self.ctx)
        self.assertEqual(self.remote_bump(), self.head)

    def test_a_release_pr_that_moved_after_the_review_is_not_overwritten(self) -> None:
        action = self.notes()
        self.api.prs[31]["head"]["sha"] = "f" * 40
        with self.assertRaisesRegex(rt.Refused, "moved to ffffffffffff after the review"):
            rt.do_push_release_notes(action, self.ctx)
        self.assertEqual(self.remote_bump(), self.head)

    def test_only_a_release_pr_branch_and_rt_notes_bundle_are_admitted(self) -> None:
        action = self.notes()
        for bad, why in (({"branch": BRANCH}, "only a bump/X.Y.Z release PR"),
                         ({"branch": "rt-ci"}, "only a bump/X.Y.Z release PR"),
                         ({"bundle_branch": "bump/0.15.1"}, "rt-notes/X.Y.Z"),
                         ({"bundle_url": "https://example.com/x"}, "https S3 URL")):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(rt.Refused, why):
                    rt.validate({**action, **bad}, self.ctx)

    def test_a_pr_that_is_not_the_release_pr_is_refused(self) -> None:
        action = self.notes()
        self.api.prs[31]["title"] = "feat: sneaky"
        with self.assertRaisesRegex(rt.Refused, "not an open bump/X.Y.Z release PR"):
            rt.do_push_release_notes(action, self.ctx)
        with self.assertRaisesRegex(rt.Refused, "not an open bump/X.Y.Z release PR"):
            rt.do_comment_release_pr({"kind": "comment_release_pr", "id": "x", "branch": "bump/0.15.1", "number": 31,
                                      "body": "t"}, self.ctx)


if __name__ == "__main__":
    unittest_main()


class TestDispatchReleaseBump(TestCase):
    def test_live_mode_dispatches_the_repos_own_release_bump_with_the_job_token(self) -> None:
        api = FakeApi()
        ctx = rt.Context(repo=REPO, default_branch="mainline", token="", mode="live", workdir=Path("/nonexistent"),
                         api=api, download=lambda url, limit: b"")
        action = {"kind": "dispatch_workflow", "id": "t|sessions|release_pr|0123456789abcdef|0",
                  "workflow": "release_bump.yml", "ref": "mainline", "inputs": {}}
        (result,) = rt.execute([action], ctx)
        self.assertEqual(result, {"id": action["id"], "ok": True, "outputs": {
            "workflow": "release_bump.yml", "ref": "mainline", "dispatched": True}})
        self.assertEqual(api.calls, [("POST", f"/repos/{REPO}/actions/workflows/release_bump.yml/dispatches",
                                      {"ref": "mainline", "inputs": {}})])


class FakeGateway:
    """The gateway's answers, phase by phase, and what the script sent it."""

    def __init__(self, plans: list[dict[str, Any]], reply: dict[str, Any] | None = None) -> None:
        self.plans = plans
        self.reply = reply or {}
        self.calls: list[dict[str, Any]] = []
        self.failures = 0

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if payload["phase"] in ("report", "dispatched"):
            reply = self.reply(payload) if callable(self.reply) else self.reply
            return {"approved": [], "failed": [], "ignored": [], "advanced": [], "at": "2026-11-05T10:00:00+00:00",
                    **reply}
        if self.failures:
            self.failures -= 1
            raise rt.GatewayError("the gateway refused: throttled")
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    def phases(self) -> list[str]:
        return [c["phase"] for c in self.calls]


def plan_of(actions: list[dict[str, Any]] | None = None, busy: bool = False, **fields: Any) -> dict[str, Any]:
    return {"mode": "rehearsal", "node": "sessions", "actions": actions or [], "started": [], "deferred": {},
            "notes": [], "wait": {"busy": busy, "trains": ["t"] if busy else [], "reason": "t: running" if busy else ""},
            "dispatch": {"allowed": [], "targets": []}, "notes_live": False, **fields}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestWaiting(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.api = FakeApi()
        self.api.pr(7)
        self.ctx = rt.Context(repo=REPO, default_branch="rt-ci", token="", mode="rehearsal", workdir=self.tmp,
                              api=self.api, download=lambda url, limit: b"")
        self.clock = Clock()
        self.dispatch_file = self.tmp / "dispatch.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def converse(self, gateway: FakeGateway, wait_s: float = 600.0) -> rt.Conversation:
        return rt.converse(self.ctx, gateway, wait_s=wait_s, poll_s=20.0, now=self.clock.time, sleep=self.clock.sleep,
                           dispatch_file=self.dispatch_file)

    def comment(self, n: int = 0) -> dict[str, Any]:
        return {"kind": "comment", "id": f"t|sessions|pr|0123456789abcdef|{n}", "number": 7, "body": "b"}

    def test_a_run_with_no_busy_train_plans_acts_reports_and_ends(self) -> None:
        gateway = FakeGateway([plan_of([self.comment()])])
        out = self.converse(gateway)
        self.assertEqual(gateway.phases(), ["plan", "report"])
        self.assertEqual(self.clock.slept, [])
        self.assertEqual([r["ok"] for r in out.results], [True])

    def test_a_busy_train_keeps_the_run_until_its_next_handoff_then_polls_once_more(self) -> None:
        gateway = FakeGateway([plan_of([self.comment()], busy=True), plan_of(busy=True), plan_of(busy=True),
                               plan_of([self.comment(1)]), plan_of()], reply={"advanced": ["t"]})
        out = self.converse(gateway)
        # The last report advanced the train again, so the run asks once more before its final poll.
        self.assertEqual(gateway.phases(), ["plan", "report", "wait", "wait", "wait", "report", "wait", "plan"])
        waits = [c for c in gateway.calls if c["phase"] == "wait"]
        self.assertEqual({tuple(c["advanced"]) for c in waits}, {("t",)})
        self.assertEqual({c["after"] for c in waits}, {"2026-11-05T10:00:00+00:00"})
        self.assertEqual(self.clock.slept, [20.0, 20.0, 20.0, 20.0])
        self.assertEqual(len(out.results), 2)
        self.assertEqual(out.ended, "nothing to wait for")

    def test_a_report_that_advanced_a_train_is_followed_by_a_wait_not_an_exit(self) -> None:
        # The plan said "not busy" before the report; the train it advanced is asked about again.
        gateway = FakeGateway([plan_of([self.comment()]), plan_of()], reply={"advanced": ["t"]})
        self.converse(gateway)
        self.assertEqual(gateway.phases(), ["plan", "report", "wait", "plan"])
        self.assertEqual((gateway.calls[2]["after"], gateway.calls[2]["advanced"]), ("2026-11-05T10:00:00+00:00", ["t"]))
        self.assertEqual(gateway.calls[3]["advanced"], ["t"])

    def test_a_report_that_moved_nothing_does_not_move_the_answered_gate_time(self) -> None:
        # A pr_status read while the run waits: the gate it watches is a person's, not one this run answered.
        times = iter(["2026-11-05T10:00:00+00:00", "2026-11-05T10:05:00+00:00"])
        gateway = FakeGateway([plan_of([self.comment()]), plan_of([self.comment(1)], busy=True), plan_of()],
                              reply=lambda p: {"advanced": ["t"] if p["results"][0]["id"].endswith("|0") else [],
                                               "at": next(times)})
        self.converse(gateway)
        waits = [c for c in gateway.calls if c["phase"] == "wait"]
        self.assertEqual({c["after"] for c in waits}, {"2026-11-05T10:00:00+00:00"})

    def test_the_wait_ends_at_its_limit(self) -> None:
        gateway = FakeGateway([plan_of(busy=True)])
        out = self.converse(gateway, wait_s=100.0)
        self.assertEqual(gateway.phases(), ["plan", "wait", "wait", "wait", "wait", "wait"])
        self.assertLessEqual(self.clock.now - 1000.0, 100.0)
        self.assertIn("limit", out.ended)

    def test_a_train_the_plan_started_is_waited_for(self) -> None:
        gateway = FakeGateway([plan_of(started=["rt-gha-sessions-x"], busy=True), plan_of()])
        self.converse(gateway)
        self.assertEqual(gateway.calls[1]["advanced"], ["rt-gha-sessions-x"])

    def test_a_failing_gateway_is_retried_then_the_wait_gives_up(self) -> None:
        gateway = FakeGateway([plan_of(busy=True), plan_of()])
        gateway.failures = 0
        first = gateway.plans[0]
        gateway.plans = [first, first]
        calls = 0

        def flaky(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if payload["phase"] == "wait":
                raise rt.GatewayError("throttled")
            return gateway(payload)

        out = rt.converse(self.ctx, flaky, wait_s=600.0, poll_s=20.0, now=self.clock.time, sleep=self.clock.sleep,
                          dispatch_file=self.dispatch_file)
        self.assertEqual(calls, 1 + rt.MAX_GATEWAY_FAILURES)
        self.assertIn("throttled", out.ended)
        self.assertTrue(out.failed)

    def test_fanout_targets_are_collected_for_the_dispatch_step(self) -> None:
        target = {"key": "DISPATCH#release#example-owner/cli#openjd-sessions#0.15.2", "repo": "example-owner/cli",
                  "workflow": "release_train.yml", "ref": "rt-ci", "reason": "openjd-sessions 0.15.2 released"}
        gateway = FakeGateway([plan_of(busy=True), plan_of(dispatch={"allowed": ["example-owner/cli"],
                                                                     "targets": [target]}), plan_of()])
        self.converse(gateway)
        doc = rt.json.loads(self.dispatch_file.read_text())
        self.assertEqual(doc, {"allowed": ["example-owner/cli"], "targets": [target]})

    def test_after_dispatching_release_bump_the_run_waits_for_its_release_pr(self) -> None:
        self.ctx.mode = "live"
        self.ctx.default_branch = "mainline"
        bump = {"kind": "dispatch_workflow", "id": "t|sessions|release_pr|0123456789abcdef|0",
                "workflow": "release_bump.yml", "ref": "mainline", "inputs": {}}
        scan = {"kind": "find_release_pr", "id": "scan|sessions|0123abcd"}
        found = {"reviews": ["rt-gha-notes-sessions-0-16-0-aaaaaaaaaaaa"]}
        gateway = FakeGateway([plan_of([bump, scan], notes_live=True, mode="live"),
                               plan_of([scan], notes_live=True, mode="live"),
                               plan_of([scan], notes_live=True, mode="live"),
                               plan_of(notes_live=True, mode="live")],
                              reply=lambda p: found if len([c for c in gateway.calls if c["phase"] == "report"]) >= 3
                              else {"advanced": ["t"]})
        self.api.calls.clear()
        out = self.converse(gateway)
        self.assertEqual(gateway.phases(), ["plan", "report", "wait", "report", "wait", "report", "plan"])
        self.assertTrue(out.release_pr_seen)

    def test_the_wait_for_a_release_pr_is_bounded(self) -> None:
        self.ctx.mode = "live"
        self.ctx.default_branch = "mainline"
        bump = {"kind": "dispatch_workflow", "id": "t|sessions|release_pr|0123456789abcdef|0",
                "workflow": "release_bump.yml", "ref": "mainline", "inputs": {}}
        gateway = FakeGateway([plan_of([bump], notes_live=True, mode="live"), plan_of(notes_live=True, mode="live")])
        out = self.converse(gateway, wait_s=3600.0)
        self.assertFalse(out.release_pr_seen)
        self.assertLessEqual(self.clock.now - 1000.0, rt.RELEASE_PR_WAIT_S + 20.0)

    def test_every_planning_request_carries_the_schedule_and_the_weekly_variable(self) -> None:
        self.ctx.schedule = "17 15 * * 1"
        self.ctx.weekly_enabled = "false"
        gateway = FakeGateway([plan_of([self.comment()]), plan_of()], reply={"advanced": ["t"]})
        self.converse(gateway)
        weekly = {"schedule": "17 15 * * 1", "weekly_enabled": "false"}
        self.assertEqual(gateway.calls[0], {"phase": "plan", "mode": "rehearsal", **weekly})
        self.assertEqual(gateway.calls[2], {"phase": "wait", "mode": "rehearsal", "after": "2026-11-05T10:00:00+00:00",
                                            "advanced": ["t"], **weekly})
        self.assertEqual(gateway.calls[3], {"phase": "plan", "mode": "rehearsal", "after": "2026-11-05T10:00:00+00:00",
                                            "advanced": ["t"], **weekly})

    def test_a_run_without_a_schedule_sends_empty_values(self) -> None:
        gateway = FakeGateway([plan_of()])
        self.converse(gateway)
        self.assertEqual(gateway.calls, [{"phase": "plan", "mode": "rehearsal", "schedule": "", "weekly_enabled": ""}])

    def test_a_report_carries_only_its_results(self) -> None:
        self.ctx.schedule = "17 15 * * 1"
        self.ctx.weekly_enabled = "true"
        gateway = FakeGateway([plan_of([self.comment()])])
        self.converse(gateway)
        self.assertEqual(gateway.phases(), ["plan", "report"])
        self.assertEqual(set(gateway.calls[1]), {"phase", "results"})


class TestIdToken(TestCase):
    """The OIDC ID token for the gateway: a transient failure of the runner's token service is retried."""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example/x?api-version=2",
                                           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-t0ken"})
        env.start()
        self.addCleanup(env.stop)
        self.sleeps: list[float] = []

    def urlopen(self, outcomes: list[Any]) -> Any:
        self.calls: list[str] = []

        def fake(req: Any, timeout: float) -> Any:
            self.calls.append(req.full_url)
            outcome: Any = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return contextlib.nullcontext(io.BytesIO(json.dumps({"value": outcome}).encode()))

        return mock.patch.object(rt.urllib.request, "urlopen", fake)

    def http_error(self, code: int) -> Exception:
        return rt.urllib.error.HTTPError("https://token.example/x", code, "boom", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    def test_a_server_error_from_the_token_service_is_retried(self) -> None:
        with self.urlopen([self.http_error(503), rt.urllib.error.URLError("reset"), "id-t0ken"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rt.id_token("rt-train-gateway", sleep=self.sleeps.append), "id-t0ken")
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(c.endswith("&audience=rt-train-gateway") for c in self.calls))
        self.assertEqual(self.sleeps, [2.0, 4.0])

    def test_a_token_service_that_keeps_failing_is_a_gateway_error_not_a_crash(self) -> None:
        with self.urlopen([self.http_error(503)] * rt.ID_TOKEN_ATTEMPTS):
            with self.assertRaisesRegex(rt.GatewayError, "no OIDC ID token after 3 attempts: HTTP 503"):
                rt.id_token("rt-train-gateway", sleep=self.sleeps.append)
        self.assertEqual(len(self.calls), rt.ID_TOKEN_ATTEMPTS)

    def test_a_refused_token_request_is_not_retried(self) -> None:
        with self.urlopen([self.http_error(403)]):
            with self.assertRaisesRegex(rt.GatewayError, "no OIDC ID token: HTTP 403"):
                rt.id_token("rt-train-gateway", sleep=self.sleeps.append)
        self.assertEqual((len(self.calls), self.sleeps), (1, []))

    def test_a_failed_token_while_waiting_counts_as_a_failed_gateway_call(self) -> None:
        # The run's wait loop retries a GatewayError and ends cleanly after MAX_GATEWAY_FAILURES; before, the
        # token service's 503 escaped it as a traceback and failed the job.
        outcomes: list[Any] = [self.http_error(503)] * (rt.ID_TOKEN_ATTEMPTS * rt.MAX_GATEWAY_FAILURES)
        with self.urlopen(outcomes):
            plans: list[dict[str, Any]] = [{"actions": [], "wait": {"busy": True}, "dispatch": {}}]

            def call(payload: dict[str, Any]) -> dict[str, Any]:
                if payload["phase"] == "plan":
                    return plans[0]
                rt.id_token("rt-train-gateway", sleep=self.sleeps.append)
                raise AssertionError("unreachable: the token never arrives")

            ctx: Any = rt.Context(repo=REPO, default_branch="mainline", token="t", mode="rehearsal",
                                  workdir=Path(tempfile.mkdtemp()), api=lambda *a, **k: None, download=lambda u, n: b"")
            clock: list[float] = [0.0]

            def tick(s: float) -> None:
                clock[0] += s

            conv: Any = rt.converse(ctx, call, wait_s=3600.0, now=lambda: clock[0], sleep=tick, poll_s=1.0)
        self.assertTrue(conv.failed)
        self.assertIn("the gateway failed 3 times while waiting: no OIDC ID token after 3 attempts", conv.ended)


class DispatchApi:
    """GitHub as the dispatch step sees it, with the dispatch token."""

    def __init__(self, default_branch: str = "rt-ci", status: int = 204) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.default_branch = default_branch
        self.statuses = [status]

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET":
            return {"default_branch": self.default_branch}
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != 204:
            raise rt.ApiError(status, "nope")
        return None


TARGET = {"key": "DISPATCH#release#OpenJobDescription/openjd-cli#openjd-sessions#0.15.2",
          "repo": "OpenJobDescription/openjd-cli", "workflow": "release_train.yml", "ref": "rt-ci",
          "reason": "openjd-sessions 0.15.2 released"}
WA_TARGET = {"key": "DISPATCH#release#aws-deadline/deadline-cloud-worker-agent#openjd-sessions#0.15.2",
             "repo": "aws-deadline/deadline-cloud-worker-agent", "workflow": "release_train.yml", "ref": "rt-ci",
             "reason": "openjd-sessions 0.15.2 released"}
CLI = "OpenJobDescription/openjd-cli"
WA = "aws-deadline/deadline-cloud-worker-agent"
OPENJD_TOKEN = "openjd-t0ken-value"
AWS_DEADLINE_TOKEN = "aws-deadline-t0ken-value"
BOTH_TOKENS = {"openjobdescription": OPENJD_TOKEN, "aws-deadline": AWS_DEADLINE_TOKEN}


class TestDispatchStep(TestCase):
    def dispatch(self, tokens: dict[str, str] | None = None, repos: str = CLI, allowed: list[str] | None = None,
                 targets: list[dict[str, Any]] | None = None, api: DispatchApi | None = None) -> list[dict[str, Any]]:
        self.api = api or DispatchApi()
        self.tokens_used: list[str] = []
        doc = {"allowed": [CLI] if allowed is None else allowed, "targets": [TARGET] if targets is None else targets}

        def api_for(token: str) -> DispatchApi:
            self.tokens_used.append(token)
            return self.api

        return rt.dispatch(doc, BOTH_TOKENS if tokens is None else tokens, rt.parse_repos(repos), api_for,
                           sleep=lambda s: None)

    def test_an_allowed_target_is_dispatched_on_its_default_branch_only(self) -> None:
        (result,) = self.dispatch()
        self.assertEqual(result, {"key": TARGET["key"], "repo": CLI, "ok": True, "status": 204,
                                  "outcome": "dispatched", "error": ""})
        self.assertEqual(self.api.calls, [("GET", f"/repos/{CLI}", None), (
            "POST", f"/repos/{CLI}/actions/workflows/release_train.yml/dispatches", {"ref": "rt-ci"})])

    def test_a_rehearsal_claim_naming_its_train_is_dispatched(self) -> None:
        (result,) = self.dispatch(targets=[{**TARGET, "key": TARGET["key"] + "#rt-gha-sessions-release-only-1"}])
        self.assertTrue(result["ok"])

    def test_no_token_skips_dispatch_without_calling_github(self) -> None:
        (result,) = self.dispatch(tokens={})
        self.assertEqual((result["ok"], result["outcome"]), (False, "no-token"))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.tokens_used, [])

    def test_each_target_gets_the_token_of_its_own_org(self) -> None:
        cli, wa = self.dispatch(targets=[TARGET, WA_TARGET], repos=f"{CLI} {WA}", allowed=[CLI, WA])
        self.assertEqual((cli["outcome"], wa["outcome"]), ("dispatched", "dispatched"))
        self.assertEqual(self.tokens_used, [OPENJD_TOKEN, AWS_DEADLINE_TOKEN])

    def test_the_owner_matches_its_org_case_insensitively(self) -> None:
        for repo, token in (("openjobdescription/openjd-cli", OPENJD_TOKEN), ("AWS-Deadline/deadline-cloud-worker-agent",
                                                                             AWS_DEADLINE_TOKEN)):
            with self.subTest(repo=repo):
                target = {**TARGET, "repo": repo, "key": f"DISPATCH#release#{repo}#openjd-sessions#0.15.2"}
                (result,) = self.dispatch(targets=[target], repos=repo, allowed=[repo])
                self.assertEqual(result["outcome"], "dispatched")
                self.assertEqual(self.tokens_used, [token])

    def test_a_missing_org_token_skips_only_that_orgs_targets(self) -> None:
        for tokens, skipped, dispatched, used in (
                ({"aws-deadline": AWS_DEADLINE_TOKEN}, CLI, WA, AWS_DEADLINE_TOKEN),
                ({"openjobdescription": OPENJD_TOKEN}, WA, CLI, OPENJD_TOKEN)):
            with self.subTest(tokens=sorted(tokens)):
                results = self.dispatch(tokens=tokens, targets=[TARGET, WA_TARGET], repos=f"{CLI} {WA}",
                                        allowed=[CLI, WA])
                by_repo = {r["repo"]: r for r in results}
                self.assertEqual((by_repo[skipped]["ok"], by_repo[skipped]["outcome"]), (False, "no-token"))
                self.assertEqual((by_repo[dispatched]["ok"], by_repo[dispatched]["outcome"]), (True, "dispatched"))
                # Never the other org's token for the skipped target.
                self.assertEqual(self.tokens_used, [used])
                self.assertEqual([c[1] for c in self.api.calls if c[0] == "POST"],
                                 [f"/repos/{dispatched}/actions/workflows/release_train.yml/dispatches"])

    def test_an_unknown_owner_is_refused_without_calling_github(self) -> None:
        for repo in ("example-owner/cli", "seant-aws/openjd-cli", "OpenJobDescription-x/openjd-cli"):
            with self.subTest(repo=repo):
                target = {**TARGET, "repo": repo, "key": f"DISPATCH#release#{repo}#openjd-sessions#0.15.2"}
                (result,) = self.dispatch(targets=[target], repos=repo, allowed=[repo])
                self.assertEqual((result["ok"], result["outcome"]), (False, "refused"))
                self.assertIn("neither OpenJobDescription nor aws-deadline", result["error"])
                self.assertEqual(self.api.calls, [])
                self.assertEqual(self.tokens_used, [])

    def test_a_repo_missing_from_either_list_is_refused_without_calling_github(self) -> None:
        for repos, allowed in (("OpenJobDescription/other", [CLI]), (CLI, []), ("", [CLI])):
            with self.subTest(repos=repos, allowed=allowed):
                (result,) = self.dispatch(repos=repos, allowed=allowed)
                self.assertEqual((result["ok"], result["outcome"]), (False, "refused"))
                self.assertEqual(self.api.calls, [])

    def test_another_workflow_ref_or_malformed_target_is_refused(self) -> None:
        for bad in ({"workflow": "release_bump.yml"}, {"workflow": "../release_train.yml"}, {"ref": "x y"},
                    {"repo": "OpenJobDescription/cli/../../x"}, {"key": "DISPATCH#handoff#x"}):
            with self.subTest(bad=bad):
                (result,) = self.dispatch(targets=[{**TARGET, **bad}], repos=f"{CLI},{bad.get('repo', '')}",
                                          allowed=[CLI, str(bad.get("repo", ""))])
                self.assertEqual((result["ok"], result["outcome"]), (False, "refused"))
                self.assertEqual(self.api.calls, [])

    def test_a_ref_that_is_not_the_targets_default_branch_is_refused(self) -> None:
        (result,) = self.dispatch(api=DispatchApi(default_branch="mainline"))
        self.assertEqual((result["ok"], result["outcome"]), (False, "refused"))
        self.assertEqual([c[0] for c in self.api.calls], ["GET"])

    def test_a_server_error_is_retried_and_a_rejected_token_is_not(self) -> None:
        api = DispatchApi()
        api.statuses = [502, 502, 204]
        (result,) = self.dispatch(api=api)
        self.assertTrue(result["ok"])
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 3)
        (result,) = self.dispatch(api=DispatchApi(status=401))
        self.assertEqual((result["ok"], result["status"], result["outcome"]), (False, 401, "failed"))
        self.assertEqual(len([c for c in self.api.calls if c[0] == "POST"]), 1)

    def test_the_token_never_appears_in_a_result(self) -> None:
        for api in (DispatchApi(), DispatchApi(status=403), DispatchApi(status=500)):
            results = self.dispatch(api=api, targets=[TARGET, WA_TARGET], repos=f"{CLI} {WA}", allowed=[CLI, WA])
            self.assertNotIn(OPENJD_TOKEN, rt.json.dumps(results))
            self.assertNotIn(AWS_DEADLINE_TOKEN, rt.json.dumps(results))

    def test_the_callers_list_parses_commas_spaces_and_lines(self) -> None:
        self.assertEqual(rt.parse_repos(" a/b,c/d\n e/f  "), frozenset({"a/b", "c/d", "e/f"}))
        self.assertEqual(rt.parse_repos(""), frozenset())


class TestRunDispatch(TestCase):
    """The dispatch step as the workflow runs it: tokens from the environment, output to the log and summary."""

    def run_step(self, env: dict[str, str], status: int = 204) -> tuple[str, str, list[dict[str, Any]], list[str]]:
        api = DispatchApi(status=status)
        tokens_used: list[str] = []
        sent: list[dict[str, Any]] = []

        def github_api(token: str, base: str = "") -> DispatchApi:
            tokens_used.append(token)
            return api

        with tempfile.TemporaryDirectory() as tmp:
            dispatch_file = Path(tmp) / "dispatch.json"
            dispatch_file.write_text(json.dumps({"allowed": [CLI, WA], "targets": [TARGET, WA_TARGET]}))
            summary = Path(tmp) / "summary.md"
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"RT_DISPATCH_FILE": str(dispatch_file), "RT_DISPATCH_REPOS": f"{CLI} {WA}",
                                              "RT_GATEWAY_FUNCTION": "fn", "RT_GATEWAY_AUDIENCE": "aud",
                                              "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp, **env}), \
                    mock.patch.object(rt, "github_api", github_api), \
                    mock.patch.object(rt, "id_token", lambda audience: "id-t0ken"), \
                    mock.patch.object(rt, "invoke", lambda fn, payload, workdir: sent.append(payload) or {}), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(rt.run_dispatch(), 0)
            return out.getvalue(), summary.read_text(), sent, tokens_used

    def test_the_step_reads_one_token_per_org(self) -> None:
        log, _, sent, used = self.run_step({"RT_DISPATCH_TOKEN_OPENJOBDESCRIPTION": OPENJD_TOKEN,
                                            "RT_DISPATCH_TOKEN_AWS_DEADLINE": AWS_DEADLINE_TOKEN})
        self.assertEqual(used, [OPENJD_TOKEN, AWS_DEADLINE_TOKEN])
        self.assertEqual([r["outcome"] for r in sent[0]["results"]], ["dispatched", "dispatched"])
        self.assertNotIn("::notice::", log)

    def test_a_missing_token_is_a_notice_naming_its_org(self) -> None:
        log, summary, sent, used = self.run_step({"RT_DISPATCH_TOKEN_AWS_DEADLINE": AWS_DEADLINE_TOKEN})
        self.assertEqual(used, [AWS_DEADLINE_TOKEN])
        self.assertEqual([r["outcome"] for r in sent[0]["results"]], ["no-token", "dispatched"])
        notices = [line for line in log.splitlines() if line.startswith("::notice::")]
        self.assertEqual(len(notices), 1)
        self.assertIn("OpenJobDescription", notices[0])
        self.assertIn(f"| {CLI} | no-token", summary)

    def test_tokens_never_appear_in_the_log_summary_or_report(self) -> None:
        for status in (204, 401, 403, 500):
            with self.subTest(status=status):
                log, summary, sent, _ = self.run_step({"RT_DISPATCH_TOKEN_OPENJOBDESCRIPTION": OPENJD_TOKEN,
                                                       "RT_DISPATCH_TOKEN_AWS_DEADLINE": AWS_DEADLINE_TOKEN},
                                                      status=status)
                self.assertTrue(log)
                for text in (log, summary, json.dumps(sent)):
                    self.assertNotIn(OPENJD_TOKEN, text)
                    self.assertNotIn(AWS_DEADLINE_TOKEN, text)


PUBLISH_RUN = "https://github.com/example-owner/example-repo/actions/runs/42118768518"


class TestInvoke(TestCase):
    """The gateway is named by its function name, or by its full ARN when it lives in another account."""

    ARN = "arn:aws:lambda:us-east-2:111122223333:function:rt-train-gateway"

    def invoke(self, function: str) -> tuple[dict[str, Any], list[list[str]]]:
        argv: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            argv.append(list(args))
            Path(args[-1]).write_text(json.dumps({"actions": []}))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"StatusCode": 200}), stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rt.subprocess, "run", fake_run):
            reply = rt.invoke(function, {"phase": "plan"}, Path(tmp))
        return reply, argv

    def test_an_arn_is_invoked_as_is_in_its_own_region(self) -> None:
        reply, argv = self.invoke(self.ARN)
        self.assertEqual(reply, {"actions": []})
        (args,) = argv
        self.assertEqual(args[args.index("--function-name") + 1], self.ARN)
        self.assertEqual(args[args.index("--region") + 1], "us-east-2")

    def test_a_qualified_arn_keeps_its_qualifier(self) -> None:
        _, argv = self.invoke(f"{self.ARN}:live")
        (args,) = argv
        self.assertEqual(args[args.index("--function-name") + 1], f"{self.ARN}:live")
        self.assertEqual(args[args.index("--region") + 1], "us-east-2")

    def test_a_function_name_uses_the_configured_region(self) -> None:
        _, argv = self.invoke("rt-train-gateway")
        (args,) = argv
        self.assertEqual(args[args.index("--function-name") + 1], "rt-train-gateway")
        self.assertNotIn("--region", args)

    def test_a_malformed_gateway_is_refused_before_invoking(self) -> None:
        for function in ("", "arn:aws:s3:::example-bucket", "arn:aws:lambda:us-east-2:1234:function:fn",
                         "fn --debug", "arn:aws:lambda:us-east-2:111122223333:function:", "x" * 65):
            with self.subTest(function=function), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(rt.subprocess, "run") as run:
                with self.assertRaisesRegex(rt.GatewayError, "gateway must be a Lambda function name or ARN"):
                    rt.invoke(function, {"phase": "plan"}, Path(tmp))
                run.assert_not_called()
                self.assertEqual(list(Path(tmp).iterdir()), [])


class TestWorkflowConfiguration(TestCase):
    """The gateway and its region come from the caller's variables first, so no caller or code change is needed
    to point the train at a gateway in another account."""

    WORKFLOW = (Path(__file__).parents[1] / ".github" / "workflows" / "reusable_release_train.yml").read_text()

    def test_both_steps_prefer_the_gateway_variable(self) -> None:
        self.assertEqual(self.WORKFLOW.count(
            "RT_GATEWAY_FUNCTION: ${{ vars.RELEASE_TRAIN_GATEWAY || inputs.gateway_function }}"), 2)
        self.assertNotIn("RT_GATEWAY_FUNCTION: ${{ inputs.gateway_function }}", self.WORKFLOW)

    def test_the_region_prefers_the_region_variable(self) -> None:
        self.assertIn("aws-region: ${{ vars.RELEASE_TRAIN_REGION || inputs.aws_region }}", self.WORKFLOW)
        self.assertNotIn("aws-region: ${{ inputs.aws_region }}", self.WORKFLOW)


class TestTriggeringRun(TestCase):
    """A workflow_run run follows Release: Publish; only a successful Publish may record a release and fan out."""

    def run_step(self, env: dict[str, str]) -> tuple[int, str, str, list[str]]:
        calls: list[str] = []

        def github_api(token: str, base: str = "") -> Any:
            def call(method: str, path: str, body: Any = None) -> Any:
                calls.append(f"github {method} {path}")
                return {"default_branch": "rt-ci"}
            return call

        def converse(ctx: Any, call: Any, **kwargs: Any) -> Any:
            calls.append("gateway")
            return rt.Conversation(plan={"actions": []}, ended="no train is busy")

        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            summary.write_text("")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "t", "GITHUB_REF": "refs/heads/rt-ci",
                                              "RT_GATEWAY_FUNCTION": "fn", "RT_GATEWAY_AUDIENCE": "aud",
                                              "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp,
                                              "RT_DISPATCH_FILE": str(Path(tmp) / "dispatch.json"), **env}), \
                    mock.patch.object(rt, "github_api", github_api), mock.patch.object(rt, "converse", converse), \
                    mock.patch.object(rt, "invoke", lambda *a: calls.append("invoke") or {}), \
                    contextlib.redirect_stdout(out):
                code = rt.run()
            return code, out.getvalue(), summary.read_text(), calls

    def publish(self, conclusion: str) -> dict[str, str]:
        return {"GITHUB_EVENT_NAME": "workflow_run", "RT_TRIGGER_CONCLUSION": conclusion,
                "RT_TRIGGER_RUN_URL": PUBLISH_RUN}

    def test_a_failed_publish_skips_the_gateway_and_warns(self) -> None:
        code, log, summary, calls = self.run_step(self.publish("failure"))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        warnings = [line for line in log.splitlines() if line.startswith("::warning::")]
        self.assertEqual(len(warnings), 1)
        self.assertIn(PUBLISH_RUN, warnings[0])
        self.assertIn("failure", warnings[0])
        self.assertIn(PUBLISH_RUN, summary)
        self.assertIn("failure", summary)

    def test_a_cancelled_publish_skips_the_gateway_and_warns(self) -> None:
        code, log, summary, calls = self.run_step(self.publish("cancelled"))
        self.assertEqual((code, calls), (0, []))
        self.assertIn(f"::warning::Release: Publish run {PUBLISH_RUN} concluded cancelled", log)
        self.assertIn("cancelled", summary)

    def test_a_missing_conclusion_is_not_a_success(self) -> None:
        code, log, _, calls = self.run_step(self.publish(""))
        self.assertEqual((code, calls), (0, []))
        self.assertIn("::warning::", log)

    def test_a_successful_publish_asks_the_gateway(self) -> None:
        code, log, _, calls = self.run_step(self.publish("success"))
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["github GET /repos/example-owner/example-repo", "gateway"])
        self.assertNotIn("::warning::", log)

    def test_other_events_ignore_the_conclusion(self) -> None:
        for event in ("push", "workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                code, log, _, calls = self.run_step({"GITHUB_EVENT_NAME": event, "RT_TRIGGER_CONCLUSION": "",
                                                     "RT_TRIGGER_RUN_URL": ""})
                self.assertEqual(code, 0)
                self.assertEqual(calls[-1], "gateway")
                self.assertNotIn("::warning::", log)

    def ctx_of(self, env: dict[str, str]) -> Any:
        seen: list[Any] = []

        def converse(ctx: Any, call: Any, **kwargs: Any) -> Any:
            seen.append(ctx)
            return rt.Conversation(plan={"actions": []}, ended="no train is busy")

        with mock.patch.object(rt, "converse", converse), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "t",
                                             "GITHUB_REF": "refs/heads/rt-ci", "RT_GATEWAY_FUNCTION": "fn",
                                             "RT_GATEWAY_AUDIENCE": "aud", "RUNNER_TEMP": tmp, **env}), \
                mock.patch.object(rt, "github_api", lambda t, base="": lambda *a, **k: {"default_branch": "rt-ci"}), \
                contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            self.assertEqual(rt.run(), 0)
        (ctx,) = seen
        return ctx

    def test_a_scheduled_run_passes_its_cron_and_the_weekly_variable(self) -> None:
        ctx = self.ctx_of({"GITHUB_EVENT_NAME": "schedule", "RT_SCHEDULE": "17 15 * * 1", "RT_WEEKLY_ENABLED": "false"})
        self.assertEqual((ctx.schedule, ctx.weekly_enabled), ("17 15 * * 1", "false"))

    def test_other_events_pass_an_empty_schedule(self) -> None:
        for event in ("push", "workflow_dispatch"):
            with self.subTest(event=event):
                ctx = self.ctx_of({"GITHUB_EVENT_NAME": event, "RT_SCHEDULE": "", "RT_WEEKLY_ENABLED": ""})
                self.assertEqual((ctx.schedule, ctx.weekly_enabled), ("", ""))

    def test_an_unset_schedule_and_variable_are_empty(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("RT_SCHEDULE", None)
            os.environ.pop("RT_WEEKLY_ENABLED", None)
            ctx = self.ctx_of({"GITHUB_EVENT_NAME": "workflow_dispatch"})
        self.assertEqual((ctx.schedule, ctx.weekly_enabled), ("", ""))

    def test_the_dispatch_step_dispatches_nothing_after_a_failed_publish(self) -> None:
        api_calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_file = Path(tmp) / "dispatch.json"
            dispatch_file.write_text(json.dumps({"allowed": [CLI], "targets": [TARGET]}))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"RT_DISPATCH_FILE": str(dispatch_file), "RT_DISPATCH_REPOS": CLI,
                                              "RT_DISPATCH_TOKEN_OPENJOBDESCRIPTION": OPENJD_TOKEN,
                                              "RT_GATEWAY_FUNCTION": "fn", "RT_GATEWAY_AUDIENCE": "aud",
                                              "RUNNER_TEMP": tmp, **self.publish("failure")}), \
                    mock.patch.object(rt, "github_api", lambda t, base="": api_calls.append(t)), \
                    mock.patch.object(rt, "invoke", lambda *a: api_calls.append("invoke") or {}), \
                    mock.patch.object(rt, "id_token", lambda audience: "id-t0ken"), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(rt.run_dispatch(), 0)
        self.assertEqual(api_calls, [])
        self.assertIn("No downstream dispatches", out.getvalue())
