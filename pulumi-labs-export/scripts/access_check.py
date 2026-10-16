#!/usr/bin/env python3
"""Session-start access probes. Probe, do not assume.

    python3 scripts/access_check.py --provider modal

Needs `gh` and `git` on PATH. Prints each probe's endpoint and status and
never prints tokens or environment variables. Exits 1 if any probe fails.

Why push is probed with git and not with the REST `permissions` object: for
the Workprentice GitHub App installation token, `permissions` reads all
false even on repos the app can read and push to (verified 2026-10-16), so
`permissions.push == true` is NOT a valid probe. `git push --dry-run` is:
GitHub rejects at the receive-pack advertisement when pushing is not allowed.

Creating an issue cannot be probed without creating one. Treat a 403 on the
first real `gh issue create` as the failure and report it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402

TOKEN_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
URL_CRED_RE = re.compile(r"(://)[^/@\s]+@")


def redact(text):
    """Strip anything that looks like a token or URL credential."""
    return URL_CRED_RE.sub(r"\1***@", TOKEN_RE.sub("***", text))


def run(cmd, cwd=None, env=None):
    """Run cmd, returning (returncode, combined output); never raises on missing tools."""
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return 127, "%s not found on PATH" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after 120s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def parse_http(output):
    """Return (status int or None, body str) from `gh api -i` output."""
    m = re.match(r"HTTP/[\d.]+ (\d{3})", output.lstrip())
    status = int(m.group(1)) if m else None
    body = re.split(r"\r?\n\r?\n", output, maxsplit=1)
    return status, body[1] if len(body) > 1 else ""


def gh_get(endpoint, runner=run):
    """GET endpoint with gh; return (status, parsed JSON body or {})."""
    rc, out = runner(["gh", "api", "-i", endpoint])
    status, body = parse_http(out)
    if status is None:
        raise RuntimeError("no HTTP status from gh api %s (rc=%s): %s" % (endpoint, rc, redact(out.strip()[:200])))
    try:
        return status, json.loads(body)
    except ValueError:
        return status, {}


def probe_push(repo_url, runner=run):
    """git push --dry-run an empty commit to a throwaway ref; return (ok, detail)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory() as d:
        steps = [
            ["git", "init", "-q"],
            ["git", "-c", "user.name=access-probe", "-c", "user.email=access-probe@invalid.example",
             "commit", "--allow-empty", "-q", "-m", "access probe"],
        ]
        for s in steps:
            rc, out = runner(s, cwd=d, env=env)
            if rc != 0:
                return False, "local setup failed: " + redact(out.strip()[:200])
        rc, out = runner(["git", "push", "--dry-run", repo_url, "HEAD:refs/heads/access-probe"], cwd=d, env=env)
    return rc == 0, "" if rc == 0 else redact(out.strip()[-300:])


def run_probes(provider, runner=run):
    """Return a list of result dicts: name, endpoint, status, ok, detail."""
    n = names.derive(provider)
    results = []

    def add(name, endpoint, status, ok, detail=""):
        results.append({"name": name, "endpoint": endpoint, "status": status, "ok": ok, "detail": detail})

    ep = "/repos/pulumi/autoregistry"
    st, _ = gh_get(ep, runner)
    add("read monorepo", "GET " + ep, st, st == 200,
        "" if st == 200 else "no access to the private monorepo (404 on a private repo means no access, not a missing repo)")

    ep = "/repos/pulumi/platform-providers-team"
    st, body = gh_get(ep, runner)
    ok = st == 200 and body.get("has_issues") is True and not body.get("archived")
    detail = ""
    if st != 200:
        detail = "no access to the ticket repo"
    elif not ok:
        detail = "repo readable but issues are disabled or the repo is archived"
    add("read ticket repo", "GET " + ep, st, ok, detail)

    ep = "/repos/pulumi-labs/pulumi-" + provider
    st, _ = gh_get(ep, runner)
    exists = st == 200
    detail = ""
    if st == 404:
        detail = ("target repo not found or not visible; you cannot create it: file an issue on pulumi/platform-providers-team "
                  "asking a pulumi-labs admin to create %s and grant the Workprentice app push" % n["repo"])
    elif st != 200:
        detail = "unexpected status"
    add("read target repo", "GET " + ep, st, exists, detail)

    url = n["repo_url"] + ".git"
    if exists:
        ok, detail = probe_push(url, runner)
        add("push to target repo", "git push --dry-run %s HEAD:refs/heads/access-probe" % url,
            "accepted" if ok else "rejected", ok,
            "" if ok else "push not allowed for this credential: " + detail)
    else:
        add("push to target repo", "git push --dry-run %s HEAD:refs/heads/access-probe" % url, "skipped", False,
            "skipped because the target repo is not readable")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", required=True, help="provider slug, e.g. modal")
    args = ap.parse_args(argv)
    try:
        names.validate_slug(args.provider)
        results = run_probes(args.provider)
    except (names.SlugError, RuntimeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    for r in results:
        print("%s  %-20s %s -> %s%s" % ("PASS" if r["ok"] else "FAIL", r["name"], r["endpoint"], r["status"],
                                        ("\n      " + r["detail"]) if r["detail"] else ""))
    print("NOTE  issue creation on pulumi/platform-providers-team is NOT probed: it cannot be done without creating an "
          "issue. A 403 on the first real `gh issue create` is the failure; report it and stop.")
    failed = [r for r in results if not r["ok"]]
    if failed:
        print("\naccess check FAILED. Stop, report the exact endpoint and status above, and note that credentials are fixed "
              "per session (a fix may need a new session). Never search for, print or swap credentials.", file=sys.stderr)
        return 1
    print("\naccess check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
