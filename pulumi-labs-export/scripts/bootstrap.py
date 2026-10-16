#!/usr/bin/env python3
"""Install the pinned toolchain the pulumi-labs-export procedure needs.

Installs Go, the Pulumi CLI (with its bundled language hosts), jq, gitleaks
and GNU make into $HOME/.local/pulumi-labs-export/ and keeps symlinks in
<prefix>/bin. No compiler, no root, Python 3.8+ standard library only.

On success stdout is one line to eval, followed by '#' comment lines with
each tool's version; progress goes to stderr:

    eval "$(python3 scripts/bootstrap.py)"      # or copy the export line

It is idempotent (tools already at the pinned version are skipped), verifies
every download by SHA-256, exits 1 with a precise message on any failure and
never prints environment variables or tokens. See references/toolchain.md.
"""
import argparse
import dataclasses
import hashlib
import io
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

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

TOOL_ORDER = ["go", "pulumi", "jq", "gitleaks", "make"]


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
    kind: str = "tar"           # "tar" or "binary"
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


def sha256_file(path):
    """Return the hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, expected, label=None):
    """Raise BootstrapError unless path hashes to expected."""
    actual = sha256_file(path)
    if actual != expected.lower():
        raise BootstrapError("SHA-256 mismatch for %s: expected %s, got %s"
                             % (label or os.path.basename(path), expected.lower(), actual))


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

def run_version(cmd):
    """Run a version command; return (first output line, None) or (None, error)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PULUMI_SKIP_UPDATE_CHECK="true", PULUMI_AUTOMATION_API_SKIP_VERSION_CHECK="true"))
    except (OSError, subprocess.SubprocessError) as e:
        return None, "cannot run %s: %s" % (cmd[0], getattr(e, "strerror", None) or e.__class__.__name__)
    out = (p.stdout or p.stderr or "").strip().splitlines()
    if p.returncode != 0:
        return None, "%s exited %d: %s" % (os.path.basename(cmd[0]), p.returncode, _clean(out[0] if out else ""))
    return (out[0] if out else ""), None


def token_in(token, line):
    """True if token appears in line as a whole version token."""
    return re.search(r"(?<![\w.])%s(?![\w])" % re.escape(token), line) is not None


def check_spec(spec, prefix):
    """Return (status, detail) for spec in prefix: 'ok', 'wrong' or 'missing'."""
    exe = os.path.join(prefix, "bin", spec.exe)
    if not os.path.exists(exe):
        return "missing", ""
    line, err = run_version([exe] + list(spec.version_cmd))
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
            expected = expected_from_checksums(text, spec.filename, spec.checksums_url)
            source = spec.checksums_url
        download(spec.url, archive)
        verify_sha256(archive, expected, spec.filename)
        log("%s: SHA-256 ok (%s)" % (spec.filename, source))
        stage = os.path.join(tmp, "stage")
        if spec.kind == "binary":
            os.makedirs(stage)
            shutil.copyfile(archive, os.path.join(stage, spec.links[0][1]))
            os.chmod(os.path.join(stage, spec.links[0][1]), 0o755)
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


def run_check(prefix, specs, only, out):
    """Print what is installed without downloading; return True if everything is ok."""
    all_ok = True
    for name in TOOL_ORDER:
        if only and name != only:
            continue
        if name == "make":
            status, detail, path = check_make(prefix)
            where = " (%s)" % path if path else ""
        else:
            status, detail = check_spec(specs[name], prefix)
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
        description="Install the pinned toolchain (go, pulumi, jq, gitleaks, make) for the "
                    "pulumi-labs-export procedure into $HOME/.local/pulumi-labs-export. "
                    "On success stdout is an `export PATH=...` line to eval, then '# tool version' "
                    "comment lines; progress goes to stderr.")
    ap.add_argument("--check", action="store_true",
                    help="report what is installed and at which version; download nothing; "
                         "exit 1 if anything is missing or at the wrong version")
    ap.add_argument("--only", choices=TOOL_ORDER, help="act on a single tool")
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
        return 0 if run_check(prefix, specs, args.only, out) else 1

    failures, report = [], []
    for name in TOOL_ORDER:
        if args.only and name != args.only:
            continue
        try:
            if name == "make":
                report.append(("make", install_make(prefix, arch, err)))
                continue
            spec = specs[name]
            status, detail = check_spec(spec, prefix)
            if status == "ok":
                err("%s %s: already installed, skipping" % (name, spec.version))
                if os.path.isdir(install_dir(prefix, spec)):
                    link_tool(spec, prefix, install_dir(prefix, spec))
            else:
                install_spec(spec, prefix, err)
                status, detail = check_spec(spec, prefix)
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
    for name, ver in report:
        out("# %s: %s" % (name, ver))
    return 0


if __name__ == "__main__":
    sys.exit(main())
