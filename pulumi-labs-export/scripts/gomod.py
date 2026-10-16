"""Minimal go.mod parsing, the positive Go module-path checks and the in-tree replace check.

Shared by check.py (structural) and verify.py (build). Standard library only.
"""
import os
import posixpath
import re

SKIP_DIRS = {"node_modules", ".git", "vendor", "bin", "obj", "venv", ".venv", "__pycache__"}


def _strip(line):
    return line.split("//", 1)[0].strip()


def parse_gomod(text):
    """Parse go.mod text into {'module': str|None, 'requires': [(path, ver)], 'replaces': [(old, new)]}.

    Handles single-line and parenthesised require/replace blocks. 'replace a v => b v2'
    yields (a, b); versions are dropped.
    """
    out = {"module": None, "requires": [], "replaces": []}
    block = None
    for raw in text.splitlines():
        line = _strip(raw)
        if not line:
            continue
        if block:
            if line == ")":
                block = None
                continue
            _add(out, block, line)
            continue
        m = re.match(r"^(require|replace)\s*\($", line)
        if m:
            block = m.group(1)
            continue
        m = re.match(r"^(module|require|replace)\s+(.*)$", line)
        if not m:
            continue
        kind, rest = m.groups()
        if kind == "module":
            out["module"] = rest.strip().strip('"')
        else:
            _add(out, kind, rest)
    return out


def _add(out, kind, rest):
    if kind == "require":
        parts = rest.split()
        if len(parts) >= 2:
            out["requires"].append((parts[0].strip('"'), parts[1]))
    else:
        if "=>" not in rest:
            return
        left, right = rest.split("=>", 1)
        lp, rp = left.split(), right.split()
        if lp and rp:
            out["replaces"].append((lp[0].strip('"'), rp[0].strip('"')))


def go_example_modules(root):
    """Return sorted dirs (relative to root) under examples/ and templates/ holding a go.mod."""
    found = []
    for top in ("examples", "templates"):
        base = os.path.join(root, top)
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            if "go.mod" in fns:
                found.append(os.path.relpath(dp, root))
    return sorted(found)


def check_sdk_gomod(text, want_module):
    """Return failure strings unless the SDK go.mod declares exactly want_module."""
    got = parse_gomod(text)["module"]
    if got != want_module:
        return ["sdk go.mod declares module %r, want exactly %r" % (got, want_module)]
    return []


def check_example_gomod(text, sdk_path, gopkg):
    """Return failure strings for one example/template go.mod.

    Any requirement that looks like this provider's Go SDK (path ends in /sdk/go/<gopkg>)
    must be exactly sdk_path. A replace directive is allowed only for sdk_path and only
    to itself or to a local directory ending in sdk/go/<gopkg>.
    """
    fails = []
    g = parse_gomod(text)
    for path, _ in g["requires"]:
        if path != sdk_path and re.search(r"/sdk/go/" + re.escape(gopkg) + r"$", path):
            fails.append("requires %s, want exactly %s" % (path, sdk_path))
    for old, new in g["replaces"]:
        local_ok = new == sdk_path or re.search(r"(^|/)sdk/go/" + re.escape(gopkg) + r"/?$", new.replace("\\", "/"))
        if old != sdk_path or not local_ok:
            fails.append("replace %s => %s is not allowed (only %s => the local sdk/go/%s)" % (old, new, sdk_path, gopkg))
    return fails


def check_local_replaces(files, gomod_rel, text):
    """Return problems for relative replace directives in a go.mod: each must resolve to a go.mod inside the exported tree.

    files maps the exported paths (repo-relative, '/'-separated) to anything; gomod_rel is the go.mod being checked. A
    replace to ./shim from provider/go.mod is fine when provider/shim/go.mod is exported; one that climbs out of the
    repository, is absolute, or names a directory without an exported go.mod is a problem (the mirror would not build).
    """
    problems = []
    base = posixpath.dirname(gomod_rel)
    for old, new in parse_gomod(text)["replaces"]:
        if not new.startswith((".", "/")):
            continue
        if new.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", new):
            problems.append("%s: replace %s => %s is an absolute path" % (gomod_rel, old, new))
            continue
        target = posixpath.normpath(posixpath.join(base, new))
        if target == ".." or target.startswith("../"):
            problems.append("%s: replace %s => %s leaves the repository" % (gomod_rel, old, new))
        elif target + "/go.mod" not in files:
            problems.append("%s: replace %s => %s: %s/go.mod is not part of the export" % (gomod_rel, old, new, target))
    return problems
