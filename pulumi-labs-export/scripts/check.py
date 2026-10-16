#!/usr/bin/env python3
"""Verification gate for a carved pulumi-labs provider repo.

Run before every push and before every release:

    python3 scripts/check.py --repo ../pulumi-modal

Reports ALL failures (not just the first) and exits 1 if there are any.
Everything is derived from the provider recorded in .pulumi-labs-export.json
by render.py; nothing is typed by hand.

Drift detection for a repo produced by export.py (the generated mirror):

    python3 scripts/check.py --repo ../pulumi-modal --against-source ../autoregistry

re-runs the export at the source SHA recorded in .pulumi-labs-export.json
(local git only) and lists every path that differs, except sdk/ and the
generated schema file. A hand edit in the mirror shows up as drift.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gomod as gomodlib  # noqa: E402
import names  # noqa: E402
import render  # noqa: E402

# Provider-authored files that the skill does not template but requires.
REQUIRED_FILES = [
    "README.md",
    "CLAUDE.md",
    "docs/_index.md",
    "docs/installation-configuration.md",
    "go.mod",
    "go.sum",
]
REQUIRED_DIRS = ["examples", "sdk/dotnet", "sdk/go", "sdk/java", "sdk/nodejs", "sdk/python"]
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_HITS = 100
# Version the committed SDKs carry. `make sdks` defaults to it, export.py --generate passes it, and the release
# workflow regenerates the SDKs with the real tag, so a committed SDK never claims a version that was not released.
SDK_DEV_VERSION = "0.0.1-dev"
# Languages whose generator only honours `gen-sdk --version` (package.json, version.txt, csproj) with respectSchemaVersion.
VERSIONED_LANGUAGES = ("go", "nodejs", "python", "csharp")


def namespace_patterns(slug):
    """Return [(label, compiled regex)] of forbidden old-namespace references."""
    n = names.derive(slug)
    p = re.escape(slug)
    under = re.escape(slug.replace("-", "_"))
    pas = re.escape(n["dotnet_pascal"])
    tail = r"(?![A-Za-z0-9_-])"
    java_names = sorted({slug, slug.replace("-", ""), slug.replace("-", "_")})
    java = "|".join(re.escape(x) for x in java_names)
    return [
        ("monorepo path pulumi/autoregistry", re.compile(r"pulumi/autoregistry")),
        ("old Go path github.com/pulumi/pulumi-%s" % slug, re.compile(r"github\.com/pulumi/pulumi-" + p + tail)),
        ("old npm name @pulumi/%s" % slug, re.compile(r"@pulumi/" + p + tail)),
        ("old Python name pulumi_%s" % slug.replace("-", "_"),
         re.compile(r"(?<![A-Za-z0-9_])pulumi_" + under + r"(?![A-Za-z0-9_])")),
        ("old .NET name Pulumi.%s (new one is Pulumi.Labs.%s)" % (n["dotnet_pascal"], n["dotnet_pascal"]),
         re.compile(r"(?<!Labs\.)Pulumi\." + pas + r"(?![A-Za-z0-9_])")),
        ("old Java package com.pulumi.%s" % slug,
         re.compile(r"com\.pulumi\.(?:" + java + r")(?![A-Za-z0-9_])")),
    ]


def sdk_version_failures(repo, want=SDK_DEV_VERSION):
    """Return failures for generated SDK manifests that are not stamped with want (nodejs package.json, dotnet version.txt)."""
    fails = []
    pkg = os.path.join(repo, "sdk", "nodejs", "package.json")
    if os.path.isfile(pkg):
        try:
            got = _read_json(pkg).get("version")
        except (ValueError, AttributeError):
            got = None
        if got != want:
            if got == "${VERSION}":
                hint = ("`gen-sdk --version` was ignored: the schema's language settings lack respectSchemaVersion (WithLanguageMap replaces "
                        "the framework defaults); fix the monorepo builder, then regenerate.")
            else:
                hint = "regenerate with `make sdks VERSION=%s` (export.py --generate does)." % want
            fails.append("WRONG sdk/nodejs/package.json version: got %r, want %r: %s" % (got, want, hint))
    dotnet = os.path.join(repo, "sdk", "dotnet")
    if os.path.isdir(dotnet) and any(f.endswith(".csproj") for f in os.listdir(dotnet)):
        vt = os.path.join(dotnet, "version.txt")
        if not os.path.isfile(vt):
            fails.append("MISSING sdk/dotnet/version.txt: the generated csproj embeds it, so `dotnet build` fails. gen-sdk writes it only with "
                         "`--version` plus respectSchemaVersion in the schema's csharp language settings; fix the monorepo builder, then regenerate.")
        else:
            with open(vt, encoding="utf-8", errors="replace") as f:
                got = f.read().strip()
            if got != want:
                fails.append("WRONG sdk/dotnet/version.txt: got %r, want %r (regenerate with `make sdks VERSION=%s`)" % (got, want, want))
    return fails


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, check=False)


def tracked_files(repo):
    """Return the set of git-tracked paths or None if repo is not a git repo."""
    r = _git(repo, "ls-files", "-z")
    if r.returncode != 0:
        return None
    return {x for x in r.stdout.decode("utf-8", "replace").split("\0") if x}


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _first_diff(expected, actual):
    a = expected.decode("utf-8", "replace").splitlines()
    b = actual.decode("utf-8", "replace").splitlines()
    diff = list(difflib.unified_diff(a, b, "expected (render.py)", "actual", lineterm="", n=0))
    return "\n".join("      " + line for line in diff[:12])


def check_repo(repo, against_source=None):
    """Run every check against repo and return a list of failure strings.

    With against_source (an autoregistry checkout) the mirror is also compared with a fresh export.
    """
    fails = []
    repo = os.path.abspath(repo)
    opt_path = os.path.join(repo, render.OPTIONS_FILE)
    if not os.path.isdir(repo):
        return ["repo directory %s does not exist" % repo]
    if not os.path.exists(opt_path):
        return ["MISSING %s: this repo was never rendered. Run: python3 scripts/render.py --repo-dir %s --provider <slug> --display-name <name>" % (render.OPTIONS_FILE, repo)]
    try:
        options = _read_json(opt_path)
        slug = options["provider"]
        n = names.derive(slug)
        manifest = render.load_manifest()
    except (ValueError, KeyError, names.SlugError) as e:
        return ["INVALID %s: %s. Re-run render.py." % (render.OPTIONS_FILE, e)]

    tracked = tracked_files(repo)
    if tracked is None:
        fails.append("NOT A GIT REPO: %s. The gate scans git-tracked files; run git init/add/commit first." % repo)

    def exists(rel):
        return os.path.exists(os.path.join(repo, rel))

    def is_tracked(rel):
        return tracked is None or rel in tracked

    # 1. Required files.
    required = [e["target"] for e in manifest["files"]] + [render.OPTIONS_FILE] + REQUIRED_FILES + [
        n["cmd_dir"] + "/main.go", n["schema_path"]]
    for rel in required:
        if not exists(rel):
            fails.append("MISSING required file: %s" % rel)
        elif not is_tracked(rel):
            fails.append("UNTRACKED required file: %s exists but is not added to git" % rel)
    for rel in REQUIRED_DIRS:
        full = os.path.join(repo, rel)
        if not os.path.isdir(full) or not any(files for _, _, files in os.walk(full)):
            fails.append("MISSING or empty required directory: %s/" % rel)
        elif tracked is not None and not any(t.startswith(rel + "/") for t in tracked):
            fails.append("UNTRACKED required directory: %s/ has no git-tracked files" % rel)

    # 2. No hand edits.
    if options.get("archetype_sha") != manifest["archetype_sha"] or options.get("archetype_repo") != manifest["archetype_repo"]:
        fails.append("ARCHETYPE CHANGED: repo was rendered from %s@%s but this skill vendors %s@%s. Re-run render.py --repo-dir %s and review the diff."
                     % (options.get("archetype_repo"), options.get("archetype_sha"), manifest["archetype_repo"], manifest["archetype_sha"], repo))
    try:
        expected = render.render_all(render.make_options(
            slug, options.get("display_name"),
            [(e["platform"], e["reason"]) for e in options.get("exclude_platforms", [])], manifest), manifest)
    except (render.RenderError, KeyError, TypeError) as e:
        expected = {}
        fails.append("INVALID %s options: %s" % (render.OPTIONS_FILE, e))
    for rel, want in sorted(expected.items()):
        if not exists(rel):
            continue
        with open(os.path.join(repo, rel), "rb") as f:
            got = f.read()
        if got != want:
            fails.append("HAND-EDITED %s differs from render.py output. Archetype files must not be edited by hand; "
                         "restore with `python3 scripts/render.py --repo-dir %s`. If the archetype itself needs a change, "
                         "report it to a human.\n%s" % (rel, repo, _first_diff(want, got)))

    # 3. Forbidden files.
    for rel in ("version.txt", "provider/version.txt", "provider/go.mod"):
        if exists(rel):
            fails.append("FORBIDDEN file: %s (remove it; the module lives at the repo root and the version comes from the git tag)" % rel)
    pdir = os.path.join(repo, "provider")
    for root, _, files in os.walk(pdir):
        if "go.mod" in files:
            rel = os.path.relpath(os.path.join(root, "go.mod"), repo)
            if rel != "provider/go.mod":
                fails.append("FORBIDDEN file: %s (no go.mod anywhere under provider/)" % rel)

    # 4. Root go.mod module.
    gomod = os.path.join(repo, "go.mod")
    if os.path.exists(gomod):
        m = None
        with open(gomod, encoding="utf-8", errors="replace") as f:
            for line in f:
                mm = re.match(r"\s*module\s+(\S+)", line)
                if mm:
                    m = mm.group(1).strip('"')
                    break
        if m != n["go_module"]:
            fails.append("WRONG go.mod module: got %r, want %r" % (m, n["go_module"]))

    # 5. Schema vs naming table.
    schema_path = os.path.join(repo, n["schema_path"])
    schema = None
    if os.path.exists(schema_path):
        try:
            schema = _read_json(schema_path)
        except ValueError as e:
            fails.append("INVALID JSON in %s: %s" % (n["schema_path"], e))
    if schema is not None:
        want = [
            (("name",), slug),
            (("publisher",), n["publisher"]),
            (("repository",), n["repo_url"]),
            (("pluginDownloadURL",), n["plugin_download_url"]),
            (("language", "go", "importBasePath"), n["go_import_base_path"]),
            (("language", "nodejs", "packageName"), n["npm_package"]),
            (("language", "python", "packageName"), n["python_package"]),
            (("language", "csharp", "rootNamespace"), n["dotnet_root_namespace"]),
            (("language", "java", "basePackage"), n["java_base_package"]),
        ]
        for path, val in want:
            got = _dig(schema, *path)
            if got != val:
                fails.append("WRONG schema %s in %s: got %r, want %r" % (".".join(path), n["schema_path"], got, val))
        for lang in VERSIONED_LANGUAGES:
            if _dig(schema, "language", lang, "respectSchemaVersion") is not True:
                fails.append("MISSING schema language.%s.respectSchemaVersion=true in %s: without it `gen-sdk --version` is ignored (SDKs keep "
                             "${VERSION}, no sdk/dotnet/version.txt, the .NET build fails). The builder's WithLanguageMap replaces the framework "
                             "defaults, so it must carry them (references/export.md, step b)." % (lang, n["schema_path"]))
        for path in (("description",), ("language", "nodejs", "packageDescription")):
            got = _dig(schema, *path)
            if not isinstance(got, str) or "experimental" not in got.lower():
                fails.append("NOT MARKED EXPERIMENTAL: schema %s must mention \"experimental\" (got %r)" % (".".join(path), got))

    # 6. Generated SDK names.
    if not exists(n["dotnet_csproj"]):
        fails.append("MISSING generated SDK file %s (regenerate with `make dotnet_sdk`; schema csharp.rootNamespace must be Pulumi.Labs)" % n["dotnet_csproj"])
    if not os.path.isdir(os.path.join(repo, n["go_sdk_dir"])):
        fails.append("MISSING generated SDK dir %s/ (regenerate with `make go_sdk`; schema go.importBasePath must end in /sdk/go/%s)" % (n["go_sdk_dir"], n["go_pkg"]))

    # 6a. The committed SDKs are stamped with the dev version.
    fails.extend(sdk_version_failures(repo))

    # 6b. Positive Go module paths: the SDK module and every example/template go.mod.
    sdk_gomod = os.path.join(repo, n["go_sdk_dir"], "go.mod")
    if os.path.isdir(os.path.join(repo, n["go_sdk_dir"])):
        if not os.path.exists(sdk_gomod):
            fails.append("MISSING %s/go.mod (make go_sdk runs `go mod init %s`)" % (n["go_sdk_dir"], n["go_import_base_path"]))
        else:
            with open(sdk_gomod, encoding="utf-8", errors="replace") as f:
                for msg in gomodlib.check_sdk_gomod(f.read(), n["go_import_base_path"]):
                    fails.append("WRONG %s/go.mod: %s" % (n["go_sdk_dir"], msg))
    for rel in gomodlib.go_example_modules(repo):
        with open(os.path.join(repo, rel, "go.mod"), encoding="utf-8", errors="replace") as f:
            for msg in gomodlib.check_example_gomod(f.read(), n["go_import_base_path"], n["go_pkg"]):
                fails.append("WRONG %s/go.mod: %s" % (rel, msg))

    # 7. Namespace grep and leftover placeholders over tracked files.
    pats = namespace_patterns(slug)
    hits = []
    if tracked is not None:
        for rel in sorted(tracked):
            full = os.path.join(repo, rel)
            if not os.path.isfile(full) or rel in ("go.sum", render.OPTIONS_FILE):
                continue
            try:
                if os.path.getsize(full) > MAX_SCAN_BYTES:
                    continue
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                for label, rx in pats:
                    if rx.search(line):
                        hits.append("%s:%d: %s: %s" % (rel, lineno, label, line.strip()[:120]))
                for tok in names.PLACEHOLDERS:
                    if tok in line:
                        hits.append("%s:%d: leftover placeholder %s: %s" % (rel, lineno, tok, line.strip()[:120]))
    if hits:
        shown = hits[:MAX_HITS]
        more = len(hits) - len(shown)
        fails.append("NAMESPACE/PLACEHOLDER hits (%d). Rewrite each to the pulumi-labs name:\n%s%s"
                     % (len(hits), "\n".join("      " + h for h in shown), ("\n      ... and %d more" % more) if more else ""))

    # 8. Experimental banner.
    readme = os.path.join(repo, "README.md")
    if os.path.exists(readme):
        with open(readme, encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(15)]
        if not any("experimental" in line.lower() for line in head):
            fails.append("NOT MARKED EXPERIMENTAL: README.md needs a banner containing \"experimental\" within its first 15 lines")

    # 9. Tags.
    r = _git(repo, "tag", "-l", "v*")
    if r.returncode == 0:
        for tag in r.stdout.decode().split():
            if not re.match(r"^v0\.\d+\.\d+$", tag):
                fails.append("BAD TAG %s: while experimental every v* tag must be v0.x.y" % tag)

    # 10. Copyright headers (mirrors `make check_copyright`).
    hdr = re.compile(r"Copyright .*Pulumi Corporation")
    for root, _, files in os.walk(pdir):
        for fn in sorted(files):
            if fn.endswith(".go"):
                full = os.path.join(root, fn)
                with open(full, encoding="utf-8", errors="replace") as f:
                    if not hdr.search(f.read()):
                        fails.append("MISSING copyright header (\"Copyright ... Pulumi Corporation\"): %s" % os.path.relpath(full, repo))

    # 11. Drift against the monorepo source (mirror model).
    if against_source:
        fails.extend(_drift_failures(repo, against_source))
    return fails


def _drift_failures(repo, against_source):
    import export  # lazy: export imports this module
    try:
        diffs = export.drift(repo, against_source)
    except export.ExportError as e:
        return ["DRIFT check could not run: %s" % e]
    return ["DRIFT %s" % d for d in diffs] + (
        ["DRIFT means the mirror was edited by hand or is stale. Fixes land in the monorepo first, then re-sync: "
         "re-run export.py on a fresh branch (references/sync.md). Never patch the mirror."] if diffs else [])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="the carved repo directory")
    ap.add_argument("--against-source", metavar="MONOREPO",
                    help="also compare with a fresh export at the recorded source SHA from this autoregistry checkout (local git only)")
    args = ap.parse_args(argv)
    fails = check_repo(args.repo, args.against_source)
    if fails:
        print("check FAILED: %d problem(s)" % len(fails), file=sys.stderr)
        for i, f in enumerate(fails, 1):
            print("%2d. %s" % (i, f), file=sys.stderr)
        print("Do not push or tag until this is clean.", file=sys.stderr)
        return 1
    print("check OK: %s" % args.repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
