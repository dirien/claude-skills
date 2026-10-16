import hashlib
import json
import os
import re
import unittest

import helpers
import render

FIX = os.path.join(helpers.TESTS_DIR, "fixtures", "upstream")
REASON = "NVIDIA SDK does not build for windows"
OLD_COMMENT = """          # windows/amd64 is off the matrix: the NVIDIA AICR SDK does not
          # compile for GOOS=windows (unix-only syscall.O_NOFOLLOW without
          # build tags in pkg/evidence/internal/boundedio, pkg/collector/k8s,
          # pkg/bundler/checksum — present in v0.18.0 and v0.19.0). Restore it
          # once the upstream fix lands; tracked in
          # https://github.com/NVIDIA/aicr/issues (windows cross-compilation).
"""
NEW_COMMENT = "          # windows/amd64 is off the matrix: %s\n" % REASON
OLD_VERIFY = """      - name: Verify binary embeds a resolvable AICR SDK version
        # recipeVersion is derived from the binary's build info at runtime;
        # unit tests exercise that path only against fabricated build info,
        # so assert the real artifact here.
        run: go version -m bin/pulumi-resource-nvidia-aicr | grep -E 'github.com/NVIDIA/aicr\\s+v'
"""


def upstream(path):
    with open(os.path.join(FIX, path + ".orig"), encoding="utf-8") as f:
        return f.read()


class FidelityTest(unittest.TestCase):
    """Rendering nvidia-aicr reproduces the upstream archetype except for documented deltas."""

    @classmethod
    def setUpClass(cls):
        opts = render.make_options("nvidia-aicr", "NVIDIA AICR", [("windows/amd64", REASON)])
        cls.out = {k: v.decode() for k, v in render.render_all(opts).items()}

    def test_manifest_hashes_match_originals(self):
        m = render.load_manifest()
        for e in m["files"]:
            path = (os.path.join(helpers.SKILL_DIR, e["template"]) if e["mode"] == "verbatim"
                    else os.path.join(FIX, e["upstream_path"] + ".orig"))
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), e["upstream_sha256"], e["target"])

    def test_verbatim_files_are_byte_identical(self):
        m = render.load_manifest()
        for e in m["files"]:
            if e["mode"] == "verbatim":
                with open(os.path.join(helpers.SKILL_DIR, e["template"]), "rb") as f:
                    self.assertEqual(self.out[e["target"]].encode(), f.read())

    def test_makefile_only_csproj_fix(self):
        up = upstream("Makefile")
        self.assertIn("Pulumi.NvidiaAicr.csproj", up)
        want = up.replace("Pulumi.NvidiaAicr.csproj", "Pulumi.Labs.NvidiaAicr.csproj")
        self.assertEqual(self.out["Makefile"], want)
        self.assertNotIn("Pulumi.NvidiaAicr.csproj", self.out["Makefile"])

    def test_release_yml_only_windows_comment_differs(self):
        up = upstream(".github/workflows/release.yml")
        self.assertIn(OLD_COMMENT, up)
        self.assertEqual(self.out[".github/workflows/release.yml"], up.replace(OLD_COMMENT, NEW_COMMENT))

    def test_test_yml_deltas(self):
        up = upstream(".github/workflows/test.yml")
        self.assertIn(OLD_COMMENT, up)
        self.assertIn(OLD_VERIFY, up)
        want = up.replace(OLD_COMMENT, NEW_COMMENT).replace(OLD_VERIFY, "")
        self.assertEqual(self.out[".github/workflows/test.yml"], want)
        self.assertNotIn("AICR", self.out[".github/workflows/test.yml"])

    def test_launch_json_identical(self):
        self.assertEqual(self.out[".vscode/launch.json"], upstream(".vscode/launch.json"))

    def test_contributing_is_generic(self):
        c = self.out["CONTRIBUTING.md"]
        up = upstream("CONTRIBUTING.md")
        self.assertIn("contributing to the Pulumi NVIDIA AICR provider", c)
        self.assertIn("recipe data", up.lower())
        self.assertNotIn("recipe", c.lower())
        self.assertNotIn("ClusterStack", c)
        self.assertIn("pulumi-labs/pulumi-nvidia-aicr.git", c)

    def test_no_leftover_placeholders(self):
        for path, text in self.out.items():
            if path in ("Makefile", "CONTRIBUTING.md") or path.endswith((".yml", ".json")):
                self.assertIsNone(re.search(r"__[A-Z][A-Z0-9_]*__", text), path)


class MatrixTest(helpers.TempDirMixin, unittest.TestCase):
    def test_windows_built_by_default(self):
        out = render.render_all(render.make_options("modal", "Modal", []))
        for f in (".github/workflows/release.yml", ".github/workflows/test.yml"):
            self.assertIn("{ goos: windows, goarch: amd64 }", out[f].decode())
            self.assertNotIn("off the matrix", out[f].decode())

    def test_exclusion_writes_reason_comment(self):
        out = render.render_all(render.make_options("modal", "Modal", [("windows/amd64", "because x")]))
        rel = out[".github/workflows/release.yml"].decode()
        self.assertIn("# windows/amd64 is off the matrix: because x", rel)
        self.assertNotIn("- { goos: windows", rel)

    def test_errors(self):
        with self.assertRaisesRegex(render.RenderError, "unknown platform"):
            render.make_options("modal", "Modal", [("plan9/386", "x")])
        with self.assertRaisesRegex(render.RenderError, "--reason"):
            render.make_options("modal", "Modal", [("windows/amd64", " ")])
        with self.assertRaisesRegex(render.RenderError, "invalid provider slug"):
            render.make_options("Modal", "Modal", [])

    def test_leftover_placeholder_fails(self):
        skill = os.path.join(self.tmp, "skill")
        helpers.write(skill, "archetype/x.tmpl", "hello __PROVIDER__ __BOGUS__\n")
        entry = {"template": "archetype/x.tmpl", "target": "x", "mode": "render"}
        with self.assertRaisesRegex(render.RenderError, "leftover placeholder.*__BOGUS__"):
            render.render_entry(entry, render.make_options("modal", "Modal", []), skill_dir=skill)


class CliTest(helpers.TempDirMixin, unittest.TestCase):
    def test_pairing_and_rerender(self):
        repo = os.path.join(self.tmp, "r")
        os.makedirs(repo)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--provider", "modal", "--display-name", "Modal",
                                      "--exclude-platform", "windows/amd64"]), 1)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--provider", "modal", "--display-name", "Modal",
                                      "--exclude-platform", "windows/amd64", "--reason", "r"]), 0)
        rec = json.load(open(os.path.join(repo, ".pulumi-labs-export.json")))
        self.assertEqual(rec["provider"], "modal")
        self.assertEqual(rec["exclude_platforms"], [{"platform": "windows/amd64", "reason": "r"}])
        # re-render reuses recorded options and changes nothing
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo]), 0)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo, "--provider", "other"]), 1)

    def test_first_render_needs_provider(self):
        repo = os.path.join(self.tmp, "r")
        os.makedirs(repo)
        self.assertEqual(helpers.quiet(render.main, ["--repo-dir", repo]), 1)


if __name__ == "__main__":
    unittest.main()
