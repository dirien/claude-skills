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


def build_monorepo(root, slug="demo", display="Demo", version="0.1.0"):
    """Create a synthetic autoregistry checkout with a minimal mirrored lib/providers/<slug>; return its path.

    The provider already carries the pulumi-labs identity (the state after the identity-switch PR).
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
    write(mono, base + "provider/go.mod", "module %s\n\ngo 1.27\n\nrequire github.com/pulumi/pulumi-go-provider v1.3.1\n" % mod)
    write(mono, base + "provider/go.sum", "github.com/pulumi/pulumi-go-provider v1.3.1 h1:abc=\n")
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
