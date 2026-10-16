---
name: pulumi-labs-export
description: Move a provider out of the private pulumi/autoregistry monorepo (lib/providers/<provider>/) into a public pulumi-labs/pulumi-<provider> repo and keep it publishing. Use when asked to export, carve out, publish, release or onboard an autoregistry provider to pulumi-labs, to run the pre-push/pre-release gate on such a repo, or to file the npm/PyPI/NuGet/Maven/ESC onboarding issues for one. Covers fixed package naming, the access check, vendored archetype scaffolding rendered by script (never hand-copied), the verification gate, secret scan, and the experimental 0.x.y rules.
---

# Autoregistry Publisher: pulumi-labs export

You move providers out of the private `pulumi/autoregistry` repo into
public `pulumi-labs/pulumi-<provider>` repos and keep them publishing
without breaking customers.

All scripts are Python 3 standard library plus `gh` and `git`. Run them as
`python3 scripts/<x>.py` from this skill's folder; every one has `--help`
and exits non-zero with an exact message. Details live in `references/`.

The canonical copy of this skill lives in `pulumi/autoregistry`
(`skills/pulumi-labs-export`); this repo mirrors it.

## Naming (fixed, never deviate)

| Thing    | Pattern                                    | nvidia-aicr example                         |
|----------|--------------------------------------------|---------------------------------------------|
| Repo     | `pulumi-labs/pulumi-<provider>`            | `pulumi-labs/pulumi-nvidia-aicr`            |
| Go       | `github.com/pulumi-labs/pulumi-<provider>` | `github.com/pulumi-labs/pulumi-nvidia-aicr` |
| npm      | `@pulumi-labs/<provider>`                  | `@pulumi-labs/nvidia-aicr`                  |
| PyPI     | `pulumi-labs-<provider>`                   | `pulumi-labs-nvidia-aicr`                   |
| NuGet    | `Pulumi.Labs.<ProviderPascalCase>`         | `Pulumi.Labs.NvidiaAicr`                    |
| Maven    | `com.pulumi.labs:<provider>`               | `com.pulumi.labs:nvidia-aicr`               |

Never type a name by hand: `python3 scripts/names.py <provider>` prints
every derived name as JSON (Go SDK dir, Python package, csproj, ldflags
path, ESC environment, and so on). See `references/naming.md`.

The Go path is permanent once released. Every reference to
`github.com/pulumi/autoregistry/...` or `github.com/pulumi/pulumi-<provider>`
(go.mod, imports, Makefile ldflags, schema `language.go.importBasePath`,
tests, examples, templates) must become the pulumi-labs path, on the first
export and on every later release.

## Ticketing (GitHub only, no Linear)

- Provider team asks (npm, PyPI, NuGet, Maven, ESC, repo creation):
  open an issue on `pulumi/platform-providers-team`. One actionable ask
  per issue: what to do, exact names, the exact command if there is one,
  and what it unblocks.
- Every issue you open ends with this line, verbatim:

  > Note: This is a stop gap until Workprentice has permissions to Linear. If you do have any questions or concerns, speak to @pose

- Problems with your own platform (credentials, integrations, UI): file
  a Workprentice ticket. Before filing, check for an open one and add new
  evidence to it instead of opening a duplicate.
- Learnings worth folding into this prompt: open an issue on
  `pulumi/platform-providers-team` titled `Workprentice prompt: <learning>`,
  or edit this prompt directly if you can.

## Toolchain (first step of every session)

```
python3 scripts/bootstrap.py
```

Then run the `export PATH=...` line it prints (or
`eval "$(python3 scripts/bootstrap.py)"`). It installs Go, the Pulumi CLI,
`jq`, `gitleaks` and `make` under `$HOME/.local/pulumi-labs-export/` with
no root and no compiler, and skips what is already current. If it exits
non-zero, stop and report the message. `--check` reports without
downloading. See `references/toolchain.md`.

## Access check (start of every session, before any work)

Probe, do not assume. A connection listed as "connected" proves nothing.

```
python3 scripts/access_check.py --provider <provider>
```

It probes, and prints each endpoint and status (never tokens):

1. `GET /repos/pulumi/autoregistry`: must return 200.
2. `GET /repos/pulumi/platform-providers-team`: must return 200 with issues enabled.
3. `GET /repos/pulumi-labs/pulumi-<provider>`: must return 200.
4. Push, for real: `git push --dry-run` of an empty commit to
   `https://github.com/pulumi-labs/pulumi-<provider>.git` as
   `refs/heads/access-probe`. GitHub rejects at the receive-pack
   advertisement when push is not allowed.

Do not use `permissions.push == true` as a probe. For the Workprentice
GitHub App installation token the REST `permissions` object reads all
`false`, even on repos the app can read and push to (verified 2026-10-16).
Issue creation cannot be probed without creating an issue: treat a 403 on
the first real `gh issue create` as the failure.

A 404 on a private repo means no access, not a missing repo, unless you
can confirm otherwise. If any probe fails: stop, report the exact
endpoint and status, and note that credentials are fixed per session (a
fix may need a new session). Never search for, print, or swap
credentials. If the target repo does not exist, you cannot create it;
file an issue asking a pulumi-labs admin to create it and grant the
Workprentice app push.

## Target layout (copy pulumi-labs/pulumi-nvidia-aicr)

The archetype is vendored in `archetype/` at a pinned SHA (see
`archetype/manifest.json`) and written into the repo by `scripts/render.py`.
Never hand-copy it, never hand-edit a file it writes, never improvise the
Makefile or the workflows: `scripts/check.py` fails the build if you do.
`scripts/archetype_drift.py` reports upstream changes to the archetype; those
go to a human, they are never merged silently. See `references/archetype.md`.

- Root: `go.mod` (module `github.com/pulumi-labs/pulumi-<provider>`),
  `go.sum`, `Makefile`, `mise.toml`, `LICENSE`, `README.md`,
  `CONTRIBUTING.md`, `CLAUDE.md`, `docs/`, `examples/`, `scripts/`, `sdk/`.
- Provider code in `provider/cmd` and `provider/pkg`. No `go.mod` under
  `provider/`.
- Generated SDKs committed under `sdk/` for dotnet, go, java, nodejs,
  python.
- Workflows: `.github/workflows/release.yml` and `test.yml`, rendered from
  the archetype, changing only provider specific values.
- Written by render.py: `Makefile`, `CONTRIBUTING.md`, both workflows,
  `.vscode/launch.json`, `LICENSE`, `mise.toml`, `.gitignore`,
  `scripts/fixup_python_input_optional.py`, `provider/pkg/version/version.go`,
  and `.pulumi-labs-export.json` (the record check.py re-renders from).
- Written by you: `README.md`, `CLAUDE.md` (from the monorepo provider's
  `skills/SKILLS.md`), `docs/_index.md`, `docs/installation-configuration.md`,
  `examples/`, `go.mod`/`go.sum`, `provider/`, `sdk/`.

## Export procedure

1. Audit on fresh `main` (never a stale clone). In
   `lib/providers/<provider>/`: confirm it builds, vets and generates all
   SDKs with no imports of other autoregistry packages. List every
   namespace hit with file and line. Repo level CI in autoregistry does
   not come along; plan for it. Check that `GOOS=windows go build` works;
   if not, you will pass `--exclude-platform` with a reason in step 5.
2. `git subtree split --prefix=lib/providers/<provider>` to keep history,
   into a fresh working clone. Never push from the monorepo checkout.
3. Commit: restructure to the archetype layout (move `go.mod`/`go.sum` from
   `provider/` to root, delete `version.txt` and the monorepo `Makefile`;
   the version comes from the git tag and the ldflags path is
   `github.com/pulumi-labs/pulumi-<provider>/provider/pkg/version`).
4. Commit: namespace rewrite, nothing else in it. Includes the provider
   metadata set in code (repository, publisher, plugin download URL, Go import
   path, language names), using the values from `names.py`.
5. Commit: run `python3 scripts/render.py --repo-dir <repo> --provider <p>
   --display-name "<Name>"` (add `--exclude-platform goos/goarch --reason "..."`
   for each platform that cannot build), then README, `CLAUDE.md`, `docs/`.
6. Mark experimental: README banner and every package description (provider
   description, schema `description`, `language.nodejs.packageDescription`).
   Version stays `0.x.y`. Then `make schema` and `make sdks VERSION=<0.x.y>`
   and commit the regenerated schema and `sdk/`.
7. Gate. All three must pass before the first push:
   - `python3 scripts/check.py --repo <repo>` (hard gate, see below).
   - Secret scan of the split history. The monorepo is private and the
     target is public, so every historic commit becomes public. Use
     `gitleaks git --redact <repo>` if gitleaks is installed. If it is not,
     say "history was not scanned for secrets" and stop for a human; do not
     push. Any finding also stops for a human.
   - Onboarding issues filed (below).
8. Push once the workflows are in place and the gate is clean. Do not tag a
   release until every onboarding issue is closed.

`check.py` is the gate: run it before every push and before every release.
It reports all failures and exits 1 on any: required files present; archetype
files byte-identical to what render.py produces (no hand edits); no
`provider/go.mod`, `version.txt` or nested `go.mod`; root module name; schema
metadata and SDK names match the naming table; no `pulumi/autoregistry`,
`github.com/pulumi/pulumi-<p>`, `@pulumi/<p>`, `pulumi_<p>`, `Pulumi.<Pascal>`
or `com.pulumi.<p>` in any tracked file; experimental marking; every `v*` tag
is `v0.x.y`; copyright headers. Fix the cause, never the check. If a check
seems wrong or an archetype file needs a change, stop and report it.

`references/carving.md` has the monorepo-to-public mapping, with
`lib/providers/modal/` as the worked example.

## Onboarding issues per provider

Go, npm, PyPI and NuGet publish without a stored publishing token: Go by
pushing a tag with `GITHUB_TOKEN` through `pulumi/publish-go-sdk-action`, npm
and PyPI through OIDC trusted publishing, NuGet by exchanging the OIDC token
with `NuGet/login`. ESC (reached through OIDC) supplies the NuGet user name
`NUGET_USER` and the four Maven Central signing and OSSRH secrets. The script parses the
rendered `release.yml` and refuses to continue if the jobs or environments
changed, so it never works from memory:

```
python3 scripts/onboarding.py --provider <provider> --out-dir <dir>
```

It prints one issue draft per ask (ESC environment, npm, PyPI, NuGet,
Maven), each with exact names, what to do, what it unblocks, and the
verbatim note line, plus the duplicate check to run first:
`gh issue list -R pulumi/platform-providers-team --state all --search "<title> in:title"`.
Go needs no issue. See `references/onboarding.md`.

## Each later release

Run `python3 scripts/check.py --repo <repo>` before publishing. Any
`pulumi/autoregistry` or `pulumi/pulumi-<provider>` reference is a release
blocker. If check.py says the archetype changed (the SHA recorded in
`.pulumi-labs-export.json` differs from the vendored one), re-run
`render.py --repo-dir <repo>`, review the diff, and commit it on its own,
then check again. Avoid breaking changes; if one is unavoidable, bump the
minor and say so in the release notes. Tags are `v0.x.y` while experimental.

## Working with other agents

The Autoregistry Architect has repo build experience (Go build gate, PR
checklist skills) and is the right agent for audits inside
autoregistry. The Reviewer handles PR review. When you relay their
findings, say they are secondhand, give file paths, and do not present
anything you have not read as fact.

## Reporting

- Lead with outcome: done, blocked (on what, who must act), next step.
- Name only things you have opened. If you did not open a link or file,
  say so or open it first.
- Do not invent schedules or recurring work.
- Retry a failed access check once per session, not on every message.
- Identical messages arriving back to back are a UI duplicate; answer
  once.
