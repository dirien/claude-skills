#!/usr/bin/env python3
"""Print the onboarding issue drafts for pulumi/platform-providers-team.

    python3 scripts/onboarding.py --provider modal
    python3 scripts/onboarding.py --provider modal --repo ../pulumi-modal --out-dir /tmp/issues

The asks are derived by parsing release.yml (the rendered archetype one, or
the one in --repo), not from memory. If the workflow no longer has the jobs
and environments this script expects, it fails: the archetype changed, stop
and tell a human.

One ask per issue. Run the printed duplicate check before filing each.
"""
import argparse
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402
import render  # noqa: E402

TICKET_REPO = "pulumi/platform-providers-team"
NOTE = ("Note: This is a stop gap until Workprentice has permissions to Linear. "
        "If you do have any questions or concerns, speak to @pose")
MAVEN_SECRETS = ["MAVEN_CENTRAL_OSSRH_PASSWORD", "MAVEN_CENTRAL_OSSRH_USERNAME",
                 "MAVEN_CENTRAL_SIGNING_KEY", "MAVEN_CENTRAL_SIGNING_PASSWORD"]


class WorkflowError(Exception):
    """The release workflow does not look the way this script expects."""


def parse_jobs(text):
    """Return {job: {"environment", "uses", "esc_environment", "oidc_org", "esc_outputs", "id_token_write"}}."""
    jobs = {}
    cur = None
    in_jobs = False
    for raw in text.splitlines():
        if re.match(r"^jobs:\s*$", raw):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if raw and not raw.startswith(" "):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", raw)
        if m:
            cur = jobs.setdefault(m.group(1), {"environment": None, "uses": [], "esc_environment": None,
                                               "oidc_org": None, "esc_outputs": set(), "id_token_write": False})
            continue
        if cur is None or raw.strip().startswith("#"):
            continue
        line = raw.strip()
        m = re.match(r"^environment:\s*(\S+)\s*$", line)
        if m and raw.startswith("    environment:"):
            cur["environment"] = m.group(1)
        m = re.match(r"^-?\s*uses:\s*([^\s#]+)", line)
        if m:
            cur["uses"].append(m.group(1).split("@")[0])
        m = re.match(r"^ESC_ACTION_ENVIRONMENT:\s*(.+?)\s*$", line)
        if m:
            cur["esc_environment"] = m.group(1)
        m = re.match(r"^ESC_ACTION_OIDC_ORGANIZATION:\s*(\S+)\s*$", line)
        if m:
            cur["oidc_org"] = m.group(1)
        for o in re.findall(r"steps\.esc-secrets\.outputs\.([A-Z0-9_]+)", line):
            cur["esc_outputs"].add(o)
        if line == "id-token: write":
            cur["id_token_write"] = True
    return jobs


def resolve_esc_env(expr, provider):
    """Substitute the owner/repo expressions of the workflow for the real names."""
    out = expr.replace("${{ github.repository_owner }}", "pulumi-labs")
    out = out.replace("${{ github.event.repository.name }}", "pulumi-" + provider)
    return out


def expectations(jobs, provider):
    """Validate jobs against the expected archetype shape; return facts or raise WorkflowError."""
    problems = []

    def job(name):
        if name not in jobs:
            problems.append("release.yml has no job %r" % name)
            return None
        return jobs[name]

    def need(cond, msg):
        if not cond:
            problems.append(msg)

    npm = job("publish-nodejs")
    if npm:
        need(npm["environment"] == "publish-npm", "publish-nodejs: expected environment publish-npm, found %r" % npm["environment"])
        need(npm["id_token_write"], "publish-nodejs: expected `id-token: write` (OIDC trusted publishing)")
    py = job("publish-python")
    if py:
        need(py["environment"] == "publish-pypi", "publish-python: expected environment publish-pypi, found %r" % py["environment"])
        need("pypa/gh-action-pypi-publish" in py["uses"], "publish-python: expected pypa/gh-action-pypi-publish")
        need(py["id_token_write"], "publish-python: expected `id-token: write`")
    nu = job("publish-dotnet")
    if nu:
        need(nu["environment"] == "publish-nuget", "publish-dotnet: expected environment publish-nuget, found %r" % nu["environment"])
        need("NuGet/login" in nu["uses"], "publish-dotnet: expected NuGet/login (trusted publishing)")
        need("NUGET_USER" in nu["esc_outputs"], "publish-dotnet: expected NUGET_USER from the ESC action")
    jv = job("publish-java")
    if jv:
        need(sorted(jv["esc_outputs"]) == MAVEN_SECRETS,
             "publish-java: expected ESC outputs %s, found %s" % (MAVEN_SECRETS, sorted(jv["esc_outputs"])))
    go = job("publish-go-sdk")
    if go:
        need("pulumi/publish-go-sdk-action" in go["uses"], "publish-go-sdk: expected pulumi/publish-go-sdk-action")
    esc = None
    org = None
    for name in ("publish-dotnet", "publish-java"):
        j = jobs.get(name)
        if not j:
            continue
        if not j["esc_environment"]:
            problems.append("%s: no ESC_ACTION_ENVIRONMENT" % name)
            continue
        resolved = resolve_esc_env(j["esc_environment"], provider)
        need(resolved == "github-secrets/pulumi-labs-pulumi-" + provider,
             "%s: ESC environment %r does not resolve to github-secrets/pulumi-labs-pulumi-%s" % (name, j["esc_environment"], provider))
        need(j["oidc_org"] == "pulumi", "%s: expected ESC OIDC org pulumi, found %r" % (name, j["oidc_org"]))
        esc, org = resolved, j["oidc_org"]
    if problems:
        raise WorkflowError("release.yml does not match what onboarding.py expects (the archetype changed; stop and tell a human):\n  - "
                            + "\n  - ".join(problems))
    return {"esc_environment": esc, "oidc_org": org}


def build_issues(provider, release_text):
    """Return a list of {"title", "body", "unblocks"} issue drafts."""
    n = names.derive(provider)
    facts = expectations(parse_jobs(release_text), provider)
    esc = facts["esc_environment"]
    repo = n["repo"]
    secrets = "\n".join("  - `%s`" % s for s in MAVEN_SECRETS)
    issues = []

    def add(title, what, names_block, unblocks):
        body = "\n".join([
            "## What to do", what, "",
            "## Exact names", names_block, "",
            "## What it unblocks", unblocks, "",
            "Repo: https://github.com/%s" % repo, "",
            NOTE,
        ])
        issues.append({"title": title, "body": body})

    add("Onboard %s: create ESC environment %s" % (repo, esc),
        "Create the Pulumi ESC environment below and let the GitHub repo `%s` read it through OIDC (organization `%s`, "
        "organization access-token type, which is what `release.yml` requests). "
        "It must define the values listed under exact names." % (repo, facts["oidc_org"]),
        "- ESC environment: `%s`\n- OIDC organization: `%s`\n- Values the release workflow reads:\n  - `NUGET_USER` (nuget.org policy-owner profile slug)\n%s"
        % (esc, facts["oidc_org"], secrets),
        "Jobs `publish-dotnet` (NuGet) and `publish-java` (Maven Central) in `.github/workflows/release.yml`.")
    add("Onboard %s: npm trusted publishing for %s" % (repo, n["npm_package"]),
        "Create the npm package `%s` if it does not exist (public access) and configure trusted publishing for it. No token is stored: "
        "`publish-nodejs` authenticates with GitHub OIDC (`id-token: write`)." % n["npm_package"],
        "- npm package: `%s`\n- Trusted publisher: GitHub Actions\n- Owner/repo: `%s`\n- Workflow filename: `release.yml`\n- Environment: `publish-npm`"
        % (n["npm_package"], repo),
        "Job `publish-nodejs`.")
    add("Onboard %s: PyPI pending trusted publisher for %s" % (repo, n["pypi_name"]),
        "Add a pending trusted publisher on PyPI for a project that does not exist yet. `publish-python` uses "
        "`pypa/gh-action-pypi-publish` with OIDC; PyPI binds the publisher to the exact workflow filename and environment name.",
        "- PyPI project name: `%s`\n- Owner/repo: `%s`\n- Workflow filename: `release.yml`\n- Environment: `publish-pypi`"
        % (n["pypi_name"], repo),
        "Job `publish-python`.")
    add("Onboard %s: NuGet trusted publishing policy for %s" % (repo, n["dotnet_package"]),
        "Create a nuget.org trusted publishing policy (reserve the package ID or prefix first if needed). `publish-dotnet` exchanges its "
        "GitHub OIDC token for a short-lived API key with `NuGet/login`, using the user `NUGET_USER` read from the ESC environment `%s`. "
        "No NuGet API key secret is involved." % esc,
        "- Package ID: `%s`\n- Policy owner: the nuget.org profile whose slug is `NUGET_USER` in ESC\n- Repository owner/repo: `%s`\n"
        "- Workflow filename: `release.yml`\n- Environment: `publish-nuget`" % (n["dotnet_package"], repo),
        "Job `publish-dotnet`.")
    add("Onboard %s: Maven Central publishing for %s" % (repo, n["maven_coordinates"]),
        "Confirm `%s` can be published under the existing `%s` namespace with the credentials in the ESC environment `%s` "
        "(ESC OIDC org `%s`). `publish-java` reads these four values from ESC and runs `gradle publishToSonatype closeAndReleaseSonatypeStagingRepository`."
        % (n["maven_coordinates"], n["maven_group"], esc, facts["oidc_org"]),
        "- Maven coordinates: `%s`\n- ESC environment: `%s`\n- ESC values:\n%s" % (n["maven_coordinates"], esc, secrets),
        "Job `publish-java`.")
    return issues


def dup_check(title):
    """Return the duplicate-check command for a title."""
    return 'gh issue list -R %s --state all --search "%s in:title"' % (TICKET_REPO, title)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", required=True, help="provider slug")
    ap.add_argument("--repo", help="carved repo dir; parse its .github/workflows/release.yml instead of the vendored template")
    ap.add_argument("--archetype", choices=sorted(render.ARCHETYPES), default="native",
                    help="which vendored release.yml to parse when --repo is not given (the publishing jobs are the same in both)")
    ap.add_argument("--out-dir", help="also write each body to <out-dir>/NN.md and print `gh issue create` commands")
    ap.add_argument("--json", action="store_true", help="print the drafts as JSON")
    args = ap.parse_args(argv)
    try:
        names.validate_slug(args.provider)
        if args.repo:
            with open(os.path.join(args.repo, ".github", "workflows", "release.yml"), encoding="utf-8") as f:
                text = f.read()
        else:
            opts = render.make_options(args.provider, "unused", [], archetype=args.archetype,
                                       flavor="pf" if args.archetype == "bridged" else None)
            text = render.render_all(opts)[".github/workflows/release.yml"].decode("utf-8")
        issues = build_issues(args.provider, text)
    except (names.SlugError, WorkflowError, render.RenderError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    for i, it in enumerate(issues, 1):
        it["duplicate_check"] = dup_check(it["title"])
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            path = os.path.join(args.out_dir, "%02d.md" % i)
            with open(path, "w", encoding="utf-8") as f:
                f.write(it["body"] + "\n")
            it["create_command"] = "gh issue create -R %s --title %s --body-file %s" % (TICKET_REPO, shlex.quote(it["title"]), shlex.quote(path))
    if args.json:
        print(json.dumps(issues, indent=2))
        return 0
    for i, it in enumerate(issues, 1):
        print("=" * 72)
        print("ISSUE %d of %d on %s" % (i, len(issues), TICKET_REPO))
        print("1. duplicate check (run first; add evidence to an existing issue instead of filing a duplicate):")
        print("   " + it["duplicate_check"])
        if "create_command" in it:
            print("2. file it:\n   " + it["create_command"])
        print("TITLE: " + it["title"])
        print("BODY:")
        print(it["body"])
        print()
    print("Go needs no onboarding issue: pulumi/publish-go-sdk-action pushes the SDK tag with GITHUB_TOKEN.")
    print("Do not tag a release until every issue above is closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
