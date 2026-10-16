import hashlib
import unittest

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
