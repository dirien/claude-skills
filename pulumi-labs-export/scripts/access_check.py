#!/usr/bin/env python3
"""Session-start access probes. Probe, do not assume.

    python3 scripts/access_check.py --provider modal

Needs `gh` and `git` on PATH. Prints each probe's endpoint and status and
never prints tokens or environment variables. Exits 1 if any probe fails.
Exits 2 when this process has no GitHub credentials (see below).

    python3 scripts/access_check.py --print-commands --provider modal

Some runtimes inject GitHub credentials only into a `gh` or `git` that is
invoked directly as the command, not into one started as a child of python
or a shell. Then the probes above cannot work from this script (exit 2).
`--print-commands` prints the same probes as one direct command per line
(no pipes, no cd) so the agent can run each line itself. Lines starting
with `#` are comments. PASS rule: every command exits 0, and the push
probe output contains no "denied" or "403".

Why push is probed with git and not with the REST `permissions` object: for
the Workprentice GitHub App installation token, `permissions` reads all
false even on repos the app can read and push to (verified 2026-10-16), so
`permissions.push == true` is NOT a valid probe. `git push --dry-run` is:
GitHub rejects at the receive-pack advertisement when pushing is not allowed.

Creating an issue cannot be probed without creating one. Treat a 403 on the
first real `gh issue create` as the failure and report it.

    python3 scripts/access_check.py --admin --provider modal

`--admin` (repository setup only) adds one informational probe that is not
part of the PASS rule: GET /repos/pulumi-labs/pulumi-<provider>/actions/permissions.
Only a repository admin can read it (fine-grained permission "Administration"
read; the Workprentice App has no Administration permission). 200 means this
identity is an admin and may set the repo up through `repo_setup.sh plan`
(SKILL.md, "Repository setup"); 403 or 404 means it is not. For a missing repo
no read can show whether this identity may create it: the create call that
`repo_setup.sh plan create` prints is that probe.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402

TOKEN_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
URL_CRED_RE = re.compile(r"(://)[^/@\s]+@")


EXIT_NO_CREDENTIALS = 2
NO_CREDENTIALS_HINT = ("credentials are not available to this process; run the probes directly with "
                       "`python3 scripts/access_check.py --print-commands --provider <provider>` "
                       "(each printed line as its own command, not via a shell or script)")
GIT_AUTH_MARKERS = ("could not read username", "could not read password", "authentication failed",
                    "terminal prompts disabled", "no such device or address")


class CredentialsUnavailable(RuntimeError):
    """A gh or git child process had no GitHub credentials."""


def gh_needs_login(rc, out):
    """True if gh output shows it has no credentials (rc=4 or the auth login message)."""
    return rc == 4 or "gh auth login" in out


def git_auth_failed(out):
    """True if git output shows a missing-credentials failure (not a permission denial)."""
    low = out.lower()
    return any(m in low for m in GIT_AUTH_MARKERS)


def probe_commands(provider, tmpdir):
    """Return the probe commands as argv lists, each runnable as one direct binary invocation."""
    n = names.derive(provider)
    url = n["repo_url"] + ".git"
    return [
        ["gh", "api", "/repos/pulumi/autoregistry"],
        ["gh", "api", "/repos/pulumi/platform-providers-team"],
        ["gh", "api", "/repos/pulumi-labs/pulumi-" + provider],
        ["git", "-C", tmpdir, "init", "-q"],
        ["git", "-C", tmpdir, "-c", "user.name=access-probe", "-c", "user.email=access-probe@invalid.example",
         "commit", "--allow-empty", "-q", "-m", "access probe"],
        ["git", "-C", tmpdir, "push", "--dry-run", url, "HEAD:refs/heads/access-probe"],
    ]


def admin_probe_endpoint(provider):
    """The read only a repository admin can make (Actions settings need the Administration permission)."""
    return "/repos/pulumi-labs/pulumi-%s/actions/permissions" % provider


def print_commands(provider, tmpdir=None, out=None, admin=False):
    """Print the probe commands, one per line, plus the PASS rule as # comments (and the admin probe if asked)."""
    out = out or sys.stdout
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="access-probe-")
    print("# Run each non-comment line below as its own command (a direct gh or git invocation, not via a shell or script).", file=out)
    for c in probe_commands(provider, tmpdir):
        print(shlex.join(c), file=out)
    print("# PASS rule: every command exits 0, and the push probe output contains no \"denied\" or \"403\".", file=out)
    print("# Read the three gh api responses: the ticket repo must have has_issues true and archived false.", file=out)
    if admin:
        print("# Admin probe (repository setup only, not part of the PASS rule):", file=out)
        print(shlex.join(["gh", "api", admin_probe_endpoint(provider)]), file=out)
        print("# 200: this identity is an admin of the repo: set it up with `scripts/repo_setup.sh plan` (SKILL.md, \"Repository setup\").", file=out)
        print("# 403 or 404: it is not, or the repo does not exist: use the issue path. If the third probe returned 404 (no repo),", file=out)
        print("# no read shows whether this identity may create it: the create call that `repo_setup.sh plan create` prints is that probe.", file=out)


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
    if gh_needs_login(rc, out):
        raise CredentialsUnavailable("gh api %s: no GitHub credentials (rc=%s)" % (endpoint, rc))
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
                if git_auth_failed(out):
                    raise CredentialsUnavailable("git: " + redact(out.strip()[:200]))
                return False, "local setup failed: " + redact(out.strip()[:200])
        rc, out = runner(["git", "push", "--dry-run", repo_url, "HEAD:refs/heads/access-probe"], cwd=d, env=env)
    if rc != 0 and git_auth_failed(out):
        raise CredentialsUnavailable("git push: " + redact(out.strip()[-200:]))
    return rc == 0, "" if rc == 0 else redact(out.strip()[-300:])


def run_probes(provider, runner=run, admin=False):
    """Return a list of result dicts: name, endpoint, status, ok, detail (info: True for the informational admin probe)."""
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
        detail = ("target repo not found or not visible. Only through `repo_setup.sh plan create` (SKILL.md, \"Repository setup\") "
                  "may you create it; otherwise file an issue on pulumi/platform-providers-team asking a pulumi-labs admin to "
                  "create %s and grant the Workprentice app push" % n["repo"])
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
    if admin:
        ep = admin_probe_endpoint(provider)
        if exists:
            st, _ = gh_get(ep, runner)
            add("admin on target repo", "GET " + ep, st, True,
                "yes: this identity may set the repo up with repo_setup.sh plan" if st == 200 else
                "no: use the issue path for repository setup")
        else:
            add("admin on target repo", "GET " + ep, "skipped", True,
                "the repo does not exist; the create call that repo_setup.sh plan create prints is the probe")
        results[-1]["info"] = True
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", required=True, help="provider slug, e.g. modal")
    ap.add_argument("--print-commands", action="store_true",
                    help="print the probes as direct commands, one per line, instead of running them")
    ap.add_argument("--admin", action="store_true",
                    help="also probe whether this identity is an admin of the target repo (repository setup only; informational)")
    args = ap.parse_args(argv)
    try:
        names.validate_slug(args.provider)
        if args.print_commands:
            print_commands(args.provider, admin=args.admin)
            return 0
        results = run_probes(args.provider, admin=args.admin)
    except CredentialsUnavailable as e:
        print("error: %s" % e, file=sys.stderr)
        print("HINT: " + NO_CREDENTIALS_HINT, file=sys.stderr)
        return EXIT_NO_CREDENTIALS
    except (names.SlugError, RuntimeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    for r in results:
        print("%s  %-20s %s -> %s%s" % ("INFO" if r.get("info") else "PASS" if r["ok"] else "FAIL", r["name"], r["endpoint"], r["status"],
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
