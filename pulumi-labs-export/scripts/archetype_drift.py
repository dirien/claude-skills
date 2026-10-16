#!/usr/bin/env python3
"""Report upstream changes to the vendored archetype. Never updates anything.

    python3 scripts/archetype_drift.py
    python3 scripts/archetype_drift.py --ref main

Compares the sha256 of every vendored upstream original (recorded in
archetype/manifest.json) with the same file on the archetype repo's branch,
and lists upstream files in scaffolding locations that are not vendored.

Exit 0: no drift. Exit 2: drift found (a human decides what to do; changed
files are never merged silently). Exit 1: could not check.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402

# Files the archetype repo has that are provider-authored, not scaffolding.
PROVIDER_AUTHORED = {"README.md", "CLAUDE.md", "go.mod", "go.sum"}
PROVIDER_AUTHORED_PREFIXES = ("docs/", "examples/", "sdk/", "provider/")
# Locations where a new upstream file is probably new scaffolding.
SCAFFOLD_PREFIXES = (".github/", ".vscode/", "scripts/")


class DriftError(Exception):
    """Upstream could not be read."""


def gh_raw(repo, path, ref):
    """Fetch one upstream file's bytes with gh."""
    try:
        r = subprocess.run(["gh", "api", "repos/%s/contents/%s?ref=%s" % (repo, path, ref),
                            "-H", "Accept: application/vnd.github.raw"], capture_output=True, timeout=120)
    except FileNotFoundError:
        raise DriftError("gh not found on PATH")
    if r.returncode != 0:
        raise DriftError("cannot fetch %s@%s: %s" % (path, ref, r.stderr.decode("utf-8", "replace").strip()[:200]))
    return r.stdout


def gh_tree(repo, ref):
    """Return (commit sha, [blob paths]) of the upstream ref."""
    try:
        r = subprocess.run(["gh", "api", "repos/%s/git/trees/%s?recursive=1" % (repo, ref)], capture_output=True, timeout=120)
    except FileNotFoundError:
        raise DriftError("gh not found on PATH")
    if r.returncode != 0:
        raise DriftError("cannot list tree of %s@%s: %s" % (repo, ref, r.stderr.decode("utf-8", "replace").strip()[:200]))
    d = json.loads(r.stdout)
    return d.get("sha"), [t["path"] for t in d.get("tree", []) if t.get("type") == "blob"]


def compare(manifest, fetch, tree):
    """Return (changed [(path, old, new)], new_files [path]); fetch/tree are injectable."""
    changed = []
    for e in manifest["files"]:
        new = hashlib.sha256(fetch(manifest["archetype_repo"], e["upstream_path"])).hexdigest()
        if new != e["upstream_sha256"]:
            changed.append((e["upstream_path"], e["upstream_sha256"], new))
    vendored = {e["upstream_path"] for e in manifest["files"]}
    new_files = [p for p in tree
                 if p not in vendored and p not in PROVIDER_AUTHORED
                 and not p.startswith(PROVIDER_AUTHORED_PREFIXES)
                 and (p.startswith(SCAFFOLD_PREFIXES) or "/" not in p)]
    return changed, sorted(new_files)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="main", help="upstream branch or SHA to compare against (default main)")
    args = ap.parse_args(argv)
    manifest = render.load_manifest()
    repo = manifest["archetype_repo"]
    try:
        sha, tree = gh_tree(repo, args.ref)
        changed, new_files = compare(manifest, lambda r, p: gh_raw(r, p, args.ref), tree)
    except DriftError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("vendored: %s @ %s" % (repo, manifest["archetype_sha"]))
    print("upstream: %s @ %s (%s)" % (repo, args.ref, sha))
    if not changed and not new_files:
        print("no drift: every vendored original matches upstream")
        return 0
    for path, old, new in changed:
        print("CHANGED  %s\n         vendored original sha256 %s\n         upstream now             %s" % (path, old, new))
    for path in new_files:
        print("NEW      %s (upstream file in a scaffolding location, not vendored; decide whether it belongs)" % path)
    print("\nThis script updates nothing. Give this list to a human: they decide whether to re-vendor "
          "(new SHA, new templates, new tests) in a PR to this skill. Never merge archetype changes silently.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
