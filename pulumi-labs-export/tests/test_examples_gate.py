import json
import os
import shutil
import stat
import tempfile
import unittest

import helpers
import examples_gate as eg
import verify


def fake_tool(bindir, name, script):
    """Write an executable shell script `name` into bindir."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class ClassifyTest(unittest.TestCase):
    def test_runtime_scalar_and_structured(self):
        self.assertEqual(eg.runtime_of("name: a\nruntime: nodejs\n"), "nodejs")
        self.assertEqual(eg.runtime_of("name: a\nruntime:\n  name: python\n  options:\n    toolchain: pip\n"), "python")
        self.assertEqual(eg.runtime_of("runtime: jvm # java\n"), "jvm")
        self.assertIsNone(eg.runtime_of("name: a\n"))

    def test_name_tokens(self):
        self.assertEqual(eg.lang_of_name("chroma-go-rag"), "go")
        self.assertEqual(eg.lang_of_name("modal-cloud-typescript"), "ts")
        self.assertEqual(eg.lang_of_name("x-dotnet"), "csharp")
        self.assertIsNone(eg.lang_of_name("plain"))

    def test_classify_prefers_the_runtime_and_falls_back_to_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            helpers.write(d, "examples/a-ts/Pulumi.yaml", "runtime: nodejs\n")
            helpers.write(d, "examples/odd-name/Pulumi.yaml", "runtime:\n  name: go\n")
            helpers.write(d, "templates/b-java/Pulumi.yaml", "runtime: jvm\n")
            helpers.write(d, "templates/c-python/README.md", "no manifest\n")
            helpers.write(d, "templates/mystery/README.md", "x\n")
            got = {e["rel"]: (e["lang"], e["kind"]) for e in eg.classify(d)}
            self.assertEqual(got, {"examples/a-ts": ("ts", "example"), "examples/odd-name": ("go", "example"),
                                   "templates/b-java": ("java", "template"), "templates/c-python": ("python", "template"),
                                   "templates/mystery": ("unknown", "template")})
            langs = eg.sdk_languages(eg.classify(d))
            self.assertEqual(langs, ["nodejs", "python", "go", "java"])

    def test_no_examples_means_no_sdks(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(eg.classify(d), [])
            self.assertEqual(eg.sdk_languages([]), [])


class RewriteTest(unittest.TestCase):
    def test_ts_dependency_points_at_the_local_sdk(self):
        pj = json.dumps({"dependencies": {"@pulumi/clerk": "^0.1.0", "@pulumi/pulumi": "^3.0.0"}})
        new = json.loads(eg.ts_dep_rewrite(pj, "@pulumi/clerk", "/sdk/nodejs"))
        self.assertEqual(new["dependencies"], {"@pulumi/clerk": "file:/sdk/nodejs", "@pulumi/pulumi": "^3.0.0"})
        self.assertIsNone(eg.ts_dep_rewrite(pj, "@pulumi/other", "/x"))

    def test_csproj_package_becomes_a_project_reference(self):
        proj = ('<Project><ItemGroup>\n    <PackageReference Include="Pulumi" Version="3.*" />\n'
                '    <PackageReference Include="Pulumi.Clerk" Version="0.1.*" />\n</ItemGroup></Project>')
        new = eg.csproj_rewrite(proj, "Pulumi.Clerk", "/sdk/dotnet/Pulumi.Clerk.csproj")
        self.assertIn('<PackageReference Include="Pulumi" Version="3.*" />', new)
        self.assertNotIn("Pulumi.Clerk\" Version", new)
        self.assertIn('<ProjectReference Include="/sdk/dotnet/Pulumi.Clerk.csproj" />', new)
        self.assertIsNone(eg.csproj_rewrite(proj, "Pulumi.Other", "/x"))
        multi = '<ItemGroup><PackageReference Include="Pulumi.Clerk"><Version>0.1.0</Version></PackageReference></ItemGroup>'
        self.assertIn("ProjectReference", eg.csproj_rewrite(multi, "Pulumi.Clerk", "/x.csproj"))
        self.assertNotIn("<Version>", eg.csproj_rewrite(multi, "Pulumi.Clerk", "/x.csproj"))

    POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><properties><maven.compiler.source>17</maven.compiler.source>'
           '<pv>1.1.0</pv></properties><dependencies>'
           '<dependency><groupId>com.pulumi</groupId><artifactId>pulumi</artifactId><version>${pv}</version></dependency>'
           '<dependency><groupId>com.pulumi</groupId><artifactId>clerk</artifactId><version>[0.1,1.0)</version></dependency>'
           '<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13</version><scope>test</scope></dependency>'
           '</dependencies></project>')

    def test_pom_to_gradle_swaps_the_sdk_for_the_local_jar(self):
        info = eg.pom_info(self.POM)
        self.assertEqual(info["deps"][0], ("com.pulumi", "pulumi", "1.1.0", "compile"))
        gradle = eg.gradle_for_pom(info, {"clerk"}, "/sdk/clerk.jar")
        self.assertIn("implementation 'com.pulumi:pulumi:1.1.0'", gradle)
        self.assertNotIn("com.pulumi:clerk", gradle)
        self.assertIn("testImplementation 'junit:junit:4.13'", gradle)
        self.assertIn("implementation files('/sdk/clerk.jar')", gradle)
        self.assertIn("toVersion('17')", gradle)

    def test_bad_pom_is_a_value_error(self):
        with self.assertRaises(ValueError):
            eg.pom_info("<project>")


class PythonTest(unittest.TestCase):
    def test_syntax_errors_are_found_without_writing_files(self):
        with tempfile.TemporaryDirectory() as d:
            helpers.write(d, "ok/__main__.py", "import pulumi\nx = '${PROJECT}'\n")
            helpers.write(d, "bad/__main__.py", "def f(:\n")
            self.assertEqual(eg.py_syntax_errors(eg.py_files(os.path.join(d, "ok")), d), [])
            errs = eg.py_syntax_errors(eg.py_files(os.path.join(d, "bad")), d)
            self.assertEqual(len(errs), 1)
            self.assertTrue(errs[0].startswith("bad/__main__.py:1"))
            self.assertEqual([f for f in os.listdir(os.path.join(d, "ok")) if f != "__main__.py"], [])

    def test_sdk_references(self):
        src = ("import pulumi\nimport pulumi_clerk as clerk\nfrom pulumi_clerk import Org, config\n"
               "from pulumi_clerk.sub import Thing\nimport pulumi_other\n"
               "o = clerk.Organization('x', name='n')\nc = clerk.config.token.nope\npulumi.export('a', o.id)\n")
        self.assertEqual(eg.sdk_references(src, "pulumi_clerk"),
                         ["pulumi_clerk", "pulumi_clerk.Org", "pulumi_clerk.Organization", "pulumi_clerk.config",
                          "pulumi_clerk.config.token", "pulumi_clerk.sub", "pulumi_clerk.sub.Thing"])

    def test_resolver_flags_missing_names(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            helpers.write(d, "fakesdk/__init__.py", "class Real: pass\n")
            helpers.write(d, "refs.json", json.dumps(["fakesdk", "fakesdk.Real", "fakesdk.Missing", "nomodule.X"]))
            p = subprocess.run([sys.executable, "-c", eg.RESOLVER, os.path.join(d, "refs.json")], cwd=d, capture_output=True, text=True)
            bad = {r for r, _ in json.loads(p.stdout)}
            self.assertEqual(bad, {"fakesdk.Missing", "nomodule.X"})

    def test_sdk_module_is_the_package_directory(self):
        with tempfile.TemporaryDirectory() as d:
            helpers.write(d, "pulumi_modal/__init__.py", "")
            helpers.write(d, "pyproject.toml", "")
            self.assertEqual(eg.python_sdk_module(d), "pulumi_modal")


class SummarizeTest(unittest.TestCase):
    def test_picks_error_lines_and_drops_scratch_paths(self):
        text = "Picked up JAVA_TOOL_OPTIONS: -Dx\n/tmp/a/b/ex-examples_clerk-go/main.go:21:5: unknown field redirectUris\nFAIL\n"
        self.assertEqual(eg.summarize(text), "main.go:21:5: unknown field redirectUris")

    def test_falls_back_to_the_last_lines(self):
        self.assertEqual(eg.summarize("one\ntwo\nthree"), "two | three")


class FakeBinsTest(unittest.TestCase):
    """The per-language compile steps with fake npm/node/dotnet/gradle on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.calls = os.path.join(self.tmp, "calls.txt")
        for tool in ("npm", "node", "dotnet", "gradle"):
            fake_tool(self.bin, tool, 'echo "%s $@ [$PWD]" >> %s\nif [ -f BREAK ]; then echo "error TS2353: nope"; exit 1; fi\n' % (tool, self.calls))
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"])
        self.gate = verify.Gate(os.path.join(self.tmp, "logs"), self.env)
        self.root = os.path.join(self.tmp, "prov")
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch)

    def calls_text(self):
        with open(self.calls) as f:
            return f.read()

    def test_compile_ts_links_the_sdk_and_reports_per_example(self):
        sdk = os.path.join(self.tmp, "sdk/nodejs")
        helpers.write(sdk, "package.json", json.dumps({"name": "@pulumi/clerk"}))
        for name, brk in (("ok-ts", False), ("bad-ts", True)):
            helpers.write(self.root, "examples/%s/package.json" % name, json.dumps({"dependencies": {"@pulumi/clerk": "^0.1.0"}}))
            helpers.write(self.root, "examples/%s/index.ts" % name, "export {};\n")
            if brk:
                helpers.write(self.root, "examples/%s/BREAK" % name, "")
        entries = [{"rel": "examples/ok-ts", "lang": "ts"}, {"rel": "examples/bad-ts", "lang": "ts"}]
        # the shared TypeScript install must produce tsc.js: make the fake npm create it
        fake_tool(self.bin, "npm", 'echo "npm $@ [$PWD]" >> %s\nmkdir -p node_modules/typescript/bin && touch node_modules/typescript/bin/tsc\n'
                  'if [ -f BREAK ]; then exit 1; fi\n' % self.calls)
        eg.compile_ts(self.gate, self.root, entries, sdk, self.scratch)
        step = self.gate.steps[-1]
        self.assertEqual(step["id"], "compile_ts")
        self.assertEqual(step["status"], "FAIL")
        self.assertEqual({i["name"]: i["status"] for i in step["items"]}, {"examples/ok-ts": "PASS", "examples/bad-ts": "FAIL"})
        with open(os.path.join(self.scratch, "ex-examples_ok-ts", "package.json")) as f:
            self.assertEqual(json.load(f)["dependencies"]["@pulumi/clerk"], "file:" + sdk)
        self.assertIn("--install-links", self.calls_text())
        # the repository copy is untouched
        with open(os.path.join(self.root, "examples/ok-ts/package.json")) as f:
            self.assertEqual(json.load(f)["dependencies"]["@pulumi/clerk"], "^0.1.0")
        self.assertEqual(eg.failure_report("clerk", self.gate.steps)["keys"], ["clerk:compile_ts:examples/bad-ts"])

    def test_compile_ts_fails_a_project_that_does_not_depend_on_the_sdk(self):
        sdk = os.path.join(self.tmp, "sdk/nodejs")
        helpers.write(sdk, "package.json", json.dumps({"name": "@pulumi/clerk"}))
        helpers.write(self.root, "templates/t-ts/package.json", json.dumps({"dependencies": {"@pulumi/wrong": "1"}}))
        fake_tool(self.bin, "npm", "mkdir -p node_modules/typescript/bin && touch node_modules/typescript/bin/tsc\n")
        eg.compile_ts(self.gate, self.root, [{"rel": "templates/t-ts", "lang": "ts"}], sdk, self.scratch)
        item = self.gate.steps[-1]["items"][0]
        self.assertEqual(item["status"], "FAIL")
        self.assertIn("@pulumi/clerk", item["detail"])

    def test_compile_csharp_uses_a_project_reference(self):
        sdk = os.path.join(self.tmp, "sdk/dotnet")
        helpers.write(sdk, "Pulumi.Clerk.csproj", "<Project/>")
        helpers.write(self.root, "examples/a-csharp/A.csproj", '<Project><ItemGroup><PackageReference Include="Pulumi.Clerk" Version="0.1.*" /></ItemGroup></Project>')
        helpers.write(self.root, "examples/b-csharp/B.csproj", '<Project><ItemGroup><PackageReference Include="Pulumi" Version="3.*" /></ItemGroup></Project>')
        eg.compile_csharp(self.gate, self.root, [{"rel": "examples/a-csharp", "lang": "csharp"}, {"rel": "examples/b-csharp", "lang": "csharp"}],
                          sdk, self.scratch)
        items = {i["name"]: i for i in self.gate.steps[-1]["items"]}
        self.assertEqual(items["examples/a-csharp"]["status"], "PASS")
        self.assertEqual(items["examples/b-csharp"]["status"], "FAIL")
        with open(os.path.join(self.scratch, "ex-examples_a-csharp", "A.csproj")) as f:
            self.assertIn("ProjectReference", f.read())

    def test_compile_java_writes_a_gradle_build_with_the_sdk_jar(self):
        sdk = os.path.join(self.tmp, "sdk/java")
        helpers.write(sdk, "build/libs/com.pulumi.clerk.jar", "jar")
        helpers.write(sdk, "build/libs/com.pulumi.clerk-sources.jar", "jar")
        helpers.write(self.root, "examples/j-java/pom.xml", ('<project><dependencies><dependency><groupId>com.pulumi</groupId>'
                      '<artifactId>pulumi</artifactId><version>1.+</version></dependency><dependency><groupId>com.pulumi</groupId>'
                      '<artifactId>clerk</artifactId><version>0.1</version></dependency></dependencies></project>'))
        helpers.write(self.root, "examples/nopom-java/App.java", "class A {}")
        eg.compile_java(self.gate, self.root, [{"rel": "examples/j-java", "lang": "java"}, {"rel": "examples/nopom-java", "lang": "java"}],
                        sdk, {"clerk"}, self.scratch)
        items = {i["name"]: i["status"] for i in self.gate.steps[-1]["items"]}
        self.assertEqual(items, {"examples/j-java": "PASS", "examples/nopom-java": "FAIL"})
        with open(os.path.join(self.scratch, "ex-examples_j-java", "build.gradle")) as f:
            text = f.read()
        self.assertIn("com.pulumi.clerk.jar", text)
        self.assertNotIn("sources.jar", text)
        self.assertNotIn("com.pulumi:clerk", text)

    def test_compile_yaml_checks_tokens_against_the_schema(self):
        schema = {"name": "clerk", "resources": {"clerk:index/organization:Organization": {}}, "functions": {"clerk:index/getOrg:getOrg": {}}}
        ok = "resources:\n  o:\n    type: clerk:index:Organization\n    properties: {}\n  b:\n    type: aws:s3:Bucket\n"
        bad = "resources:\n  o:\n    type: clerk:index:Team\nvariables:\n  v:\n    fn::invoke:\n      function: clerk:index:getOrg\n"
        helpers.write(self.root, "examples/a-yaml/Pulumi.yaml", ok)
        helpers.write(self.root, "examples/b-yaml/Pulumi.yaml", bad)
        eg.compile_yaml(self.gate, self.root, [{"rel": "examples/a-yaml", "lang": "yaml"}, {"rel": "examples/b-yaml", "lang": "yaml"}], schema)
        items = {i["name"]: i for i in self.gate.steps[-1]["items"]}
        self.assertEqual((items["examples/a-yaml"]["status"], items["examples/b-yaml"]["status"]), ("PASS", "FAIL"))
        self.assertIn("clerk:index:Team", items["examples/b-yaml"]["detail"])
        self.assertNotIn("getOrg", items["examples/b-yaml"]["detail"])


class FailureKeysTest(unittest.TestCase):
    def steps(self):
        return [
            {"id": "toolchain", "status": "PASS", "items": [], "detail": ""},
            {"id": "compile_go", "status": "FAIL", "detail": "x", "items": [
                {"name": "examples/team-auth-go", "path": "examples/team-auth-go", "status": "FAIL", "detail": "unknown field"},
                {"name": "examples/ok-go", "status": "PASS", "detail": ""}]},
            {"id": "dp_no_explicit_provider", "status": "FAIL", "detail": "x", "items": [
                {"name": "templates/clerk-cloud-ts/index.ts", "path": "templates/clerk-cloud-ts/index.ts", "status": "FAIL", "detail": "new clerk.Provider("},
                {"name": "templates/clerk-cloud-ts/index.ts", "path": "templates/clerk-cloud-ts/index.ts", "status": "FAIL", "detail": "second hit, same file"}]},
            {"id": "build_dotnet_sdk", "status": "FAIL", "detail": "exit 1 from make", "items": []},
            {"id": "compile_csharp", "status": "SKIP", "detail": "the .NET SDK did not build", "items": []},
        ]

    def test_keys_are_stable_sorted_and_deduplicated(self):
        rep = eg.failure_report("clerk", self.steps())
        self.assertEqual(rep["keys"], ["clerk:build_dotnet_sdk:sdk/dotnet", "clerk:compile_go:examples/team-auth-go",
                                       "clerk:dp_no_explicit_provider:templates/clerk-cloud-ts/index.ts"])
        self.assertEqual(rep["skipped_checks"], ["compile_csharp"])
        self.assertEqual(rep["counts"], {"build_dotnet_sdk": 1, "compile_go": 1, "dp_no_explicit_provider": 1})
        self.assertEqual(rep["failures"][1]["detail"], "unknown field")

    def test_default_provider_steps_carry_paths_without_line_numbers(self):
        gate = verify.Gate(tempfile.mkdtemp(), {})
        with tempfile.TemporaryDirectory() as d:
            helpers.write(d, "provider/pkg/c.go", 'package p\ntype C struct{ T string `pulumi:"t" env:"T"` }\ntype D struct{ T string `pulumi:"t" env:"T"` }\n')
            helpers.write(d, "templates/t-ts/index.ts", "const p = new clerk.Provider('x', {});\n")
            schema = {"config": {"variables": {"apiKey": {"type": "string"}}, "defaults": ["region"]}}
            verify.default_provider_steps(gate, schema, os.path.join(d, "provider"), d)
        rep = eg.failure_report("clerk", gate.steps)
        self.assertEqual(rep["keys"], ["clerk:dp_env_defaults:config/apiKey", "clerk:dp_no_env_tags:provider/pkg/c.go",
                                       "clerk:dp_no_explicit_provider:templates/t-ts/index.ts",
                                       "clerk:dp_no_required_config:config.defaults"])

    def test_provider_info_path(self):
        self.assertEqual(verify._info_path("resources.go:42 apiKey"), "provider/resources.go#apiKey")

    def test_markdown_lists_the_keys(self):
        steps = [dict(s, seconds=0, warnings=[], log_tail="", log=None) for s in self.steps()]
        report = {"overall": "FAIL", "mode": "monorepo", "repo": "r", "started": "a", "finished": "b", "steps": steps, "tools": {},
                  "examples": eg.failure_report("clerk", self.steps())}
        md = verify.render_markdown(report)
        self.assertIn("## Failure keys (3 enforced, 0 report-only, 1 skipped checks)", md)
        self.assertIn("- `clerk:compile_go:examples/team-auth-go`", md)


class ToolchainSubsetTest(unittest.TestCase):
    def test_only_the_requested_tools_are_probed(self):
        tmp = tempfile.mkdtemp()
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        g = verify.Gate(os.path.join(tmp, "logs"), {"PATH": empty})
        verify.toolchain_step(g, tmp, "/prefix", {"go", "pulumi"})
        probed = [i["name"] for i in g.steps[0]["items"]]
        self.assertEqual(probed, ["go", "pulumi"])

    def test_profile_flag_exists(self):
        with self.assertRaises(SystemExit):
            verify.main(["--monorepo", ".", "--provider", "x", "--profile", "bogus"])


class ProviderGoModLayoutTest(unittest.TestCase):
    """Issue #1248: a monorepo provider's go.mod may sit at lib/providers/<p>/go.mod or provider/go.mod."""

    def _pdir(self, *files):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for rel in files:
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            open(os.path.join(d, rel), "w").close()
        return d

    def test_legacy_layout(self):
        self.assertTrue(verify.provider_go_mod_present(self._pdir("provider/go.mod")))

    def test_top_level_layout(self):
        self.assertTrue(verify.provider_go_mod_present(self._pdir("go.mod", "provider/cmd/x/main.go")))

    def test_missing(self):
        self.assertFalse(verify.provider_go_mod_present(self._pdir("provider/cmd/x/main.go")))


if __name__ == "__main__":
    unittest.main()
