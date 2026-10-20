import hashlib
import io
import json
import unittest
import urllib.error
from unittest import mock

import helpers
import access_check
import archetype_drift
import render


class AccessCheckTest(unittest.TestCase):
    def test_redact(self):
        s = "fatal: https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r and github_pat_ABCDEFGHIJKLMNOPQRSTUV"
        out = access_check.redact(s)
        self.assertNotIn("ghs_", out)
        self.assertNotIn("github_pat_", out)
        self.assertNotIn("x-access-token", out)

    def test_parse_http(self):
        st, body = access_check.parse_http('HTTP/2.0 200 OK\r\nContent-Type: x\r\n\r\n{"has_issues": true}')
        self.assertEqual(st, 200)
        self.assertEqual(body, '{"has_issues": true}')
        self.assertEqual(access_check.parse_http("HTTP/2.0 404 Not Found\r\n\r\n{}")[0], 404)
        self.assertIsNone(access_check.parse_http("boom")[0])

    def fake(self, repo_status, push_rc, calls):
        def runner(cmd, cwd=None, env=None):
            calls.append(cmd)
            if cmd[:2] == ["gh", "api"]:
                ep = cmd[-1]
                if ep == "/repos/pulumi-labs/pulumi-modal":
                    return 0, "HTTP/2.0 %d x\r\n\r\n{\"permissions\":{\"push\":false}}" % repo_status
                return 0, "HTTP/2.0 200 OK\r\n\r\n{\"has_issues\": true, \"archived\": false}"
            if cmd[:2] == ["git", "push"]:
                return push_rc, "remote: Permission denied ghs_abcdefghijklmnopqrstuvwx"
            return 0, ""
        return runner

    def test_probes_pass_and_do_not_use_permissions_object(self):
        calls = []
        res = access_check.run_probes("modal", self.fake(200, 0, calls))
        self.assertTrue(all(r["ok"] for r in res), res)
        push = [c for c in calls if c[:2] == ["git", "push"]][0]
        self.assertEqual(push[2:], ["--dry-run", "https://github.com/pulumi-labs/pulumi-modal.git", "HEAD:refs/heads/access-probe"])
        self.assertEqual([r["endpoint"] for r in res][:3], ["GET /repos/pulumi/autoregistry", "GET /repos/pulumi/platform-providers-team",
                                                             "GET /repos/pulumi-labs/pulumi-modal"])

    def test_push_rejected_is_failure_and_redacted(self):
        res = access_check.run_probes("modal", self.fake(200, 1, []))
        push = res[-1]
        self.assertFalse(push["ok"])
        self.assertNotIn("ghs_", push["detail"])

    def test_missing_repo(self):
        res = access_check.run_probes("modal", self.fake(404, 0, []))
        self.assertFalse(res[2]["ok"])
        self.assertEqual(res[2]["status"], 404)
        self.assertEqual(res[3]["status"], "skipped")

    def test_admin_probe_is_informational(self):
        for admin_status, word in ((200, "yes"), (403, "no")):
            def runner(cmd, cwd=None, env=None):
                if cmd[:2] == ["gh", "api"] and cmd[-1].endswith("/actions/permissions"):
                    return 0, "HTTP/2.0 %d x\r\n\r\n{}" % admin_status
                return self.fake(200, 0, [])(cmd, cwd, env)
            res = access_check.run_probes("modal", runner, admin=True)
            self.assertEqual(len(res), 5)
            self.assertEqual(res[-1]["endpoint"], "GET /repos/pulumi-labs/pulumi-modal/actions/permissions")
            self.assertTrue(res[-1]["ok"] and res[-1]["info"])
            self.assertTrue(res[-1]["detail"].startswith(word), res[-1])
        self.assertEqual(len(access_check.run_probes("modal", self.fake(200, 0, []))), 4)

    def test_admin_probe_skipped_for_a_missing_repo(self):
        calls = []
        res = access_check.run_probes("modal", self.fake(404, 0, calls), admin=True)
        self.assertEqual(res[-1]["status"], "skipped")
        self.assertIn("plan create", res[-1]["detail"])
        self.assertFalse([c for c in calls if c[-1].endswith("/actions/permissions")])
        self.assertIn("repo_setup.sh plan create", res[2]["detail"])


class CredentialDetectionTest(unittest.TestCase):
    def test_print_commands_are_direct_and_shell_free(self):
        import shlex
        buf = io.StringIO()
        access_check.print_commands("modal", "/tmp/probe", buf)
        lines = buf.getvalue().splitlines()
        cmds = [shlex.split(l) for l in lines if not l.startswith("#")]
        self.assertEqual(len(cmds), 6)
        self.assertEqual([c[:3] for c in cmds[:3]], [["gh", "api", "/repos/pulumi/autoregistry"],
                                                      ["gh", "api", "/repos/pulumi/platform-providers-team"],
                                                      ["gh", "api", "/repos/pulumi-labs/pulumi-modal"]])
        for c in cmds[3:]:
            self.assertEqual(c[:3], ["git", "-C", "/tmp/probe"])
        self.assertEqual(cmds[3][3:], ["init", "-q"])
        self.assertIn("--allow-empty", cmds[4])
        self.assertEqual(cmds[5][3:], ["push", "--dry-run", "https://github.com/pulumi-labs/pulumi-modal.git", "HEAD:refs/heads/access-probe"])
        for l in lines:
            if not l.startswith("#"):
                self.assertNotRegex(l, r"[|;&]|\bcd\b")
        text = buf.getvalue()
        self.assertIn("PASS rule", text)
        self.assertIn("denied", text)
        self.assertIn("403", text)

    def test_print_commands_admin_adds_one_direct_probe(self):
        import shlex
        buf = io.StringIO()
        access_check.print_commands("modal", "/tmp/probe", buf, admin=True)
        cmds = [shlex.split(l) for l in buf.getvalue().splitlines() if not l.startswith("#")]
        self.assertEqual(len(cmds), 7)
        self.assertEqual(cmds[6], ["gh", "api", "/repos/pulumi-labs/pulumi-modal/actions/permissions"])
        self.assertIn("not part of the PASS rule", buf.getvalue())

    def test_print_commands_flag_runs_nothing(self):
        with mock.patch.object(access_check, "run") as r, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(access_check.main(["--print-commands", "--provider", "modal"]), 0)
        r.assert_not_called()
        self.assertIn("gh api /repos/pulumi-labs/pulumi-modal", out.getvalue())

    def test_gh_login_message_exits_2(self):
        def runner(cmd, cwd=None, env=None):
            return 1, "To get started with GitHub CLI, please run:  gh auth login"
        with self.assertRaises(access_check.CredentialsUnavailable):
            access_check.run_probes("modal", runner)
        cp = mock.Mock(returncode=1, stdout="", stderr="To get started with GitHub CLI, please run:  gh auth login")
        with mock.patch.object(access_check.subprocess, "run", return_value=cp), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(access_check.main(["--provider", "modal"]), 2)
        self.assertIn("--print-commands", err.getvalue())
        self.assertIn("credentials are not available to this process", err.getvalue())

    def test_gh_rc4_exits_2(self):
        with self.assertRaises(access_check.CredentialsUnavailable):
            access_check.gh_get("/repos/x/y", lambda cmd, cwd=None, env=None: (4, ""))

    def test_git_auth_error_is_credentials_not_denied(self):
        def runner(cmd, cwd=None, env=None):
            if cmd[:2] == ["gh", "api"]:
                return 0, "HTTP/2.0 200 OK\r\n\r\n{\"has_issues\": true}"
            if cmd[:2] == ["git", "push"]:
                return 128, "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
            return 0, ""
        with self.assertRaises(access_check.CredentialsUnavailable):
            access_check.run_probes("modal", runner)

    def test_permission_denied_is_still_plain_failure(self):
        def runner(cmd, cwd=None, env=None):
            if cmd[:2] == ["gh", "api"]:
                return 0, "HTTP/2.0 200 OK\r\n\r\n{\"has_issues\": true}"
            if cmd[:2] == ["git", "push"]:
                return 128, "remote: Permission denied. fatal: unable to access: The requested URL returned error: 403"
            return 0, ""
        res = access_check.run_probes("modal", runner)
        self.assertFalse(res[-1]["ok"])


class DriftFetchTest(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(archetype_drift.raw_url("o/r", ".github/x.yml", "main"),
                         "https://raw.githubusercontent.com/o/r/main/.github/x.yml")
        self.assertEqual(archetype_drift.tree_url("o/r", "main"), "https://api.github.com/repos/o/r/git/trees/main?recursive=1")

    def test_main_with_injected_fetcher_no_drift(self):
        m = render.load_manifest()
        seen = []

        def fetcher(url):
            seen.append(url)
            if "api.github.com" in url:
                return json.dumps({"sha": "abc", "tree": []}).encode()
            return b"same"
        real = render.load_manifest
        fake = dict(m, files=[dict(e, upstream_sha256=hashlib.sha256(b"same").hexdigest()) for e in m["files"]])
        with mock.patch.object(render, "load_manifest", lambda: fake), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(archetype_drift.main([], fetcher), 0)
        self.assertTrue(any(u.startswith("https://raw.githubusercontent.com/") for u in seen))
        self.assertTrue(real)

    def test_rate_limit_message(self):
        hdrs = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1760000000"}
        err = urllib.error.HTTPError("https://api.github.com/x", 403, "rate limited", hdrs, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(archetype_drift.DriftError) as cm:
                archetype_drift.http_get("https://api.github.com/x")
        self.assertIn("rate limit", str(cm.exception))

    def test_main_reports_fetch_error_exit_1(self):
        def fetcher(url):
            raise archetype_drift.DriftError("boom")
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(archetype_drift.main([], fetcher), 1)

    def test_no_gh_dependency(self):
        import inspect
        self.assertNotIn("subprocess", inspect.getsource(archetype_drift))


class DriftTest(unittest.TestCase):
    def test_compare(self):
        m = render.load_manifest()
        orig = {}
        for e in m["files"]:
            orig[e["upstream_path"]] = b"same"
            e = dict(e)
        manifest = {"archetype_repo": m["archetype_repo"], "files": []}
        for e in m["files"]:
            e = dict(e)
            e["upstream_sha256"] = hashlib.sha256(b"same").hexdigest()
            manifest["files"].append(e)
        fetch = lambda repo, path: b"changed" if path == "Makefile" else b"same"  # noqa: E731
        tree = ["Makefile", "README.md", "sdk/go/x.go", ".github/workflows/new.yml", ".golangci.yml", "examples/a", "provider/x.go"]
        changed, new = archetype_drift.compare(manifest, fetch, tree)
        self.assertEqual([c[0] for c in changed], ["Makefile"])
        self.assertEqual(new, [".github/workflows/new.yml", ".golangci.yml"])

    def test_no_drift(self):
        m = render.load_manifest()
        data = {e["upstream_path"]: b"x" for e in m["files"]}
        manifest = {"archetype_repo": m["archetype_repo"], "files": [dict(e, upstream_sha256=hashlib.sha256(b"x").hexdigest()) for e in m["files"]]}
        changed, new = archetype_drift.compare(manifest, lambda r, p: data[p], list(data))
        self.assertEqual((changed, new), ([], []))


if __name__ == "__main__":
    unittest.main()
