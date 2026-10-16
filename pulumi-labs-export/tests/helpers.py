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
            "go": {"importBasePath": n["go_import_base_path"]},
            "nodejs": {"packageName": n["npm_package"], "packageDescription": "Experimental %s provider." % slug},
            "python": {"packageName": n["python_package"]},
            "csharp": {"rootNamespace": n["dotnet_root_namespace"]},
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
