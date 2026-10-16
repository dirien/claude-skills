import json
import os
import unittest

import helpers
import gomod
import names
import verify


class SchemaChecksTest(unittest.TestCase):
    def schema(self, variables, defaults=None, required=None):
        return {"config": {"variables": variables, "defaults": defaults or []},
                "provider": {"requiredInputs": required or []}}

    def test_secret_and_name_matches_need_env(self):
        s = self.schema({
            "tokenId": {"type": "string", "secret": True, "defaultInfo": {"environment": ["MODAL_TOKEN_ID"]}},
            "apiKey": {"type": "string"},                       # name matches, no env
            "region": {"type": "string", "secret": True},        # secret, no env
            "endpoint": {"type": "string"},                      # neither: not checked
            "dbPassword": {"type": "string", "defaultInfo": {"environment": []}},
            "CLIENT_CREDENTIALS": {"type": "string", "defaultInfo": {"environment": ["X"]}},
        })
        got = {i["name"]: i["status"] for i in verify.check_config_env_defaults(s)}
        self.assertEqual(got, {"tokenId": "PASS", "apiKey": "FAIL", "region": "FAIL", "dbPassword": "FAIL",
                               "CLIENT_CREDENTIALS": "PASS"})

    def test_no_config_passes(self):
        self.assertEqual([i["status"] for i in verify.check_config_env_defaults({})], ["PASS"])

    def test_required_config(self):
        ok = verify.check_no_required_config(self.schema({}))
        self.assertEqual([i["status"] for i in ok], ["PASS", "PASS"])
        bad = verify.check_no_required_config(self.schema({}, defaults=["region"], required=["token"]))
        self.assertEqual([(i["name"], i["status"]) for i in bad],
                         [("config.defaults", "FAIL"), ("provider.requiredInputs", "FAIL")])


class SdkVersionTest(unittest.TestCase):
    def test_placeholder_is_not_a_version(self):
        for bad in (None, "", "${VERSION}", "latest", 3):
            self.assertFalse(verify.real_sdk_version(bad), bad)
        for good in ("0.1.0", "0.0.1-dev", "1.2.3+build.5"):
            self.assertTrue(verify.real_sdk_version(good), good)


class EnvTagTest(unittest.TestCase):
    def test_pulumi_field_with_env_tag_flagged(self):
        src = 'type C struct {\n\tTok *string `pulumi:"tok,optional" env:"X_TOKEN"`\n\tOk *string `pulumi:"ok,optional"`\n}\n'
        self.assertEqual([h[0] for h in verify.find_env_tags(src)], [2])

    def test_env_tag_without_pulumi_ignored_and_comment_text(self):
        self.assertEqual(verify.find_env_tags('type F struct { A string `env:"A"` }\n'), [])
        self.assertEqual(verify.find_env_tags('// use env:"X" is wrong\nx := 1\n'), [])

    def test_scan_skips_tests_and_vendor(self):
        with helpers.tempfile.TemporaryDirectory() as d:
            bad = 'package p\ntype C struct{ T string `pulumi:"t" env:"T"` }\n'
            helpers.write(d, "pkg/c.go", bad)
            helpers.write(d, "pkg/c_test.go", bad)
            helpers.write(d, "vendor/x.go", bad)
            helpers.write(d, "shim/x.go", bad)
            self.assertEqual(len(verify.scan_env_tags(d)), 1)


class ExplicitProviderTest(unittest.TestCase):
    CASES = [
        (".ts", 'const p = new modal.Provider("p", {});', True),
        (".js", "const p = new Provider('p');", True),
        (".ts", 'const r = new modal.Secret("s", {});', False),
        (".ts", '// new modal.Provider("x")', False),
        (".py", 'p = pulumi_modal.Provider("p")', True),
        (".py", 'r = pulumi.ResourceOptions(provider=p)', False),
        (".py", 'x = modal.ProviderResource("p")', False),
        (".go", 'p, err := modal.NewProvider(ctx, "p", &modal.ProviderArgs{})', True),
        (".go", 'r, err := modal.NewSecret(ctx, "s", nil)', False),
        (".cs", 'var p = new Modal.Provider("p");', True),
        (".cs", 'var p = new Provider("p", new ProviderArgs());', True),
        (".cs", 'var s = new Modal.Secret("s");', False),
        (".java", 'var p = new Provider("p", ProviderArgs.builder().build());', True),
        (".java", 'var s = new Secret("s");', False),
        (".yaml", "  prov:\n    type: pulumi:providers:modal\n", True),
        (".yml", "resources:\n  s:\n    type: modal:Secret\n", False),
        (".md", 'new modal.Provider("p")', False),
    ]

    def test_cases(self):
        for ext, snippet, want in self.CASES:
            self.assertEqual(bool(verify.find_explicit_provider(ext, snippet)), want, (ext, snippet))


class CredentialRequireTest(unittest.TestCase):
    CRED = {"tokenid", "tokensecret"}
    CASES = [
        (".ts", 'const t = config.requireSecret("tokenId");', ["tokenId"]),
        (".ts", 'const t = config.get("modal:tokenId");', ["modal:tokenId"]),
        (".ts", 'const n = config.require("volumeName");', []),
        (".ts", 'const fs = require("fs");', []),
        (".py", 'x = config.require_secret("token_secret")', ["token_secret"]),
        (".py", 'x = config.get_secret("TokenId")', ["TokenId"]),
        (".py", 'x = config.require("queue_name")', []),
        (".go", 'tok := cfg.RequireSecret("tokenId")', ["tokenId"]),
        (".go", 'tok := config.Get(ctx, "tokenSecret")', ["tokenSecret"]),
        (".cs", 'var t = config.RequireSecret("tokenId");', ["tokenId"]),
        (".java", 'var t = config.requireSecret("tokenSecret");', ["tokenSecret"]),
        # App secrets stored in a Secret resource are not the provider's credentials: must pass.
        (".ts", 'const hf = config.requireSecret("hfToken");', []),
        (".py", 'worker_api_key = config.require_secret("workerApiKey")', []),
        (".go", 'hfToken := cfg.RequireSecret("hfToken")', []),
        (".cs", 'var k = config.RequireSecret("workerApiKey");', []),
        (".java", 'var n = config.require("secretName");', []),
        (".ts", '// config.requireSecret("tokenId")', []),
        (".yaml", 'key: ${tokenId}', []),
    ]

    def test_cases(self):
        for ext, snippet, want in self.CASES:
            self.assertEqual([k for _, k, _ in verify.find_credential_requires(ext, snippet, self.CRED)], want, (ext, snippet))

    def test_no_provider_credentials_means_nothing_flagged(self):
        self.assertEqual(verify.find_credential_requires(".ts", 'config.requireSecret("tokenId")', set()), [])

    def test_names_from_schema(self):
        schema = {"config": {"variables": {"tokenId": {"secret": True}, "api_key": {}, "serverAddr": {}}}}
        self.assertEqual(verify.provider_credential_names(schema), {"tokenid", "apikey"})
        self.assertEqual(verify.norm_key("modal:token_id"), "tokenid")

    def test_scan_examples_and_steps(self):
        with helpers.tempfile.TemporaryDirectory() as d:
            helpers.write(d, "examples/a-ts/index.ts", 'const p = new modal.Provider("p");\nconfig.requireSecret("tokenId");\n')
            helpers.write(d, "templates/b-ts/index.ts", 'const hf = config.requireSecret("hfToken");\n')
            helpers.write(d, "templates/b-go/main.go", 'modal.NewSecret(ctx, "s", nil)\n')
            helpers.write(d, "examples/a-ts/node_modules/x/index.ts", 'new a.Provider("x")')
            prov, cred = verify.scan_examples(d, {"tokenid"})
            self.assertEqual(len(prov), 1)
            self.assertIn("examples/a-ts/index.ts:1", prov[0])
            self.assertEqual(len(cred), 1)
            self.assertNotIn("hfToken", cred[0])
            gate = verify.Gate(os.path.join(d, "logs"), {})
            schema = {"config": {"variables": {"tokenId": {"secret": True}}}}
            verify.default_provider_steps(gate, schema, os.path.join(d, "provider"), d)
            st = {s["id"]: s["status"] for s in gate.steps}
            self.assertEqual(st, {"dp_env_defaults": "FAIL", "dp_no_required_config": "PASS", "dp_no_env_tags": "PASS",
                                  "dp_no_explicit_provider": "FAIL", "dp_no_credential_require": "FAIL"})
            self.assertEqual(gate.steps[0]["items"][0]["name"], "tokenId")

    def test_hf_token_template_alone_passes(self):
        with helpers.tempfile.TemporaryDirectory() as d:
            helpers.write(d, "templates/ml/index.ts", 'const hfToken = config.requireSecret("hfToken");\nconst k = config.requireSecret("workerApiKey");\n')
            gate = verify.Gate(os.path.join(d, "logs"), {})
            schema = {"config": {"variables": {"tokenId": {"secret": True, "defaultInfo": {"environment": ["T"]}}}}}
            verify.default_provider_steps(gate, schema, os.path.join(d, "provider"), d)
            self.assertTrue(gate.ok, gate.steps)


class GoModTest(unittest.TestCase):
    SDK = "github.com/pulumi-labs/pulumi-modal/sdk/go/modal"

    def test_parse_blocks_and_replace(self):
        g = gomod.parse_gomod('module ${PROJECT} // c\n\ngo 1.25\n\nrequire (\n\ta.b/c v1.0.0 // indirect\n\td.e/f v2.0.0\n)\n'
                              'require x.y/z v0.1.0\nreplace (\n\tx.y/z v0.1.0 => ../z\n)\nreplace a.b/c => ./c\n')
        self.assertEqual(g["module"], "${PROJECT}")
        self.assertEqual(g["requires"], [("a.b/c", "v1.0.0"), ("d.e/f", "v2.0.0"), ("x.y/z", "v0.1.0")])
        self.assertEqual(g["replaces"], [("x.y/z", "../z"), ("a.b/c", "./c")])

    def test_sdk_module(self):
        self.assertEqual(gomod.check_sdk_gomod("module %s\n" % self.SDK, self.SDK), [])
        self.assertTrue(gomod.check_sdk_gomod("module github.com/pulumi/pulumi-modal/sdk/go/modal\n", self.SDK))
        self.assertTrue(gomod.check_sdk_gomod("", self.SDK))

    def test_example_gomod(self):
        ok = "module e\nrequire %s v0.1.0\nrequire github.com/pulumi/pulumi/sdk/v3 v3.1.0\n" % self.SDK
        self.assertEqual(gomod.check_example_gomod(ok, self.SDK, "modal"), [])
        old = "module e\nrequire github.com/pulumi/pulumi-modal/sdk/go/modal v0.1.0\n"
        self.assertEqual(len(gomod.check_example_gomod(old, self.SDK, "modal")), 1)
        local = ok + "replace %s => ../../sdk/go/modal\n" % self.SDK
        self.assertEqual(gomod.check_example_gomod(local, self.SDK, "modal"), [])
        for bad in ("replace %s => /tmp/fork\n" % self.SDK, "replace github.com/x/y => ../y\n",
                    "replace %s => github.com/me/fork v1.0.0\n" % self.SDK):
            self.assertEqual(len(gomod.check_example_gomod(ok + bad, self.SDK, "modal")), 1, bad)


class GoHelpersTest(helpers.TempDirMixin, unittest.TestCase):
    def test_example_dirs(self):
        helpers.write(self.tmp, "examples/a-go/go.mod", "module a\n")
        helpers.write(self.tmp, "examples/a-go/main.go", "package main\n")
        helpers.write(self.tmp, "examples/b-go/main.go", "package main\n")      # no go.mod
        helpers.write(self.tmp, "templates/c-go/go.mod", "module ${PROJECT}\n")
        helpers.write(self.tmp, "examples/d-ts/index.ts", "x")
        helpers.write(self.tmp, "examples/a-go/node_modules/x/go.mod", "module x\n")
        self.assertEqual(verify.go_example_dirs(self.tmp), ["examples/a-go", "examples/b-go", "templates/c-go"])

    def test_consumer_main_and_symbol(self):
        helpers.write(self.tmp, "sdk/doc.go", "package modal\n")
        helpers.write(self.tmp, "sdk/a.go", "package modal\n\ntype Secret struct{}\n\nfunc (s *Secret) M() {}\n\nfunc NewSecret() {}\n")
        helpers.write(self.tmp, "sdk/a_test.go", "package modal\nfunc TestX() {}\n")
        sym = verify.first_exported_symbol(os.path.join(self.tmp, "sdk"))
        self.assertEqual(sym, ("func", "NewSecret", "modal"))
        main = verify.consumer_main_go("github.com/pulumi-labs/pulumi-modal/sdk/go/modal", sym)
        self.assertIn('import sdk "github.com/pulumi-labs/pulumi-modal/sdk/go/modal"', main)
        self.assertIn("var _ = sdk.NewSecret", main)
        helpers.write(self.tmp, "t/t.go", "package t\ntype Only struct{}\n")
        self.assertIn("var _ *sdk.Only", verify.consumer_main_go("p", verify.first_exported_symbol(os.path.join(self.tmp, "t"))))
        self.assertIsNone(verify.consumer_main_go("p", None))

    def test_sdk_dir_from_schema(self):
        s = {"language": {"go": {"importBasePath": "github.com/pulumi-labs/pulumi-x/sdk/go/xx"}}}
        self.assertEqual(verify.go_sdk_dir_for(self.tmp, s), "sdk/go/xx")

    def test_metadata_warnings(self):
        n = names.derive("modal")
        old = helpers.schema_for("modal", publisher="Pulumi", repository="https://github.com/pulumi/autoregistry")
        old["language"]["go"]["importBasePath"] = "github.com/pulumi/pulumi-modal/sdk/go/modal"
        old["pluginDownloadURL"] = "github://api.github.com/pulumi/pulumi-modal"
        w = verify.metadata_warnings(old, n)
        self.assertEqual(len(w), 4)
        self.assertEqual(verify.metadata_warnings(helpers.schema_for("modal"), n), [])


class GateCmdTest(helpers.TempDirMixin, unittest.TestCase):
    def gate(self):
        return verify.Gate(os.path.join(self.tmp, "logs"), dict(os.environ))

    def test_pass_fail_skip_and_error_patterns(self):
        g = self.gate()
        self.assertEqual(g.cmd("a", "a", ["sh", "-c", "echo ok"], self.tmp)[0], "PASS")
        self.assertEqual(g.cmd("b", "b", ["sh", "-c", "echo boom; exit 3"], self.tmp)[0], "FAIL")
        self.assertEqual(g.cmd("c", "c", ["sh", "-c", "echo 'skipping nodejs SDK build (npm not installed)'"], self.tmp, fail_on_skip=True)[0], "FAIL")
        self.assertEqual(g.cmd("c2", "c", ["sh", "-c", "echo 'skipping x'"], self.tmp)[0], "PASS")
        self.assertEqual(g.cmd("d", "d", ["sh", "-c", "echo 'error: could not generate'"], self.tmp, error_res=verify.CODEGEN_ERROR_RES)[0], "FAIL")
        self.assertEqual(g.cmd("e", "e", ["sh", "-c", "echo 'go: finding module for package x'; echo 'warning: deprecated'"], self.tmp,
                               error_res=verify.CODEGEN_ERROR_RES, collect_warnings=True)[0], "PASS")
        self.assertEqual(g.steps[-1]["warnings"], ["warning: deprecated"])
        self.assertEqual(g.cmd("f", "f", ["sh", "-c", "echo 'go: github.com/x/y@v1: reading go.mod: 404'"], self.tmp, error_res=verify.CODEGEN_ERROR_RES)[0], "FAIL")
        self.assertEqual(g.cmd("g", "g", ["/nonexistent-binary"], self.tmp)[0], "FAIL")
        self.assertTrue(os.path.exists(g.steps[0]["log"]))
        self.assertFalse(g.ok)

    def test_java_warn_lines_and_cli_notice_are_warnings_not_failures(self):
        g = self.gate()
        script = ("echo 'WARN: secret property tokenId (property of config) does not have an Output type'; "
                  "echo \"warning: You are running a very old version of Pulumi. upgrade from '3.241.0' to '3.268.0'\"")
        st, _ = g.cmd("j", "j", ["sh", "-c", script], self.tmp, error_res=verify.CODEGEN_ERROR_RES, collect_warnings=True)
        self.assertEqual(st, "PASS")
        self.assertEqual(len(g.steps[-1]["warnings"]), 1)
        self.assertIn("tokenId", g.steps[-1]["warnings"][0])

    def test_consumer_smoke_and_go_examples_with_fake_go(self):
        bindir = os.path.join(self.tmp, "bin")
        helpers.write(self.tmp, "bin/go", "#!/bin/sh\necho \"go $@\" >> %s/calls.txt\nif [ \"$1\" = build ] && [ -f FAIL ]; then echo broken; exit 1; fi\n" % self.tmp)
        os.chmod(os.path.join(bindir, "go"), 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        sdk = os.path.join(self.tmp, "sdk/go/modal")
        helpers.write(self.tmp, "sdk/go/modal/a.go", "package modal\n\nfunc NewSecret() {}\n")
        helpers.write(self.tmp, "repo/examples/a-go/main.go", "package main\n")
        helpers.write(self.tmp, "repo/examples/a-go/go.mod", "module ${PROJECT}\n")
        g = verify.Gate(os.path.join(self.tmp, "logs"), env)
        sc = os.path.join(self.tmp, "scratch")
        os.makedirs(sc)
        mod = "github.com/pulumi-labs/pulumi-modal/sdk/go/modal"
        verify.consumer_smoke(g, "go_consumer_smoke", sdk, mod, sc)
        verify.go_examples(g, os.path.join(self.tmp, "repo"), sdk, mod, sc)
        self.assertEqual([(s["id"], s["status"]) for s in g.steps], [("go_consumer_smoke", "PASS"), ("go_examples", "PASS")])
        with open(os.path.join(self.tmp, "calls.txt")) as f:
            calls = f.read()
        self.assertIn("go mod init example.com/consumer", calls)
        self.assertIn("-replace=%s=%s" % (mod, sdk), calls)
        self.assertEqual(g.steps[1]["items"][0]["name"], "examples/a-go")

    def test_codegen_patterns(self):
        bad = ["error: Failed to generate", "Error: x", "panic: oops", "go: cannot find module providing package x",
               "go: github.com/a/b@v1.0.0: reading go.mod: 404", "go: updates to go.mod needed; to update it:",
               "Traceback (most recent call last):", "failed to load schema"]
        for line in bad:
            self.assertTrue(any(r.search(line) for r in verify.CODEGEN_ERROR_RES), line)
        for line in ["go: downloading github.com/x v1.0.0", "go: finding module for package a/b", "Generating nodejs SDK...",
                     "warning: something deprecated"]:
            self.assertFalse(any(r.search(line) for r in verify.CODEGEN_ERROR_RES), line)

    def test_toolchain_step_fails_when_missing(self):
        g = verify.Gate(os.path.join(self.tmp, "logs"), {"PATH": os.path.join(self.tmp, "empty")})
        os.makedirs(os.path.join(self.tmp, "empty"))
        verify.toolchain_step(g, self.tmp, "/prefix")
        step = g.steps[0]
        self.assertEqual(step["status"], "FAIL")
        missing = [i["name"] for i in step["items"] if i["status"] == "FAIL"]
        for t in ("go", "pulumi", "make", "jq", "node", "npm", "dotnet", "java", "gradle"):
            self.assertIn(t, missing)


class ReportAndPushableTest(helpers.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo = helpers.build_repo(self.tmp)
        self.rd = os.path.join(self.tmp, "reports")
        self.state = verify.git_state(self.repo)

    def write_report(self, **over):
        steps = [{"id": i, "status": "PASS"} for i in verify.REQUIRED_STEPS]
        r = {"mode": "repo", "head_sha": self.state["head"], "tree_sha": self.state["tree"], "porcelain": [],
             "steps": steps, "overall": "PASS"}
        r.update(over)
        os.makedirs(self.rd, exist_ok=True)
        with open(os.path.join(self.rd, "pulumi-modal-%s.json" % self.state["head"]), "w") as f:
            json.dump(r, f)
        return r

    def test_no_report(self):
        ok, msg = verify.assert_pushable(self.repo, self.rd)
        self.assertFalse(ok)
        self.assertIn("no report for HEAD", msg)

    def test_passing_report(self):
        self.write_report()
        ok, msg = verify.assert_pushable(self.repo, self.rd)
        self.assertTrue(ok, msg)

    def test_dirty_worktree(self):
        self.write_report()
        helpers.write(self.repo, "stray.txt", "x")
        ok, msg = verify.assert_pushable(self.repo, self.rd)
        self.assertFalse(ok)
        self.assertIn("dirty", msg)

    def test_new_commit_invalidates(self):
        self.write_report()
        helpers.write(self.repo, "more.txt", "x")
        helpers.commit_all(self.repo, "next")
        ok, msg = verify.assert_pushable(self.repo, self.rd)
        self.assertFalse(ok)
        self.assertIn("no report for HEAD", msg)

    def test_failing_missing_and_mismatched(self):
        r = self.write_report()
        r["steps"][3]["status"] = "FAIL"
        self.write_report(steps=r["steps"])
        ok, msg = verify.assert_pushable(self.repo, self.rd)
        self.assertFalse(ok)
        self.assertIn("make_lint", msg)
        self.write_report(steps=[s for s in r["steps"] if s["id"] != "go_consumer_smoke"])
        self.assertIn("lacks required", verify.assert_pushable(self.repo, self.rd)[1])
        self.write_report(tree_sha="0" * 40)
        self.assertIn("does not match", verify.assert_pushable(self.repo, self.rd)[1])
        self.write_report(mode="monorepo")
        self.assertIn("not a gate report", verify.assert_pushable(self.repo, self.rd)[1])
        self.write_report(porcelain=[" M x"])
        self.assertIn("dirty worktree", verify.assert_pushable(self.repo, self.rd)[1])
        self.write_report(overall="FAIL")
        self.assertFalse(verify.assert_pushable(self.repo, self.rd)[0])

    def test_cli_assert_pushable_exit_codes(self):
        args = ["--assert-pushable", "--repo", self.repo, "--report-dir", self.rd]
        self.assertEqual(helpers.quiet(verify.main, args), 1)
        self.write_report()
        self.assertEqual(helpers.quiet(verify.main, args), 0)

    def test_report_files_and_markdown(self):
        gate = verify.Gate(os.path.join(self.tmp, "logs"), {})
        gate.add("a", "A", "PASS")
        gate.add("b", "B", "FAIL", "boom\nmore", items=[{"name": "i", "status": "FAIL", "detail": "d"}],
                 tail="last line", warnings=["w1"])
        report, jp, mp = verify.finish_report(gate, "repo", self.repo, self.state, {"go": "go1"}, "t0", self.rd, "pulumi-modal-" + self.state["head"])
        self.assertEqual(report["overall"], "FAIL")
        with open(jp) as f:
            data = json.load(f)
        for k in ("head_sha", "tree_sha", "porcelain", "tools", "steps", "skill", "started", "finished"):
            self.assertIn(k, data)
        with open(mp) as f:
            md = f.read()
        self.assertIn("| FAIL | b |", md)
        self.assertIn("last line", md)
        self.assertIn("w1", md)
        self.assertTrue(jp.startswith(self.rd) and not jp.startswith(self.repo))

    def test_empty_gate_is_not_a_pass(self):
        report, _, _ = verify.finish_report(verify.Gate(os.path.join(self.tmp, "l"), {}), "repo", self.repo, self.state, {}, "t", self.rd, "x")
        self.assertEqual(report["overall"], "FAIL")


if __name__ == "__main__":
    unittest.main()
