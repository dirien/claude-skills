#!/usr/bin/env python3
"""Install the pinned toolchain the pulumi-labs-export procedure needs.

Installs Go, the Pulumi CLI (with its bundled language hosts), jq, gitleaks
and GNU make (profile "minimal") plus Node.js 22 LTS, the .NET 8 SDK, Temurin
JDK 17 and Gradle 9 (profile "full", the default, needed by verify.py to build
every SDK) into $HOME/.local/pulumi-labs-export/ and keeps symlinks in
<prefix>/bin. No compiler, no root, Python 3.8+ standard library only.

On success stdout is the lines to eval (the PATH line, then the extra exports
GRADLE_USER_HOME, JAVA_HOME, DOTNET_ROOT and friends), followed by '#' comment lines with each
tool's version; progress goes to stderr:

    eval "$(python3 scripts/bootstrap.py)"      # or copy the export line

It is idempotent (tools already at the pinned version are skipped), verifies
every download by SHA-256, exits 1 with a precise message on any failure and
never prints environment variables or tokens. See references/toolchain.md.
"""
import argparse
import dataclasses
import hashlib
import io
import glob
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import stat
import tarfile
import tempfile
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pins. Go is pinned to the .go-version of pulumi/autoregistry; its hashes come
# from https://go.dev/dl/?mode=json&include=all (go.dev publishes no checksum
# file). The Pulumi version is read from archetype/mise.toml.tmpl. jq, gitleaks
# and Pulumi hashes come from the checksum file next to the release assets.
GO_VERSION = "1.27.2"
GO_SHA256 = {
    "amd64": "ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5",
    "arm64": "94f3e30b8e374bc285e7dadc11e0865726b9bc6e85b841ccceaabc0214c6b7c8",
}
JQ_VERSION = "1.8.2"
GITLEAKS_VERSION = "8.30.1"

# Debian bookworm make, hashes from the bookworm Packages index (SHA256 field).
MAKE_DEB_VERSION = "4.3-4.1"
MAKE_DEB_SHA256 = {
    "amd64": "a1a83af8cbd854af887b72ad196b1f4af58387815e21ced1000253a116a46e2a",
    "arm64": "7ef5f5c0e3f909c811f941598107119af0a0554641b5cc590f584cea6a55d204",
}
MAKE_DEB_URL = "https://deb.debian.org/debian/pool/main/m/make-dfsg/make_%s_%s.deb"

# Full profile: pinned like the archetype CI (node 20 / python 3.11 / dotnet 8 /
# temurin 17 + gradle) but on current maintained releases. Hashes are read from
# the checksum file published next to each release (Node SHASUMS256.txt,
# .NET releases.json sha512, Temurin <asset>.sha256.txt, Gradle <asset>.sha256).
NODE_VERSION = "22.23.3"          # Node.js 22 "Jod" LTS
DOTNET_SDK_VERSION = "8.0.426"    # .NET SDK 8.0 (net6.0 projects build via the auto-downloaded targeting pack)
TEMURIN_VERSION = "17.0.20.1+1"   # release tag jdk-17.0.20.1+1
GRADLE_VERSION = "9.8.1"          # Gradle 9.x needs JVM 17+

CORE_TOOLS = ["go", "pulumi", "jq", "gitleaks", "make"]
EXTRA_TOOLS = ["node", "dotnet", "java", "gradle"]
TOOL_ORDER = CORE_TOOLS + EXTRA_TOOLS
PROFILES = {"minimal": CORE_TOOLS, "full": TOOL_ORDER}


class BootstrapError(Exception):
    """A precise, user-facing failure."""


@dataclasses.dataclass
class Spec:
    """How to fetch, verify, unpack and probe one tool."""

    name: str
    version: str
    url: str
    filename: str
    sha256: Optional[str] = None          # pinned hash, or None to use checksums_url
    checksums_url: Optional[str] = None   # upstream checksum file listing filename
    kind: str = "tar"           # "tar", "zip" or "binary"
    hash_algo: str = "sha256"   # algorithm of the published checksum
    checksums_style: str = "sums"   # "sums" (sha256sum text), "bare" (just the hash) or "dotnet" (releases.json)
    strip: int = 0              # leading path components to drop when unpacking
    links: object = None        # list of (link name, path in install dir) or "executables"
    exe: Optional[str] = None             # executable (in <prefix>/bin) used to probe the version
    version_cmd: tuple = ()
    version_token: str = ""


def detect_arch(machine=None):
    """Return 'amd64' or 'arm64' for this machine, or raise BootstrapError."""
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise BootstrapError("unsupported CPU architecture %r (need x86_64 or aarch64)" % m)


def read_pulumi_version(skill_dir=SKILL_DIR):
    """Read the pinned Pulumi CLI version from archetype/mise.toml.tmpl."""
    path = os.path.join(skill_dir, "archetype", "mise.toml.tmpl")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise BootstrapError("cannot read %s: %s" % (path, e.strerror or e))
    m = re.search(r'^\s*pulumi\s*=\s*"(\d+\.\d+\.\d+[^"]*)"', text, re.M)
    if not m:
        raise BootstrapError('no `pulumi = "<version>"` line in %s' % path)
    return m.group(1)


def tool_specs(arch, pulumi_version):
    """Return the ordered {name: Spec} of downloadable tools for arch."""
    if arch not in ("amd64", "arm64"):
        raise BootstrapError("unsupported architecture %r" % arch)
    px = "x64" if arch == "amd64" else "arm64"
    go_file = "go%s.linux-%s.tar.gz" % (GO_VERSION, arch)
    pu_file = "pulumi-v%s-linux-%s.tar.gz" % (pulumi_version, px)
    jq_file = "jq-linux-%s" % arch
    gl_file = "gitleaks_%s_linux_%s.tar.gz" % (GITLEAKS_VERSION, px)
    pu_rel = "https://github.com/pulumi/pulumi/releases/download/v%s/" % pulumi_version
    jq_rel = "https://github.com/jqlang/jq/releases/download/jq-%s/" % JQ_VERSION
    gl_rel = "https://github.com/gitleaks/gitleaks/releases/download/v%s/" % GITLEAKS_VERSION
    nx = "x64" if arch == "amd64" else "arm64"
    tx = "x64" if arch == "amd64" else "aarch64"
    node_file = "node-v%s-linux-%s.tar.gz" % (NODE_VERSION, nx)
    node_rel = "https://nodejs.org/dist/v%s/" % NODE_VERSION
    dn_file = "dotnet-sdk-%s-linux-%s.tar.gz" % (DOTNET_SDK_VERSION, nx)
    dn_chan = ".".join(DOTNET_SDK_VERSION.split(".")[:2])
    tem_ver = TEMURIN_VERSION.split("+")[0]
    tem_major = tem_ver.split(".")[0]
    tem_file = "OpenJDK%sU-jdk_%s_linux_hotspot_%s.tar.gz" % (tem_major, tx, TEMURIN_VERSION.replace("+", "_"))
    tem_url = "https://github.com/adoptium/temurin%s-binaries/releases/download/jdk-%s/%s" % (
        tem_major, TEMURIN_VERSION.replace("+", "%2B"), tem_file)
    gr_file = "gradle-%s-bin.zip" % GRADLE_VERSION
    gr_url = "https://services.gradle.org/distributions/" + gr_file
    return {
        "go": Spec("go", GO_VERSION, "https://go.dev/dl/" + go_file, go_file,
                   sha256=GO_SHA256[arch], strip=1,
                   links=[("go", "bin/go"), ("gofmt", "bin/gofmt")],
                   exe="go", version_cmd=("version",), version_token="go" + GO_VERSION),
        "pulumi": Spec("pulumi", pulumi_version, pu_rel + pu_file, pu_file,
                       checksums_url=pu_rel + "pulumi-%s-checksums.txt" % pulumi_version,
                       strip=1, links="executables",
                       exe="pulumi", version_cmd=("version",), version_token="v" + pulumi_version),
        "jq": Spec("jq", JQ_VERSION, jq_rel + jq_file, jq_file,
                   checksums_url=jq_rel + "sha256sum.txt", kind="binary",
                   links=[("jq", "jq")],
                   exe="jq", version_cmd=("--version",), version_token="jq-" + JQ_VERSION),
        "gitleaks": Spec("gitleaks", GITLEAKS_VERSION, gl_rel + gl_file, gl_file,
                         checksums_url=gl_rel + "gitleaks_%s_checksums.txt" % GITLEAKS_VERSION,
                         links=[("gitleaks", "gitleaks")],
                         exe="gitleaks", version_cmd=("version",), version_token=GITLEAKS_VERSION),
        "node": Spec("node", NODE_VERSION, node_rel + node_file, node_file,
                     checksums_url=node_rel + "SHASUMS256.txt", strip=1,
                     links=[("node", "bin/node"), ("npm", "bin/npm"), ("npx", "bin/npx")],
                     exe="node", version_cmd=("--version",), version_token="v" + NODE_VERSION),
        "dotnet": Spec("dotnet", DOTNET_SDK_VERSION,
                       "https://builds.dotnet.microsoft.com/dotnet/Sdk/%s/%s" % (DOTNET_SDK_VERSION, dn_file), dn_file,
                       checksums_url="https://builds.dotnet.microsoft.com/dotnet/release-metadata/%s/releases.json" % dn_chan,
                       checksums_style="dotnet", hash_algo="sha512",
                       links=[("dotnet", "dotnet")],
                       exe="dotnet", version_cmd=("--version",), version_token=DOTNET_SDK_VERSION),
        "java": Spec("temurin", TEMURIN_VERSION, tem_url, tem_file,
                     checksums_url=tem_url + ".sha256.txt", strip=1,
                     links=[("java", "bin/java"), ("javac", "bin/javac"), ("jar", "bin/jar")],
                     exe="java", version_cmd=("-version",), version_token=tem_ver),
        "gradle": Spec("gradle", GRADLE_VERSION, gr_url, gr_file,
                       checksums_url=gr_url + ".sha256", checksums_style="bare",
                       kind="zip", strip=1, links=[("gradle", "bin/gradle")],
                       exe="gradle", version_cmd=("--version",), version_token=GRADLE_VERSION),
    }


def make_deb_plan(arch):
    """Return (url, sha256) of the pinned Debian bookworm make .deb for arch."""
    if arch not in MAKE_DEB_SHA256:
        raise BootstrapError("unsupported architecture %r" % arch)
    return MAKE_DEB_URL % (MAKE_DEB_VERSION, arch), MAKE_DEB_SHA256[arch]


# ---------------------------------------------------------------- checksums

def parse_checksums(text):
    """Parse 'sha256  filename' lines (sha256sum format) into {filename: hex}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def file_hash(path, algo="sha256"):
    """Return the hex digest of a file for algo ('sha256' or 'sha512')."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path):
    """Return the hex SHA-256 of a file."""
    return file_hash(path, "sha256")


def verify_hash(path, expected, algo="sha256", label=None):
    """Raise BootstrapError unless path hashes to expected under algo."""
    actual = file_hash(path, algo)
    if actual != expected.lower():
        raise BootstrapError("%s mismatch for %s: expected %s, got %s"
                             % (algo.upper().replace("SHA", "SHA-"), label or os.path.basename(path),
                                expected.lower(), actual))


def verify_sha256(path, expected, label=None):
    """Raise BootstrapError unless path hashes to expected."""
    actual = sha256_file(path)
    if actual != expected.lower():
        raise BootstrapError("SHA-256 mismatch for %s: expected %s, got %s"
                             % (label or os.path.basename(path), expected.lower(), actual))


def parse_bare_hash(text, source, algo="sha256"):
    """Return the hash from a file holding just '<hex>' (optionally '<hex>  name')."""
    want = {"sha256": 64, "sha512": 128}[algo]
    m = re.match(r"^\s*([0-9a-fA-F]+)\b", text)
    if not m or len(m.group(1)) != want:
        raise BootstrapError("%s does not start with a %d-digit %s hash" % (source, want, algo.upper()))
    return m.group(1).lower()


def dotnet_hash_from_releases(text, filename, source="releases.json"):
    """Return the sha512 of filename from a .NET releases.json document.

    Walks the whole document for file entries whose url ends in filename; all
    matches must agree, otherwise BootstrapError.
    """
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise BootstrapError("%s is not valid JSON: %s" % (source, e))
    found = set()

    def walk(node):
        if isinstance(node, dict):
            url, h = node.get("url"), node.get("hash")
            if isinstance(url, str) and isinstance(h, str) and url.rsplit("/", 1)[-1] == filename \
                    and re.fullmatch(r"[0-9a-fA-F]{128}", h):
                found.add(h.lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    if not found:
        raise BootstrapError("%s has no sha512 entry for %s" % (source, filename))
    if len(found) > 1:
        raise BootstrapError("%s lists conflicting sha512 values for %s" % (source, filename))
    return found.pop()


def expected_hash(spec, text):
    """Return the expected hex digest for spec from its checksum file text."""
    if spec.checksums_style == "bare":
        return parse_bare_hash(text, spec.checksums_url, spec.hash_algo)
    if spec.checksums_style == "dotnet":
        return dotnet_hash_from_releases(text, spec.filename, spec.checksums_url)
    return expected_from_checksums(text, spec.filename, spec.checksums_url)


def expected_from_checksums(text, filename, source):
    """Look filename up in a checksum file's text; raise if absent."""
    sums = parse_checksums(text)
    if filename not in sums:
        raise BootstrapError("%s has no SHA-256 entry for %s" % (source, filename))
    return sums[filename]


# ----------------------------------------------------------------- download

def _clean(s, limit=200):
    s = "".join(c if c.isprintable() else " " for c in s).strip()
    return s[:limit]


def download(url, dest, retries=3, timeout=120):
    """Download url to dest (via dest + '.part'); retry transient errors."""
    host = urllib.parse.urlsplit(url).hostname
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "pulumi-labs-export-bootstrap"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r, open(dest + ".part", "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(dest + ".part", dest)
            return
        except urllib.error.HTTPError as e:
            try:
                body = _clean(e.read(400).decode("utf-8", "replace"))
            except Exception:
                body = ""
            if e.code in (401, 403, 404, 407, 451):
                raise BootstrapError("download of %s refused by %s: HTTP %d %s (not retrying)"
                                     % (url, host, e.code, body))
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, OSError) as e:
            last = _clean(str(getattr(e, "reason", e)))
        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))
    raise BootstrapError("download of %s from %s failed after %d attempts: %s" % (url, host, retries, last))


def fetch_text(url, tmpdir):
    """Download a small text file and return its contents."""
    path = os.path.join(tmpdir, "fetch-" + hashlib.sha1(url.encode()).hexdigest()[:8])
    download(url, path)
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ------------------------------------------------------------------ unpack

def _rel_parts(name, strip):
    norm = posixpath.normpath(name)
    if name.startswith("/") or norm == ".." or norm.startswith("../"):
        raise BootstrapError("unsafe path in archive: %r" % name)
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    return parts[strip:]


def extract_tar(fileobj_or_path, dest, strip=0):
    """Safely extract a tar (gz/xz/bz2) into dest, dropping `strip` leading components.

    Rejects absolute paths, '..' and links that leave dest; only regular
    files, directories, symlinks and hard links are accepted.
    """
    os.makedirs(dest, exist_ok=True)
    opener = tarfile.open(fileobj=fileobj_or_path, mode="r:*") if hasattr(fileobj_or_path, "read") \
        else tarfile.open(fileobj_or_path, "r:*")
    with opener as tf:
        for m in tf:
            parts = _rel_parts(m.name, strip)
            if not parts:
                continue
            rel = "/".join(parts)
            target = os.path.join(dest, *parts)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if m.isreg():
                src = tf.extractfile(m)
                if src is None:
                    raise BootstrapError("cannot read archive member %r" % m.name)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.chmod(target, (m.mode & 0o777) | 0o200)
            elif m.issym():
                link_dest = posixpath.normpath(posixpath.join(posixpath.dirname(rel), m.linkname))
                if m.linkname.startswith("/") or link_dest == ".." or link_dest.startswith("../"):
                    raise BootstrapError("symlink %r escapes the install directory" % m.name)
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(m.linkname, target)
            elif m.islnk():
                lparts = _rel_parts(m.linkname, strip)
                src_path = os.path.join(dest, *lparts) if lparts else ""
                if not lparts or not os.path.isfile(src_path):
                    raise BootstrapError("hard link %r points at a missing or unsafe target" % m.name)
                if os.path.lexists(target):
                    os.remove(target)
                shutil.copy2(src_path, target)
            else:
                raise BootstrapError("unsupported entry type in archive: %r" % m.name)


def extract_zip(path, dest, strip=0):
    """Safely extract a zip into dest, dropping `strip` leading components.

    Restores the unix mode bits (zipfile does not) and rejects absolute paths,
    '..' and symlinks.
    """
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            parts = _rel_parts(info.filename, strip)
            if not parts:
                continue
            mode = (info.external_attr >> 16) & 0o177777
            if stat.S_ISLNK(mode):
                raise BootstrapError("unsupported symlink entry in zip: %r" % info.filename)
            target = os.path.join(dest, *parts)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.chmod(target, ((mode & 0o777) or 0o644) | 0o200)


# ---------------------------------------------------------------- ar / deb

AR_MAGIC = b"!<arch>\n"


def read_ar(data):
    """Parse an `ar` archive (as used by .deb) into a list of (name, bytes)."""
    if data[:8] != AR_MAGIC:
        raise BootstrapError("not an ar archive (bad magic); is this really a .deb?")
    pos, out = 8, []
    while pos < len(data):
        hdr = data[pos:pos + 60]
        if len(hdr) < 60 or hdr[58:60] != b"`\n":
            raise BootstrapError("corrupt ar header at byte %d" % pos)
        name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(hdr[48:58].decode("ascii").strip())
        except ValueError:
            raise BootstrapError("corrupt ar size field at byte %d" % pos)
        pos += 60
        body = data[pos:pos + size]
        if len(body) != size:
            raise BootstrapError("truncated ar member %r" % name)
        out.append((name, body))
        pos += size + (size & 1)
    return out


def deb_extract_file(deb_path, member, dest_path):
    """Write member (e.g. 'usr/bin/make') from a .deb's data tarball to dest_path, mode 0755."""
    with open(deb_path, "rb") as f:
        members = read_ar(f.read())
    data = [(n, b) for n, b in members if n.startswith("data.tar")]
    if not data:
        raise BootstrapError("%s has no data.tar member" % os.path.basename(deb_path))
    name, body = data[0]
    if name.endswith(".zst"):
        raise BootstrapError("%s uses zstd compression, which the Python standard library cannot read"
                             % name)
    if not name.endswith((".xz", ".gz", ".bz2", "")):
        raise BootstrapError("unsupported data member %s in .deb" % name)
    want = member.lstrip("./")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
        for m in tf:
            if m.name.lstrip("./") == want and m.isreg():
                src = tf.extractfile(m)
                if src is None:
                    raise BootstrapError("cannot read %s from %s" % (member, name))
                with open(dest_path, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest_path, 0o755)
                return
    raise BootstrapError("%s not found in %s" % (member, name))


# ------------------------------------------------------------------- state

def run_version(cmd, extra_env=None, token=None):
    """Run a version command; return (first output line, None) or (None, error)."""
    env = dict(os.environ, PULUMI_SKIP_UPDATE_CHECK="true", PULUMI_AUTOMATION_API_SKIP_VERSION_CHECK="true")
    env.update(extra_env or {})
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "cannot run %s: %s" % (cmd[0], getattr(e, "strerror", None) or e.__class__.__name__)
    # The JVM announces JAVA_TOOL_OPTIONS (set by the sandbox proxy) on stderr first.
    out = [l for l in (p.stdout or p.stderr or "").strip().splitlines() if not l.startswith("Picked up ")]
    if p.returncode != 0:
        return None, "%s exited %d: %s" % (os.path.basename(cmd[0]), p.returncode, _clean(out[0] if out else ""))
    if token:
        for line in out:
            if token_in(token, line):
                return line, None
    return (out[0] if out else ""), None


def token_in(token, line):
    """True if token appears in line as a whole version token."""
    return re.search(r"(?<![\w.])%s(?![\w])" % re.escape(token), line) is not None


def has_icu(patterns=None):
    """True if an ICU library (needed by .NET unless globalization is invariant) is installed."""
    pats = patterns if patterns is not None else [
        "/usr/lib/libicuuc.so*", "/usr/lib/*-linux-gnu/libicuuc.so*", "/usr/lib64/libicuuc.so*",
        "/lib/*-linux-gnu/libicuuc.so*", "/usr/local/lib/libicuuc.so*", "/usr/lib/*/libicuuc.so*"]
    return any(glob.glob(p) for p in pats)


def gradle_user_home(prefix):
    """Return the Gradle user home used for every Gradle run: <prefix>/gradle-home."""
    return os.path.join(prefix, "gradle-home")


def toolchain_env(prefix, specs, icu=None):
    """Return the ordered {VAR: value} exports the full-profile tools need.

    GRADLE_USER_HOME=<prefix>/gradle-home is always included: in the agent runtime
    ~/.gradle is not usable and Gradle fails without a writable user home. The other
    variables are included only for tools unpacked under prefix: JAVA_HOME,
    DOTNET_ROOT, DOTNET_CLI_TELEMETRY_OPTOUT, DOTNET_NOLOGO and, when no libicu is
    installed, DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1.
    """
    env = {"GRADLE_USER_HOME": gradle_user_home(prefix)}
    if "java" in specs and os.path.isdir(install_dir(prefix, specs["java"])):
        env["JAVA_HOME"] = install_dir(prefix, specs["java"])
    if "dotnet" in specs and os.path.isdir(install_dir(prefix, specs["dotnet"])):
        env["DOTNET_ROOT"] = install_dir(prefix, specs["dotnet"])
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        env["DOTNET_NOLOGO"] = "1"
        if not (has_icu() if icu is None else icu):
            env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    return env


def check_spec(spec, prefix, specs=None):
    """Return (status, detail) for spec in prefix: 'ok', 'wrong' or 'missing'."""
    exe = os.path.join(prefix, "bin", spec.exe)
    if not os.path.exists(exe):
        return "missing", ""
    line, err = run_version([exe] + list(spec.version_cmd), toolchain_env(prefix, specs or {spec.exe: spec}),
                           spec.version_token)
    if err:
        return "wrong", err
    return ("ok" if token_in(spec.version_token, line) else "wrong"), line


def check_make(prefix):
    """Return (status, detail, path) for GNU make from <prefix>/bin or PATH."""
    cands = [os.path.join(prefix, "bin", "make")]
    on_path = shutil.which("make")
    if on_path and on_path not in cands:
        cands.append(on_path)
    why = ""
    for c in cands:
        if not os.path.exists(c):
            continue
        line, err = run_version([c, "--version"])
        if err:
            why = err
        elif line is not None and "GNU Make" in line:
            return "ok", line, c
        else:
            why = "not GNU make: %s" % _clean(line)
    return ("wrong" if why else "missing"), why, ""


# ----------------------------------------------------------------- install

def _link(prefix, link_name, target):
    """Create/replace prefix/bin/<link_name> as a relative symlink to target."""
    bindir = os.path.join(prefix, "bin")
    os.makedirs(bindir, exist_ok=True)
    final = os.path.join(bindir, link_name)
    tmp = "%s.tmp%d" % (final, os.getpid())
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.relpath(target, bindir), tmp)
    os.replace(tmp, final)


def link_tool(spec, prefix, installdir):
    """Symlink the tool's executables into <prefix>/bin."""
    if spec.links == "executables":
        links = []
        for n in sorted(os.listdir(installdir)):
            p = os.path.join(installdir, n)
            if os.path.isfile(p) and not os.path.islink(p) and os.access(p, os.X_OK):
                links.append((n, n))
    else:
        links = spec.links
    for name, rel in links:
        target = os.path.join(installdir, rel)
        if not os.path.exists(target):
            raise BootstrapError("%s %s: expected %s in the archive but it is missing" % (spec.name, spec.version, rel))
        _link(prefix, name, target)


def install_dir(prefix, spec):
    """Directory a tool version is unpacked into."""
    return os.path.join(prefix, "opt", "%s-%s" % (spec.name, spec.version))


def _scratch(prefix):
    os.makedirs(os.path.join(prefix, "tmp"), exist_ok=True)
    return tempfile.mkdtemp(prefix="dl-", dir=os.path.join(prefix, "tmp"))


def _publish(stage, final):
    os.makedirs(os.path.dirname(final), exist_ok=True)
    if os.path.lexists(final):
        shutil.rmtree(final)
    os.replace(stage, final)


def install_spec(spec, prefix, log):
    """Download, verify, unpack and link one tool."""
    tmp = _scratch(prefix)
    try:
        archive = os.path.join(tmp, spec.filename)
        log("%s %s: downloading %s" % (spec.name, spec.version, spec.url))
        if spec.sha256:
            expected, source = spec.sha256, "pinned in bootstrap.py"
        else:
            text = fetch_text(spec.checksums_url, tmp)
            expected = expected_hash(spec, text)
            source = spec.checksums_url
        download(spec.url, archive)
        verify_hash(archive, expected, spec.hash_algo, spec.filename)
        log("%s: %s ok (%s)" % (spec.filename, spec.hash_algo.upper().replace("SHA", "SHA-"), source))
        stage = os.path.join(tmp, "stage")
        if spec.kind == "binary":
            os.makedirs(stage)
            shutil.copyfile(archive, os.path.join(stage, spec.links[0][1]))
            os.chmod(os.path.join(stage, spec.links[0][1]), 0o755)
        elif spec.kind == "zip":
            try:
                extract_zip(archive, stage, spec.strip)
            except (zipfile.BadZipFile, OSError, EOFError) as e:
                raise BootstrapError("cannot unpack %s: %s" % (spec.filename, e))
        else:
            try:
                extract_tar(archive, stage, spec.strip)
            except (tarfile.TarError, OSError, EOFError) as e:
                raise BootstrapError("cannot unpack %s: %s" % (spec.filename, e))
        final = install_dir(prefix, spec)
        _publish(stage, final)
        link_tool(spec, prefix, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_make(prefix, arch, log):
    """Install GNU make: existing > apt-get download > pinned Debian .deb. Returns a status line."""
    status, detail, path = check_make(prefix)
    if status == "ok":
        return "%s (%s)" % (detail, path)
    reasons = []
    tmp = _scratch(prefix)
    dest = os.path.join(prefix, "opt", "make-%s" % MAKE_DEB_VERSION, "make")
    try:
        # Route 2: apt-get download (works without root when the apt lists are present).
        if shutil.which("apt-get"):
            log("make: trying apt-get download make")
            try:
                p = subprocess.run(["apt-get", "download", "make"], cwd=tmp, capture_output=True,
                                   text=True, timeout=180)
                debs = [f for f in os.listdir(tmp) if f.startswith("make_") and f.endswith(".deb")]
                if p.returncode != 0 or not debs:
                    last = (p.stderr or p.stdout or "").strip().splitlines()
                    raise BootstrapError("apt-get download make failed (exit %d): %s"
                                         % (p.returncode, _clean(last[-1] if last else "no output")))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                deb_extract_file(os.path.join(tmp, debs[0]), "usr/bin/make", dest)
                _link(prefix, "make", dest)
                line, err = run_version([os.path.join(prefix, "bin", "make"), "--version"])
                if err or "GNU Make" not in (line or ""):
                    raise BootstrapError("make from apt does not run: %s" % (err or line))
                return "%s (apt-get download)" % line
            except (BootstrapError, OSError, subprocess.SubprocessError, tarfile.TarError) as e:
                reasons.append("apt route: %s" % e)
                log("make: %s" % reasons[-1])
        else:
            reasons.append("apt route: apt-get not installed")
        # Route 3: pinned Debian bookworm .deb.
        url, sha = make_deb_plan(arch)
        log("make: downloading pinned Debian package %s" % url)
        try:
            deb = os.path.join(tmp, "make.deb")
            download(url, deb)
            verify_sha256(deb, sha, os.path.basename(url))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            deb_extract_file(deb, "usr/bin/make", dest)
            _link(prefix, "make", dest)
            line, err = run_version([os.path.join(prefix, "bin", "make"), "--version"])
            if err or "GNU Make" not in (line or ""):
                raise BootstrapError("make from the Debian package does not run: %s" % (err or line))
            return "%s (pinned Debian bookworm package)" % line
        except (BootstrapError, OSError, tarfile.TarError) as e:
            reasons.append("pinned .deb route: %s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise BootstrapError("could not install make; " + "; ".join(reasons))


# --------------------------------------------------------------------- cli

def default_prefix():
    """The default install prefix under $HOME."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "pulumi-labs-export")


def path_line(prefix):
    """Return the one line to eval to put the toolchain on PATH."""
    esc = re.sub(r'(["\\$`])', r"\\\1", os.path.join(prefix, "bin"))
    return 'export PATH="%s:$PATH"' % esc


def export_line(var, value):
    """Return an eval-able `export VAR="value"` line with shell metacharacters escaped."""
    return 'export %s="%s"' % (var, re.sub(r'(["\\$`])', r"\\\1", value))


def run_check(prefix, specs, only, out, profile="minimal"):
    """Print what is installed without downloading; return True if everything is ok."""
    all_ok = True
    for name in PROFILES[profile]:
        if only and name != only:
            continue
        if name == "make":
            status, detail, path = check_make(prefix)
            where = " (%s)" % path if path else ""
        else:
            status, detail = check_spec(specs[name], prefix, specs)
            where = ""
            if status == "ok" or status == "wrong":
                where = " (%s)" % os.path.join(prefix, "bin", specs[name].exe)
        want = "" if name == "make" else " [want %s]" % specs[name].version
        out("%-9s %-8s %s%s%s" % (name, status.upper(), detail, where, want if status != "ok" else ""))
        all_ok = all_ok and status == "ok"
    return all_ok


def main(argv=None, out=None, err=None):
    """CLI entry point; returns the process exit code."""
    out = out or (lambda s: print(s, flush=True))
    err = err or (lambda s: print(s, file=sys.stderr, flush=True))
    ap = argparse.ArgumentParser(
        description="Install the pinned toolchain for the pulumi-labs-export procedure into "
                    "$HOME/.local/pulumi-labs-export: go, pulumi, jq, gitleaks, make (profile minimal) "
                    "plus node, dotnet, java, gradle (profile full, the default). "
                    "On success stdout is an `export PATH=...` line to eval, the extra exports "
                    "(JAVA_HOME, DOTNET_ROOT, ...), then '# tool version' comment lines; "
                    "progress goes to stderr.")
    ap.add_argument("--check", action="store_true",
                    help="report what is installed and at which version; download nothing; "
                         "exit 1 if anything is missing or at the wrong version")
    ap.add_argument("--only", choices=TOOL_ORDER, help="act on a single tool (overrides --profile)")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="full",
                    help="minimal = go, pulumi, jq, gitleaks, make; full (default) adds node, dotnet, java, gradle")
    ap.add_argument("--prefix", default=None, help="install prefix (default: $HOME/.local/pulumi-labs-export)")
    args = ap.parse_args(argv)
    prefix = os.path.abspath(args.prefix or default_prefix())
    try:
        if sys.platform != "linux":
            raise BootstrapError("only Linux is supported (this is %s)" % sys.platform)
        arch = detect_arch()
        specs = tool_specs(arch, read_pulumi_version())
    except BootstrapError as e:
        err("bootstrap: error: %s" % e)
        return 1
    if args.check:
        return 0 if run_check(prefix, specs, args.only, out, args.profile) else 1

    try:
        os.makedirs(gradle_user_home(prefix), exist_ok=True)
    except OSError as e:
        err("bootstrap: error: cannot create %s: %s" % (gradle_user_home(prefix), e.strerror or e.__class__.__name__))
        return 1
    failures, report = [], []
    for name in PROFILES[args.profile]:
        if args.only and name != args.only:
            continue
        try:
            if name == "make":
                report.append(("make", install_make(prefix, arch, err)))
                continue
            spec = specs[name]
            status, detail = check_spec(spec, prefix, specs)
            if status == "ok":
                err("%s %s: already installed, skipping" % (name, spec.version))
                if os.path.isdir(install_dir(prefix, spec)):
                    link_tool(spec, prefix, install_dir(prefix, spec))
            else:
                install_spec(spec, prefix, err)
                status, detail = check_spec(spec, prefix, specs)
                if status != "ok":
                    raise BootstrapError("%s installed but its version check failed: %s" % (name, detail or "no output"))
            report.append((name, detail))
        except BootstrapError as e:
            failures.append("%s: %s" % (name, e))
        except OSError as e:
            failures.append("%s: filesystem error: %s%s" % (name, e.strerror or e.__class__.__name__,
                                                            " (%s)" % e.filename if e.filename else ""))
    if failures:
        for f in failures:
            err("bootstrap: error: " + f)
        return 1
    out(path_line(prefix))
    for var, val in toolchain_env(prefix, specs).items():
        out(export_line(var, val))
    for name, ver in report:
        out("# %s: %s" % (name, ver))
    return 0


if __name__ == "__main__":
    sys.exit(main())
