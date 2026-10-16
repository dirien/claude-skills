import json
import os
import re
import unittest

import helpers
import check
import names


class CheckBase(helpers.TempDirMixin, unittest.TestCase):
    slug = "modal"
    display = "Modal"

    def setUp(self):
        super().setUp()
        self.repo = helpers.build_repo(self.tmp, self.slug, self.display)
        self.n = names.derive(self.slug)

    def run_check(self):
        helpers.commit_all(self.repo, "mutation")
        return check.check_repo(self.repo)

    def assertFails(self, pattern):
        fails = self.run_check()
        self.assertTrue(any(re.search(pattern, f) for f in fails), "no failure matching %r in:\n%s" % (pattern, "\n".join(fails)))
        return fails

    def edit(self, rel, fn):
        path = os.path.join(self.repo, rel)
        with open(path, encoding="utf-8") as f:
            s = f.read()
        helpers.write(self.repo, rel, fn(s))


class PassTest(CheckBase):
    def test_valid_repo_passes(self):
        self.assertEqual(check.check_repo(self.repo), [])

    def test_valid_hyphenated_repo_passes(self):
        repo = helpers.build_repo(self.tmp, "better-uptime", "Better Uptime")
        self.assertEqual(check.check_repo(repo), [])

    def test_main_exit_codes(self):
        self.assertEqual(helpers.quiet(check.main, ["--repo", self.repo]), 0)
        os.remove(os.path.join(self.repo, "README.md"))
        self.assertEqual(helpers.quiet(check.main, ["--repo", self.repo]), 1)

    def test_not_rendered(self):
        fails = check.check_repo(self.tmp)
        self.assertEqual(len(fails), 1)
        self.assertIn("render.py", fails[0])


class FailTest(CheckBase):
    def test_missing_file(self):
        os.remove(os.path.join(self.repo, "docs/_index.md"))
        self.assertFails(r"MISSING required file: docs/_index\.md")

    def test_missing_archetype_file(self):
        os.remove(os.path.join(self.repo, "mise.toml"))
        self.assertFails(r"MISSING required file: mise\.toml")

    def test_empty_sdk_dir(self):
        os.remove(os.path.join(self.repo, "sdk/java/placeholder.txt"))
        self.assertFails(r"required directory: sdk/java/")

    def test_sdk_gomod_wrong_module(self):
        helpers.write(self.repo, self.n["go_sdk_dir"] + "/go.mod", "module github.com/pulumi/pulumi-modal/sdk/go/modal\n\ngo 1.27\n")
        self.assertFails(r"WRONG sdk/go/modal/go.mod: sdk go.mod declares module")

    def test_sdk_gomod_missing(self):
        os.remove(os.path.join(self.repo, self.n["go_sdk_dir"], "go.mod"))
        self.assertFails(r"MISSING sdk/go/modal/go.mod")

    def test_example_requires_wrong_sdk_path(self):
        helpers.write(self.repo, "examples/modal-go/go.mod",
                      "module example\n\nrequire github.com/pulumi/pulumi-modal/sdk/go/modal v0.1.0\n")
        self.assertFails(r"examples/modal-go/go.mod: requires github.com/pulumi/pulumi-modal/sdk/go/modal, want exactly github.com/pulumi-labs/pulumi-modal/sdk/go/modal")

    def test_example_with_right_path_passes(self):
        helpers.write(self.repo, "templates/modal-cloud-go/go.mod",
                      "module ${PROJECT}\n\nrequire (\n\tgithub.com/pulumi-labs/pulumi-modal/sdk/go/modal v0.1.0\n)\n")
        self.assertEqual(self.run_check(), [])

    def test_example_replace_elsewhere_fails(self):
        helpers.write(self.repo, "examples/modal-go/go.mod",
                      "module example\n\nrequire github.com/pulumi-labs/pulumi-modal/sdk/go/modal v0.1.0\n"
                      "replace github.com/pulumi-labs/pulumi-modal/sdk/go/modal => /home/me/fork\n")
        self.assertFails(r"examples/modal-go/go.mod: replace .* is not allowed")

    def test_hand_edited_makefile(self):
        self.edit("Makefile", lambda s: s + "\nextra:\n\techo hi\n")
        self.assertFails(r"HAND-EDITED Makefile")

    def test_hand_edited_verbatim_file(self):
        self.edit("LICENSE", lambda s: s + " ")
        self.assertFails(r"HAND-EDITED LICENSE")

    def test_hand_edited_workflow(self):
        self.edit(".github/workflows/release.yml", lambda s: s.replace("v*.*.*", "v*"))
        self.assertFails(r"HAND-EDITED \.github/workflows/release\.yml")

    def test_monorepo_reference(self):
        self.edit("README.md", lambda s: s + "\nimport github.com/pulumi/autoregistry/lib/providers/modal\n")
        fails = self.assertFails(r"README\.md:\d+: monorepo path pulumi/autoregistry")
        self.assertTrue(any("README.md:" in f for f in fails))

    def test_provider_go_mod(self):
        helpers.write(self.repo, "provider/go.mod", "module x\n")
        self.assertFails(r"FORBIDDEN file: provider/go\.mod")

    def test_nested_go_mod_and_version_txt(self):
        helpers.write(self.repo, "provider/pkg/go.mod", "module x\n")
        helpers.write(self.repo, "version.txt", "0.1.0\n")
        fails = self.run_check()
        self.assertTrue(any("provider/pkg/go.mod" in f for f in fails))
        self.assertTrue(any("FORBIDDEN file: version.txt" in f for f in fails))

    def test_wrong_import_base_path(self):
        s = helpers.schema_for(self.slug)
        s["language"]["go"]["importBasePath"] = "github.com/pulumi/pulumi-modal/sdk/go/modal"
        helpers.write(self.repo, self.n["schema_path"], json.dumps(s))
        fails = self.assertFails(r"WRONG schema language\.go\.importBasePath")
        self.assertTrue(any("github.com/pulumi/pulumi-modal" in f for f in fails))  # also the namespace grep

    def test_wrong_publisher_and_missing_url(self):
        s = helpers.schema_for(self.slug, publisher="Pulumi")
        del s["pluginDownloadURL"]
        helpers.write(self.repo, self.n["schema_path"], json.dumps(s))
        self.assertFails(r"WRONG schema publisher")
        self.assertFails(r"WRONG schema pluginDownloadURL")

    def test_missing_banner(self):
        helpers.write(self.repo, "README.md", "# Pulumi Modal\n\nNothing to see.\n")
        self.assertFails(r"README\.md needs a banner")

    def test_banner_beyond_line_15(self):
        helpers.write(self.repo, "README.md", "# t\n" + "\n" * 20 + "experimental\n")
        self.assertFails(r"README\.md needs a banner")

    def test_schema_not_marked_experimental(self):
        s = helpers.schema_for(self.slug, description="A provider.")
        helpers.write(self.repo, self.n["schema_path"], json.dumps(s))
        self.assertFails(r"schema description must mention")

    def test_leftover_placeholder(self):
        self.edit("README.md", lambda s: s + "\nName: __PROVIDER__\n")
        self.assertFails(r"README\.md:\d+: leftover placeholder __PROVIDER__")

    def test_old_npm_name(self):
        self.edit("README.md", lambda s: s + "\nnpm i @pulumi/modal\n")
        self.assertFails(r"README\.md:\d+: old npm name @pulumi/modal")

    def test_old_python_dotnet_java_names(self):
        self.edit("README.md", lambda s: s + "\nimport pulumi_modal\nusing Pulumi.Modal;\nimport com.pulumi.modal.Foo;\n")
        self.assertFails(r"old Python name pulumi_modal")
        self.assertFails(r"old \.NET name Pulumi\.Modal")
        self.assertFails(r"old Java package com\.pulumi\.modal")

    def test_old_go_path(self):
        self.edit("README.md", lambda s: s + "\ngo get github.com/pulumi/pulumi-modal/sdk/go/modal\n")
        self.assertFails(r"old Go path github\.com/pulumi/pulumi-modal")

    def test_wrong_go_mod_module(self):
        helpers.write(self.repo, "go.mod", "module github.com/pulumi/autoregistry/lib/providers/modal/provider\n")
        self.assertFails(r"WRONG go\.mod module")

    def test_missing_generated_sdk_names(self):
        os.remove(os.path.join(self.repo, self.n["dotnet_csproj"]))
        self.assertFails(r"MISSING generated SDK file sdk/dotnet/Pulumi\.Labs\.Modal\.csproj")

    def test_bad_tag(self):
        helpers.git(self.repo, "tag", "v1.0.0")
        helpers.git(self.repo, "tag", "v0.1.0")
        fails = self.assertFails(r"BAD TAG v1\.0\.0")
        self.assertFalse(any("BAD TAG v0.1.0" in f for f in fails))

    def test_missing_copyright(self):
        helpers.write(self.repo, "provider/pkg/provider/x.go", "package provider\n")
        self.assertFails(r"MISSING copyright header.*provider/pkg/provider/x\.go")

    def test_untracked_required_file(self):
        helpers.git(self.repo, "rm", "-q", "--cached", "CLAUDE.md")
        fails = check.check_repo(self.repo)  # no re-add: the file stays untracked
        self.assertTrue(any(re.search(r"UNTRACKED required file: CLAUDE\.md", f) for f in fails), fails)

    def test_stale_archetype_sha(self):
        p = os.path.join(self.repo, ".pulumi-labs-export.json")
        d = json.load(open(p))
        d["archetype_sha"] = "0" * 40
        json.dump(d, open(p, "w"))
        self.assertFails(r"ARCHETYPE CHANGED")

    def test_reports_all_failures(self):
        os.remove(os.path.join(self.repo, "README.md"))
        helpers.write(self.repo, "provider/go.mod", "module x\n")
        self.edit("Makefile", lambda s: s + "x")
        fails = self.run_check()
        self.assertGreaterEqual(len(fails), 3)


class NamespaceGrepTest(unittest.TestCase):
    def hits(self, slug, line):
        return [label for label, rx in check.namespace_patterns(slug) if rx.search(line)]

    def test_no_false_positives(self):
        for slug, lines in {
            "modal": ["pulumi_labs_modal", "Pulumi.Labs.Modal", "com.pulumi.labs", "com.pulumi.labs:modal",
                      "@pulumi-labs/modal", "github.com/pulumi-labs/pulumi-modal", "pulumi-labs-modal"],
            "nvidia-aicr": ["pulumi_labs_nvidia_aicr", "Pulumi.Labs.NvidiaAicr", "com.pulumi.labs:nvidia-aicr",
                            "github.com/pulumi-labs/pulumi-nvidia-aicr/sdk/go/nvidiaaicr"],
            "better-uptime": ["Pulumi.Labs.BetterUptime", "pulumi_labs_better_uptime"],
        }.items():
            for line in lines:
                self.assertEqual(self.hits(slug, line), [], (slug, line))

    def test_other_providers_are_not_flagged(self):
        self.assertEqual(self.hits("modal", "github.com/pulumi/pulumi-modal-extra"), [])
        self.assertEqual(self.hits("aws", "pulumi_aws_native"), [])

    def test_true_positives(self):
        self.assertTrue(self.hits("modal", "github.com/pulumi/autoregistry/lib"))
        self.assertTrue(self.hits("modal", "pulumi/autoregistry"))
        self.assertTrue(self.hits("modal", "github.com/pulumi/pulumi-modal/sdk/go/modal"))
        self.assertTrue(self.hits("modal", "@pulumi/modal"))
        self.assertTrue(self.hits("modal", "import pulumi_modal as m"))
        self.assertTrue(self.hits("modal", "using Pulumi.Modal;"))
        self.assertTrue(self.hits("modal", "com.pulumi.modal.Secret"))
        self.assertTrue(self.hits("better-uptime", "Pulumi.BetterUptime.Monitor"))
        self.assertTrue(self.hits("better-uptime", "pulumi_better_uptime"))
        self.assertTrue(self.hits("better-uptime", "@pulumi/better-uptime"))
        self.assertTrue(self.hits("better-uptime", "com.pulumi.betteruptime"))


class SdkVersionTest(CheckBase):
    def test_placeholder_package_json_names_the_cause(self):
        helpers.write(self.repo, "sdk/nodejs/package.json", json.dumps({"version": "${VERSION}"}))
        fails = self.assertFails(r"WRONG sdk/nodejs/package.json version: got '\$\{VERSION\}'")
        self.assertTrue(any("respectSchemaVersion" in f for f in fails))

    def test_release_version_is_not_the_committed_one(self):
        helpers.write(self.repo, "sdk/nodejs/package.json", json.dumps({"version": "0.1.0"}))
        self.assertFails(r"WRONG sdk/nodejs/package.json version: got '0.1.0', want '0.0.1-dev'")

    def test_dotnet_version_txt_required_and_checked(self):
        os.remove(os.path.join(self.repo, "sdk/dotnet/version.txt"))
        self.assertFails(r"MISSING sdk/dotnet/version.txt")
        helpers.write(self.repo, "sdk/dotnet/version.txt", "0.1.0\n")
        self.assertFails(r"WRONG sdk/dotnet/version.txt: got '0.1.0'")

    def test_dev_version_passes_and_no_sdk_is_not_judged(self):
        self.assertEqual(check.sdk_version_failures(self.repo), [])
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertEqual(check.sdk_version_failures(empty), [])

    def test_respect_schema_version_required_for_each_language(self):
        for lang in check.VERSIONED_LANGUAGES:
            s = helpers.schema_for(self.slug)
            del s["language"][lang]["respectSchemaVersion"]
            helpers.write(self.repo, self.n["schema_path"], json.dumps(s))
            fails = self.assertFails(r"MISSING schema language\.%s\.respectSchemaVersion=true" % lang)
            self.assertEqual(len([f for f in fails if "respectSchemaVersion" in f]), 1)


if __name__ == "__main__":
    unittest.main()
