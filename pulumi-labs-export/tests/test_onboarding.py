import json
import unittest

import helpers
import onboarding
import render


def release_text(provider="modal"):
    return render.render_all(render.make_options(provider, "X", []))[".github/workflows/release.yml"].decode()


class OnboardingTest(unittest.TestCase):
    def test_every_issue_ends_with_note(self):
        issues = onboarding.build_issues("modal", release_text())
        self.assertEqual(len(issues), 5)
        for it in issues:
            self.assertTrue(it["body"].endswith(onboarding.NOTE), it["title"])
            self.assertEqual(it["body"].splitlines()[-1],
                             "Note: This is a stop gap until Workprentice has permissions to Linear. "
                             "If you do have any questions or concerns, speak to @pose")

    def test_names_come_from_workflow(self):
        issues = {i["title"]: i["body"] for i in onboarding.build_issues("better-uptime", release_text("better-uptime"))}
        blob = "\n".join(issues.values())
        for s in ["github-secrets/pulumi-labs-pulumi-better-uptime", "@pulumi-labs/better-uptime", "pulumi-labs-better-uptime",
                  "Pulumi.Labs.BetterUptime", "com.pulumi.labs:better-uptime", "publish-npm", "publish-pypi", "publish-nuget",
                  "NUGET_USER", "MAVEN_CENTRAL_SIGNING_KEY", "MAVEN_CENTRAL_SIGNING_PASSWORD", "MAVEN_CENTRAL_OSSRH_USERNAME",
                  "MAVEN_CENTRAL_OSSRH_PASSWORD", "release.yml"]:
            self.assertIn(s, blob)
        self.assertNotIn("nvidia", blob.lower())

    def test_parse_jobs(self):
        jobs = onboarding.parse_jobs(release_text())
        self.assertEqual(jobs["publish-nodejs"]["environment"], "publish-npm")
        self.assertEqual(jobs["publish-python"]["environment"], "publish-pypi")
        self.assertEqual(jobs["publish-dotnet"]["environment"], "publish-nuget")
        self.assertIn("NuGet/login", jobs["publish-dotnet"]["uses"])
        self.assertIn("pulumi/publish-go-sdk-action", jobs["publish-go-sdk"]["uses"])
        self.assertIsNone(jobs["publish-java"]["environment"])

    def test_fails_when_job_or_environment_missing(self):
        text = release_text()
        with self.assertRaisesRegex(onboarding.WorkflowError, "no job 'publish-nodejs'"):
            onboarding.build_issues("modal", text.replace("publish-nodejs:", "publish-npm-renamed:"))
        with self.assertRaisesRegex(onboarding.WorkflowError, "expected environment publish-pypi"):
            onboarding.build_issues("modal", text.replace("environment: publish-pypi", "environment: pypi"))
        with self.assertRaisesRegex(onboarding.WorkflowError, "MAVEN_CENTRAL"):
            onboarding.build_issues("modal", text.replace("MAVEN_CENTRAL_SIGNING_KEY", "MAVEN_SIGNING_KEY"))
        with self.assertRaisesRegex(onboarding.WorkflowError, "NuGet/login"):
            onboarding.build_issues("modal", text.replace("NuGet/login", "other/login"))

    def test_dup_check_and_cli(self):
        t = "Onboard x: y"
        self.assertEqual(onboarding.dup_check(t), 'gh issue list -R pulumi/platform-providers-team --state all --search "Onboard x: y in:title"')
        self.assertEqual(helpers.quiet(onboarding.main, ["--provider", "modal", "--json"]), 0)
        self.assertEqual(helpers.quiet(onboarding.main, ["--provider", "Bad"]), 1)


if __name__ == "__main__":
    unittest.main()
