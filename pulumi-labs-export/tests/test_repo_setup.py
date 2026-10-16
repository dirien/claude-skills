"""repo_setup.sh is for humans: it must parse, document itself and refuse to run unattended."""
import os
import shutil
import subprocess
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "repo_setup.sh")


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class RepoSetupTest(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              env={k: v for k, v in os.environ.items() if k != "CI"}, start_new_session=True)

    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)

    def test_header_says_humans_only(self):
        with open(SCRIPT, encoding="utf-8") as f:
            head = f.read(900)
        self.assertIn("HUMANS ONLY", head)
        self.assertIn("NEVER run this", head)

    def test_help(self):
        p = self.run_script("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("HUMANS ONLY", p.stdout)

    def test_refuses_without_terminal(self):
        if not (shutil.which("gh") and shutil.which("jq")):
            self.skipTest("gh and jq are needed to reach the terminal check")
        p = self.run_script("create", "demo")
        self.assertEqual(p.returncode, 2)
        self.assertIn("interactive terminal", p.stderr)

    def test_rejects_bad_slug_in_dry_run(self):
        if not (shutil.which("gh") and shutil.which("jq")):
            self.skipTest("gh and jq are needed")
        p = self.run_script("--dry-run", "create", "Bad_Slug")
        self.assertEqual(p.returncode, 2)
        self.assertIn("bad slug", p.stderr)

    def test_dry_run_changes_nothing_and_names_every_setting(self):
        if not (shutil.which("gh") and shutil.which("jq")):
            self.skipTest("gh and jq are needed")
        p = self.run_script("--dry-run", "create", "demo")
        self.assertEqual(p.returncode, 0, p.stderr)
        for needle in ("--public", "publish-npm", "publish-pypi", "publish-nuget", "Release tags: people only",
                       "vulnerability-alerts", "secret_scanning_push_protection"):
            self.assertIn(needle, p.stdout + p.stderr)
        self.assertNotIn("add-readme", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
