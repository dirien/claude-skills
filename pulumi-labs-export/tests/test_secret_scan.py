import json
import os
import unittest

import helpers
import secret_scan

FINDING = {"RuleID": "stripe-access-token", "File": "examples/x.py", "StartLine": 11, "Commit": "b7f34eb54fdc24f6163d26954927259149a8e3ec",
           "Fingerprint": "b7f3:examples/x.py:stripe-access-token:11", "Secret": "REDACTED"}
TOML = "[extend]\nuseDefault = true\n\n[allowlist]\nregexes = ['''doc_placeholder''']\n"


class ScanBase(helpers.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo = helpers.build_repo(self.tmp)
        self.rd = os.path.join(self.tmp, "reports")
        self.bin, self.log = helpers.install_fake_gitleaks(os.path.join(self.tmp, "fake"))

    def scan(self, findings=None, **env):
        with helpers.env(FAKE_GITLEAKS_LOG=self.log, FAKE_GITLEAKS_FINDINGS=json.dumps(findings or {}), **env):
            return secret_scan.scan(self.repo, self.rd, gitleaks=self.bin)

    def calls(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]


class ScanTest(ScanBase):
    def test_clean_repo_passes_and_report_is_keyed_by_head(self):
        r = self.scan()
        head = secret_scan.git_state(self.repo)["head"]
        self.assertEqual(r["overall"], "PASS")
        self.assertEqual(r["head_sha"], head)
        path = secret_scan.report_path(self.repo, head, self.rd)
        self.assertTrue(path.endswith("pulumi-modal-%s-secrets.json" % head))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(path[:-5] + ".md"))
        self.assertEqual(r["gitleaks"]["version"], "8.30.1-fake")
        self.assertEqual(r["commits_scanned"], 1)

    def test_scans_history_and_tree_with_redaction_and_default_rules(self):
        self.scan()
        kinds = [c[0] for c in self.calls()]
        self.assertEqual(kinds, ["git", "dir"])
        for argv in self.calls():
            self.assertIn("--redact", argv)
            self.assertNotIn("--config", argv)  # no allowlist file: default rules only
        self.assertEqual(self.calls()[0][-1], os.path.abspath(self.repo))  # the repo, so the whole history
        self.assertNotEqual(self.calls()[1][-1], os.path.abspath(self.repo))  # an extracted HEAD tree, not the worktree

    def test_history_finding_fails_without_storing_the_secret(self):
        r = self.scan({"git": [FINDING]})
        self.assertEqual(r["overall"], "FAIL")
        self.assertEqual(len(r["history_findings"]), 1)
        f = r["history_findings"][0]
        self.assertEqual((f["rule"], f["file"], f["line"], f["commit"]), ("stripe-access-token", "examples/x.py", 11, "b7f34eb54fdc"))
        self.assertNotIn("Secret", f)
        with open(secret_scan.report_path(self.repo, r["head_sha"], self.rd)) as fh:
            self.assertNotIn("REDACTED", fh.read())

    def test_tree_finding_alone_fails(self):
        r = self.scan({"dir": [FINDING]})
        self.assertEqual((r["overall"], len(r["history_findings"]), len(r["tree_findings"])), ("FAIL", 0, 1))

    def test_dirty_worktree_fails_the_report(self):
        helpers.write(self.repo, "stray.txt", "x")
        r = self.scan()
        self.assertEqual(r["overall"], "FAIL")
        self.assertTrue(r["porcelain"])

    def test_allowlist_is_passed_recorded_and_hashed(self):
        helpers.write(self.repo, ".gitleaks.toml", TOML)
        helpers.commit_all(self.repo, "allowlist")
        r = self.scan()
        for argv in self.calls():
            self.assertEqual(argv[argv.index("--config") + 1], os.path.join(self.repo, ".gitleaks.toml"))
        self.assertEqual(r["config"]["file"], ".gitleaks.toml")
        self.assertEqual(len(r["config"]["sha256"]), 64)
        self.assertIn("doc_placeholder", r["config"]["content"])
        with open(secret_scan.report_path(self.repo, r["head_sha"], self.rd)[:-5] + ".md") as f:
            self.assertIn("doc_placeholder", f.read())

    def test_config_that_drops_default_rules_is_refused(self):
        for bad in ("[allowlist]\nregexes = ['x']\n", "[extend]\nuseDefault = false\n", "title = 'x'\n"):
            helpers.write(self.repo, ".gitleaks.toml", bad)
            helpers.commit_all(self.repo, "bad")
            with self.assertRaises(secret_scan.ScanError) as cm:
                self.scan()
            self.assertIn("useDefault = true", str(cm.exception))

    def test_missing_gitleaks_is_unavailable_not_clean(self):
        with self.assertRaises(secret_scan.ScanUnavailable) as cm:
            secret_scan.scan(self.repo, self.rd, gitleaks=os.path.join(self.tmp, "nope"), prefix=os.path.join(self.tmp, "noprefix"))
        self.assertIn("NOT scanned", str(cm.exception))
        self.assertFalse(os.path.exists(self.rd))

    def test_gitleaks_crash_is_unavailable_not_clean(self):
        with self.assertRaises(secret_scan.ScanUnavailable):
            self.scan(FAKE_GITLEAKS_CRASH="1")


class CliTest(ScanBase):
    def run_main(self, findings=None):
        with helpers.env(FAKE_GITLEAKS_LOG=self.log, FAKE_GITLEAKS_FINDINGS=json.dumps(findings or {})):
            return helpers.quiet(secret_scan.main, ["--repo", self.repo, "--report-dir", self.rd, "--gitleaks", self.bin])

    def test_exit_codes(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.run_main({"git": [FINDING]}), 1)
        with helpers.env(FAKE_GITLEAKS_CRASH="1"):
            self.assertEqual(self.run_main(), 2)
        self.assertEqual(helpers.quiet(secret_scan.main, ["--repo", self.repo, "--report-dir", self.rd, "--gitleaks", "/nonexistent/gitleaks"]), 2)


if __name__ == "__main__":
    unittest.main()
