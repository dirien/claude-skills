"""Tests for the bridged (Terraform-bridged provider) archetype: render, export, check, identity, verify helpers, drift."""
import json
import os
import re
import stat
import unittest

import helpers
import archetype_drift
import check
import export
import gomod
import identity
import names
import onboarding
import render
import verify


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class BridgedBase(helpers.TempDirMixin, unittest.TestCase):
    slug = "demo"
    flavor = "pf"
    switched = True

    def setUp(self):
        super().setUp()
        self.mono = helpers.build_bridged_monorepo(self.tmp, self.slug, flavor=self.flavor, switched=self.switched)
        self.out = helpers.build_out(self.tmp)
        self.n = names.derive(self.slug)

    def do_export(self, out=None, **kw):
        return export.export(self.mono, self.slug, out or self.out, skill_commit=None, **kw)

    def mutate(self, rel, fn):
        path = "lib/providers/%s/%s" % (self.slug, rel)
        helpers.write(self.mono, path, fn(read(os.path.join(self.mono, path))))
        helpers.commit_all(self.mono, "mutate " + rel)

    def assertRefused(self, pattern):
        with self.assertRaises(export.ExportError) as cm:
            self.do_export()
        self.assertRegex(str(cm.exception), pattern)
        return str(cm.exception)


class RenderTest(unittest.TestCase):
    def test_manifest_templates_exist_and_are_pinned(self):
        m = render.load_manifest("bridged")
        self.assertEqual(m["archetype_repo"], "pulumi/pulumi-tf-provider-boilerplate")
        self.assertRegex(m["archetype_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(m["also_derived_from"][0]["repo"], "pulumi-labs/pulumi-nvidia-aicr")
        for e in m["files"]:
            self.assertTrue(os.path.isfile(os.path.join(helpers.SKILL_DIR, e["template"])), e["template"])
            self.assertTrue(e.get("derived_from") or e.get("upstream_sha256"), "%s has no provenance" % e["target"])

    def test_native_options_are_unchanged(self):
        o = render.make_options("modal", "Modal", [])
        self.assertEqual(sorted(o), ["archetype_repo", "archetype_sha", "display_name", "exclude_platforms", "provider"])
        self.assertEqual(render.archetype_of(o), "native")

    def test_bridged_options_record_archetype_and_flavor(self):
        o = render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf")
        self.assertEqual((o["archetype"], o["flavor"]), ("bridged", "pf"))
        self.assertEqual(o["archetype_repo"], "pulumi/pulumi-tf-provider-boilerplate")
        self.assertEqual(render.options_from_record(o), o)

    def test_invalid_combinations(self):
        for kw in ({"archetype": "bridged"}, {"archetype": "bridged", "flavor": "mux"}, {"archetype": "native", "flavor": "pf"},
                   {"archetype": "other"}):
            with self.assertRaises(render.RenderError, msg=str(kw)):
                render.make_options("clerk", "Clerk", [], **kw)

    def test_file_set_per_flavor(self):
        for flavor, imports in (("pf", "pkg/pf/tfbridge"), ("sdkv2", "pkg/tfbridge")):
            o = render.make_options("better-uptime", "Better Uptime", [], archetype="bridged", flavor=flavor)
            out = {k: v.decode() for k, v in render.render_all(o).items()}
            self.assertEqual(sorted(out), [".github/workflows/release.yml", ".github/workflows/test.yml", ".gitignore", ".vscode/launch.json",
                                           "CONTRIBUTING.md", "LICENSE", "Makefile", "mise.toml",
                                           "provider/cmd/pulumi-resource-better-uptime/main.go", "provider/cmd/pulumi-tfgen-better-uptime/main.go",
                                           "scripts/fixup_python_input_optional.py"])
            main = out["provider/cmd/pulumi-resource-better-uptime/main.go"]
            self.assertIn("//go:embed schema.json", main)
            self.assertIn(imports, main)
            self.assertIn('betteruptime "github.com/pulumi-labs/pulumi-better-uptime/provider"', main)
            for text in out.values():
                self.assertNotRegex(text, r"__[A-Z][A-Z0-9_]*__")

    def test_flavor_changes_only_the_cmd_mains(self):
        a = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))
        b = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="sdkv2"))
        self.assertEqual([k for k in a if a[k] != b[k]],
                         ["provider/cmd/pulumi-resource-clerk/main.go", "provider/cmd/pulumi-tfgen-clerk/main.go"])

    def test_every_main_has_a_pulumi_copyright_header(self):
        for flavor in render.FLAVORS:
            for rel, data in render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor=flavor)).items():
                if rel.endswith("main.go"):
                    self.assertRegex(data.decode(), r"Copyright .*Pulumi Corporation")

    def test_shared_files_are_the_native_archetype_bytes(self):
        nat = render.render_all(render.make_options("clerk", "Clerk", []))
        br = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))
        for rel in (".gitignore", ".vscode/launch.json", "LICENSE", "mise.toml", "scripts/fixup_python_input_optional.py"):
            self.assertEqual(nat[rel], br[rel], rel)
        self.assertNotIn("provider/pkg/version/version.go", br)  # the provider's own file is exported instead

    def test_publishing_jobs_are_the_native_ones(self):
        nat = render.render_all(render.make_options("clerk", "Clerk", [])) [".github/workflows/release.yml"].decode()
        br = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))[".github/workflows/release.yml"].decode()
        marker = "  # The Go SDK is shipped via a path-prefixed tag"
        self.assertEqual(nat[nat.index(marker):], br[br.index(marker):])
        for job in ("publish-go-sdk:", "publish-nodejs:", "publish-python:", "publish-dotnet:", "publish-java:"):
            self.assertIn(job, br)

    def test_release_builds_from_provider_module_and_guards_the_schema(self):
        br = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))[".github/workflows/release.yml"].decode()
        self.assertEqual(br.count("cache-dependency-path: provider/go.sum"), 2)
        self.assertNotIn("cache-dependency-path: go.sum", br)
        self.assertIn("git diff --exit-code -- provider/cmd/pulumi-resource-clerk", br)
        self.assertIn("-X github.com/pulumi-labs/pulumi-clerk/provider/pkg/version.Version=", br)

    def test_test_workflow_checks_schema_metadata_and_tidy(self):
        br = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))[".github/workflows/test.yml"].decode()
        self.assertIn("git diff --exit-code provider/cmd/pulumi-resource-clerk;", br)
        self.assertIn("make tidy", br)
        self.assertNotIn("cache-dependency-path: go.sum", br)

    def test_makefile_keeps_the_native_target_contract(self):
        nat = render.render_all(render.make_options("clerk", "Clerk", []))["Makefile"].decode()
        br = render.render_all(render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf"))["Makefile"].decode()
        marker = "# Generate Node.js SDK"
        self.assertEqual(nat[nat.index(marker):], br[br.index(marker):])  # sdk targets, sdk_fixups, build_*_sdk, set_version, check_copyright
        for target in ("provider:", "schema:", "tfgen:", "test:", "lint:", "tidy:"):
            self.assertRegex(br, r"(?m)^" + re.escape(target))
        self.assertIn("schema --out $(CMD_DIR)", br)
        self.assertIn("jq 'del(.version)'", br)
        self.assertIn("export PULUMI_CONVERT ?= 0", br)
        self.assertNotIn("-race", br)

    def test_archetype_files_are_deterministic(self):
        o = render.make_options("clerk", "Clerk", [("windows/amd64", "no windows build")], archetype="bridged", flavor="pf")
        self.assertEqual(render.render_all(o), render.render_all(dict(o)))
        self.assertIn("windows/amd64 is off the matrix", render.render_all(o)[".github/workflows/release.yml"].decode())


class NamesTest(unittest.TestCase):
    def test_bridged_fields(self):
        n = names.derive("better-uptime")
        self.assertEqual(n["provider_module"], "github.com/pulumi-labs/pulumi-better-uptime/provider")
        self.assertEqual(n["tfgen_binary"], "pulumi-tfgen-better-uptime")
        self.assertEqual(n["tfgen_cmd_dir"], "provider/cmd/pulumi-tfgen-better-uptime")
        self.assertEqual(n["bridge_metadata_path"], "provider/cmd/pulumi-resource-better-uptime/bridge-metadata.json")
        self.assertEqual(n["ldflags_version_var"], n["provider_module"] + "/pkg/version.Version")


class MainShapeTest(unittest.TestCase):
    def problems(self, text, kind, flavor, slug="demo"):
        return export.cmd_main_problems(text, kind, flavor, slug)

    def test_standard_mains_have_no_problems(self):
        self.assertEqual(self.problems(helpers.BRIDGED_RESOURCE_MAIN_PF, "resource", "pf"), [])
        self.assertEqual(self.problems(helpers.BRIDGED_TFGEN_MAIN_PF, "tfgen", "pf"), [])
        self.assertEqual(self.problems(helpers.BRIDGED_RESOURCE_MAIN_SDKV2, "resource", "sdkv2"), [])
        self.assertEqual(self.problems(helpers.BRIDGED_TFGEN_MAIN_SDKV2, "tfgen", "sdkv2"), [])

    def test_inline_metadata_form_is_standard(self):
        text = helpers.BRIDGED_RESOURCE_MAIN_PF.replace(
            "meta := pf.ProviderMetadata{PackageSchema: PackageSchema}\n\tpf.Main(context.Background(), \"demo\", demo.Provider(), meta)",
            'pf.Main(context.Background(), "demo", demo.Provider(), pf.ProviderMetadata{PackageSchema: demo.PackageSchema})')
        self.assertEqual(self.problems(text, "resource", "pf"), [])

    def test_extra_statements_are_reported(self):
        text = helpers.BRIDGED_TFGEN_MAIN_PF.replace("pftfgen.Main(", 'os.Setenv("X", "1")\n\tpftfgen.Main(')
        self.assertRegex(" ".join(self.problems(text, "tfgen", "pf")), "extra statement")

    def test_wrong_name_and_missing_call(self):
        self.assertRegex(" ".join(self.problems(helpers.BRIDGED_TFGEN_MAIN_PF, "tfgen", "pf", "other")), "package name argument")
        self.assertRegex(" ".join(self.problems("package main\n\nfunc main() {}\n", "tfgen", "pf")), "exactly one")
        self.assertRegex(" ".join(self.problems("package main\n", "tfgen", "pf")), "no func main")

    def test_comments_do_not_count_as_statements(self):
        text = helpers.BRIDGED_TFGEN_MAIN_PF.replace("pftfgen.Main(", "// os.Setenv(\"X\", \"1\")\n\t/* more */\n\tpftfgen.Main(")
        self.assertEqual(self.problems(text, "tfgen", "pf"), [])


class ExportTest(BridgedBase):
    def test_module_stays_in_provider_with_pulumi_labs_path(self):
        o = self.do_export()
        self.assertEqual((o["archetype"], o["flavor"]), ("bridged", "pf"))
        mod = read(os.path.join(self.out, "provider/go.mod"))
        self.assertTrue(mod.startswith("module github.com/pulumi-labs/pulumi-demo/provider\n"))
        files = helpers.tree(self.out)
        for rel in ("go.mod", "go.sum", "version.txt"):
            self.assertNotIn(rel, files)
        self.assertIn("provider/go.sum", files)
        self.assertNotIn("github.com/pulumi/pulumi-demo", read(os.path.join(self.out, "provider/resources.go")))
        self.assertIn("pulumi-labs/pulumi-demo/provider/pkg/version", read(os.path.join(self.out, "provider/resources.go")))
        self.assertIn("pulumi-labs/pulumi-demo/provider", read(os.path.join(self.out, "provider/resources_test.go")))

    def test_in_repo_shim_replace_is_kept(self):
        self.do_export()
        mod = read(os.path.join(self.out, "provider/go.mod"))
        self.assertIn("replace %s => ./shim" % helpers.BRIDGED_SHIM % "demo", mod)
        self.assertIn("replace github.com/hashicorp/terraform-plugin-sdk/v2 => github.com/pulumi/terraform-plugin-sdk/v2", mod)
        self.assertEqual(read(os.path.join(self.out, "provider/shim/go.mod")), "module %s\n\ngo 1.27\n" % helpers.BRIDGED_SHIM % "demo")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "provider/shim/shim.go")))

    def test_cmd_mains_come_from_the_archetype_and_embed_the_schema(self):
        self.do_export()
        main = read(os.path.join(self.out, "provider/cmd/pulumi-resource-demo/main.go"))
        self.assertIn("//go:embed schema.json", main)
        self.assertNotIn('[]byte("{}")', main)
        self.assertIn('demo "github.com/pulumi-labs/pulumi-demo/provider"', main)
        self.assertIn("pftfgen.Main(", read(os.path.join(self.out, "provider/cmd/pulumi-tfgen-demo/main.go")))

    def test_version_go_is_the_providers_own_file(self):
        self.do_export()
        self.assertIn('var Version = "0.0.1-alpha.0"', read(os.path.join(self.out, "provider/pkg/version/version.go")))

    def test_bridge_metadata_is_copied_and_schema_is_not(self):
        helpers.write(self.mono, "lib/providers/demo/provider/cmd/pulumi-resource-demo/schema.json", "{}")
        helpers.commit_all(self.mono)
        self.do_export()
        files = helpers.tree(self.out)
        self.assertEqual(files["provider/cmd/pulumi-resource-demo/bridge-metadata.json"][0], b"{}")
        self.assertNotIn("provider/cmd/pulumi-resource-demo/schema.json", files)

    def test_deterministic_and_idempotent(self):
        self.do_export()
        first = helpers.tree(self.out)
        self.do_export()
        self.assertEqual(first, helpers.tree(self.out))
        other = helpers.build_out(self.tmp, "mirror2")
        self.do_export(out=other)
        self.assertEqual(first, helpers.tree(other))

    def test_options_file_records_bridged(self):
        self.do_export()
        rec = json.loads(read(os.path.join(self.out, render.OPTIONS_FILE)))
        self.assertEqual((rec["archetype"], rec["flavor"], rec["provider"], rec["source_version"]), ("bridged", "pf", "demo", "0.1.0"))

    def test_display_name_from_provider_info(self):
        self.assertEqual(self.do_export()["display_name"], "Demo")

    def test_files_are_not_world_executable_by_accident(self):
        self.do_export()
        for rel, (_, is_exec) in helpers.tree(self.out).items():
            self.assertFalse(is_exec and rel.endswith((".go", ".mod")), rel)

    def test_never_copied(self):
        self.do_export()
        files = helpers.tree(self.out)
        self.assertFalse([f for f in files if f.startswith("skills/")])
        self.assertNotIn("MONOREPO MAKEFILE MARKER", read(os.path.join(self.out, "Makefile")))

    def test_check_passes_on_the_export_with_generated_stubs(self):
        out, _ = helpers.build_bridged_mirror(self.tmp, self.mono, name="m3")
        self.assertEqual(check.check_repo(out, self.mono), [])


class ExportSdkV2Test(BridgedBase):
    flavor = "sdkv2"

    def test_sdkv2_flavor(self):
        o = self.do_export()
        self.assertEqual(o["flavor"], "sdkv2")
        main = read(os.path.join(self.out, "provider/cmd/pulumi-resource-demo/main.go"))
        self.assertIn("tfbridge.Main(\"demo\", version.Version", main)
        self.assertIn("//go:embed schema.json", main)
        self.assertIn("tfgen.Main(", read(os.path.join(self.out, "provider/cmd/pulumi-tfgen-demo/main.go")))
        self.assertEqual(check.check_repo(helpers.build_bridged_mirror(self.tmp, self.mono, name="m4")[0], self.mono), [])


class ExportRefusalTest(BridgedBase):
    def test_version_txt_is_required_and_explained(self):
        os.remove(os.path.join(self.mono, "lib/providers/demo/version.txt"))
        helpers.commit_all(self.mono)
        msg = self.assertRefused(r"version\.txt not found")
        self.assertIn("bridged", msg)
        self.assertIn("0.x.y", msg)

    def test_version_must_be_zero_x(self):
        helpers.write(self.mono, "lib/providers/demo/version.txt", "1.0.0\n")
        helpers.commit_all(self.mono)
        self.assertRefused(r"0\.x\.y")

    def test_replace_leaving_the_repository_is_refused(self):
        self.mutate("provider/go.mod", lambda s: s.replace("=> ./shim", "=> ../../outside"))
        self.assertRefused(r"leaves the repository")

    def test_replace_to_a_sibling_directory_without_go_mod_is_refused(self):
        self.mutate("provider/go.mod", lambda s: s.replace("=> ./shim", "=> ../outside"))
        self.assertRefused(r"outside/go\.mod is not part of the export")

    def test_replace_to_a_missing_directory_is_refused(self):
        self.mutate("provider/go.mod", lambda s: s.replace("=> ./shim", "=> ./nothere"))
        self.assertRefused(r"nothere/go\.mod is not part of the export")

    def test_absolute_replace_is_refused(self):
        self.mutate("provider/go.mod", lambda s: s.replace("=> ./shim", "=> /abs/shim"))
        self.assertRefused(r"absolute path")

    def test_wrong_module_name(self):
        self.mutate("provider/go.mod", lambda s: s.replace("module github.com/pulumi/pulumi-demo/provider", "module example.com/x"))
        self.assertRefused(r"declares module example\.com/x, want github\.com/pulumi-labs/pulumi-demo/provider or github\.com/pulumi/pulumi-demo/provider")

    def test_module_already_renamed_in_the_monorepo_is_accepted(self):
        def rename(s):
            return s.replace("github.com/pulumi/pulumi-demo/provider", "github.com/pulumi-labs/pulumi-demo/provider")
        for rel in ("provider/go.mod", "provider/resources.go", "provider/resources_test.go"):
            self.mutate(rel, rename)
        self.do_export()
        self.assertTrue(read(os.path.join(self.out, "provider/go.mod")).startswith("module github.com/pulumi-labs/pulumi-demo/provider\n"))

    def test_nonstandard_cmd_main_is_refused(self):
        self.mutate("provider/cmd/pulumi-tfgen-demo/main.go", lambda s: s.replace("pftfgen.Main(", 'os.Setenv("X", "1")\n\tpftfgen.Main('))
        msg = self.assertRefused(r"not the standard shape")
        self.assertIn("pulumi-tfgen-demo/main.go", msg)

    def test_unknown_flavor_is_refused(self):
        self.mutate("provider/cmd/pulumi-tfgen-demo/main.go", lambda s: re.sub(r'(?m)^\s*pftfgen "[^"]+"\n', "", s))
        self.assertRefused(r"cannot tell the bridge flavor")

    def test_missing_version_package_is_refused(self):
        os.remove(os.path.join(self.mono, "lib/providers/demo/provider/pkg/version/version.go"))
        helpers.commit_all(self.mono)
        self.assertRefused(r"version\.go not found")

    def test_missing_docs_are_refused(self):
        os.remove(os.path.join(self.mono, "lib/providers/demo/docs/_index.md"))
        helpers.commit_all(self.mono)
        self.assertRefused(r"docs/_index\.md")

    def test_old_publisher_and_repository_are_refused_with_file_and_line(self):
        self.mutate("provider/resources.go", lambda s: s.replace('Publisher:   "Pulumi Labs"', 'Publisher:   "Pulumi"'))
        msg = self.assertRefused(r"still has pulumi/ identity values")
        self.assertRegex(msg, r'provider/resources\.go:\d+: Publisher must be "Pulumi Labs"')

    def test_github_org_must_be_the_upstream_org(self):
        self.mutate("provider/resources.go", lambda s: s.replace('Name:        "demo",', 'Name:        "demo",\n\t\tGitHubOrg:   "pulumi",'))
        self.assertRefused(r"GitHubOrg is the UPSTREAM")

    def test_old_import_base_path_is_refused(self):
        self.mutate("provider/resources.go", lambda s: s.replace('"github.com/pulumi-labs/pulumi-demo/sdk/go/demo"', '"github.com/pulumi/pulumi-demo/sdk/go/demo"'))
        self.assertRefused(r"old Go path|ImportBasePath")

    def test_refusal_leaves_out_untouched(self):
        helpers.write(self.out, "keep.txt", "x")
        self.mutate("provider/go.mod", lambda s: s.replace("=> ./shim", "=> ../../outside"))
        with self.assertRaises(export.ExportError):
            self.do_export()
        self.assertTrue(os.path.exists(os.path.join(self.out, "keep.txt")))

    def test_switching_archetype_in_place_is_refused(self):
        self.do_export()
        os.rename(os.path.join(self.mono, "lib/providers/demo/provider/cmd/pulumi-tfgen-demo"), os.path.join(self.tmp, "gone"))
        helpers.commit_all(self.mono)
        with self.assertRaises(export.ExportError) as cm:
            self.do_export()
        self.assertRegex(str(cm.exception), "Refusing to switch")


class NativeStillRefusesRelativeReplaceTest(helpers.TempDirMixin, unittest.TestCase):
    def test_native_regression(self):
        mono = helpers.build_monorepo(self.tmp, "demo")
        path = "lib/providers/demo/provider/go.mod"
        helpers.write(mono, path, read(os.path.join(mono, path)) + "\nreplace example.com/x => ../x\n")
        helpers.commit_all(mono)
        with self.assertRaises(export.ExportError) as cm:
            export.export(mono, "demo", helpers.build_out(self.tmp), skill_commit=None)
        self.assertIn("relative replace", str(cm.exception))
        self.assertEqual(export.detect_archetype(os.path.join(mono, "lib/providers/demo"), "demo"), "native")


class CheckTest(BridgedBase):
    def setUp(self):
        super().setUp()
        self.repo, _ = helpers.build_bridged_mirror(self.tmp, self.mono, name="chk")

    def fails(self, **kw):
        return check.check_repo(self.repo, **kw)

    def assertFails(self, pattern, **kw):
        fails = self.fails(**kw)
        self.assertTrue(any(re.search(pattern, f) for f in fails), "no failure matching %r in:\n%s" % (pattern, "\n".join(fails)))

    def test_clean(self):
        self.assertEqual(self.fails(), [])
        self.assertEqual(self.fails(against_source=self.mono), [])

    def test_root_go_mod_is_forbidden(self):
        helpers.write(self.repo, "go.mod", "module %s\n" % self.n["go_module"])
        helpers.commit_all(self.repo)
        self.assertFails(r"FORBIDDEN file: go\.mod")

    def test_version_txt_is_forbidden(self):
        helpers.write(self.repo, "version.txt", "0.1.0\n")
        helpers.commit_all(self.repo)
        self.assertFails(r"FORBIDDEN file: version\.txt")

    def test_wrong_provider_module(self):
        helpers.write(self.repo, "provider/go.mod", read(os.path.join(self.repo, "provider/go.mod")).replace("pulumi-demo/provider", "pulumi-demo"))
        helpers.commit_all(self.repo)
        self.assertFails(r"WRONG provider/go\.mod module")

    def test_replace_must_stay_inside_and_resolve(self):
        helpers.write(self.repo, "provider/go.mod", read(os.path.join(self.repo, "provider/go.mod")) + "\nreplace example.com/y => ../../y\n")
        helpers.commit_all(self.repo)
        self.assertFails(r"BAD replace: provider/go\.mod: replace example\.com/y => \.\./\.\./y leaves the repository")

    def test_missing_bridge_metadata(self):
        os.remove(os.path.join(self.repo, self.n["bridge_metadata_path"]))
        helpers.commit_all(self.repo)
        self.assertFails(r"MISSING required file: provider/cmd/pulumi-resource-demo/bridge-metadata\.json")

    def test_missing_version_go(self):
        os.remove(os.path.join(self.repo, "provider/pkg/version/version.go"))
        helpers.commit_all(self.repo)
        self.assertFails(r"MISSING required file: provider/pkg/version/version\.go")

    def test_hand_edited_cmd_main(self):
        p = os.path.join(self.repo, "provider/cmd/pulumi-resource-demo/main.go")
        helpers.write(self.repo, "provider/cmd/pulumi-resource-demo/main.go", read(p) + "\n// edit\n")
        helpers.commit_all(self.repo)
        self.assertFails(r"HAND-EDITED provider/cmd/pulumi-resource-demo/main\.go")

    def test_github_org_in_provider_info(self):
        p = os.path.join(self.repo, "provider/resources.go")
        helpers.write(self.repo, "provider/resources.go", read(p).replace('Name:        "demo",', 'Name:        "demo",\n\t\tGitHubOrg:   "pulumi-labs",'))
        helpers.commit_all(self.repo)
        self.assertFails(r"WRONG GitHubOrg in provider/resources\.go")

    def test_respect_schema_version_is_still_required_on_the_schema(self):
        p = os.path.join(self.repo, self.n["schema_path"])
        s = json.loads(read(p))
        del s["language"]["csharp"]["respectSchemaVersion"]
        helpers.write(self.repo, self.n["schema_path"], json.dumps(s))
        helpers.commit_all(self.repo)
        self.assertFails(r"MISSING schema language\.csharp\.respectSchemaVersion")

    def test_drift_on_hand_edited_provider_source(self):
        helpers.write(self.repo, "provider/resources.go", read(os.path.join(self.repo, "provider/resources.go")) + "\n// hand edit\n")
        helpers.commit_all(self.repo)
        self.assertFails(r"DRIFT provider/resources\.go: content differs", against_source=self.mono)

    def test_generated_files_do_not_count_as_drift(self):
        helpers.write(self.repo, self.n["bridge_metadata_path"], '{"auto-settings": {}}')
        helpers.write(self.repo, self.n["cmd_dir"] + "/Pulumi.yaml", "name: demo\nlanguage: schema\n# changed\n")
        helpers.write(self.repo, self.n["schema_path"], read(os.path.join(self.repo, self.n["schema_path"])) + "\n")
        helpers.commit_all(self.repo)
        self.assertEqual([f for f in self.fails(against_source=self.mono) if "DRIFT" in f], [])

    def test_copyright_header_in_shim(self):
        helpers.write(self.repo, "provider/shim/shim.go", "package shim\n")
        helpers.commit_all(self.repo)
        self.assertFails(r"MISSING copyright header.*provider/shim/shim\.go")


class IdentityTest(helpers.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.mono = helpers.build_bridged_monorepo(self.tmp, "demo", switched=False)

    def hits(self):
        return identity.scan(self.mono, "demo")

    def texts(self, category=None):
        return [h.render() for h in self.hits() if category is None or h.category == category]

    def test_provider_info_fields_are_checked(self):
        meta = " | ".join(self.texts(identity.CAT_META))
        for want in ("ProviderInfo.Publisher is 'Pulumi'", "ProviderInfo.Repository is 'https://github.com/pulumi/autoregistry'",
                     "ProviderInfo.PluginDownloadURL", "JavaScript.PackageName is '@pulumi/demo', want '@pulumi-labs/demo'",
                     "Python.PackageName is 'pulumi_demo', want 'pulumi_labs_demo'", "CSharp.RootNamespace is 'Pulumi', want 'Pulumi.Labs'",
                     "Java.BasePackage is 'com.pulumi', want 'com.pulumi.labs'", "Golang.ImportBasePath is",
                     "JavaScript.RespectSchemaVersion", "Python.RespectSchemaVersion", "Python.PyProject", "CSharp.RespectSchemaVersion",
                     "Golang.RespectSchemaVersion", 'must say "experimental"'):
            self.assertIn(want, meta)
        self.assertNotIn("Java.RespectSchemaVersion", meta)

    def test_hits_point_at_the_field_line(self):
        lines = {h.text.split(" ")[0]: h.line for h in self.hits() if h.category == identity.CAT_META and h.rel == "provider/resources.go"}
        src = read(os.path.join(self.mono, "lib/providers/demo/provider/resources.go")).splitlines()
        self.assertIn("Publisher:", src[lines["ProviderInfo.Publisher"] - 1])

    def test_switched_provider_has_no_hits_apart_from_nothing(self):
        mono = helpers.build_bridged_monorepo(os.path.join(self.tmp, "b"), "demo", switched=True) if os.makedirs(os.path.join(self.tmp, "b")) is None else None
        self.assertEqual([h.render() for h in identity.scan(mono, "demo")], [])

    def test_the_vanity_module_path_is_not_a_hit_and_is_not_rewritten(self):
        self.assertFalse([h for h in self.hits() if "pulumi-demo/provider" in h.text and h.category == identity.CAT_GO])
        identity.apply(self.mono, "demo")
        test = read(os.path.join(self.mono, "lib/providers/demo/provider/resources_test.go"))
        self.assertIn("github.com/pulumi/pulumi-demo/provider", test)
        self.assertNotIn("pulumi-labs", test)

    def test_apply_rewrites_examples_but_not_provider_info(self):
        identity.apply(self.mono, "demo")
        ex = read(os.path.join(self.mono, "lib/providers/demo/examples/demo-go/main.go"))
        self.assertIn("github.com/pulumi-labs/pulumi-demo/sdk/go/demo", ex)
        self.assertIn('Publisher:   "Pulumi"', read(os.path.join(self.mono, "lib/providers/demo/provider/resources.go")))

    def test_github_org_is_flagged(self):
        p = os.path.join(self.mono, "lib/providers/demo/provider/resources.go")
        helpers.write(self.mono, "lib/providers/demo/provider/resources.go", read(p).replace('Name:        "demo",', 'Name:        "demo",\n\t\tGitHubOrg:   "pulumi",'))
        self.assertTrue([t for t in self.texts(identity.CAT_META) if "GitHubOrg is the UPSTREAM" in t])

    def test_version_txt_is_a_hit_when_missing_or_not_zero_x(self):
        p = os.path.join(self.mono, "lib/providers/demo/version.txt")
        os.remove(p)
        self.assertTrue([t for t in self.texts(identity.CAT_VERSION) if "missing" in t])
        helpers.write(self.mono, "lib/providers/demo/version.txt", "2.0.0\n")
        self.assertTrue([t for t in self.texts(identity.CAT_VERSION) if "0.x.y" in t])

    def test_nonstandard_cmd_main_is_a_hit(self):
        p = "lib/providers/demo/provider/cmd/pulumi-tfgen-demo/main.go"
        helpers.write(self.mono, p, read(os.path.join(self.mono, p)).replace("pftfgen.Main(", 'os.Setenv("X", "1")\n\tpftfgen.Main('))
        self.assertTrue(self.texts(identity.CAT_CMD))

    def test_archetype_owned_files_are_not_scanned_for_copyright_or_identity(self):
        for rel in ("provider/cmd/pulumi-resource-demo/main.go", "provider/cmd/pulumi-tfgen-demo/main.go"):
            p = "lib/providers/demo/" + rel
            helpers.write(self.mono, p, "package main\n\nfunc main() { /* github.com/pulumi/pulumi-demo/sdk/go/demo */ }\n")
        self.assertFalse([h for h in self.hits() if h.rel.startswith("provider/cmd/") and h.category in (identity.CAT_COPY, identity.CAT_GO)])

    def test_native_detection_is_unchanged(self):
        mono = helpers.build_monorepo(os.path.join(self.tmp, "n") if os.makedirs(os.path.join(self.tmp, "n")) is None else "", "demo")
        self.assertFalse(identity.is_bridged(os.path.join(mono, "lib/providers/demo"), "demo"))
        self.assertTrue(identity.is_bridged(os.path.join(self.mono, "lib/providers/demo"), "demo"))

    def test_go_block_skips_strings_and_comments(self):
        text = 'x := T{ A: "}", // }\n B: `{`, C: U{D: 1}, }\nrest'
        self.assertEqual(identity.go_block(text, text.index("{")), text[text.index("{"):text.index("}\nrest") + 1])
        self.assertIsNone(identity.go_block("T{ A: 1", 1))


class VerifyHelpersTest(helpers.TempDirMixin, unittest.TestCase):
    def test_required_steps_per_archetype(self):
        self.assertEqual(verify.required_steps("native"), verify.REQUIRED_STEPS)
        self.assertEqual(verify.required_steps("anything"), verify.REQUIRED_STEPS)
        extra = set(verify.required_steps("bridged")) - set(verify.REQUIRED_STEPS)
        self.assertEqual(extra, {"metadata_diff", "go_mod_tidy", "binary_schema", "dp_provider_info_defaults"})

    def test_provider_info_defaults(self):
        d = os.path.join(self.tmp, "provider")
        os.makedirs(d)
        def items(config):
            helpers.write(d, "resources.go", "package x\n\nvar _ = tfbridge.ProviderInfo{\n\tConfig: map[string]*tfbridge.SchemaInfo{\n%s\n\t},\n}\n" % config)
            return {i["name"].split(" ")[-1]: i for i in verify.check_provider_info_defaults(d)}
        ok = items('"api_key": {Default: &tfbridge.DefaultInfo{EnvVars: []string{"X_API_KEY", "Y_KEY"}}},\n"region": {Type: "x"},')
        self.assertEqual(ok["api_key"]["status"], "PASS")
        self.assertNotIn("region", ok)
        self.assertEqual(items('"api_key": {Default: &tfbridge.DefaultInfo{Value: "sk_live"}},')["api_key"]["status"], "FAIL")
        self.assertEqual(items('"api_key": {Default: &tfbridge.DefaultInfo{EnvVars: []string{}}},')["api_key"]["status"], "FAIL")
        self.assertEqual(items('"api_key": {Default: &tfbridge.DefaultInfo{EnvVars: []string{"lower"}}},')["api_key"]["status"], "FAIL")
        self.assertEqual(items('"api_key": {},')["api_key"]["status"], "PASS")
        self.assertEqual(verify.check_provider_info_defaults(os.path.join(self.tmp, "nothing"))[0]["name"], "(none)")

    def test_top_level_entries_skip_nested_maps(self):
        block = '{\n"a": {"b": {X: 1}},\n"c": {Y: 2},\n}'
        self.assertEqual([k for k, _, _ in verify._top_level_entries(block)], ["a", "c"])

    def test_schema_without_version(self):
        self.assertEqual(verify._schema_without_version('noise\n{"name": "x", "version": "1"}'), {"name": "x"})

    def make_gate(self, pulumi_output, rc=0):
        bindir = os.path.join(self.tmp, "bin")
        helpers.write(self.tmp, "bin/pulumi", "#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % (pulumi_output, rc))
        os.chmod(os.path.join(bindir, "pulumi"), 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        return verify.Gate(os.path.join(self.tmp, "logs"), env)

    def clone_with(self, schema):
        n = names.derive("demo")
        clone = os.path.join(self.tmp, "clone")
        helpers.write(clone, n["schema_path"], json.dumps(schema))
        helpers.write(clone, "bin/" + n["binary"], "x")
        return clone, n

    def test_binary_schema_passes_when_equal_modulo_version(self):
        clone, n = self.clone_with({"name": "demo", "resources": {"a": 1}})
        gate = self.make_gate(json.dumps({"name": "demo", "version": "0.0.1-dev", "resources": {"a": 1}}))
        quiet = helpers.quiet
        quiet(verify.binary_schema_step, gate, clone, n)
        self.assertEqual(gate.steps[-1]["id"], "binary_schema")
        self.assertEqual(gate.steps[-1]["status"], "PASS")

    def test_binary_schema_fails_and_names_the_differing_keys(self):
        clone, n = self.clone_with({"name": "demo", "resources": {"a": 1}})
        gate = self.make_gate(json.dumps({"name": "demo", "resources": {"a": 2}}))
        helpers.quiet(verify.binary_schema_step, gate, clone, n)
        self.assertEqual(gate.steps[-1]["status"], "FAIL")
        self.assertIn("resources", gate.steps[-1]["detail"])

    def test_binary_schema_fails_when_the_command_fails(self):
        clone, n = self.clone_with({"name": "demo"})
        gate = self.make_gate("boom", rc=3)
        helpers.quiet(verify.binary_schema_step, gate, clone, n)
        self.assertEqual(gate.steps[-1]["status"], "FAIL")

    def test_default_provider_steps_add_the_bridged_step(self):
        gate = verify.Gate(os.path.join(self.tmp, "logs2"), dict(os.environ))
        code = os.path.join(self.tmp, "p")
        helpers.write(code, "resources.go", "package x\n")
        helpers.quiet(verify.default_provider_steps, gate, {"config": {"variables": {}}}, code, self.tmp, True)
        self.assertIn("dp_provider_info_defaults", [s["id"] for s in gate.steps])
        gate2 = verify.Gate(os.path.join(self.tmp, "logs3"), dict(os.environ))
        helpers.quiet(verify.default_provider_steps, gate2, {"config": {"variables": {}}}, code, self.tmp)
        self.assertNotIn("dp_provider_info_defaults", [s["id"] for s in gate2.steps])

    def test_tfgen_env_turns_the_converter_off(self):
        self.assertEqual(verify.tfgen_env({"PULUMI_CONVERT": "1", "A": "b"}), {"PULUMI_CONVERT": "0", "A": "b"})
        self.assertEqual(verify.tfgen_env({})["PULUMI_CONVERT"], "0")

    def test_stamp_bridged_schema(self):
        schema = {"language": {"go": {"respectSchemaVersion": True}, "nodejs": {"packageName": "x"}}}
        n = names.derive("demo")
        before = verify.metadata_warnings(schema, n)
        self.assertEqual(sum("respectSchemaVersion" in w for w in before), 3)
        self.assertEqual(verify.stamp_bridged_schema(schema, n),
                         ["language.nodejs.respectSchemaVersion", "language.python.respectSchemaVersion",
                          "language.csharp.respectSchemaVersion", "language.go.importBasePath"])
        self.assertEqual(schema["language"]["nodejs"], {"packageName": "x", "respectSchemaVersion": True})
        self.assertEqual(schema["language"]["go"]["importBasePath"], n["go_import_base_path"])
        self.assertEqual(verify.stamp_bridged_schema(schema, n), [])
        self.assertEqual(sum("respectSchemaVersion" in w for w in verify.metadata_warnings(schema, n)), 0)

    def test_stamp_bridged_schema_keeps_an_existing_import_path(self):
        schema = {"language": {"go": {"importBasePath": "github.com/pulumi/pulumi-demo/sdk/go/demo"}}}
        verify.stamp_bridged_schema(schema, names.derive("demo"))
        self.assertEqual(schema["language"]["go"]["importBasePath"], "github.com/pulumi/pulumi-demo/sdk/go/demo")
        self.assertEqual(verify.stamp_bridged_schema({}, names.derive("demo"))[-1], "language.go.importBasePath")

    def test_github_org_warning(self):
        code = os.path.join(self.tmp, "gh")
        for body, want in (('Name: "x",\n\t\tGitHubOrg: "acme",', 0), ('Name: "x",', 1)):
            helpers.write(code, "provider.go", "package p\n\nfunc P() {\n\t_ = tfbridge.ProviderInfo{\n\t\t%s\n\t}\n}\n" % body)
            got = verify.github_org_warnings(code)
            self.assertEqual(len(got), want, got)
        self.assertIn("provider.go:4", got[0])
        self.assertEqual(verify.github_org_warnings(os.path.join(self.tmp, "missing")), [])


class AssertPushableTest(helpers.TempDirMixin, unittest.TestCase):
    """A bridged repo's report must contain the bridged steps; a native-shaped report does not satisfy it."""

    def setUp(self):
        super().setUp()
        mono = helpers.build_bridged_monorepo(self.tmp, "demo")
        self.repo, _ = helpers.build_bridged_mirror(self.tmp, mono, name="pr")
        self.reports = os.path.join(self.tmp, "reports")
        os.makedirs(self.reports)
        self.state = verify.git_state(self.repo)

    def write_report(self, steps, archetype=None):
        r = {"mode": "repo", "head_sha": self.state["head"], "tree_sha": self.state["tree"], "porcelain": [], "overall": "PASS",
             "steps": [{"id": i, "status": "PASS"} for i in steps]}
        if archetype:
            r["archetype"] = archetype
        helpers.write(self.reports, "pr-%s.json" % self.state["head"], json.dumps(r))
        helpers.write(self.reports, "pr-%s-secrets.json" % self.state["head"], json.dumps({
            "mode": "secret-scan", "head_sha": self.state["head"], "tree_sha": self.state["tree"], "porcelain": [], "overall": "PASS",
            "history_findings": [], "tree_findings": []}))

    def test_native_shaped_report_is_not_enough(self):
        self.write_report(verify.REQUIRED_STEPS)
        ok, msg = verify.assert_pushable(self.repo, self.reports)
        self.assertFalse(ok)
        self.assertIn("report is for the native archetype", msg)

    def test_bridged_report_without_bridged_steps_is_not_enough(self):
        self.write_report(verify.REQUIRED_STEPS, archetype="bridged")
        ok, msg = verify.assert_pushable(self.repo, self.reports)
        self.assertFalse(ok)
        self.assertRegex(msg, "lacks required step.*metadata_diff")

    def test_complete_bridged_report_is_pushable(self):
        self.write_report(verify.REQUIRED_STEPS_BRIDGED, archetype="bridged")
        ok, msg = verify.assert_pushable(self.repo, self.reports)
        self.assertTrue(ok, msg)


class DriftTest(unittest.TestCase):
    def test_tracked_paths(self):
        tracks = ["Makefile", "provider/cmd/"]
        self.assertTrue(archetype_drift.tracked("Makefile", tracks))
        self.assertTrue(archetype_drift.tracked("provider/cmd/pulumi-resource-xyz/main.go", tracks))
        self.assertFalse(archetype_drift.tracked("provider/resources.go", tracks))
        self.assertFalse(archetype_drift.tracked("Makefile.bak", tracks))

    def test_bridged_drift_lists_only_tracked_files(self):
        m = render.load_manifest("bridged")
        body = json.dumps({"status": "ahead", "ahead_by": 3, "files": [{"filename": "Makefile"}, {"filename": "README.md"},
                                                                           {"filename": "provider/cmd/pulumi-tfgen-xyz/main.go"}]}).encode()
        urls = []

        def fetcher(url):
            urls.append(url)
            return body
        status, ahead, hits = archetype_drift.bridged_drift(m, "main", fetcher)
        self.assertEqual((status, ahead, hits), ("ahead", 3, ["Makefile", "provider/cmd/pulumi-tfgen-xyz/main.go"]))
        self.assertIn("/compare/%s...main" % m["archetype_sha"], urls[0])

    def test_main_exit_codes(self):
        quiet = helpers.quiet
        clean = json.dumps({"status": "identical", "ahead_by": 0, "files": []}).encode()
        self.assertEqual(quiet(archetype_drift.main, ["--archetype", "bridged"], lambda url: clean), 0)
        dirty = json.dumps({"status": "ahead", "ahead_by": 1, "files": [{"filename": ".ci-mgmt.yaml"}]}).encode()
        self.assertEqual(quiet(archetype_drift.main, ["--archetype", "bridged"], lambda url: dirty), 2)

    def test_garbled_response(self):
        self.assertEqual(helpers.quiet(archetype_drift.main, ["--archetype", "bridged"], lambda url: b"nope"), 1)


class OnboardingTest(unittest.TestCase):
    def test_bridged_release_workflow_has_the_expected_publishing_jobs(self):
        o = render.make_options("clerk", "Clerk", [], archetype="bridged", flavor="pf")
        text = render.render_all(o)[".github/workflows/release.yml"].decode()
        issues = onboarding.build_issues("clerk", text)
        self.assertEqual(len(issues), 5)
        self.assertIn("github-secrets/pulumi-labs-pulumi-clerk", issues[0]["title"])

    def test_cli_accepts_archetype(self):
        self.assertEqual(helpers.quiet(onboarding.main, ["--provider", "clerk", "--archetype", "bridged"]), 0)


class RenderCliTest(helpers.TempDirMixin, unittest.TestCase):
    def test_render_cli_bridged_and_rerender(self):
        repo = os.path.join(self.tmp, "r")
        os.makedirs(repo)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--provider", "clerk", "--display-name", "Clerk",
                                                     "--archetype", "bridged", "--flavor", "sdkv2"]), 0)
        rec = json.loads(read(os.path.join(repo, render.OPTIONS_FILE)))
        self.assertEqual((rec["archetype"], rec["flavor"]), ("bridged", "sdkv2"))
        before = helpers.tree(repo)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo]), 0)  # the recorded options are reused
        self.assertEqual(before, helpers.tree(repo))
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--archetype", "native"]), 1)

    def test_bridged_needs_a_flavor(self):
        repo = os.path.join(self.tmp, "r2")
        os.makedirs(repo)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--provider", "clerk", "--display-name", "Clerk", "--archetype", "bridged"]), 1)


class GomodReplaceTest(unittest.TestCase):
    def test_check_local_replaces(self):
        files = {"provider/go.mod": None, "provider/shim/go.mod": None}
        ok = "module x\nreplace a => ./shim\nreplace b => github.com/c/d v1.0.0\n"
        self.assertEqual(gomod.check_local_replaces(files, "provider/go.mod", ok), [])
        for bad, pat in (("replace a => ../../x\n", "leaves"), ("replace a => ../x\n", "not part"), ("replace a => ./missing\n", "not part"), ("replace a => /abs\n", "absolute"),
                         ("replace a => C:\\\\abs\n", "")):
            probs = gomod.check_local_replaces(files, "provider/go.mod", "module x\n" + bad)
            if pat:
                self.assertRegex(" ".join(probs), pat)
        self.assertEqual(gomod.check_local_replaces(files, "provider/shim/go.mod", "module s\nreplace a => ../shim\n"), [])


if __name__ == "__main__":
    unittest.main()
