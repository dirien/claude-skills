"""Shared test helpers: path setup and a synthetic carved-repo fixture."""
import contextlib
import io
import json
import os
import tempfile
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import names  # noqa: E402
import render  # noqa: E402

GO_HEADER = "// Copyright 2026, Pulumi Corporation.\n"


def write(repo, rel, content):
    """Write text content under repo, creating parents."""
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def git(repo, *args):
    """Run git in repo with a throwaway identity; raises on failure."""
    cmd = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"] + list(args)
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def commit_all(repo, msg="fixture"):
    """Stage everything and commit."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def schema_for(slug, **over):
    """Return a schema dict that satisfies check.py for slug; override top-level keys via over."""
    n = names.derive(slug)
    s = {
        "name": slug,
        "description": "An experimental provider for %s." % slug,
        "publisher": n["publisher"],
        "repository": n["repo_url"],
        "pluginDownloadURL": n["plugin_download_url"],
        "language": {
            "go": {"importBasePath": n["go_import_base_path"], "respectSchemaVersion": True},
            "nodejs": {"packageName": n["npm_package"], "packageDescription": "Experimental %s provider." % slug,
                       "respectSchemaVersion": True},
            "python": {"packageName": n["python_package"], "respectSchemaVersion": True},
            "csharp": {"rootNamespace": n["dotnet_root_namespace"], "respectSchemaVersion": True},
            "java": {"basePackage": n["java_base_package"]},
        },
    }
    s.update(over)
    return s


def build_repo(root, slug="modal", display="Modal"):
    """Create a minimal valid carved repo under root, committed; return its path."""
    repo = os.path.join(root, "pulumi-" + slug)
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    n = names.derive(slug)
    render.write_repo(repo, render.make_options(slug, display, []))
    write(repo, "README.md", "# Pulumi %s\n\n> **Experimental.** This provider is experimental and may change.\n\nUses pulumi_labs_%s, Pulumi.Labs.%s and com.pulumi.labs.\n"
          % (display, slug.replace("-", "_"), n["dotnet_pascal"]))
    write(repo, "CLAUDE.md", "# Dev guide\n")
    write(repo, "docs/_index.md", "---\ntitle: x\nmeta_desc: x\nlayout: package\n---\n")
    write(repo, "docs/installation-configuration.md", "---\ntitle: x\nmeta_desc: x\nlayout: package\n---\n")
    write(repo, "go.mod", "module %s\n\ngo 1.27\n" % n["go_module"])
    write(repo, "go.sum", "")
    write(repo, "examples/README.md", "examples\n")
    write(repo, n["cmd_dir"] + "/main.go", GO_HEADER + "package main\n\nfunc main() {}\n")
    write(repo, n["schema_path"], json.dumps(schema_for(slug), indent=2) + "\n")
    write(repo, "provider/pkg/provider/provider.go", GO_HEADER + "package provider\n")
    write(repo, n["dotnet_csproj"], "<Project/>\n")
    write(repo, n["go_sdk_dir"] + "/doc.go", "package x\n")
    write(repo, n["go_sdk_dir"] + "/go.mod", "module %s\n\ngo 1.27\n" % n["go_import_base_path"])
    for lang in ("java", "nodejs", "python"):
        write(repo, "sdk/%s/placeholder.txt" % lang, "x\n")
    write(repo, "sdk/nodejs/package.json", json.dumps({"name": n["npm_package"], "version": "0.0.1-dev"}, indent=2) + "\n")
    write(repo, "sdk/dotnet/version.txt", "0.0.1-dev\n")
    commit_all(repo)
    return repo


class TempDirMixin:
    """unittest mixin giving each test a temp dir in self.tmp."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)


def quiet(fn, *args, **kw):
    """Call fn with stdout and stderr swallowed; return its result."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kw)


MONO_MODULE = "github.com/pulumi/autoregistry/lib/providers/%s/provider"


def build_monorepo(root, slug="demo", display="Demo", version="0.1.0", layout="nested"):
    """Create a synthetic autoregistry checkout with a minimal mirrored lib/providers/<slug>; return its path.

    The provider already carries the pulumi-labs identity (the state after the identity-switch PR).
    layout="nested" puts the Go module at lib/providers/<slug>/provider/go.mod (module .../<slug>/provider);
    layout="root" puts it at lib/providers/<slug>/go.mod (module .../<slug>), the #1248 target. Go import
    paths (.../<slug>/provider/pkg/...) are the same in both.
    """
    mono = os.path.join(root, "autoregistry")
    os.makedirs(mono)
    subprocess.run(["git", "init", "-q", "-b", "main", mono], check=True)
    n = names.derive(slug)
    base = "lib/providers/%s/" % slug
    mod = MONO_MODULE % slug
    write(mono, "carved-providers.json", json.dumps([{
        "name": slug, "repo": n["repo"], "goModule": n["go_module"], "mode": "mirror", "registryName": slug,
        "listed": False, "exportedAt": None, "lastSyncedCommit": None, "lastSyncedVersion": None}], indent=2) + "\n")
    write(mono, "README.md", "monorepo root, must never be copied\n")
    write(mono, base + "version.txt", version + "\n")
    write(mono, base + "Makefile", "# MONOREPO MAKEFILE MARKER\nbuild:\n\techo hi\n")
    write(mono, base + ".gitignore", "# MONOREPO GITIGNORE MARKER\n")
    write(mono, base + "README.md", "# Demo\n\n> **Experimental.** Demo provider.\n")
    write(mono, base + "CHANGELOG.md", "# Changelog\n\n- first\n")
    write(mono, base + "skills/SKILLS.md", "# Demo dev guide\n\nBuild with make.\n")
    write(mono, base + "docs/_index.md", "---\ntitle: Demo\nmeta_desc: x\nlayout: package\n---\n")
    write(mono, base + "docs/installation-configuration.md", "---\ntitle: Demo\nmeta_desc: x\nlayout: package\n---\n")
    modroot = os.path.dirname(mod) if layout == "root" else mod
    modbase = base if layout == "root" else base + "provider/"
    write(mono, modbase + "go.mod", "module %s\n\ngo 1.27\n\nrequire github.com/pulumi/pulumi-go-provider v1.3.1\n" % modroot)
    write(mono, modbase + "go.sum", "github.com/pulumi/pulumi-go-provider v1.3.1 h1:abc=\n")
    write(mono, base + "provider/cmd/pulumi-resource-%s/main.go" % slug,
          GO_HEADER + 'package main\n\nimport (\n\t"%s/pkg/provider"\n\t"%s/pkg/version"\n)\n\nfunc main() { provider.New(version.Version) }\n' % (mod, mod))
    write(mono, base + "provider/pkg/provider/provider.go", GO_HEADER +
          'package provider\n\n// New builds the provider.\nfunc New(v string) {\n\t_ = infer.NewProviderBuilder().\n'
          '\t\tWithDisplayName("%s").\n\t\tWithPublisher("Pulumi Labs").\n\t\tWithRepository("%s").\n'
          '\t\tWithGoImportPath("%s")\n}\n' % (display, n["repo_url"], n["go_import_base_path"]))
    write(mono, base + "provider/pkg/provider/provider_test.go", GO_HEADER + 'package provider\n\nimport _ "%s/pkg/version"\n' % mod)
    write(mono, base + "provider/pkg/version/version.go", GO_HEADER + "package version\n\nvar Version string\n")
    write(mono, base + "examples/%s-go/main.go" % slug, GO_HEADER + 'package main\n\nimport _ "%s/pkg/version"\n\nfunc main() {}\n' % mod)
    write(mono, base + "examples/%s-go/go.mod" % slug, "module example\n\ngo 1.27\n\nrequire %s v0.1.0\n" % n["go_import_base_path"])
    write(mono, base + "templates/%s-go/Pulumi.yaml" % slug, "name: t\nruntime: go\n")
    script = os.path.join(mono, base, "examples", "run.sh")
    write(mono, base + "examples/run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    commit_all(mono, "monorepo fixture")
    return mono


def build_out(root, name="mirror"):
    """Create an empty git-init'd mirror directory; return its path."""
    out = os.path.join(root, name)
    os.makedirs(out)
    subprocess.run(["git", "init", "-q", "-b", "main", out], check=True)
    return out


def tree(root):
    """Return {relative path: (bytes, exec bit)} of every file under root outside .git."""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            full = os.path.join(dp, fn)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = (f.read(), bool(os.stat(full).st_mode & 0o111))
    return out


FAKE_GITLEAKS = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a[:1] == ["version"]:
    print("8.30.1-fake")
    sys.exit(0)
with open(os.environ["FAKE_GITLEAKS_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
if os.environ.get("FAKE_GITLEAKS_CRASH"):
    print("fatal: boom", file=sys.stderr)
    sys.exit(126)
kind = a[0]
findings = json.loads(os.environ.get("FAKE_GITLEAKS_FINDINGS", "{}")).get(kind, [])
out = a[a.index("--report-path") + 1]
with open(out, "w") as f:
    json.dump(findings, f)
sys.exit(1 if findings else 0)
"""


def install_fake_gitleaks(root):
    """Write an executable fake gitleaks under root; return (path, log path). Findings come from FAKE_GITLEAKS_FINDINGS."""
    path = os.path.join(root, "gitleaks")
    write(root, "gitleaks", FAKE_GITLEAKS)
    os.chmod(path, 0o755)
    return path, os.path.join(root, "gitleaks.log")


@contextlib.contextmanager
def env(**kw):
    """Temporarily set (or with None, unset) environment variables."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ----------------------------------------------------------------- bridged archetype fixtures

BRIDGED_VANITY = "github.com/pulumi/pulumi-%s/provider"
BRIDGED_SHIM = "github.com/example/terraform-provider-%s/pulumi-bridge-shim"


def bridged_resources_go(slug="demo", display="Demo", switched=True):
    """Return a resources.go with a tfbridge.ProviderInfo literal, in the monorepo shape (switched: pulumi-labs identity)."""
    n = names.derive(slug)
    pub = "Pulumi Labs" if switched else "Pulumi"
    repo = n["repo_url"] if switched else "https://github.com/pulumi/autoregistry"
    url = n["plugin_download_url"] if switched else "github://api.github.com/pulumi/autoregistry"
    npm = n["npm_package"] if switched else "@pulumi/" + slug
    py = n["python_package"] if switched else "pulumi_" + slug.replace("-", "_")
    cs = n["dotnet_root_namespace"] if switched else "Pulumi"
    java = n["java_base_package"] if switched else "com.pulumi"
    go = n["go_import_base_path"] if switched else "github.com/pulumi/pulumi-%s/sdk/go/%s" % (slug, n["go_pkg"])
    rsv = "RespectSchemaVersion: true,\n\t\t\t" if switched else ""
    pyproj = "PyProject: struct{ Enabled bool }{true}," if switched else ""
    desc = "Experimental: manage Demo." if switched else "Manage Demo."
    return GO_HEADER + '''// Package %(pkg)s is the bridged provider.
package %(pkg)s

import (
	pf "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/pf/tfbridge"
	"github.com/pulumi/pulumi-terraform-bridge/v3/pkg/tfbridge"

	shim "%(shim)s"
	"%(vanity)s/pkg/version"
)

// Version is the provider version.
var Version = version.Version

// Provider returns the ProviderInfo.
func Provider() tfbridge.ProviderInfo {
	return tfbridge.ProviderInfo{
		P:           pf.ShimProvider(shim.NewProvider()),
		Name:        "%(slug)s",
		DisplayName: "%(display)s",
		Publisher:   "%(pub)s",
		Description: "%(desc)s",
		License:     "Apache-2.0",
		Repository:  "%(repo)s",
		PluginDownloadURL: "%(url)s",
		Config: map[string]*tfbridge.SchemaInfo{
			"api_key": {
				Default: &tfbridge.DefaultInfo{
					EnvVars: []string{"DEMO_API_KEY"},
				},
			},
		},
		JavaScript: &tfbridge.JavaScriptInfo{
			PackageName: "%(npm)s",
			%(rsv)sDependencies: map[string]string{"@pulumi/pulumi": "^3.0.0"},
		},
		Python: &tfbridge.PythonInfo{
			PackageName: "%(py)s",
			%(rsv)s%(pyproj)s
		},
		Golang: &tfbridge.GolangInfo{
			ImportBasePath: "%(go)s",
			%(rsv)sGenerateResourceContainerTypes: true,
		},
		CSharp: &tfbridge.CSharpInfo{
			RootNamespace: "%(cs)s",
			%(rsv)sPackageReferences: map[string]string{"Pulumi": "3.*"},
		},
		Java: &tfbridge.JavaInfo{
			BasePackage: "%(java)s",
		},
	}
}
''' % {"pkg": slug.replace("-", ""), "shim": BRIDGED_SHIM % slug, "vanity": BRIDGED_VANITY % slug, "slug": slug,
       "display": display, "pub": pub, "desc": desc, "repo": repo, "url": url, "npm": npm, "py": py, "go": go, "cs": cs,
       "java": java, "rsv": rsv, "pyproj": pyproj}


BRIDGED_RESOURCE_MAIN_PF = GO_HEADER + '''// Command pulumi-resource-demo is the provider resource server binary.
package main

import (
	"context"

	demo "github.com/pulumi/pulumi-demo/provider"
	pf "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/pf/tfbridge"
)

// PackageSchema is a placeholder.
var PackageSchema = []byte("{}")

func main() {
	meta := pf.ProviderMetadata{PackageSchema: PackageSchema}
	pf.Main(context.Background(), "demo", demo.Provider(), meta)
}
'''
BRIDGED_TFGEN_MAIN_PF = GO_HEADER + '''// Command pulumi-tfgen-demo generates the schema.
package main

import (
	demo "github.com/pulumi/pulumi-demo/provider"
	pftfgen "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/pf/tfgen"
)

func main() {
	pftfgen.Main("demo", demo.Provider())
}
'''
BRIDGED_RESOURCE_MAIN_SDKV2 = GO_HEADER + '''// Command pulumi-resource-demo is the provider resource server binary.
package main

import (
	demo "github.com/pulumi/pulumi-demo/provider"
	"github.com/pulumi/pulumi-terraform-bridge/v3/pkg/tfbridge"
)

func main() {
	prov := demo.Provider()
	tfbridge.Main("demo", demo.Version, prov, nil)
}
'''
BRIDGED_TFGEN_MAIN_SDKV2 = GO_HEADER + '''// Command pulumi-tfgen-demo generates the schema.
package main

import (
	demo "github.com/pulumi/pulumi-demo/provider"
	tfgen "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/tfgen"
)

func main() {
	tfgen.Main("demo", demo.Version, demo.Provider())
}
'''


def build_bridged_monorepo(root, slug="demo", display="Demo", version="0.1.0", flavor="pf", switched=True):
    """Create a synthetic autoregistry checkout with a Terraform-bridged lib/providers/<slug> (the monorepo shape).

    The provider module is the vanity path github.com/pulumi/pulumi-<slug>/provider and reaches its shim module through
    a relative replace. switched=True carries the pulumi-labs identity (state after the identity-switch PR).
    """
    mono = os.path.join(root, "autoregistry")
    os.makedirs(mono)
    subprocess.run(["git", "init", "-q", "-b", "main", mono], check=True)
    n = names.derive(slug)
    base = "lib/providers/%s/" % slug
    mod = BRIDGED_VANITY % slug
    shim = BRIDGED_SHIM % slug
    write(mono, "carved-providers.json", json.dumps([{
        "name": slug, "repo": n["repo"], "goModule": n["go_module"], "mode": "mirror", "registryName": slug,
        "listed": False, "exportedAt": None, "lastSyncedCommit": None, "lastSyncedVersion": None}], indent=2) + "\n")
    write(mono, "README.md", "monorepo root, must never be copied\n")
    if version:
        write(mono, base + "version.txt", version + "\n")
    write(mono, base + "Makefile", "# MONOREPO MAKEFILE MARKER\nbuild:\n\techo hi\n")
    write(mono, base + ".gitignore", "# MONOREPO GITIGNORE MARKER\nschema.json\nsdk/\n")
    write(mono, base + "README.md", "# Demo\n\n> **Experimental.** Demo provider.\n")
    write(mono, base + "CHANGELOG.md", "# Changelog\n\n- first\n")
    write(mono, base + "skills/SKILLS.md", "# Demo dev guide\n\nBuild with make.\n")
    write(mono, base + "docs/_index.md", "---\ntitle: Demo\nmeta_desc: x\nlayout: package\n---\n")
    write(mono, base + "docs/installation-configuration.md", "---\ntitle: Demo\nmeta_desc: x\nlayout: package\n---\n")
    write(mono, base + "provider/go.mod", "module %s\n\ngo 1.27\n\nrequire (\n\t%s v0.0.0\n\tgithub.com/pulumi/pulumi-terraform-bridge/v3 v3.125.0\n)\n\n"
          "replace %s => ./shim\n\nreplace github.com/hashicorp/terraform-plugin-sdk/v2 => github.com/pulumi/terraform-plugin-sdk/v2 v2.0.0-20260318212141-5525259d096b\n"
          % (mod, shim, shim))
    write(mono, base + "provider/go.sum", "github.com/pulumi/pulumi-terraform-bridge/v3 v3.125.0 h1:abc=\n")
    write(mono, base + "provider/resources.go", bridged_resources_go(slug, display, switched))
    write(mono, base + "provider/resources_test.go", GO_HEADER + 'package %s_test\n\nimport (\n\t"testing"\n\n\t%s "%s"\n)\n\n'
          'func TestProvider(t *testing.T) { _ = %s.Provider() }\n' % (n["go_pkg"], n["go_pkg"], mod, n["go_pkg"]))
    write(mono, base + "provider/pkg/version/version.go", GO_HEADER + 'package version\n\n// Version is the dev default.\nvar Version = "0.0.1-alpha.0"\n')
    mains = {"pf": (BRIDGED_RESOURCE_MAIN_PF, BRIDGED_TFGEN_MAIN_PF), "sdkv2": (BRIDGED_RESOURCE_MAIN_SDKV2, BRIDGED_TFGEN_MAIN_SDKV2)}[flavor]
    for text, rel in zip(mains, ("provider/cmd/pulumi-resource-%s/main.go", "provider/cmd/pulumi-tfgen-%s/main.go")):
        write(mono, base + rel % slug, text.replace("demo", slug).replace("pulumi/pulumi-" + slug, "pulumi/pulumi-" + slug))
    write(mono, base + "provider/cmd/pulumi-resource-%s/bridge-metadata.json" % slug, "{}")
    write(mono, base + "provider/shim/go.mod", "module %s\n\ngo 1.27\n" % shim)
    write(mono, base + "provider/shim/go.sum", "")
    write(mono, base + "provider/shim/shim.go", GO_HEADER + "package shim\n\n// NewProvider returns the upstream provider.\nfunc NewProvider() int { return 0 }\n")
    write(mono, base + "examples/%s-go/main.go" % slug, GO_HEADER + 'package main\n\nimport _ "%s/sdk/go/%s"\n\nfunc main() {}\n' % (
        n["go_module"] if switched else "github.com/pulumi/pulumi-" + slug, n["go_pkg"]))
    write(mono, base + "examples/%s-go/go.mod" % slug, "module example\n\ngo 1.27\n\nrequire %s v0.1.0\n" % n["go_import_base_path"])
    write(mono, base + "templates/%s-go/Pulumi.yaml" % slug, "name: t\nruntime: go\n")
    commit_all(mono, "bridged monorepo fixture")
    return mono


def build_bridged_mirror(root, mono, slug="demo", name="mirror"):
    """Export slug from the bridged fixture monorepo into a fresh git repo and add stub generated files; return (repo, options).

    The stubs stand in for `make schema` and `make sdks` (which need the Go and Pulumi toolchains), so check.py can run.
    """
    import export  # noqa: E402  (imported lazily: export imports check, which tests patch)
    out = build_out(root, name)
    options = export.export(mono, slug, out, skill_commit=None)
    n = names.derive(slug)
    write(out, n["schema_path"], json.dumps(schema_for(slug), indent=2) + "\n")
    write(out, n["cmd_dir"] + "/Pulumi.yaml", "name: %s\nlanguage: schema\n" % slug)
    write(out, n["dotnet_csproj"], "<Project/>\n")
    write(out, n["go_sdk_dir"] + "/doc.go", "package x\n")
    write(out, n["go_sdk_dir"] + "/go.mod", "module %s\n\ngo 1.27\n" % n["go_import_base_path"])
    for lang in ("java", "nodejs", "python"):
        write(out, "sdk/%s/placeholder.txt" % lang, "x\n")
    write(out, "sdk/nodejs/package.json", json.dumps({"name": n["npm_package"], "version": "0.0.1-dev"}, indent=2) + "\n")
    write(out, "sdk/dotnet/version.txt", "0.0.1-dev\n")
    commit_all(out, "export")
    return out, options
