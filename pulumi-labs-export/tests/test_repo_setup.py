"""repo_setup.sh: create/protect/verify are for humans and refuse to run unattended; plan is the agent path."""
import copy
import json
import os
import shlex
import shutil
import subprocess
import tempfile
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


REPO = "pulumi-labs/pulumi-demo"
R = "repos/" + REPO
ENVS = ("publish-npm", "publish-pypi", "publish-nuget")
WRITE_FLAGS = {"-X", "--method", "--input", "-f", "-F", "--field", "--raw-field", "-H", "--header"}

# GET responses of an existing, empty, unconfigured repo (pulumi-labs/pulumi-modal on 2026-10-18).
UNCONFIGURED = {
    R: {"name": "pulumi-demo", "full_name": REPO, "private": False, "visibility": "public", "archived": False,
        "default_branch": "main", "has_issues": True, "has_wiki": True, "has_projects": True, "has_discussions": False,
        "allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True, "allow_auto_merge": False,
        "allow_update_branch": False, "delete_branch_on_merge": False,
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE", "squash_merge_commit_message": "COMMIT_MESSAGES",
        "permissions": {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True},
        "security_and_analysis": {"dependabot_security_updates": {"status": "disabled"},
                                  "secret_scanning": {"status": "disabled"},
                                  "secret_scanning_push_protection": {"status": "disabled"}}},
    R + "/actions/permissions": {"enabled": True, "allowed_actions": "all", "sha_pinning_required": False},
    R + "/actions/permissions/workflow": {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True},
    R + "/actions/permissions/fork-pr-contributor-approval": {"approval_policy": "first_time_contributors"},
    R + "/automated-security-fixes": {"enabled": False, "paused": False},
    R + "/private-vulnerability-reporting": {"enabled": False},
    R + "/environments": {"total_count": 0, "environments": []},
    R + "/rulesets": [],
}


def ruleset_response(body, rid):
    """A ruleset as GitHub returns it: the body plus ids and parameters GitHub fills in."""
    r = copy.deepcopy(body)
    r.update({"id": rid, "source_type": "Repository", "source": REPO, "node_id": "RRS_x", "current_user_can_bypass": "always",
              "created_at": "2026-10-18T00:00:00Z", "updated_at": "2026-10-18T00:00:00Z"})
    for rule in r["rules"]:
        params = rule.get("parameters")
        if rule["type"] == "pull_request":
            params.update({"required_reviewers": [], "automatic_copilot_code_review_enabled": False})
        if rule["type"] == "required_status_checks":
            params["do_not_enforce_on_create"] = False
            params["required_status_checks"] = [dict(c, integration_id=15368) for c in params["required_status_checks"]]
    return r


def configured(bodies, protected=False):
    """GET responses after every write of the plan went through, shaped like GitHub's (extra fields included)."""
    s = copy.deepcopy(UNCONFIGURED)
    settings = bodies["settings.json"]
    repo = s[R]
    repo.update({k: v for k, v in settings.items() if k != "security_and_analysis"})
    repo["security_and_analysis"].update(settings["security_and_analysis"])
    s[R + "/actions/permissions/workflow"] = dict(bodies["workflow.json"])
    s[R + "/actions/permissions/fork-pr-contributor-approval"] = dict(bodies["fork-pr.json"])
    s[R + "/private-vulnerability-reporting"] = {"enabled": True}
    s[R + "/environments"] = {"total_count": 3, "environments": [
        {"id": i, "name": e, "deployment_branch_policy": bodies["environment.json"]["deployment_branch_policy"],
         "protection_rules": [{"id": i, "type": "branch_policy"}]} for i, e in enumerate(ENVS)]}
    for i, e in enumerate(ENVS):
        s[R + "/environments/%s/deployment-branch-policies" % e] = {
            "total_count": 1, "branch_policies": [dict(bodies["tag-policy.json"], id=i, node_id="x")]}
    s[R + "/rulesets"] = [{"id": 101, "name": "Release tags: people only", "target": "tag", "source_type": "Repository",
                           "source": REPO, "enforcement": "active"}]
    s[R + "/rulesets/101"] = ruleset_response(bodies["ruleset-tags.json"], 101)
    if protected:
        s[R + "/rulesets"].append({"id": 102, "name": "Default branch: PR only", "target": "branch",
                                   "source_type": "Repository", "source": REPO, "enforcement": "active"})
        s[R + "/rulesets/102"] = ruleset_response(bodies["ruleset-branch.json"], 102)
        s[R + "/commits?per_page=1"] = [{"sha": "0123456789abcdef0123456789abcdef01234567"}]
        s[R + "/contents/.github/workflows/release.yml"] = {"name": "release.yml", "type": "file"}
    s["orgs/pulumi-labs/installations"] = {"total_count": 1, "installations": [
        {"app_slug": "workprentice", "repository_selection": "all"}]}
    return s


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are needed")
class RepoSetupPlanTest(unittest.TestCase):
    """plan is the agent path: no terminal, no gh call, read-only checks whose jq programs print only what is left."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repo-setup-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A gh that records every call: plan must never run it (credentials reach only a gh the agent runs itself).
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.gh_log = os.path.join(self.tmp, "gh-called")
        with open(os.path.join(self.bin, "gh"), "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % shlex.quote(self.gh_log))
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        self.out = os.path.join(self.tmp, "bodies")

    def plan(self, *args, env_extra=None):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), CI="true")
        env.update(env_extra or {})
        return subprocess.run(["bash", SCRIPT, "--out", self.out, "plan", *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env, start_new_session=True)

    def bodies(self):
        b = {}
        for n in os.listdir(self.out):
            with open(os.path.join(self.out, n), encoding="utf-8") as f:
                b[n] = json.load(f)
        return b

    def checks(self, *args):
        p = self.plan(*args)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(self.gh_log), "plan must not call gh")
        return p.stdout, [shlex.split(l) for l in p.stdout.splitlines() if l and not l.startswith("#")]

    def assert_read_only(self, argv):
        self.assertEqual(argv[:2], ["gh", "api"], argv)
        self.assertFalse(WRITE_FLAGS & set(argv), argv)
        self.assertTrue(argv[2].startswith(R) or argv[2] == "orgs/pulumi-labs/installations", argv[2])

    def evaluate(self, lines, state):
        """Evaluate each check's jq program against state (path -> response, missing path = HTTP error); follow CHECK."""
        out = []
        for argv in lines:
            self.assert_read_only(argv)
            if argv[2] not in state or "--jq" not in argv:
                continue  # an HTTP error or a 204: the plan's '# on HTTP' comment decides, not the program
            prog = argv[argv.index("--jq") + 1]
            p = subprocess.run(["jq", "-r", prog], input=json.dumps(state[argv[2]]), capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, "%s: %s" % (argv[2], p.stderr))
            for line in p.stdout.splitlines():
                out.append(line)
                if line.startswith("CHECK "):
                    out.extend(self.evaluate([shlex.split(line[len("CHECK "):])], state))
        return out

    def writes(self, out):
        return [l for l in out if l.split(" ", 1)[0] in ("RUN", "TRY")]

    def test_runs_without_terminal_or_gh_and_even_in_ci(self):
        text, lines = self.checks("create", "demo")
        self.assertIn("# repo_setup.sh plan create " + REPO, text)
        self.assertGreater(len(lines), 10)
        for name in ("create", "settings", "actions", "workflow", "fork-pr", "environment", "tag-policy",
                     "ruleset-tags", "ruleset-branch"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name + ".json")), name)

    def path_without_jq(self):
        """A PATH dir with every tool from the current PATH except jq, the way an agent's shell starts."""
        tools = os.path.join(self.tmp, "nojq")
        os.mkdir(tools)
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                dst = os.path.join(tools, n)
                src = os.path.join(d, n)
                if n != "jq" and not os.path.lexists(dst) and os.access(src, os.X_OK):
                    os.symlink(src, dst)
        return self.bin + os.pathsep + tools

    def test_finds_jq_from_bootstrap(self):
        home = os.path.join(self.tmp, "home")
        boot = os.path.join(home, ".local", "pulumi-labs-export", "bin")
        os.makedirs(boot)
        os.symlink(shutil.which("jq"), os.path.join(boot, "jq"))
        p = self.plan("create", "demo", env_extra={"PATH": self.path_without_jq(), "HOME": home})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("# repo_setup.sh plan create " + REPO, p.stdout)

    def test_no_jq_anywhere_says_bootstrap(self):
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        p = self.plan("create", "demo", env_extra={"PATH": self.path_without_jq(), "HOME": home})
        self.assertEqual(p.returncode, 2)
        self.assertIn("bootstrap.py", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_every_line_is_one_direct_read_only_gh_command(self):
        for args in (("create", "demo"), ("protect", "demo"), ("verify", "demo"), ("verify", "demo", "--imported")):
            text, lines = self.checks(*args)
            for raw in text.splitlines():
                if raw and not raw.startswith("#"):
                    lex = shlex.shlex(raw, posix=True, punctuation_chars=True)
                    lex.whitespace_split = True
                    self.assertFalse({"|", "||", "&&", ";", ">", ">>", "<", "&"} & set(lex), raw[:80])
            for argv in lines:
                self.assert_read_only(argv)

    def test_bodies_match_the_human_path(self):
        self.checks("create", "demo")
        sent = []
        for sub in ("create", "protect"):  # the human dry run echoes each JSON body it would send on stderr
            p = subprocess.run(["bash", SCRIPT, "--dry-run", sub, "demo"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, env={k: v for k, v in os.environ.items() if k != "CI"})
            self.assertEqual(p.returncode, 0, p.stderr)
            dec, rest = json.JSONDecoder(), p.stderr.strip()
            while rest:
                obj, end = dec.raw_decode(rest)
                sent.append(obj)
                rest = rest[end:].strip()
        b = self.bodies()
        for name in ("settings", "actions", "workflow", "fork-pr", "environment", "ruleset-tags", "ruleset-branch"):
            self.assertIn(b[name + ".json"], sent, name)
        self.assertEqual(b["tag-policy.json"], {"name": "v*.*.*", "type": "tag"})  # the human path discards that stderr
        self.assertEqual(b["create.json"]["visibility"], "public")
        self.assertEqual(b["create.json"]["name"], "pulumi-demo")
        self.assertFalse(b["create.json"]["auto_init"])

    def test_unconfigured_repo_gets_exactly_the_missing_writes(self):
        _, lines = self.checks("create", "demo")
        out = self.evaluate(lines, UNCONFIGURED)
        self.assertFalse([l for l in out if l.startswith("STOP")], out)
        w = [l.split(" ")[:5] for l in self.writes(out)]
        self.assertIn(["RUN", "gh", "api", "-X", "PATCH"], w)
        joined = "\n".join(self.writes(out))
        for needle in ("PUT %s/actions/permissions/workflow " % R, "PUT %s/actions/permissions/fork-pr-contributor-approval " % R,
                       "PUT %s/private-vulnerability-reporting " % R, "POST %s/rulesets " % R) + tuple(
                "PUT %s/environments/%s " % (R, e) for e in ENVS):
            self.assertIn(needle, joined)
        self.assertNotIn("%s/actions/permissions --input" % R, joined)  # Actions are already enabled
        self.assertNotIn("automated-security-fixes", joined)            # already off
        self.assertTrue(all(l.startswith("RUN gh api -X") or l.startswith("TRY gh api -X") for l in self.writes(out)))
        self.assertIn("fix repository: secret_scanning_push_protection is disabled, want enabled", out)

    def test_configured_repo_needs_nothing(self):
        _, lines = self.checks("create", "demo")
        out = self.evaluate(lines, configured(self.bodies()))
        self.assertEqual(self.writes(out), [])
        self.assertFalse([l for l in out if l.split(" ", 1)[0] in ("fix", "STOP")], out)
        self.assertIn("ok ruleset Release tags: people only", out)
        self.assertIn("ok repository settings", out)

    def test_partial_state_reruns_only_what_is_left(self):
        _, lines = self.checks("create", "demo")
        state = configured(self.bodies())
        state[R + "/environments"]["environments"].pop()                    # publish-nuget missing
        del state[R + "/environments/publish-nuget/deployment-branch-policies"]
        state[R + "/rulesets/101"]["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
        state[R]["allow_merge_commit"] = True
        w = self.writes(self.evaluate(lines, state))
        self.assertEqual(len(w), 3, w)
        self.assertTrue(w[0].startswith("RUN gh api -X PATCH %s --input " % R), w[0])
        self.assertTrue(w[1].startswith("RUN gh api -X PUT %s/environments/publish-nuget --input " % R), w[1])
        self.assertTrue(w[2].startswith("RUN gh api -X PUT %s/rulesets/101 --input " % R), w[2])

    def test_stops_instead_of_changing_what_it_must_not(self):
        _, lines = self.checks("create", "demo")
        for change, needle in ((lambda s: s[R].update(visibility="private", private=True), "not public"),
                               (lambda s: s[R].update(archived=True), "archived"),
                               (lambda s: s[R].pop("security_and_analysis"), "not an admin"),
                               (lambda s: s[R + "/environments/publish-npm/deployment-branch-policies"]["branch_policies"].append(
                                   {"id": 9, "name": "main", "type": "branch"}), "other deployment policies")):
            state = configured(self.bodies())
            change(state)
            out = self.evaluate(lines, state)
            stops = [l for l in out if l.startswith("STOP")]
            self.assertEqual(len(stops), 1, out)
            self.assertIn(needle, stops[0])

    def test_protect_adds_only_the_branch_ruleset(self):
        _, lines = self.checks("protect", "demo")
        state = configured(self.bodies())
        state[R + "/commits?per_page=1"] = [{"sha": "0123456789abcdef0123456789abcdef01234567"}]
        w = self.writes(self.evaluate(lines, state))
        self.assertEqual(len(w), 1, w)
        self.assertIn("-X POST %s/rulesets --input" % R, w[0])
        self.assertTrue(w[0].rstrip().endswith("ruleset-branch.json --silent"), w[0])
        out = self.evaluate(lines, configured(self.bodies(), protected=True))
        self.assertEqual(self.writes(out), [])
        self.assertIn("ok ruleset Default branch: PR only", out)

    def test_verify_passes_only_a_configured_repo(self):
        _, lines = self.checks("verify", "demo", "--imported")
        out = self.evaluate(lines, configured(self.bodies(), protected=True))
        self.assertFalse([l for l in out if l.startswith("FAIL")], out)
        self.assertIn("PASS ruleset Default branch: PR only", out)
        self.assertIn("PASS release.yml present", out)
        _, lines = self.checks("verify", "demo")
        out = self.evaluate(lines, UNCONFIGURED)
        fails = [l for l in out if l.startswith("FAIL")]
        self.assertIn("FAIL ruleset Release tags: people only is missing", fails)
        self.assertIn("FAIL environment publish-npm is missing", fails)
        self.assertFalse(self.writes(out))

    def test_refuses_what_is_out_of_scope(self):
        for args, needle in ((("plan", "create", "demo", "other"), "exactly one slug"),
                             (("--org", "pulumi", "plan", "create", "demo"), "only on pulumi-labs"),
                             (("plan", "create", "Bad_Slug"), "bad slug"),
                             (("plan", "delete", "demo"), "create, protect or verify"),
                             (("plan", "create", "demo", "--imported"), "--imported goes with plan verify")):
            p = subprocess.run(["bash", SCRIPT, "--out", self.out, *args], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(p.returncode, 2, args)
            self.assertIn(needle, p.stderr)


if __name__ == "__main__":
    unittest.main()
