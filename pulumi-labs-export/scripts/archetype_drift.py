#!/usr/bin/env python3
"""Report upstream changes to the vendored archetype. Never updates anything.

    python3 scripts/archetype_drift.py
    python3 scripts/archetype_drift.py --ref main

Compares the sha256 of every vendored upstream original (recorded in
archetype/manifest.json) with the same file on the archetype repo's branch,
and lists upstream files in scaffolding locations that are not vendored.

Exit 0: no drift. Exit 2: drift found (a human decides what to do; changed
files are never merged silently). Exit 1: could not check.

Needs no credentials and no `gh`: the archetype repo is public, so it uses
unauthenticated HTTPS (raw.githubusercontent.com for content, the GitHub REST
API for the tree). The unauthenticated API allows 60 requests per hour per IP;
a rate-limit response is reported as such.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402

# Files the archetype repo has that are provider-authored, not scaffolding.
PROVIDER_AUTHORED = {"README.md", "CLAUDE.md", "go.mod", "go.sum"}
PROVIDER_AUTHORED_PREFIXES = ("docs/", "examples/", "sdk/", "provider/")
# Locations where a new upstream file is probably new scaffolding.
SCAFFOLD_PREFIXES = (".github/", ".vscode/", "scripts/")


class DriftError(Exception):
    """Upstream could not be read."""


def http_get(url):
    """GET url unauthenticated over HTTPS; return bytes or raise DriftError with a clear reason."""
    req = urllib.request.Request(url, headers={"User-Agent": "pulumi-labs-export-archetype-drift",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        if e.code in (403, 429) and (remaining == "0" or e.code == 429 or e.headers.get("Retry-After")):
            reset = e.headers.get("X-RateLimit-Reset", "unknown")
            raise DriftError("GitHub rate limit hit for unauthenticated requests (HTTP %d, resets at epoch %s); "
                             "wait and retry, do not look for credentials" % (e.code, reset))
        raise DriftError("GET %s failed: HTTP %d" % (url, e.code))
    except (urllib.error.URLError, OSError) as e:
        raise DriftError("GET %s failed: %s" % (url, e))


def raw_url(repo, path, ref):
    """URL of one upstream file's raw content."""
    return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, urllib.parse.quote(ref, safe="/"), urllib.parse.quote(path))


def tree_url(repo, ref):
    """GitHub REST API URL of the recursive tree of ref."""
    return "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (repo, urllib.parse.quote(ref, safe=""))


def gh_raw(repo, path, ref, fetcher=http_get):
    """Fetch one upstream file's bytes (fetcher is injectable for offline tests)."""
    return fetcher(raw_url(repo, path, ref))


def gh_tree(repo, ref, fetcher=http_get):
    """Return (tree sha, [blob paths]) of the upstream ref."""
    try:
        d = json.loads(fetcher(tree_url(repo, ref)))
    except ValueError as e:
        raise DriftError("unreadable tree response for %s@%s: %s" % (repo, ref, e))
    if d.get("truncated"):
        raise DriftError("upstream tree of %s@%s is truncated by the API; cannot check for new files" % (repo, ref))
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


def main(argv=None, fetcher=http_get):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="main", help="upstream branch or SHA to compare against (default main)")
    args = ap.parse_args(argv)
    manifest = render.load_manifest()
    repo = manifest["archetype_repo"]
    try:
        sha, tree = gh_tree(repo, args.ref, fetcher)
        changed, new_files = compare(manifest, lambda r, p: gh_raw(r, p, args.ref, fetcher), tree)
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
