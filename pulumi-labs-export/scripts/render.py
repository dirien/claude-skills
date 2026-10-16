#!/usr/bin/env python3
"""Write the vendored archetype scaffolding into a carved provider repo.

Verbatim files are copied byte for byte. Rendered files get only the
provider-specific placeholders replaced. A record of the choices is written to
<repo>/.pulumi-labs-export.json so check.py can re-render and prove nobody
hand-edited anything.

    python3 scripts/render.py --repo-dir ../pulumi-modal --provider modal \\
        --display-name Modal
    python3 scripts/render.py --repo-dir ../pulumi-nvidia-aicr \\
        --provider nvidia-aicr --display-name "NVIDIA AICR" \\
        --exclude-platform windows/amd64 --reason "upstream SDK does not build for windows"

Re-render after the vendored archetype changed (flags may be omitted, the
recorded options are reused):

    python3 scripts/render.py --repo-dir ../pulumi-modal

Two archetypes exist. "native" (the default; archetype/) is pulumi-nvidia-aicr, a
pulumi-go-provider repo. "bridged" (archetype-bridged/, references/bridged.md) is a
Terraform-bridged provider laid out like pulumi-tf-provider-boilerplate:

    python3 scripts/render.py --repo-dir ../pulumi-clerk --provider clerk \\
        --display-name Clerk --archetype bridged --flavor pf
"""
import argparse
import json
import os
import re
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPTIONS_FILE = ".pulumi-labs-export.json"
LEFTOVER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")
MATRIX_TOKEN = "__PLATFORM_MATRIX__"
# Archetype name -> vendored template directory (relative to SKILL_DIR). "native" is the default and the only
# one whose options file carries no "archetype" key, so existing mirrors render byte for byte as before.
ARCHETYPES = {"native": "archetype", "bridged": "archetype-bridged"}
DEFAULT_ARCHETYPE = "native"
# Bridged providers wrap either a terraform-plugin-framework provider (pf) or a terraform-plugin-sdk one (sdkv2);
# the two bridge entry points differ, so the cmd/ mains are rendered per flavor.
FLAVORS = ("pf", "sdkv2")
# Order matches the archetype's build matrix.
PLATFORMS = ["linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64"]


class RenderError(Exception):
    """Raised for any condition the operator must fix; message is actionable."""


def archetype_of(options):
    """Return the archetype name recorded in options (native when the key is absent)."""
    return (options or {}).get("archetype") or DEFAULT_ARCHETYPE


def load_manifest(archetype=DEFAULT_ARCHETYPE):
    """Return the parsed manifest.json of an archetype (archetype/ or archetype-bridged/)."""
    if archetype not in ARCHETYPES:
        raise RenderError("unknown archetype %r; known: %s" % (archetype, ", ".join(sorted(ARCHETYPES))))
    with open(os.path.join(SKILL_DIR, ARCHETYPES[archetype], "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def make_options(provider, display_name, excludes, manifest=None, archetype=DEFAULT_ARCHETYPE, flavor=None):
    """Validate inputs and return the options dict recorded in OPTIONS_FILE.

    excludes is a list of (platform, reason) pairs. archetype is "native" or "bridged"; a bridged repo also records its
    flavor ("pf" or "sdkv2"). A native options dict has exactly the keys it always had.
    """
    if archetype not in ARCHETYPES:
        raise RenderError("unknown archetype %r; known: %s" % (archetype, ", ".join(sorted(ARCHETYPES))))
    if archetype == "bridged" and flavor not in FLAVORS:
        raise RenderError("a bridged repo needs a flavor, one of %s (got %r)" % (", ".join(FLAVORS), flavor))
    if archetype == "native" and flavor:
        raise RenderError("flavor only applies to the bridged archetype")
    manifest = manifest or load_manifest(archetype)
    try:
        names.validate_slug(provider)
    except names.SlugError as e:
        raise RenderError(str(e))
    if not display_name or not display_name.strip() or "\n" in display_name:
        raise RenderError("--display-name must be a non-empty single line (e.g. \"Modal\")")
    seen = set()
    ex = []
    for platform, reason in excludes:
        if platform not in PLATFORMS:
            raise RenderError("unknown platform %r; the archetype matrix has: %s" % (platform, ", ".join(PLATFORMS)))
        if platform in seen:
            raise RenderError("platform %s excluded twice" % platform)
        seen.add(platform)
        if not reason or not reason.strip() or "\n" in reason:
            raise RenderError("--reason for %s must be a non-empty single line" % platform)
        ex.append({"platform": platform, "reason": reason.strip()})
    if len(seen) == len(PLATFORMS):
        raise RenderError("cannot exclude every platform")
    options = {
        "provider": provider,
        "display_name": display_name.strip(),
        "archetype_repo": manifest["archetype_repo"],
        "archetype_sha": manifest["archetype_sha"],
        "exclude_platforms": ex,
    }
    if archetype != DEFAULT_ARCHETYPE:
        options["archetype"] = archetype
        options["flavor"] = flavor
    return options


def options_from_record(rec, manifest=None):
    """Rebuild validated options from a recorded OPTIONS_FILE dict (the inverse of make_options)."""
    return make_options(rec["provider"], rec.get("display_name"),
                        [(e["platform"], e["reason"]) for e in rec.get("exclude_platforms", [])], manifest,
                        archetype=archetype_of(rec), flavor=rec.get("flavor"))


def resolve_entries(options, manifest=None):
    """Return [(target path, manifest entry)] for options: entries of another flavor are dropped, tokens in targets resolved."""
    manifest = manifest or load_manifest(archetype_of(options))
    tokens = names.template_tokens(options["provider"])
    out = []
    for e in manifest["files"]:
        if e.get("flavor") not in (None, options.get("flavor")):
            continue
        target = e["target"]
        for tok, val in tokens.items():
            target = target.replace(tok, val)
        out.append((target, e))
    return out


def _matrix_lines(entry, options):
    pad = entry.get("matrix_pad", 9)
    excluded = {e["platform"]: e["reason"] for e in options["exclude_platforms"]}
    lines = []
    for plat in PLATFORMS:
        if plat in excluded:
            continue
        goos, goarch = plat.split("/")
        lines.append("          - { goos: %s%s }" % ((goos + ",").ljust(max(pad, len(goos) + 2)), "goarch: " + goarch))
    for plat in PLATFORMS:
        if plat not in excluded:
            continue
        goos, goarch = plat.split("/")
        text = "%s is off the matrix: %s" % (plat, excluded[plat])
        for line in textwrap.wrap(text, width=70):
            lines.append("          # " + line)
        if entry.get("matrix_commented_entry"):
            lines.append("          # - { goos: %s, goarch: %s }" % (goos, goarch))
    return "\n".join(lines)


def render_entry(entry, options, skill_dir=SKILL_DIR):
    """Return the bytes of one manifest entry for the given options."""
    with open(os.path.join(skill_dir, entry["template"]), "rb") as f:
        raw = f.read()
    if entry["mode"] == "verbatim":
        return raw
    if entry["mode"] != "render":
        raise RenderError("manifest entry %s has unknown mode %r" % (entry["target"], entry["mode"]))
    text = raw.decode("utf-8")
    tokens = names.template_tokens(options["provider"])
    tokens["__DISPLAY_NAME__"] = options["display_name"]
    if MATRIX_TOKEN in text:
        text = text.replace(MATRIX_TOKEN, _matrix_lines(entry, options))
    for tok, val in tokens.items():
        text = text.replace(tok, val)
    left = sorted(set(LEFTOVER_RE.findall(text)))
    if left:
        raise RenderError("leftover placeholder(s) %s in rendered %s: template bug, fix the template or render.py" % (", ".join(left), entry["target"]))
    return text.encode("utf-8")


def render_all(options, manifest=None):
    """Return {target path: bytes} for every archetype file of the archetype recorded in options."""
    manifest = manifest or load_manifest(archetype_of(options))
    return {target: render_entry(e, options) for target, e in resolve_entries(options, manifest)}


def read_options(repo_dir):
    """Return the recorded options of repo_dir or None if absent."""
    path = os.path.join(repo_dir, OPTIONS_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def options_bytes(options):
    """Canonical serialisation of the options file."""
    return (json.dumps(options, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_repo(repo_dir, options):
    """Write all archetype files plus OPTIONS_FILE; return [(path, action)]."""
    files = render_all(options)
    files[OPTIONS_FILE] = options_bytes(options)
    report = []
    for rel in sorted(files):
        dest = os.path.join(repo_dir, rel)
        old = None
        if os.path.exists(dest):
            with open(dest, "rb") as f:
                old = f.read()
        if old == files[rel]:
            report.append((rel, "unchanged"))
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(files[rel])
        report.append((rel, "created" if old is None else "overwritten"))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", required=True, help="the carved repo (must exist)")
    ap.add_argument("--archetype", choices=sorted(ARCHETYPES), help="native (default) or bridged; optional when %s exists" % OPTIONS_FILE)
    ap.add_argument("--flavor", choices=FLAVORS, help="bridged only: pf (terraform-plugin-framework) or sdkv2")
    ap.add_argument("--provider", help="provider slug; optional when %s already exists" % OPTIONS_FILE)
    ap.add_argument("--display-name", help="human name used in CONTRIBUTING.md, e.g. Modal")
    ap.add_argument("--exclude-platform", action="append", default=[], metavar="GOOS/GOARCH",
                    help="drop a leg from the build matrix; repeatable, each needs a --reason")
    ap.add_argument("--reason", action="append", default=[],
                    help="why the platform is excluded (written as a comment); pairs with --exclude-platform by order")
    args = ap.parse_args(argv)
    try:
        if not os.path.isdir(args.repo_dir):
            raise RenderError("--repo-dir %s is not a directory" % args.repo_dir)
        if len(args.exclude_platform) != len(args.reason):
            raise RenderError("each --exclude-platform needs exactly one --reason (got %d platforms, %d reasons)" % (len(args.exclude_platform), len(args.reason)))
        prev = read_options(args.repo_dir)
        provider = args.provider or (prev or {}).get("provider")
        display = args.display_name or (prev or {}).get("display_name")
        if prev and args.provider and args.provider != prev["provider"]:
            raise RenderError("%s records provider %r, refusing to re-render as %r" % (OPTIONS_FILE, prev["provider"], args.provider))
        if args.exclude_platform:
            excludes = list(zip(args.exclude_platform, args.reason))
        else:
            excludes = [(e["platform"], e["reason"]) for e in (prev or {}).get("exclude_platforms", [])]
        if not provider:
            raise RenderError("--provider is required for the first render")
        if not display:
            raise RenderError("--display-name is required for the first render")
        archetype = args.archetype or archetype_of(prev)
        flavor = args.flavor or (prev or {}).get("flavor")
        if prev and args.archetype and args.archetype != archetype_of(prev):
            raise RenderError("%s records archetype %r, refusing to re-render as %r" % (OPTIONS_FILE, archetype_of(prev), args.archetype))
        options = make_options(provider, display, excludes, archetype=archetype, flavor=flavor)
        for key, val in (prev or {}).items():  # keep what export.py recorded (source SHA, version, ...)
            options.setdefault(key, val)
        report = write_repo(args.repo_dir, options)
    except RenderError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    for rel, action in report:
        print("%-12s %s" % (action, rel))
    print("archetype %s @ %s" % (options["archetype_repo"], options["archetype_sha"]))
    print("next: python3 scripts/check.py --repo %s" % args.repo_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
