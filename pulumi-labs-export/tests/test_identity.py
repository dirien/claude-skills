import json
import os
import unittest

import helpers
import identity
import names

MOD = "github.com/pulumi/autoregistry/lib/providers/demo/provider"
HDR = helpers.GO_HEADER

OLD_PROVIDER_GO = HDR + '''package provider

func New() {
	_ = infer.NewProviderBuilder().
		WithDisplayName("Demo").
		WithDescription("Manage Demo.").
		WithRepository("https://github.com/pulumi/autoregistry").
		WithGoImportPath("github.com/pulumi/pulumi-demo/sdk/go/demo").
		WithPublisher("Pulumi").
		Build()
}
'''

NEW_PROVIDER_GO = HDR + '''package provider

func New() {
	_ = infer.NewProviderBuilder().
		WithDisplayName("Demo").
		WithDescription("Experimental: manage Demo.").
		WithRepository("https://github.com/pulumi-labs/pulumi-demo").
		WithLanguageMap(map[string]any{"nodejs": map[string]any{"respectSchemaVersion": true}}).
		WithGoImportPath("github.com/pulumi-labs/pulumi-demo/sdk/go/demo").
		WithPublisher("Pulumi Labs").
		WithPluginDownloadURL("github://api.github.com/pulumi-labs/pulumi-demo").
		Build()
}
'''

FILES = {
    "README.md": "# Demo\n\nnpm install @pulumi/demo\npip install pulumi_demo\ngo get github.com/pulumi/pulumi-demo/sdk/go/demo\n"
                 "dotnet add package Pulumi.Demo\n\n```xml\n<dependency>\n  <groupId>com.pulumi</groupId>\n  <artifactId>demo</artifactId>\n</dependency>\n```\n"
                 "See https://github.com/pulumi/autoregistry/tree/main/lib/providers/demo\n",
    "CHANGELOG.md": "# Changelog\n\n- Renamed @pulumi/demo.\n",
    "provider/cmd/pulumi-resource-demo/main.go": HDR + 'package main\n\nimport (\n\t"%s/pkg/provider"\n)\n\nfunc main() { provider.New() }\n' % MOD,
    "provider/pkg/provider/provider.go": OLD_PROVIDER_GO,
    "provider/pkg/provider/provider_test.go": HDR + 'package provider\n\nimport _ "%s/pkg/version"\n' % MOD,
    "provider/pkg/provider/metadata_test.go": HDR + 'package provider\n\nfunc TestMeta(t *testing.T) {\n'
        '\tif got := m["publisher"].(string); got != "Pulumi" {\n\t\tt.Errorf("Publisher = %q, want %q", got, "Pulumi")\n\t}\n'
        '\tconst want = "github.com/pulumi/pulumi-demo/sdk/go/demo"\n}\n',
    "examples/demo-ts/index.ts": 'import * as demo from "@pulumi/demo";\n',
    "examples/demo-ts/package.json": '{\n  "name": "demo-ts",\n  "dependencies": {"@pulumi/demo": "^0.1.0", "@pulumi/pulumi": "^3"}\n}\n',
    "examples/demo-py/__main__.py": "import pulumi_demo as demo\n",
    "examples/demo-py/requirements.txt": "pulumi_demo>=0.1.0,<1.0.0\n",
    "examples/demo-cs/Program.cs": "using Pulumi.Demo;\nusing Pulumi;\n",
    "examples/demo-cs/demo-cs.csproj": '<PackageReference Include="Pulumi" Version="3.*" />\n<PackageReference Include="Pulumi.Demo" Version="0.1.*" />\n',
    "examples/demo-java/pom.xml": "<groupId>com.pulumi</groupId>\n<artifactId>demo-java</artifactId>\n<dependency>\n  <groupId>com.pulumi</groupId>\n"
                                  "  <artifactId>demo</artifactId>\n</dependency>\n",
    "examples/demo-java/src/main/java/com/pulumi/example/App.java": "package com.pulumi.example;\nimport com.pulumi.Pulumi;\nimport com.pulumi.demo.Thing;\n",
    "examples/demo-go/main.go": HDR + 'package main\n\nimport (\n\t"github.com/pulumi/pulumi-demo/sdk/go/demo"\n\t"%s/pkg/version"\n)\n' % MOD,
    "examples/demo-go/go.mod": "module github.com/pulumi/autoregistry/lib/providers/demo/examples/demo-go\n\nrequire github.com/pulumi/pulumi-demo/sdk/go/demo v0.1.0\n",
    "templates/demo-cloud-go/go.mod": "module ${PROJECT}\n\nrequire github.com/pulumi/pulumi-demo/sdk/go/demo v0.1.0\n",
    "templates/demo-cloud-python/requirements.txt": "pulumi_demo\n",
    "provider/go.mod": "module %s\n\ngo 1.27\n" % MOD,
    "version.txt": "0.1.0\n",
}


def build_old_mono(root, files=None):
    mono = helpers.build_monorepo(root, "demo")  # then replace the provider dir with the OLD identity content
    base = os.path.join(mono, "lib", "providers", "demo")
    import shutil
    shutil.rmtree(base)
    for rel, content in (files or FILES).items():
        helpers.write(mono, "lib/providers/demo/" + rel, content)
    helpers.write(mono, "carved-providers.json", "[]\n")
    helpers.commit_all(mono, "old identity")
    return mono


class IdentityBase(helpers.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.mono = build_old_mono(self.tmp)
        self.pdir = os.path.join(self.mono, "lib/providers/demo")

    def read(self, rel):
        with open(os.path.join(self.pdir, rel), encoding="utf-8") as f:
            return f.read()

    def hits(self):
        return identity.scan(self.mono, "demo")

    def cats(self, hits=None):
        return {(h.rel, h.line, h.category) for h in (self.hits() if hits is None else hits)}

    def tree_bytes(self):
        out = {}
        for dp, _, fns in os.walk(self.pdir):
            for fn in fns:
                p = os.path.join(dp, fn)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.pdir)] = f.read()
        return out


class CheckTest(IdentityBase):
    def test_every_category_is_listed_with_file_and_line(self):
        got = self.cats()
        want = {
            ("README.md", 3, identity.CAT_NPM), ("README.md", 4, identity.CAT_PY), ("README.md", 5, identity.CAT_GO),
            ("README.md", 6, identity.CAT_NUGET), ("README.md", 10, identity.CAT_JAVA), ("README.md", 14, identity.CAT_REPO),
            ("CHANGELOG.md", 3, identity.CAT_NPM),
            ("provider/pkg/provider/provider.go", 8, identity.CAT_REPO), ("provider/pkg/provider/provider.go", 9, identity.CAT_GO),
            ("provider/pkg/provider/provider.go", 10, identity.CAT_PUB),
            ("provider/pkg/provider/metadata_test.go", 5, identity.CAT_PUB), ("provider/pkg/provider/metadata_test.go", 6, identity.CAT_PUB),
            ("provider/pkg/provider/metadata_test.go", 8, identity.CAT_GO),
            ("examples/demo-ts/index.ts", 1, identity.CAT_NPM), ("examples/demo-ts/package.json", 3, identity.CAT_NPM),
            ("examples/demo-py/__main__.py", 1, identity.CAT_PY), ("examples/demo-py/requirements.txt", 1, identity.CAT_PY),
            ("examples/demo-cs/Program.cs", 1, identity.CAT_NUGET), ("examples/demo-cs/demo-cs.csproj", 2, identity.CAT_NUGET),
            ("examples/demo-java/pom.xml", 4, identity.CAT_JAVA),
            ("examples/demo-java/src/main/java/com/pulumi/example/App.java", 3, identity.CAT_JAVA),
            ("examples/demo-go/main.go", 5, identity.CAT_GO), ("examples/demo-go/go.mod", 1, identity.CAT_REPO),
            ("examples/demo-go/go.mod", 3, identity.CAT_GO), ("templates/demo-cloud-go/go.mod", 3, identity.CAT_GO),
            ("templates/demo-cloud-python/requirements.txt", 1, identity.CAT_PY),
            ("docs/_index.md", 0, identity.CAT_DOCS), ("docs/installation-configuration.md", 0, identity.CAT_DOCS),
            ("carved-providers.json", 0, identity.CAT_CARVED),
        }
        self.assertEqual(got - {h for h in got if h[2] == identity.CAT_META}, want, got ^ want)

    def test_builder_metadata_is_flagged_when_missing(self):
        meta = [h for h in self.hits() if h.category == identity.CAT_META]
        texts = " | ".join(h.text for h in meta)
        self.assertIn("missing WithPluginDownloadURL", texts)
        self.assertIn("missing WithLanguageMap", texts)
        self.assertEqual({h.rel for h in meta}, {"provider/pkg/provider/provider.go"})

    def test_language_map_after_go_import_path_is_flagged(self):
        src = NEW_PROVIDER_GO.replace(
            '\t\tWithLanguageMap(map[string]any{"nodejs": map[string]any{"respectSchemaVersion": true}}).\n', "").replace(
            '\t\tWithPublisher("Pulumi Labs").\n', '\t\tWithPublisher("Pulumi Labs").\n\t\tWithLanguageMap(map[string]any{}).\n')
        helpers.write(self.mono, "lib/providers/demo/provider/pkg/provider/provider.go", src)
        meta = [h for h in self.hits() if h.category == identity.CAT_META]
        self.assertEqual(len(meta), 1)
        self.assertIn("after WithGoImportPath", meta[0].text)
        self.assertEqual(meta[0].line, 11)

    def test_wrong_plugin_download_url_is_flagged(self):
        src = NEW_PROVIDER_GO.replace("pulumi-labs/pulumi-demo\").\n\t\tBuild", "pulumi/pulumi-demo\").\n\t\tBuild")
        helpers.write(self.mono, "lib/providers/demo/provider/pkg/provider/provider.go", src)
        meta = [h for h in self.hits() if h.category == identity.CAT_META]
        self.assertEqual(len(meta), 1)
        self.assertIn("WithPluginDownloadURL is", meta[0].text)

    def test_missing_copyright_headers(self):
        helpers.write(self.mono, "lib/providers/demo/provider/pkg/provider/extra.go", "package provider\n")
        copy = [h for h in self.hits() if h.category == identity.CAT_COPY]
        self.assertEqual([(h.rel, h.line) for h in copy], [("provider/pkg/provider/extra.go", 1)])

    def test_the_monorepo_provider_import_path_is_not_a_hit(self):
        hits = self.hits()
        for rel in ("provider/cmd/pulumi-resource-demo/main.go", "provider/pkg/provider/provider_test.go"):
            self.assertEqual([h for h in hits if h.rel == rel], [], rel)
        self.assertFalse([h for h in hits if h.rel == "provider/go.mod"])

    def test_new_names_and_other_providers_are_not_hits(self):
        extra = ("using Pulumi.Labs.Demo;\nimport pulumi_labs_demo\n@pulumi-labs/demo\ncom.pulumi.labs.demo.Thing\n"
                 "github.com/pulumi-labs/pulumi-demo/sdk/go/demo\ngithub.com/pulumi/pulumi-demo-extra\n@pulumi/demo-extra\n"
                 "pulumi_demo_extra\nPulumi.DemoExtra\ncom.pulumi.demoextra\n<groupId>com.pulumi.labs</groupId>\n<artifactId>demo</artifactId>\n")
        helpers.write(self.mono, "lib/providers/demo/docs/_index.md", extra)
        helpers.write(self.mono, "lib/providers/demo/docs/installation-configuration.md", extra)
        hits = [h for h in self.hits() if h.rel.startswith("docs/")]
        self.assertEqual(hits, [])

    def test_hyphenated_provider_names(self):
        os.makedirs(os.path.join(self.tmp, "h"))
        mono = helpers.build_monorepo(os.path.join(self.tmp, "h"), "better-uptime")
        base = "lib/providers/better-uptime/"
        helpers.write(mono, base + "README.md", "Pulumi.BetterUptime\npulumi_better_uptime\n@pulumi/better-uptime\ncom.pulumi.betteruptime.Monitor\n"
                      "github.com/pulumi/pulumi-better-uptime/sdk/go/betteruptime\n")
        helpers.commit_all(mono, "x")
        cats = sorted(h.category for h in identity.scan(mono, "better-uptime") if h.rel == "README.md")
        self.assertEqual(cats, sorted([identity.CAT_NUGET, identity.CAT_PY, identity.CAT_NPM, identity.CAT_JAVA, identity.CAT_GO]))

    def test_carved_entry_validation(self):
        entry = {"name": "demo", "repo": "pulumi-labs/pulumi-demo", "goModule": "github.com/pulumi-labs/pulumi-demo", "mode": "mirror",
                 "registryName": "demo", "listed": False, "exportedAt": None, "lastSyncedCommit": None, "lastSyncedVersion": None}
        def carved():
            return [h for h in self.hits() if h.category == identity.CAT_CARVED]
        helpers.write(self.mono, "carved-providers.json", json.dumps([entry]))
        self.assertEqual(carved(), [])
        bad = dict(entry, repo="pulumi/pulumi-demo", mode="carve")
        del bad["listed"]
        helpers.write(self.mono, "carved-providers.json", json.dumps([bad]))
        texts = " | ".join(h.text for h in carved())
        self.assertIn("no 'listed'", texts)
        self.assertIn("repo 'pulumi/pulumi-demo'", texts)
        self.assertIn("mode 'carve'", texts)
        os.remove(os.path.join(self.mono, "carved-providers.json"))
        self.assertIn("not found", carved()[0].text)
        helpers.write(self.mono, "carved-providers.json", "{nope")
        self.assertIn("not valid JSON", carved()[0].text)


class ApplyTest(IdentityBase):
    def test_rewrites_examples_templates_readme_and_tests(self):
        changed = identity.apply(self.mono, "demo")
        self.assertIn("README.md", changed)
        self.assertEqual(self.read("examples/demo-ts/index.ts"), 'import * as demo from "@pulumi-labs/demo";\n')
        self.assertIn('"@pulumi-labs/demo": "^0.1.0", "@pulumi/pulumi": "^3"', self.read("examples/demo-ts/package.json"))
        self.assertEqual(self.read("examples/demo-py/__main__.py"), "import pulumi_labs_demo as demo\n")
        self.assertEqual(self.read("examples/demo-py/requirements.txt"), "pulumi_labs_demo>=0.1.0,<1.0.0\n")
        self.assertEqual(self.read("examples/demo-cs/Program.cs"), "using Pulumi.Labs.Demo;\nusing Pulumi;\n")
        self.assertIn('Include="Pulumi.Labs.Demo" Version="0.1.*"', self.read("examples/demo-cs/demo-cs.csproj"))
        self.assertIn('Include="Pulumi" Version="3.*"', self.read("examples/demo-cs/demo-cs.csproj"))
        pom = self.read("examples/demo-java/pom.xml")
        self.assertIn("<groupId>com.pulumi</groupId>\n<artifactId>demo-java</artifactId>", pom)  # the example's own coordinates
        self.assertIn("<groupId>com.pulumi.labs</groupId>\n  <artifactId>demo</artifactId>", pom)
        app = self.read("examples/demo-java/src/main/java/com/pulumi/example/App.java")
        self.assertIn("import com.pulumi.labs.demo.Thing;", app)
        self.assertIn("import com.pulumi.Pulumi;", app)
        self.assertIn("package com.pulumi.example;", app)
        self.assertIn('"github.com/pulumi-labs/pulumi-demo/sdk/go/demo"', self.read("examples/demo-go/main.go"))
        self.assertIn("%s/pkg/version" % MOD, self.read("examples/demo-go/main.go"))  # monorepo provider path: export.py rewrites it
        gomod = self.read("examples/demo-go/go.mod")
        self.assertTrue(gomod.startswith("module github.com/pulumi-labs/pulumi-demo/examples/demo-go\n"))
        self.assertIn("require github.com/pulumi-labs/pulumi-demo/sdk/go/demo v0.1.0", gomod)
        self.assertIn("require github.com/pulumi-labs/pulumi-demo/sdk/go/demo", self.read("templates/demo-cloud-go/go.mod"))
        self.assertEqual(self.read("templates/demo-cloud-python/requirements.txt"), "pulumi_labs_demo\n")
        readme = self.read("README.md")
        for new in ("@pulumi-labs/demo", "pulumi_labs_demo", "github.com/pulumi-labs/pulumi-demo/sdk/go/demo", "Pulumi.Labs.Demo", "<groupId>com.pulumi.labs</groupId>"):
            self.assertIn(new, readme)
        meta = self.read("provider/pkg/provider/metadata_test.go")
        self.assertIn('got != "Pulumi Labs"', meta)
        self.assertIn('want %q", got, "Pulumi Labs")', meta)
        self.assertIn('const want = "github.com/pulumi-labs/pulumi-demo/sdk/go/demo"', meta)

    def test_never_touches_builder_code_docs_changelog_or_carved(self):
        before = self.tree_bytes()
        with open(os.path.join(self.mono, "carved-providers.json"), "rb") as f:
            carved = f.read()
        identity.apply(self.mono, "demo")
        after = self.tree_bytes()
        for rel in ("provider/pkg/provider/provider.go", "provider/cmd/pulumi-resource-demo/main.go", "provider/pkg/provider/provider_test.go",
                    "provider/go.mod", "CHANGELOG.md", "version.txt"):
            self.assertEqual(before[rel], after[rel], rel)
        self.assertEqual(set(before), set(after))  # creates nothing: no docs pages, no files
        with open(os.path.join(self.mono, "carved-providers.json"), "rb") as f:
            self.assertEqual(f.read(), carved)

    def test_idempotent(self):
        first = identity.apply(self.mono, "demo")
        self.assertTrue(first)
        snapshot = self.tree_bytes()
        self.assertEqual(identity.apply(self.mono, "demo"), {})
        self.assertEqual(self.tree_bytes(), snapshot)

    def test_leaves_only_manual_items(self):
        identity.apply(self.mono, "demo")
        hits = self.hits()
        self.assertTrue(hits)
        self.assertEqual({h.fix for h in hits}, {identity.MANUAL})
        self.assertEqual({h.category for h in hits},
                         {identity.CAT_REPO, identity.CAT_GO, identity.CAT_PUB, identity.CAT_META, identity.CAT_NPM, identity.CAT_DOCS, identity.CAT_CARVED})
        self.assertEqual({h.rel for h in hits if h.category in (identity.CAT_REPO, identity.CAT_GO, identity.CAT_PUB, identity.CAT_NPM)},
                         {"README.md", "CHANGELOG.md", "provider/pkg/provider/provider.go"})

    def test_binary_files_are_left_alone(self):
        path = os.path.join(self.pdir, "examples/demo-ts/logo.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\0@pulumi/demo\0")
        identity.apply(self.mono, "demo")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG\0@pulumi/demo\0")

    def test_dependency_directories_are_skipped(self):
        helpers.write(self.mono, "lib/providers/demo/examples/demo-ts/node_modules/x/index.js", 'require("@pulumi/demo")\n')
        identity.apply(self.mono, "demo")
        self.assertIn("@pulumi/demo", self.read("examples/demo-ts/node_modules/x/index.js"))
        self.assertFalse([h for h in self.hits() if "node_modules" in h.rel])

    def test_full_workflow_ends_clean(self):
        identity.apply(self.mono, "demo")
        helpers.write(self.mono, "lib/providers/demo/provider/pkg/provider/provider.go", NEW_PROVIDER_GO)
        for rel in ("docs/_index.md", "docs/installation-configuration.md"):
            helpers.write(self.mono, "lib/providers/demo/" + rel, "---\ntitle: x\n---\n")
        helpers.write(self.mono, "lib/providers/demo/CHANGELOG.md", "# Changelog\n\n- Renamed the npm package.\n")
        helpers.write(self.mono, "lib/providers/demo/README.md",
                      self.read("README.md").replace("https://github.com/pulumi/autoregistry/tree/main/lib/providers/demo", "https://github.com/pulumi-labs/pulumi-demo"))
        n = names.derive("demo")
        helpers.write(self.mono, "carved-providers.json", json.dumps([{
            "name": "demo", "repo": n["repo"], "goModule": n["go_module"], "mode": "mirror", "registryName": "demo", "listed": False,
            "exportedAt": None, "lastSyncedCommit": None, "lastSyncedVersion": None}]))
        self.assertEqual([h.render() for h in self.hits()], [])


class CliTest(IdentityBase):
    def run_main(self, *args):
        return helpers.quiet(identity.main, list(args) + ["--monorepo", self.mono, "--provider", "demo"])

    def test_check_exit_codes_and_json(self):
        self.assertEqual(self.run_main("--check"), 1)
        identity.apply(self.mono, "demo")
        helpers.write(self.mono, "lib/providers/demo/provider/pkg/provider/provider.go", NEW_PROVIDER_GO)
        self.assertEqual(self.run_main("--check"), 1)  # docs, carved entry, CHANGELOG remain

    def test_apply_exit_zero_and_prints_manual_items(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = identity.main(["--apply", "--monorepo", self.mono, "--provider", "demo"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("files changed", out)
        self.assertIn("Remaining manual items", out)
        self.assertIn("docs/_index.md", out)
        self.assertIn("carved-providers.json", out)

    def test_json_output(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = identity.main(["--check", "--json", "--monorepo", self.mono, "--provider", "demo"])
        self.assertEqual(rc, 1)
        data = json.loads(buf.getvalue())
        self.assertTrue(all(set(d) == {"file", "line", "category", "text", "fix"} for d in data))

    def test_requires_a_mode_and_a_valid_slug(self):
        with self.assertRaises(SystemExit):
            helpers.quiet(identity.main, ["--monorepo", self.mono, "--provider", "demo"])
        self.assertEqual(helpers.quiet(identity.main, ["--check", "--monorepo", self.mono, "--provider", "Bad_Slug"]), 1)


if __name__ == "__main__":
    unittest.main()
