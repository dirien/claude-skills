#!/usr/bin/env python3
"""Secret scan for a pulumi-labs mirror: gitleaks over the FULL git history plus the tree at HEAD.

    python3 scripts/secret_scan.py --repo ../pulumi-modal

Runs, with the default gitleaks rules:

1. `gitleaks git`  over every commit reachable in the repo (history keeps a
   secret even after the file is fixed), and
2. `gitleaks dir`  over the tracked files of HEAD (a `git archive`, so build
   output and untracked files are not scanned).

A reviewed allowlist may sit at the repo root as `.gitleaks.toml`
(`export.py` copies `lib/providers/<p>/.gitleaks.toml` from the monorepo). It
must extend the default rules (`[extend]` with `useDefault = true`); a config
that replaces them is refused. Allowlist entries are only ever added through a
reviewed monorepo PR (references/verify.md).

The report is written next to the verify reports, keyed by HEAD SHA:
`$HOME/.local/pulumi-labs-export/reports/<repo>-<HEAD sha>-secrets.{json,md}`.
It holds only rule, file, line and commit of each finding, never the secret.
`verify.py --assert-pushable` requires a passing report for the current HEAD.

Exit 0: no findings. Exit 1: findings (or a refused config). Exit 2: the scan
could not run (gitleaks missing or crashed), so the history was NOT scanned.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bootstrap  # noqa: E402

REPORT_VERSION = 1
CONFIG_FILE = ".gitleaks.toml"
MAX_SHOWN = 30
EXTEND_RE = re.compile(r"(?ms)^\s*\[extend\]\s*$.*?^\s*useDefault\s*=\s*true\b")


class ScanError(Exception):
    """The scan was refused (bad config, git failure); the message says what to fix."""


class ScanUnavailable(ScanError):
    """The scan could not run at all (gitleaks missing or crashed): nothing was scanned."""


def report_base(repo, head):
    """Base file name (no extension) of the secret-scan report of repo at head."""
    return "%s-%s-secrets" % (os.path.basename(os.path.abspath(repo).rstrip("/")), head)


def report_path(repo, head, report_dir):
    """Absolute path of the JSON secret-scan report of repo at head."""
    return os.path.join(report_dir, report_base(repo, head) + ".json")


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise ScanError("git %s failed in %s: %s" % (" ".join(args[:2]), repo, p.stderr.strip()[:300]))
    return p.stdout


def git_state(repo):
    """Return {'head', 'tree', 'porcelain'} of repo."""
    return {"head": _git(repo, "rev-parse", "HEAD").strip(), "tree": _git(repo, "rev-parse", "HEAD^{tree}").strip(),
            "porcelain": _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()}


def find_gitleaks(explicit=None, prefix=None):
    """Return the gitleaks executable path: --gitleaks, else the bootstrap prefix, else PATH; None if absent."""
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    cand = os.path.join(prefix or bootstrap.default_prefix(), "bin", "gitleaks")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return shutil.which("gitleaks")


def read_config(repo):
    """Return (path, sha256, text) of the repo's allowlist config, or (None, None, None). Raises ScanError if it drops the default rules."""
    path = os.path.join(repo, CONFIG_FILE)
    if not os.path.isfile(path):
        return None, None, None
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", "replace")
    if not EXTEND_RE.search(text):
        raise ScanError("%s does not extend the default rules. Start it with:\n\n[extend]\nuseDefault = true\n\n"
                        "A config without it replaces ALL default rules, so nothing would be scanned. Fix it in the monorepo "
                        "(lib/providers/<p>/.gitleaks.toml) through a reviewed PR; never edit the mirror." % CONFIG_FILE)
    return path, hashlib.sha256(raw).hexdigest(), text


def run_gitleaks(exe, kind, target, config, tmp, log):
    """Run `gitleaks <kind> <target>`; return (findings list, text output). Raises ScanError if gitleaks itself fails."""
    out = os.path.join(tmp, "%s-report.json" % kind)
    argv = [exe, kind, "--no-banner", "--redact", "--report-format", "json", "--report-path", out, "--exit-code", "1"]
    if config:
        argv += ["--config", config]
    argv.append(target)
    log("running %s" % " ".join(argv[:3] + ["..."] + argv[-1:]))
    p = subprocess.run(argv, capture_output=True, text=True, check=False)
    text = (p.stdout + p.stderr)
    if p.returncode not in (0, 1):
        raise ScanUnavailable("`gitleaks %s` failed (exit %d), so the repo was NOT scanned. Last output:\n%s"
                        % (kind, p.returncode, "\n".join(text.strip().splitlines()[-15:])))
    findings = []
    if os.path.isfile(out):
        try:
            with open(out, encoding="utf-8") as f:
                data = json.load(f) or []
        except ValueError as e:
            raise ScanUnavailable("gitleaks %s wrote an unreadable report: %s" % (kind, e))
        for d in data:
            findings.append({"rule": d.get("RuleID"), "file": d.get("File"), "line": d.get("StartLine"),
                             "commit": (d.get("Commit") or "")[:12] or None, "fingerprint": d.get("Fingerprint")})
    if p.returncode == 1 and not findings:
        raise ScanUnavailable("gitleaks %s exited 1 but wrote no findings; treating the scan as failed" % kind)
    return findings, text


def head_tree_dir(repo, tmp):
    """Extract the tracked files of HEAD into a temp dir and return it."""
    dest = os.path.join(tmp, "tree")
    os.makedirs(dest)
    p = subprocess.run(["git", "-C", repo, "archive", "--format=tar", "HEAD"], capture_output=True, check=False)
    if p.returncode != 0:
        raise ScanError("git archive HEAD failed: %s" % p.stderr.decode("utf-8", "replace").strip()[:300])
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            tf.extractall(dest)
    return dest


def commit_count(repo):
    try:
        return int(_git(repo, "rev-list", "--all", "--count").strip())
    except (ScanError, ValueError):
        return None


def scan(repo, report_dir, *, gitleaks=None, prefix=None, log=lambda _m: None):
    """Scan repo and write the report; return the report dict. Raises ScanError if the scan cannot run."""
    repo = os.path.abspath(repo)
    state = git_state(repo)
    exe = find_gitleaks(gitleaks, prefix)
    if not exe:
        raise ScanUnavailable("gitleaks is not installed, so the history was NOT scanned for secrets. Run `eval \"$(python3 scripts/bootstrap.py)\"` "
                        "(it installs gitleaks) and retry. Do not push.")
    cfg_path, cfg_sha, cfg_text = read_config(repo)
    ver = subprocess.run([exe, "version"], capture_output=True, text=True, check=False)
    tmp = tempfile.mkdtemp(prefix="secret-scan-")
    try:
        history, _ = run_gitleaks(exe, "git", repo, cfg_path, tmp, log)
        tree_dir = head_tree_dir(repo, tmp)
        tree, _ = run_gitleaks(exe, "dir", tree_dir, cfg_path, tmp, log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    overall = "PASS" if not history and not tree and not state["porcelain"] else "FAIL"
    report = {
        "report_version": REPORT_VERSION, "mode": "secret-scan", "repo": repo, "repo_name": os.path.basename(repo.rstrip("/")),
        "head_sha": state["head"], "tree_sha": state["tree"], "porcelain": state["porcelain"],
        "finished": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gitleaks": {"path": exe, "version": (ver.stdout or ver.stderr).strip().splitlines()[0] if (ver.stdout or ver.stderr).strip() else None},
        "config": {"file": CONFIG_FILE if cfg_path else None, "sha256": cfg_sha, "content": cfg_text, "extends_default": True if cfg_path else None},
        "commits_scanned": commit_count(repo),
        "history_findings": history, "tree_findings": tree, "overall": overall,
    }
    write_report(report, report_dir)
    return report


def write_report(report, report_dir):
    """Write <repo>-<sha>-secrets.json and .md into report_dir; return both paths."""
    os.makedirs(report_dir, exist_ok=True)
    base = report_base(report["repo"], report["head_sha"])
    jp, mp = os.path.join(report_dir, base + ".json"), os.path.join(report_dir, base + ".md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    lines = ["# Secret scan: %s" % report["repo_name"], "", "- Result: **%s**" % report["overall"], "- HEAD: `%s`" % report["head_sha"],
             "- Tree: `%s`" % report["tree_sha"], "- gitleaks: %s" % (report["gitleaks"]["version"] or "unknown"),
             "- Commits scanned: %s" % report["commits_scanned"],
             "- Allowlist: %s" % (("`%s` (sha256 `%s`)" % (CONFIG_FILE, report["config"]["sha256"])) if report["config"]["file"] else "none, default rules only")]
    if report["porcelain"]:
        lines.append("- Worktree was dirty (%d paths): commit first, the report is for HEAD" % len(report["porcelain"]))
    for title, key in (("History findings", "history_findings"), ("Tree findings", "tree_findings")):
        lines += ["", "## %s (%d)" % (title, len(report[key]))]
        lines += ["- `%s` %s:%s (commit %s)" % (f["rule"], f["file"], f["line"], f["commit"] or "-") for f in report[key]] or ["- none"]
    if report["config"]["content"]:
        lines += ["", "## Allowlist in effect", "", "```toml", report["config"]["content"].rstrip(), "```"]
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return jp, mp


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="the mirror repo directory (default: cwd)")
    ap.add_argument("--report-dir", default=None, help="report directory (default: $HOME/.local/pulumi-labs-export/reports)")
    ap.add_argument("--prefix", default=None, help="toolchain prefix to find gitleaks in (default: $HOME/.local/pulumi-labs-export)")
    ap.add_argument("--gitleaks", default=None, help="path to the gitleaks executable (default: <prefix>/bin, then PATH)")
    args = ap.parse_args(argv)
    prefix = os.path.abspath(args.prefix or bootstrap.default_prefix())
    report_dir = os.path.abspath(args.report_dir or os.path.join(prefix, "reports"))

    def log(msg):
        print(msg, file=sys.stderr, flush=True)
    try:
        report = scan(args.repo, report_dir, gitleaks=args.gitleaks, prefix=prefix, log=log)
    except ScanError as e:
        print("secret_scan: error: %s" % e, file=sys.stderr)
        return 2 if isinstance(e, ScanUnavailable) else 1
    jp = report_path(report["repo"], report["head_sha"], report_dir)
    print("secret scan %s: %d history finding(s), %d tree finding(s), %s commits scanned" % (
        report["overall"], len(report["history_findings"]), len(report["tree_findings"]), report["commits_scanned"]))
    for key in ("history_findings", "tree_findings"):
        for f in report[key][:MAX_SHOWN]:
            print("  %s %s:%s %s (commit %s)" % (key.split("_")[0], f["file"], f["line"], f["rule"], f["commit"] or "-"))
    if report["porcelain"]:
        print("  the worktree is dirty (%d paths): commit, then scan again" % len(report["porcelain"]))
    print("report: %s" % jp)
    if report["overall"] != "PASS":
        print("Do not push. A finding is fixed in the monorepo; an allowlist entry is added only through a reviewed monorepo PR "
              "(references/verify.md).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
