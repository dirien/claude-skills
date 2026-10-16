---
name: pulumi-labs-export
description: Mirror a provider from the private pulumi/autoregistry monorepo (lib/providers/<provider>/, stays the source of truth) into a generated public pulumi-labs/pulumi-<provider> repo, and re-sync it on every new version. Use when asked to export, carve out, publish, release or onboard an autoregistry provider to pulumi-labs, to run the pre-push/pre-release gate on such a repo, or to file the npm/PyPI/NuGet/Maven/ESC onboarding issues for one. Covers fixed package naming, the access check, the deterministic export.py and drift check, the sync flow, vendored archetype scaffolding rendered by script (never hand-copied), the verification gate (builds all five SDKs, default-provider and credential checks, nothing is pushed without a passing report), secret scan (`secret_scan.py`, full history plus tree), the identity-switch helper (`identity.py`), and the experimental 0.x.y rules.
---

# Autoregistry Publisher: pulumi-labs export

## Skill files (read first)

The platform may deliver only this SKILL.md. If `scripts/` is not next to
it, fetch the skill folder from `pulumi/autoregistry` (run each line as its
own command; never use `--filter`, partial clones fail on the lazy fetch):

```
git clone --depth 1 --no-checkout https://github.com/pulumi/autoregistry $HOME/.local/pulumi-labs-export/src
git -C $HOME/.local/pulumi-labs-export/src sparse-checkout set skills/pulumi-labs-export
git -C $HOME/.local/pulumi-labs-export/src checkout
```

If the clone already exists, refresh it with
`git -C $HOME/.local/pulumi-labs-export/src pull --ff-only`. The skill is
then `$HOME/.local/pulumi-labs-export/src/skills/pulumi-labs-export/`: run
every `scripts/...` path from there. If the fetched SKILL.md differs from
the one you were given, follow the fetched one and report that the uploaded
copy is stale. See `references/runtime.md`.

You mirror providers from the private `pulumi/autoregistry` repo into
public `pulumi-labs/pulumi-<provider>` repos and keep them in sync
without breaking customers.

All scripts are Python 3 standard library plus `gh` and `git` (`archetype_drift.py`, `export.py` and `identity.py` never call `gh` or the network; `export.py --generate` runs `make`, which downloads Go modules). Run them as
`python3 scripts/<x>.py` from this skill's folder; every one has `--help`
and exits non-zero with an exact message. Details live in `references/`. `scripts/repo_setup.sh` is the one exception to
"all scripts are Python": a bash script for humans only (never run it).

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
`eval "$(python3 scripts/bootstrap.py)"`). It always exports
`GRADLE_USER_HOME=<prefix>/gradle-home` (Gradle fails without a usable
user home in the agent runtime). It installs Go, the Pulumi CLI,
`jq`, `gitleaks`, `make`, Node.js 22, the .NET 8 SDK, Temurin JDK 17 and
Gradle 9 under `$HOME/.local/pulumi-labs-export/` with no root and no
compiler, and skips what is already current. The eval line also exports
`JAVA_HOME`, `DOTNET_ROOT` and the dotnet opt-outs. `--profile minimal`
skips the last four tools (the verify gate then fails on them). If it exits
non-zero, stop and report the message. `--check` reports without
downloading. See `references/toolchain.md`.

## Access check (start of every session, before any work)

Probe, do not assume. A connection listed as "connected" proves nothing.

```
python3 scripts/access_check.py --provider <provider>
```

If it exits 2, the script's process has no GitHub credentials (they reach
only a `gh` or `git` invoked directly). Run `python3 scripts/access_check.py
--print-commands --provider <provider>` and execute each printed line
directly as its own command, not via a shell or script. PASS rule: every
command exits 0, and the push probe output contains no "denied" or "403".
The script probes, and prints each endpoint and status (never tokens):

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
Workprentice app push. **`scripts/repo_setup.sh` is for humans only. You
never run it, and you never create, configure, protect or delete a
repository.** It exists so Engin can create the empty public repo with the
right settings (`references/repo-setup.md`). The repo must be empty (no
README); a repo with commits you did not push is a stop-and-escalate.

## The mirror model

- The monorepo provider `lib/providers/<p>/` stays the **source of truth**
  and keeps being maintained there by other agents. It is never deleted.
- `pulumi-labs/pulumi-<p>` is a **generated mirror**. On each new provider
  version you OVERWRITE it with a fresh `scripts/export.py` run. Nobody edits
  it by hand; `check.py --against-source` reports any hand edit as drift.
- **Fixes always land in the monorepo first, then sync.** That includes
  engineering feedback on the mirror. Never patch the mirror.
- The provider is listed in the monorepo root `carved-providers.json`
  (`mode: "mirror"`; fields `name`, `repo`, `goModule`, `mode`,
  `registryName`, `listed`, `exportedAt`, `lastSyncedCommit`,
  `lastSyncedVersion`). Mirrored providers are never released from the
  monorepo (`plan-release.sh` refuses them). Only people tag the mirror; you
  never tag. Versions stay `0.x.y`.
- **Versions and release notes** (`references/lifecycle.md`). Every mirrored
  provider has `version.txt` (`0.x.y`); only your release-cut PR bumps it. The
  Architect writes `[Unreleased]` CHANGELOG entries; you roll them into the
  new version's section. Weekly cut rule, `BREAKING:` escalation and the
  candidate sync (re-sync an untagged version) are in `lifecycle.md`.

The mirror's layout is the archetype's (copy of `pulumi-labs/pulumi-nvidia-aicr`,
vendored in `archetype/`, written by `render.py` which `export.py` calls; see
`references/archetype.md`). Root `go.mod`/`go.sum`, `Makefile`, workflows,
`README.md`, `CLAUDE.md`, `docs/`, `examples/`, `templates/`, `provider/cmd`,
`provider/pkg`, generated `sdk/` and `schema.json`. Never hand-copy or
hand-edit an archetype file; `check.py` fails if you do.

## Procedure

Per provider, in order. Details: `references/export.md` (a to d) and
`references/sync.md` (e).

**a. Readiness.** On fresh `main`:
`python3 scripts/verify.py --monorepo <autoregistry checkout> --provider <p>`.
Stop if it fails; fix it in the monorepo.

**b. Identity switch in the monorepo.** One PR to `pulumi/autoregistry`, with
Reviewer clearance on the head SHA and green checks, before any export. It
carries: the `carved-providers.json` entry (`listed: false`, nullable sync
fields `null`); the builder metadata (`WithPublisher("Pulumi Labs")`,
`WithRepository`, `WithLanguageMap`, then `WithGoImportPath`,
`WithPluginDownloadURL`, and "experimental" in `WithDescription` and the
nodejs `packageDescription`); the same identity in the README, examples,
templates and tests (Go imports and `go.mod` requires, and the npm, Python,
.NET and Java names); copyright headers; and the two `docs/` Registry pages.
`export.py` refuses until all of it is in. Names come from `names.py`, never
typed by hand. Do it in this order:

1. `python3 scripts/identity.py --check --monorepo <checkout> --provider <p>`
   lists every remaining old-identity hit as `file:line [category]`.
2. `python3 scripts/identity.py --apply ...` performs only the mechanical,
   string-for-string rewrites (package names and import paths in examples,
   templates, README and tests). It is idempotent and prints what is left.
3. The manual items, by hand: the builder chain (`WithLanguageMap` BEFORE
   `WithGoImportPath`, and it must carry the framework defaults including
   `respectSchemaVersion: true`, or the SDKs are not version-stamped and the
   .NET build fails; the exact map is in `references/export.md`), the
   copyright headers, the two docs pages, and the `carved-providers.json`
   entry.
4. `identity.py --check` until it prints no hits.

**c. Initial export.** Full non-shallow unfiltered clone of the monorepo,
`git subtree split --prefix=lib/providers/<p>` to keep history, pull it into
the empty mirror clone, run `export.py --out . --generate`, commit as
`Export pulumi/autoregistry@<sha>`. Then `scripts/secret_scan.py` (gitleaks over the full history and the
tree), `verify.py`, `verify.py --assert-pushable`, push `main` (the one allowed
direct push), wait for the `test` workflow to go green, and open a monorepo
PR setting `exportedAt`, `lastSyncedCommit`, `lastSyncedVersion`.

**d. Onboarding issues.** `scripts/onboarding.py` (below). Do not ask for a
tag until every onboarding issue is closed.

**e. Sync on every new version.** Run the drift check on the mirror's `main`
first and stop on any `DRIFT` (`references/sync.md`, step 0). Trigger: `lib/providers/<p>/version.txt` on
monorepo `main` is greater than the entry's `lastSyncedVersion`. Branch
`sync/v<version>` in the mirror, `export.py --out . --generate`, commit
`Sync pulumi/autoregistry@<sha> (v<version>)`, `verify.py`,
`--assert-pushable`, push the branch, open a PR, get Reviewer clearance and
green CI, squash-merge. Then a monorepo PR updates the sync fields. Then tell
Engin "ready to tag v<version>". Never tag.

**f. Release cut.** Between syncs, decide whether a new version is due and open
the release-cut PR (only `version.txt` and the CHANGELOG rollover) per
`references/lifecycle.md`. PR branches, titles and the clearance protocol are
there too, as are the mirror-side events (Dependabot or outside PRs on a mirror,
a hand edit) and breaking, removed or renamed providers.

## Before every push (initial import and every later PR branch)

Nothing goes to the external repo unless `verify.py` passed on the exact commit:

```
python3 scripts/secret_scan.py --repo <repo>               # gitleaks: full history + tree, writes <repo>-<sha>-secrets.json
python3 scripts/verify.py --repo <repo>                    # whole gate, writes the report, exit 0 only if all pass
python3 scripts/verify.py --assert-pushable --repo <repo>  # run immediately before git push; needs BOTH reports for HEAD
```

`secret_scan.py` runs gitleaks with the default rules over every commit and
the tracked tree of HEAD. A finding stops the push. A `.gitleaks.toml` at the
repo root (copied there by `export.py` from `lib/providers/<p>/.gitleaks.toml`,
`[extend] useDefault = true` plus a narrow `[allowlist]`) is the only way to
accept one; entries are added only through a reviewed monorepo PR (see
`references/verify.md`). If gitleaks is missing the script exits 2: say
"history was not scanned for secrets" and stop.

Any new commit (even a one-line fix) needs a fresh `secret_scan.py` and `verify.py` run. The gate
proves: SDK generation works for all five languages with no codegen or
`go mod tidy` errors; the Go SDK, every Go example and template compile against
the local SDK; the Go module paths are the pulumi-labs ones; every SDK builds
(a missing toolchain is a FAIL, never a skip); and the provider follows the
default-provider pattern (works with no `Provider` resource, credentials from
environment variables or per-resource attributes). If a step fails, fix the
cause and re-run; never edit the gate or push around it. `references/verify.md`
documents every check and why.

`check.py` is the structural part of the gate and runs inside it.
It reports all failures and exits 1 on any: required files present; archetype
files byte-identical to what render.py produces (no hand edits); no
`provider/go.mod`, `version.txt` or nested `go.mod`; root module name; schema
metadata and SDK names match the naming table; every language in the schema
has `respectSchemaVersion` and the committed SDKs carry `0.0.1-dev`
(`sdk/dotnet/version.txt` exists); no `pulumi/autoregistry`,
`github.com/pulumi/pulumi-<p>`, `@pulumi/<p>`, `pulumi_<p>`, `Pulumi.<Pascal>`
or `com.pulumi.<p>` in any tracked file; experimental marking; every `v*` tag
is `v0.x.y`; copyright headers. Fix the cause, never the check. If a check
seems wrong or an archetype file needs a change, stop and report it.

`references/export.md` has the monorepo-to-mirror mapping and the checklist
for step b; `check.py --repo <mirror> --against-source <monorepo>` adds drift
detection (see `references/sync.md`).

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

Follow step e. If check.py says the archetype changed (the SHA recorded in
`.pulumi-labs-export.json` differs from the vendored one), the next
`export.py` run already re-renders it; review the diff in the sync PR. Any
`pulumi/autoregistry` or `pulumi/pulumi-<provider>` reference is a release
blocker. Avoid breaking changes; if one is unavoidable, bump the minor and say
so in the release notes. Tags are `v0.x.y` while experimental, made by people.

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
