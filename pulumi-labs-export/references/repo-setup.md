# Repository setup (humans only)

> **HUMANS ONLY.** An org admin runs this, once per provider, before the card is
> approved. The Autoregistry Publisher and every other agent must NEVER create,
> configure, protect, make public or delete a repository, and must never run
> `scripts/repo_setup.sh`. The script refuses to run without an interactive
> terminal. If a repo is missing or misconfigured, the agent files the ask and
> stops (see "Access check" in SKILL.md).

`scripts/repo_setup.sh` does what is below with `gh`; this page says why each
setting exists and what to do by hand when the script cannot.

## Order of operations

1. Decide the Registry name (human, permanent) and check the monorepo provider
   passes readiness (`Provider Readiness` workflow, dispatched for the provider).
2. `scripts/repo_setup.sh create <slug>...` (add `--dry-run` first).
3. Make sure the `workprentice` GitHub App covers the new repo (below).
4. `scripts/repo_setup.sh verify <slug>...` (all PASS means ready).
5. Tick the repo items on the card and move it to Approved.
6. The Publisher exports, pushes `main` once, and the `test` run goes green.
7. `scripts/repo_setup.sh protect <slug>...` (ruleset on the default branch), then
   `scripts/repo_setup.sh verify <slug>... --imported`.

Protection comes after the import because the initial import is the one direct
push to `main`; a pull-request-only rule would block it.

## What the script sets

| Setting | Value | Why |
|---|---|---|
| Name | `pulumi-labs/pulumi-<slug>` | Fixed naming (`names.py`). |
| Visibility | public | Engineering publishes from it; the Go module and plugin download URL must be fetchable. The monorepo history is secret-scanned before the import. |
| Contents | empty: no README, license, .gitignore | The import is `git subtree split` pulled into an empty clone; any first commit makes it a non-fast-forward. `verify` fails if the repo has commits before the import. |
| Default branch | `main` | The archetype workflows and the ruleset target `main`. Create the repo with no commits; the first push of `main` sets it. |
| Issues | on | Engineering and users report here; the Publisher routes fixes to the monorepo. |
| Wiki, Projects, Discussions | off | Nobody maintains them. |
| Merge methods | squash only, delete branch on merge, PR title and body as the commit message | One commit per sync; history is linear. |
| Actions | enabled; default token read-only; Actions cannot approve PRs; fork-PR workflows need approval for all outside contributors | `release.yml` declares its own `contents: write`; outside PRs must not run workflows unreviewed. |
| Secret scanning and push protection | on | Defense in depth next to `secret_scan.py`. |
| Dependabot alerts | on | Visibility only. |
| Dependabot security updates | off | The monorepo is the source of truth and Dependabot runs there; a PR here would be drift. Version updates are not configured (the archetype ships no `dependabot.yml`; do not add one). |
| Private vulnerability reporting | on | External reporters have a private channel; a human handles it. |
| Environments | `publish-npm`, `publish-pypi`, `publish-nuget`, deployable only from tags `v*.*.*` | Names bind the registries' trusted-publisher entries. GitHub would auto-create them on first use, without the tag restriction. Do not add required reviewers unless Engin asks; they would block the release run. (Maven and Go publish without an environment.) |
| Tag ruleset `Release tags: people only` | creating, moving or deleting `v*` tags needs the repository `maintain` or `admin` role | Enforces "agents never tag". It does not affect the `sdk/go/<pkg>/vX.Y.Z` tag the release workflow pushes. Whoever tags must hold maintain or admin. |
| Default-branch ruleset `Default branch: PR only` (the `protect` step) | PR required, 0 approvals, squash only, required check `test`, no force-push, no deletion; admins may bypass only through a PR | The app merges sync PRs itself. Reviewer clearance is a PR comment, so GitHub cannot require it; 0 approvals is deliberate. The required check name is the archetype's job `test`. |

## The GitHub App (`workprentice`)

The app needs, on the org installation: Contents, Pull requests, Issues, Checks
and Workflows (read and write), and no Administration.

- Org settings, GitHub Apps, workprentice, Configure: if it says **All
  repositories**, new repos are covered at once. If **Only select repositories**,
  add each new repo by hand. This is the usual reason an access probe later
  returns 404 on a repo that exists.
- **Workflows permission.** The import and every sync push `.github/workflows/*`.
  A push containing workflow files from an app without the Workflows permission
  is rejected ("refusing to allow a GitHub App to create or update workflow ...
  without `workflows` permission"). `access_check.py` cannot see this (its probe
  commit is empty); the first real import is the test. The pulumi installation
  has it (the app has changed workflows in the monorepo); confirm the
  pulumi-labs installation does, because installations are per org.
- The app cannot rerun workflows. If the `test` run on a pushed commit is
  missing or red for a flaky reason, the Publisher escalates and a human re-runs
  it from the Actions tab (the archetype `test` workflow has no
  `workflow_dispatch`).
- Pushes made with the app's installation token do trigger `push` and
  `pull_request` workflows; only events made with the built-in `GITHUB_TOKEN`
  are suppressed. Evidence: 71 `pull_request` runs in `pulumi/autoregistry` have
  `workprentice[bot]` as the actor. The Publisher must still confirm the `test`
  run exists for the import commit and escalate if none starts within 10
  minutes.

## Verifying by hand

```
gh api repos/pulumi-labs/pulumi-<slug> --jq '[.visibility,.has_issues,.default_branch,.allow_squash_merge,.allow_merge_commit]'
gh api repos/pulumi-labs/pulumi-<slug>/commits    # before the import: HTTP 409 "Git Repository is empty"
gh api repos/pulumi-labs/pulumi-<slug>/rulesets --jq '.[].name'
gh api repos/pulumi-labs/pulumi-<slug>/environments --jq '.environments[].name'
```

## After publication

Nothing here changes. Engineering registers trusted publishers (see
`onboarding.md`), tags `v0.x.y` from a maintainer account, and `release.yml`
publishes. Making the Registry listing (`listed: true`) is a reviewed monorepo PR
after the Publisher verifies the packages.
