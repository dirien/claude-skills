#!/usr/bin/env python3
"""Helper for the one-time identity switch of a provider in the monorepo.

The identity switch moves lib/providers/<p>/ from the old `pulumi` identity
(github.com/pulumi/pulumi-<p>, @pulumi/<p>, pulumi_<p>, Pulumi.<Pascal>,
com.pulumi.<p>, publisher "Pulumi") to the pulumi-labs one, before the first
export. This script lists what is left and performs the mechanical part.

    python3 scripts/identity.py --check --monorepo <autoregistry checkout> --provider <p>
    python3 scripts/identity.py --apply --monorepo <autoregistry checkout> --provider <p>

--check prints every remaining hit as `file:line: [category] text` and exits 1
if there is any (0 when the provider is fully switched). Categories: Go import
path, npm, Python, NuGet/C#, Java/Maven, repository URL, publisher, builder
metadata (missing WithPluginDownloadURL / WithLanguageMap, WithLanguageMap
ordered after WithGoImportPath), copyright header, docs page, and the
carved-providers.json entry.

--apply rewrites ONLY unambiguous package names and import paths, string for
string (no parsing, no AST), in examples/, templates/, README.md and the
provider's *_test.go files. It never touches the provider's non-test Go code
(the builder chain and the monorepo import paths live there), docs/,
CHANGELOG.md or carved-providers.json. It is idempotent: a second run changes
nothing. Afterwards it prints what is left for the agent to do by hand.

Terraform-bridged providers (a provider/cmd/pulumi-tfgen-<p> command exists, references/bridged.md)
are detected automatically. Their identity lives in the tfbridge.ProviderInfo literal instead of an
infer builder chain: --check reads Publisher, Repository, PluginDownloadURL, GitHubOrg,
JavaScript.PackageName, Python.PackageName, CSharp.RootNamespace, Java.BasePackage and
Golang.ImportBasePath (plus the RespectSchemaVersion flags the SDK version stamping needs) and reports
what is wrong. The module path github.com/pulumi/pulumi-<p>/provider is NOT a hit: export.py rewrites it.
GitHubOrg is the upstream Terraform provider's organisation and must never be a pulumi one.

Names come from names.py. The script reads and writes the working tree only;
it never uses git, gh or the network.
"""
import argparse
import functools
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402

CARVED_FILE = "carved-providers.json"
REQUIRED_DOCS = ("docs/_index.md", "docs/installation-configuration.md")
ENTRY_KEYS = ("name", "repo", "goModule", "mode", "registryName", "listed",
              "exportedAt", "lastSyncedCommit", "lastSyncedVersion")
# What export.py copies from lib/providers/<p>/ (the only files that ever reach the mirror).
SCAN_TREES = ("provider/cmd", "provider/pkg", "examples", "templates", "docs")
# A bridged export copies the whole provider/ tree (go.mod, resources.go, shim/), minus the files the archetype owns.
BRIDGED_SCAN_TREES = ("provider", "examples", "templates", "docs")
BRIDGED_OWNED = ("provider/cmd/pulumi-resource-{p}/main.go", "provider/cmd/pulumi-resource-{p}/schema.json",
                 "provider/cmd/pulumi-tfgen-{p}/main.go")
SCAN_FILES = ("README.md", "CHANGELOG.md")
SKIP_DIRS = {"node_modules", "bin", "obj", "vendor", ".git", "venv", "__pycache__", "dist", ".pulumi", ".gradle", "build"}
SKIP_FILES = {"go.sum"}
COPYRIGHT_RE = re.compile(r"Copyright .*Pulumi Corporation")

CAT_GO = "Go import path github.com/pulumi/pulumi-<p>"
CAT_NPM = "npm name @pulumi/<p>"
CAT_PY = "Python name pulumi_<p>"
CAT_NUGET = "NuGet/C# name Pulumi.<Pascal>"
CAT_JAVA = "Java name com.pulumi.<p>"
CAT_REPO = "repository URL github.com/pulumi/autoregistry"
CAT_PUB = 'publisher "Pulumi"'
CAT_META = "builder metadata"
CAT_COPY = "copyright header"
CAT_DOCS = "docs page"
CAT_CARVED = "carved-providers.json"
CAT_VERSION = "version.txt"
CAT_CMD = "cmd main.go shape"

APPLY = "apply"      # --apply fixes it
MANUAL = "manual"    # the agent edits it by hand


class Hit:
    """One remaining old-identity finding."""

    def __init__(self, rel, line, category, text, fix):
        self.rel, self.line, self.category, self.text, self.fix = rel, line, category, text, fix

    def render(self):
        loc = self.rel if not self.line else "%s:%d" % (self.rel, self.line)
        return "%s: [%s] %s" % (loc, self.category, self.text)

    def as_dict(self):
        return {"file": self.rel, "line": self.line, "category": self.category, "text": self.text, "fix": self.fix}


@functools.lru_cache(maxsize=None)
def rules(slug, bridged=False):
    """Return [(category, compiled regex, replacement)] for the string-for-string rewrites of one provider.

    For a bridged provider the vanity module path github.com/pulumi/pulumi-<p>/provider is left alone: export.py rewrites it
    in every Go file and in go.mod, so rewriting only some of the occurrences here would break the monorepo build.
    """
    n = names.derive(slug)
    p = re.escape(slug)
    under = re.escape(slug.replace("-", "_"))
    pas = re.escape(n["dotnet_pascal"])
    tail = r"(?![A-Za-z0-9_-])"
    java_names = sorted({slug, slug.replace("-", ""), slug.replace("-", "_")}, key=lambda x: (-len(x), x))
    java = "|".join(re.escape(x) for x in java_names)
    return [
        (CAT_GO, re.compile(r"github\.com/pulumi/pulumi-" + p + tail + (r"(?!/provider(?![A-Za-z0-9_-]))" if bridged else "")),
         "github.com/pulumi-labs/pulumi-" + slug),
        (CAT_NPM, re.compile(r"@pulumi/" + p + tail), "@pulumi-labs/" + slug),
        (CAT_PY, re.compile(r"(?<![A-Za-z0-9_])pulumi_" + under + r"(?![A-Za-z0-9_])"), n["python_package"]),
        (CAT_NUGET, re.compile(r"(?<!Labs\.)(?<![A-Za-z0-9_])Pulumi\." + pas + r"(?![A-Za-z0-9_])"), n["dotnet_package"]),
        (CAT_JAVA, re.compile(r"com\.pulumi\.(" + java + r")(?![A-Za-z0-9_])"), r"com.pulumi.labs.\1"),
        # Example/template Go modules named after their monorepo location.
        (CAT_REPO, re.compile(r"github\.com/pulumi/autoregistry/lib/providers/" + p + r"/(examples|templates)/"),
         r"github.com/pulumi-labs/pulumi-" + slug + r"/\1/"),
    ]


def maven_rule(slug):
    """Multi-line rule: <groupId>com.pulumi</groupId> directly followed by <artifactId><slug></artifactId>, and gradle `com.pulumi:<slug>:`."""
    p = re.escape(slug)
    xml = re.compile(r"(<groupId>)com\.pulumi(</groupId>\s*<artifactId>" + p + r"</artifactId>)")
    gradle = re.compile(r"""(["'])com\.pulumi:""" + p + r"(?=[:\"'])")
    return xml, gradle


def monorepo_module_prefix(slug):
    return "github.com/pulumi/autoregistry/lib/providers/%s/provider" % slug


def monorepo_root_module_rx(slug):
    """The bare module path of a root-layout provider (lib/providers/<p>/go.mod, #1248), not a deeper package path."""
    return re.compile(re.escape("github.com/pulumi/autoregistry/lib/providers/%s" % slug) + r"(?![A-Za-z0-9_./-])")


def is_text(data):
    return b"\0" not in data[:8192]


def is_bridged(pdir, slug):
    """True when the provider has a tfgen command (a pulumi-terraform-bridge provider)."""
    return os.path.isdir(os.path.join(pdir, "provider", "cmd", "pulumi-tfgen-" + slug))


def provider_files(pdir, bridged=False, slug=None):
    """Yield (rel, abs) of every text file export.py copies, sorted."""
    out = []
    owned = {x.format(p=slug) for x in BRIDGED_OWNED} if bridged and slug else set()
    for tree in (BRIDGED_SCAN_TREES if bridged else SCAN_TREES):
        base = os.path.join(pdir, tree)
        for dp, dns, fns in os.walk(base):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
            for fn in sorted(fns):
                if fn not in SKIP_FILES:
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, pdir).replace(os.sep, "/")
                    if rel not in owned:
                        out.append((rel, full))
    for fn in SCAN_FILES:
        full = os.path.join(pdir, fn)
        if os.path.isfile(full):
            out.append((fn, full))
    return sorted(out)


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if not is_text(data):
        return None
    return data.decode("utf-8", "replace")


def is_builder_code(rel):
    """Non-test Go code under provider/: the builder chain and the monorepo import paths live here; --apply never edits it."""
    return rel.startswith("provider/") and rel.endswith(".go") and not rel.endswith("_test.go")


def is_provider_test(rel):
    return rel.startswith("provider/") and rel.endswith("_test.go")


def apply_scope(rel):
    """True if --apply may rewrite this file."""
    if is_builder_code(rel):
        return False
    return rel.startswith(("examples/", "templates/")) or rel == "README.md" or is_provider_test(rel)


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _snippet(line):
    return line.strip()[:120]


def text_hits(slug, rel, text, bridged=False):
    """Old-identity hits of one file's text."""
    hits = []
    fix = APPLY if apply_scope(rel) else MANUAL
    own_module = monorepo_module_prefix(slug)
    own_root_rx = monorepo_root_module_rx(slug)
    is_go = rel.endswith(".go") or rel.endswith("go.mod")
    pubs_in_test = is_provider_test(rel)
    pub_rx = re.compile(r'WithPublisher\(\s*"Pulumi"\s*\)')
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        scan = line
        if is_go and not bridged:  # export.py rewrites the monorepo provider module path itself in every Go file
            scan = own_root_rx.sub("", scan.replace(own_module, ""))
        for cat, rx, _ in rules(slug, bridged):
            if rx.search(scan):
                hits.append(Hit(rel, i, cat, _snippet(line), fix))
        if cat_repo_generic(scan) and not any(h.line == i and h.category == CAT_REPO for h in hits):
            hits.append(Hit(rel, i, CAT_REPO, _snippet(line), MANUAL))
        if is_builder_code(rel) and pub_rx.search(line):
            hits.append(Hit(rel, i, CAT_PUB, _snippet(line), MANUAL))
        if pubs_in_test and re.search(r"(?i)publisher", line) and re.search(r'"Pulumi"', line):
            hits.append(Hit(rel, i, CAT_PUB, _snippet(line), APPLY))
    xml, gradle = maven_rule(slug)
    for rx in (xml, gradle):
        for m in rx.finditer(text):
            hits.append(Hit(rel, _line_of(text, m.start()), CAT_JAVA, "Maven/Gradle coordinates com.pulumi:%s (new group is com.pulumi.labs)" % slug, fix))
    hits.sort(key=lambda h: (h.line, h.category))
    return hits


def cat_repo_generic(line):
    return "pulumi/autoregistry" in line


def builder_hits(slug, pdir):
    """Checks on the file that holds the infer provider builder chain."""
    n = names.derive(slug)
    hits = []
    found = False
    for rel, full in provider_files(pdir):
        if not is_builder_code(rel):
            continue
        text = read_text(full)
        if text is None or "NewProviderBuilder" not in text:
            continue
        found = True
        line_of = _line_of(text, text.index("NewProviderBuilder"))
        if "WithPluginDownloadURL" not in text:
            hits.append(Hit(rel, line_of, CAT_META, 'missing WithPluginDownloadURL("%s")' % n["plugin_download_url"], MANUAL))
        else:
            m = re.search(r'WithPluginDownloadURL\(\s*"([^"]*)"', text)
            if m and m.group(1) != n["plugin_download_url"]:
                hits.append(Hit(rel, _line_of(text, m.start()), CAT_META,
                                "WithPluginDownloadURL is %r, want %r" % (m.group(1), n["plugin_download_url"]), MANUAL))
        lm = text.find("WithLanguageMap")
        if lm < 0:
            hits.append(Hit(rel, line_of, CAT_META, "missing WithLanguageMap (nodejs, python, csharp, java names from names.py)", MANUAL))
        else:
            gi = text.find("WithGoImportPath")
            if gi >= 0 and lm > gi:
                hits.append(Hit(rel, _line_of(text, lm), CAT_META,
                                "WithLanguageMap is called after WithGoImportPath: it replaces the whole map and wipes the Go entry; call it BEFORE WithGoImportPath",
                                MANUAL))
    if not found:
        hits.append(Hit("provider/pkg", 0, CAT_META, "no infer.NewProviderBuilder() found in the provider's non-test Go code", MANUAL))
    return hits


def go_block(text, open_idx):
    """Return the text of the {...} block whose opening brace is at open_idx (strings and // comments skipped), or None."""
    depth, i, quote = 0, open_idx, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote != "`":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"`":
            quote = c
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                return None
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
        i += 1
    return None


def _sub_block(info, field, typ):
    """Return the nested `Field: &tfbridge.Typ{...}` block inside the ProviderInfo text, or None."""
    m = re.search(r"\b%s:\s*&tfbridge\.%s\{" % (field, typ), info)
    return go_block(info, m.end() - 1) if m else None


def _str_field(block, name):
    m = re.search(r"\b%s:\s*\"([^\"\n]*)\"" % name, block or "")
    return m.group(1) if m else None


def provider_info_hits(slug, pdir):
    """Checks on the tfbridge.ProviderInfo literal of a bridged provider (the bridged counterpart of builder_hits)."""
    n = names.derive(slug)
    hits = []
    found = False
    for rel, full in provider_files(pdir, True, slug):
        if not (is_builder_code(rel) and rel.count("/") == 1):
            continue
        text = read_text(full)
        m = re.search(r"tfbridge\.ProviderInfo\{", text or "")
        if not m:
            continue
        found = True
        info = go_block(text, m.end() - 1) or ""
        line = _line_of(text, m.start())

        base = m.end() - 1

        def at(needle, within=info, offset=0):
            """Absolute position in text of the first match of regex needle in within (a slice of info starting at offset), else None."""
            hm = re.search(needle, within)
            return base + offset + hm.start() if hm else None

        def hit(msg, pos=None):
            hits.append(Hit(rel, _line_of(text, pos) if pos is not None else line, CAT_META, msg, MANUAL))

        for field, want in (("Publisher", n["publisher"]), ("Repository", n["repo_url"]), ("PluginDownloadURL", n["plugin_download_url"])):
            got = _str_field(info, field)
            if got is None:
                hit("ProviderInfo.%s is not set to the string %r" % (field, want), at(r"\b%s:" % field))
            elif got != want:
                hit("ProviderInfo.%s is %r, want %r" % (field, got, want), at(r"\b%s:" % field))
        org = re.search(r'\bGitHubOrg:\s*"(pulumi|pulumi-labs)"', info)
        if org:
            hit("GitHubOrg is the UPSTREAM Terraform provider's org (the require line of the shim), never pulumi or pulumi-labs",
                m.end() - 1 + org.start())
        desc = _str_field(info, "Description")
        if desc is not None and "experimental" not in desc.lower():
            hit('ProviderInfo.Description must say "experimental" (it becomes the schema description and the npm packageDescription)',
                at(r"\bDescription:"))
        specs = (("JavaScript", "JavaScriptInfo", (("PackageName", n["npm_package"]),)),
                 ("Python", "PythonInfo", (("PackageName", n["python_package"]),)),
                 ("CSharp", "CSharpInfo", (("RootNamespace", n["dotnet_root_namespace"]),)),
                 ("Java", "JavaInfo", (("BasePackage", n["java_base_package"]),)),
                 ("Golang", "GolangInfo", (("ImportBasePath", n["go_import_base_path"]),)))
        for field, typ, wants in specs:
            blk = _sub_block(info, field, typ)
            if blk is None:
                hit("ProviderInfo.%s (&tfbridge.%s{...}) is missing" % (field, typ))
                continue
            boff = info.find(blk)
            for key, want in wants:
                got = _str_field(blk, key)
                if got is None:
                    if not (key == "ImportBasePath" and re.search(r"\bImportBasePath:", blk)):  # path.Join(...) is checked on the schema
                        hit("%s.%s is not set to the string %r" % (field, key, want), at(r"\b%s:" % key, blk, boff))
                elif got != want:
                    hit("%s.%s is %r, want %r" % (field, key, got, want), at(r"\b%s:" % key, blk, boff))
            if field != "Java" and not re.search(r"\bRespectSchemaVersion:\s*true", blk):
                hit("%s.RespectSchemaVersion must be true: without it `gen-sdk --version` is ignored and the SDK keeps ${VERSION} "
                    "(the .NET build then fails for want of version.txt)" % field, base + boff)
            if field == "Python" and not re.search(r"\bPyProject:", blk):
                hit("Python.PyProject must be enabled (PyProject: struct{ Enabled bool }{true}): the Python SDK publish needs pyproject.toml",
                    base + boff)
    if not found:
        hits.append(Hit("provider", 0, CAT_META, "no tfbridge.ProviderInfo{...} literal found in provider/*.go", MANUAL))
    return hits


def version_hits(pdir):
    """A mirrored bridged provider needs a version.txt (0.x.y): the monorepo otherwise versions it by git tag only."""
    path = os.path.join(pdir, "version.txt")
    if not os.path.isfile(path):
        return [Hit("version.txt", 0, CAT_VERSION, "missing: a mirrored bridged provider needs version.txt (0.x.y); a sync is triggered when it moves", MANUAL)]
    with open(path, encoding="utf-8") as f:
        v = f.read().strip()
    if not re.match(r"^0\.\d+\.\d+$", v):
        return [Hit("version.txt", 1, CAT_VERSION, "is %r: while experimental it must be 0.x.y" % v, MANUAL)]
    return []


def cmd_hits(slug, pdir):
    """The archetype replaces the cmd mains; they must be the plain shape (see export.cmd_main_problems)."""
    import export  # lazy: export imports check, which has no dependency on this module
    hits = []
    try:
        flavor = export.bridged_flavor(pdir, slug)
    except export.ExportError as e:
        return [Hit("provider/cmd", 0, CAT_CMD, str(e), MANUAL)]
    for kind, rel in (("resource", "provider/cmd/pulumi-resource-%s/main.go" % slug), ("tfgen", "provider/cmd/pulumi-tfgen-%s/main.go" % slug)):
        text = read_text(os.path.join(pdir, rel))
        if text is None:
            hits.append(Hit(rel, 0, CAT_CMD, "missing", MANUAL))
            continue
        for msg in export.cmd_main_problems(text, kind, flavor, slug):
            hits.append(Hit(rel, 0, CAT_CMD, msg, MANUAL))
    return hits


def copyright_hits(pdir, bridged=False, slug=None):
    hits = []
    owned = {x.format(p=slug) for x in BRIDGED_OWNED} if bridged and slug else set()
    base = os.path.join(pdir, "provider")
    for dp, dns, fns in os.walk(base):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn.endswith(".go"):
                full = os.path.join(dp, fn)
                text = read_text(full)
                if text is not None and not COPYRIGHT_RE.search(text):
                    rel = os.path.relpath(full, pdir).replace(os.sep, "/")
                    if rel in owned:
                        continue
                    hits.append(Hit(rel, 1, CAT_COPY, 'missing "Copyright ... Pulumi Corporation" header', MANUAL))
    return hits


def docs_hits(pdir):
    return [Hit(rel, 0, CAT_DOCS, "missing (the Registry page; model: the archetype's docs, references/archetype.md)", MANUAL)
            for rel in REQUIRED_DOCS if not os.path.isfile(os.path.join(pdir, rel))]


def carved_hits(mono, slug):
    n = names.derive(slug)
    path = os.path.join(mono, CARVED_FILE)
    if not os.path.isfile(path):
        return [Hit(CARVED_FILE, 0, CAT_CARVED, "file not found at the monorepo root", MANUAL)]
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except ValueError as e:
        return [Hit(CARVED_FILE, 0, CAT_CARVED, "not valid JSON: %s" % e, MANUAL)]
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == slug), None) if isinstance(entries, list) else None
    if entry is None:
        return [Hit(CARVED_FILE, 0, CAT_CARVED,
                    'no entry for %s: add {"name","repo","goModule","mode":"mirror","registryName","listed":false,'
                    '"exportedAt":null,"lastSyncedCommit":null,"lastSyncedVersion":null}' % slug, MANUAL)]
    hits = []
    for key in ENTRY_KEYS:
        if key not in entry:
            hits.append(Hit(CARVED_FILE, 0, CAT_CARVED, "entry for %s has no %r" % (slug, key), MANUAL))
    for key, want in (("repo", n["repo"]), ("goModule", n["go_module"]), ("mode", "mirror")):
        if key in entry and entry[key] != want:
            hits.append(Hit(CARVED_FILE, 0, CAT_CARVED, "entry for %s has %s %r, want %r" % (slug, key, entry[key], want), MANUAL))
    return hits


def provider_dir(mono, slug):
    pdir = os.path.join(mono, "lib", "providers", slug)
    if not os.path.isdir(pdir):
        raise SystemExit("identity: error: %s does not exist" % pdir)
    return pdir


def scan(mono, slug):
    """Return every remaining Hit for lib/providers/<slug> under the monorepo checkout mono."""
    names.validate_slug(slug)
    pdir = provider_dir(mono, slug)
    bridged = is_bridged(pdir, slug)
    hits = []
    for rel, full in provider_files(pdir, bridged, slug):
        text = read_text(full)
        if text is not None:
            hits.extend(text_hits(slug, rel, text, bridged))
    if bridged:
        hits.extend(provider_info_hits(slug, pdir))
        hits.extend(cmd_hits(slug, pdir))
        hits.extend(version_hits(pdir))
    else:
        hits.extend(builder_hits(slug, pdir))
    hits.extend(copyright_hits(pdir, bridged, slug))
    hits.extend(docs_hits(pdir))
    hits.extend(carved_hits(mono, slug))
    return hits


def rewrite_text(slug, rel, text, bridged=False):
    """Return text with every mechanical rewrite applied (string for string)."""
    for _, rx, repl in rules(slug, bridged):
        text = rx.sub(repl, text)
    xml, gradle = maven_rule(slug)
    text = xml.sub(r"\1com.pulumi.labs\2", text)
    text = gradle.sub(lambda m: m.group(1) + "com.pulumi.labs:" + slug, text)
    if is_provider_test(rel):
        out = []
        for line in text.split("\n"):
            if re.search(r"(?i)publisher", line):
                line = line.replace('"Pulumi"', '"Pulumi Labs"')
            out.append(line)
        text = "\n".join(out)
    return text


def apply(mono, slug):
    """Perform the mechanical rewrites; return {rel: number of changed lines} for the files that changed."""
    names.validate_slug(slug)
    pdir = provider_dir(mono, slug)
    bridged = is_bridged(pdir, slug)
    changed = {}
    for rel, full in provider_files(pdir, bridged, slug):
        if not apply_scope(rel):
            continue
        with open(full, "rb") as f:
            raw = f.read()
        if not is_text(raw):
            continue
        old = raw.decode("utf-8", "replace")
        new = rewrite_text(slug, rel, old, bridged)
        if new != old:
            n_lines = sum(1 for a, b in zip(old.split("\n"), new.split("\n")) if a != b)
            with open(full, "wb") as f:
                f.write(new.encode("utf-8"))
            changed[rel] = n_lines
    return changed


def print_hits(hits):
    for h in hits:
        print(h.render())


def summary(hits):
    by = {}
    for h in hits:
        by[h.category] = by.get(h.category, 0) + 1
    return ", ".join("%s: %d" % (c, k) for c, k in sorted(by.items()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="list every remaining old-identity hit; exit 1 if any")
    mode.add_argument("--apply", action="store_true", help="perform the mechanical rewrites, then list what is left to do by hand")
    ap.add_argument("--monorepo", default=".", help="autoregistry checkout (default: the current directory)")
    ap.add_argument("--provider", required=True, help="provider slug")
    ap.add_argument("--json", action="store_true", help="print the hits as JSON")
    args = ap.parse_args(argv)
    mono = os.path.abspath(args.monorepo)
    try:
        if args.apply:
            changed = apply(mono, args.provider)
            if not args.json:
                for rel, k in sorted(changed.items()):
                    print("rewrote %s (%d line%s)" % (rel, k, "" if k == 1 else "s"))
                print("identity --apply: %d file%s changed" % (len(changed), "" if len(changed) == 1 else "s"))
        hits = scan(mono, args.provider)
    except names.SlugError as e:
        print("identity: error: %s" % e, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([h.as_dict() for h in hits], indent=2))
        return 1 if hits else 0
    if not hits:
        print("identity: lib/providers/%s has no old-identity hits" % args.provider)
        return 0
    if args.apply:
        print("\nRemaining manual items (%d): edit these by hand, then run --check until it is clean:" % len(hits))
    print_hits(hits)
    print("\n%d hit%s (%s)" % (len(hits), "" if len(hits) == 1 else "s", summary(hits)))
    if args.check:
        fixable = sum(1 for h in hits if h.fix == APPLY)
        if fixable:
            print("%d of them are mechanical: `identity.py --apply` rewrites them" % fixable)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
