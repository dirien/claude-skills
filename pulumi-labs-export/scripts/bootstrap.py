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
every download by SHA-256, retries transient download failures with backoff,
exits 1 with a precise message on any failure and never prints environment
variables or tokens. `--cache-key` prints a hash of the pins for a CI cache of
<prefix>/opt and <prefix>/bin. See references/toolchain.md.
"""
import argparse
import dataclasses
import hashlib
import http.client
import io
import glob
import json
import os
import platform
import posixpath
import random
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
GO_VERSION = "1.27.3"
GO_SHA256 = {
    "amd64": "65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40",
    "arm64": "a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49",
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

# Downloads: attempts in all, and the exponential backoff with jitter between them (seconds,
# see backoff_delay). NO_RETRY_HTTP codes are final; every other failure is retried.
DOWNLOAD_ATTEMPTS = 5
BACKOFF_BASE = 4.0
BACKOFF_CAP = 60.0
NO_RETRY_HTTP = (401, 403, 404, 407, 451)

# Bump when the layout under <prefix>/opt or <prefix>/bin changes, so CI caches keyed on
# `--cache-key` are not reused across layouts.
CACHE_LAYOUT = 1

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


def cache_key(specs, names, arch):
    """Return a 16-hex-digit hash of everything that decides what installing `names` puts under
    <prefix>/opt and <prefix>/bin: each tool's Spec (version, URL, pinned hash or checksum URL,
    unpack rules, links), the make .deb pin, the arch and CACHE_LAYOUT. No network, no files.

    CI keys its cache of <prefix>/opt and <prefix>/bin on it, so a pin bump starts a fresh cache.
    """
    pins = {"layout": CACHE_LAYOUT, "arch": arch}
    for name in names:
        if name == "make":
            pins["make"] = [MAKE_DEB_VERSION, MAKE_DEB_SHA256.get(arch)]
        else:
            pins[name] = dataclasses.asdict(specs[name])
    return hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()[:16]


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


def backoff_delay(attempt, retry_after=None, rand=random.random):
    """Seconds to wait after failed attempt `attempt` (0-based) before the next one.

    Exponential (BACKOFF_BASE * 2**attempt, capped at BACKOFF_CAP) with equal jitter: a uniform
    value between half and all of it, so parallel CI jobs do not retry in lockstep. A numeric
    Retry-After (seconds) from a 429 or 503 raises the wait to that value, also capped.
    """
    d = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
    d = d / 2 + rand() * d / 2
    if retry_after is not None:
        d = max(d, min(BACKOFF_CAP, retry_after))
    return d


def _retry_after(err):
    """Return the numeric Retry-After of an HTTPError in seconds, or None."""
    try:
        value = err.headers.get("Retry-After") if err.headers is not None else None
        return max(0.0, float(value)) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _stderr(s):
    print(s, file=sys.stderr, flush=True)


def download(url, dest, retries=DOWNLOAD_ATTEMPTS, timeout=120, log=None, sleep=None):
    """Download url to dest (via dest + '.part'), retrying transient failures.

    Up to `retries` attempts in all. HTTP 5xx, 429 and any other status not in NO_RETRY_HTTP,
    timeouts, connection resets and truncated bodies are retried after backoff_delay() (exponential,
    with jitter, honoring Retry-After). NO_RETRY_HTTP (401, 403, 404, 407, 451) fails at once.
    Each retry is logged (to stderr by default).
    """
    log = log or _stderr
    sleep = sleep or time.sleep
    host = urllib.parse.urlsplit(url).hostname
    last = ""
    for attempt in range(retries):
        retry_after = None
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
            finally:
                e.close()
            if e.code in NO_RETRY_HTTP:
                raise BootstrapError("download of %s refused by %s: HTTP %d %s (not retrying)"
                                     % (url, host, e.code, body))
            last = "HTTP %d" % e.code
            retry_after = _retry_after(e)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            last = _clean(str(getattr(e, "reason", e))) or e.__class__.__name__
        if attempt + 1 < retries:
            delay = backoff_delay(attempt, retry_after)
            log("download of %s: attempt %d of %d failed (%s); retrying in %.1fs"
                % (url, attempt + 1, retries, last, delay))
            sleep(delay)
    raise BootstrapError("download of %s from %s failed after %d attempts: %s" % (url, host, retries, last))


def fetch_text(url, tmpdir, log=None):
    """Download a small text file and return its contents."""
    path = os.path.join(tmpdir, "fetch-" + hashlib.sha1(url.encode()).hexdigest()[:8])
    download(url, path, log=log)
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

    NUGET_PACKAGES=<prefix>/nuget-packages is always included too.
    GRADLE_USER_HOME=<prefix>/gradle-home is always included: in the agent runtime
    ~/.gradle is not usable and Gradle fails without a writable user home. The other
    variables are included only for tools unpacked under prefix: JAVA_HOME,
    DOTNET_ROOT, DOTNET_CLI_TELEMETRY_OPTOUT, DOTNET_NOLOGO and, when no libicu is
    installed, DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1.
    """
    env = {"GRADLE_USER_HOME": gradle_user_home(prefix), "NUGET_PACKAGES": nuget_packages(prefix)}
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
            text = fetch_text(spec.checksums_url, tmp, log)
            expected = expected_hash(spec, text)
            source = spec.checksums_url
        download(spec.url, archive, log=log)
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
            download(url, deb, log=log)
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


# ------------------------------------------------- sandbox environment fixes

GRADLE_BEGIN = "# BEGIN pulumi-labs-export proxy (managed block, do not edit)"
GRADLE_END = "# END pulumi-labs-export proxy"


def nuget_packages(prefix):
    """Return the NuGet package cache used for every dotnet run: <prefix>/nuget-packages."""
    return os.path.join(prefix, "nuget-packages")


def mask_proxy(url):
    """Return url with any userinfo replaced by '***', safe to log."""
    if not url:
        return url
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)?[^/@]*@", lambda m: (m.group(1) or "") + "***@", url, count=1)


def parse_proxy_url(url):
    """Parse a proxy URL into (host, port, user, password); user/password are None without userinfo.

    A missing port defaults to 443 for an https:// URL and 80 otherwise (including no scheme).
    Returns None if url is empty or has no host.
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    user = urllib.parse.unquote(parts.username) if parts.username is not None else None
    password = urllib.parse.unquote(parts.password) if parts.password is not None else None
    return host, port, user, password


def env_first(env, *names):
    """Return the first non-empty value among env[names], or ''."""
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


def translate_no_proxy(value):
    """Translate NO_PROXY (comma separated) to a Java nonProxyHosts value (| separated)."""
    return "|".join(h.strip() for h in (value or "").split(",") if h.strip())


def _prop(value):
    """Escape a value for a .properties file."""
    out = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return re.sub(r"^ ", "\\ ", out)


def gradle_proxy_lines(env):
    """Return the managed gradle.properties proxy lines for env (empty list if no proxy is set)."""
    lines = []
    for scheme, names in (("https", ("HTTPS_PROXY", "https_proxy")), ("http", ("HTTP_PROXY", "http_proxy"))):
        parsed = parse_proxy_url(env_first(env, *names))
        if not parsed:
            continue
        host, port, user, password = parsed
        lines.append("systemProp.%s.proxyHost=%s" % (scheme, _prop(host)))
        lines.append("systemProp.%s.proxyPort=%d" % (scheme, port))
        if user is not None:
            lines.append("systemProp.%s.proxyUser=%s" % (scheme, _prop(user)))
            lines.append("systemProp.%s.proxyPassword=%s" % (scheme, _prop(password or "")))
    if lines:
        nph = translate_no_proxy(env_first(env, "NO_PROXY", "no_proxy"))
        if nph:
            lines.append("systemProp.http.nonProxyHosts=%s" % _prop(nph))
            lines.append("systemProp.https.nonProxyHosts=%s" % _prop(nph))
    return lines


def rewrite_managed_block(text, block_lines):
    """Return text with the managed block replaced by block_lines (removed if empty); other lines are kept."""
    kept, skipping = [], False
    for line in text.splitlines():
        if line.strip() == GRADLE_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == GRADLE_END:
                skipping = False
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if block_lines:
        if kept:
            kept.append("")
        kept += [GRADLE_BEGIN] + list(block_lines) + [GRADLE_END]
    return "\n".join(kept) + "\n" if kept else ""


def write_gradle_properties(prefix, env=None, err=None):
    """Regenerate $GRADLE_USER_HOME/gradle.properties proxy keys from the proxy env (mode 0600).

    Only the managed block is touched; other lines are preserved. With no proxy env the block is
    removed (and the file deleted if nothing else is left). Credentials are never logged.
    """
    env = os.environ if env is None else env
    err = err or (lambda s: None)
    home = env.get("GRADLE_USER_HOME") or gradle_user_home(prefix)
    path = os.path.join(home, "gradle.properties")
    os.makedirs(home, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    lines = gradle_proxy_lines(env)
    new = rewrite_managed_block(old, lines)
    if not new:
        if os.path.exists(path):
            os.remove(path)
        err("gradle proxy: none set; managed block removed")
        return path
    if new != old or not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
    os.chmod(path, 0o600)
    if lines:
        shown = [mask_proxy(env_first(env, *n)) for n in (("HTTPS_PROXY", "https_proxy"), ("HTTP_PROXY", "http_proxy"))]
        err("gradle proxy: wrote %s (https=%s http=%s)" % (path, shown[0] or "-", shown[1] or "-"))
    return path


def ensure_nuget_marker(prefix, env=None, err=None):
    """Create the empty NuGet migrations marker so dotnet skips its named mutex.

    Uses ${XDG_DATA_HOME:-$HOME/.local/share}/NuGet/Migrations/1; if that is not writable, falls back to
    <prefix>/xdg-data. Returns {"XDG_DATA_HOME": dir} when the fallback is needed (export it), else {}.
    """
    env = os.environ if env is None else env
    err = err or (lambda s: None)
    base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "share")
    for data_home, extra in ((base, {}), (os.path.join(prefix, "xdg-data"), None)):
        if extra is None:
            extra = {"XDG_DATA_HOME": data_home}
        d = os.path.join(data_home, "NuGet", "Migrations")
        try:
            os.makedirs(d, exist_ok=True)
            marker = os.path.join(d, "1")
            if not os.path.exists(marker):
                open(marker, "a").close()
            if not os.access(marker, os.R_OK):
                raise OSError("marker not readable")
            if extra:
                err("nuget: %s not writable, using XDG_DATA_HOME=%s" % (base, data_home))
            return extra
        except OSError:
            continue
    raise BootstrapError("cannot create the NuGet migrations marker under %s or %s" % (base, os.path.join(prefix, "xdg-data")))


def apply_sandbox_fixes(prefix, env=None, err=None):
    """Apply the per-session sandbox fixes; return extra exports ({} or {"XDG_DATA_HOME": ...}).

    Always runs, even when every tool is installed: the NuGet migrations marker (dotnet crashes on
    named mutexes otherwise), the NuGet package dir and gradle.properties proxy keys (the Gradle JVM
    ignores the proxy env vars).
    """
    env = os.environ if env is None else env
    os.makedirs(nuget_packages(prefix), exist_ok=True)
    extra = ensure_nuget_marker(prefix, env, err)
    write_gradle_properties(prefix, env, err)
    return extra


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
    ap.add_argument("--cache-key", action="store_true",
                    help="print a hash of the pins of the selected tools (for a CI cache of <prefix>/opt and "
                         "<prefix>/bin) and exit; downloads nothing")
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
    if args.cache_key:
        out(cache_key(specs, [n for n in PROFILES[args.profile] if not args.only or n == args.only], arch))
        return 0
    if args.check:
        return 0 if run_check(prefix, specs, args.only, out, args.profile) else 1

    try:
        os.makedirs(gradle_user_home(prefix), exist_ok=True)
    except OSError as e:
        err("bootstrap: error: cannot create %s: %s" % (gradle_user_home(prefix), e.strerror or e.__class__.__name__))
        return 1
    extra_env = {}
    try:
        extra_env = apply_sandbox_fixes(prefix, os.environ, err)
    except (BootstrapError, OSError) as e:
        err("bootstrap: error: sandbox fixes: %s" % (getattr(e, "strerror", None) or e))
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
    for var, val in extra_env.items():
        out(export_line(var, val))
    for name, ver in report:
        out("# %s: %s" % (name, ver))
    return 0


if __name__ == "__main__":
    sys.exit(main())
