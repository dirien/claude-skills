# Repository setup

> `scripts/repo_setup.sh create`, `protect` and `verify` are **for humans**: an
> org admin runs them, once per provider, and they refuse to run without an
> interactive terminal. No agent runs them. The Autoregistry Publisher may set up
> the one repository of the provider it is carving, through
> `scripts/repo_setup.sh plan` only, and only when its pulumi-labs identity is an
> admin ("The agent path" below; "Repository setup" in SKILL.md). Otherwise it
> files the ask and stops. No agent ever deletes, renames, transfers or archives a
> repository, changes its visibility, tags, or uses admin bypass.

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

With the admin connection (below) the Publisher does steps 2, 4 and 7 itself with
`plan create`, `plan verify`, `plan protect` and `plan verify --imported`, after the
card is approved; step 3 stays with a human.

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

## The agent path (`plan`)

The Workprentice runtime gives credentials only to a `gh` or `git` the agent runs
directly (`runtime.md`), so a script cannot make the API calls for it. `plan`
therefore calls nothing. It writes the request bodies (the same ones `create` and
`protect` send) to `$HOME/.local/pulumi-labs-export/repo-setup/pulumi-<slug>/` and
prints one read-only `gh api` GET per setting. Each GET carries a `--jq` program
that compares the live value with the body and prints `ok`, or `fix <what>` and the
exact write: `RUN <command>`, or `TRY <command>` for the three settings `create`
treats as best effort (fork-PR approval, private vulnerability reporting, Dependabot
security updates off). Ruleset contents need a second read by id, which the check
prints as `CHECK <command>`. A `# on HTTP <code>:` comment says what an error status
means (404 on the repo: create it; 409 on commits: empty). Because every pass reads
the live repo, a pass after a partial failure prints only what is left, and a
configured repo prints only `ok`. `plan verify` prints PASS, FAIL and NOTE instead.

`plan` refuses more than one slug, any org but `pulumi-labs`, and prints `STOP`
instead of a write when the repo is archived or not public, when its security
settings are not visible (not an admin), when it already has commits before the
import, when an environment has deployment policies the script did not make, or
when two rulesets share a name. It never prints a delete of a repository, a
visibility change, a tag or a bypass actor beyond the bodies.

First pass of `plan create modal` against the empty `pulumi-labs/pulumi-modal`
(2026-10-18; read-only, nothing was run): it printed `fix` lines and RUN for the
general settings (merge methods, wiki, projects, update branch, delete branch on
merge, squash message, secret scanning, push protection), the workflow token (write
to read, no PR approvals) and the three environments, TRY for fork-PR approval and
private vulnerability reporting, and RUN for the tag ruleset. Actions, Dependabot
alerts and security updates printed `ok`.

### The admin identity

Plan: a fine-grained personal access token owned by the pulumi-labs org, access to
all its repositories, about 90 days, with repository permissions Administration,
Contents, Workflows, Pull requests, Issues and Actions (read and write) and Metadata
(read), set as a per-agent override of the Publisher's pulumi-labs GitHub
connection. Everything in the `pulumi` org stays on the workprentice App. Calls made
with it act as the token's owner (Engin), so mirror PRs, merges and comments by the
Publisher show as Engin, and the rulesets' admin bypass applies to it.

Fine-grained permission each call of the plan needs (GitHub REST docs, 2026-10-18),
and whether that token has it:

| Call | Needs | Token |
|---|---|---|
| `GET /repos/{o}/{r}` | Metadata read; the `security_and_analysis` block is shown only to a repository admin (docs name the role, not a token permission) | yes |
| `GET .../commits`, `GET .../contents/{path}` | Contents read | yes |
| `GET .../actions/permissions`, `.../workflow`, `.../fork-pr-contributor-approval` | Administration read | yes |
| `GET .../vulnerability-alerts`, `GET .../automated-security-fixes` | Administration read | yes |
| `GET .../private-vulnerability-reporting`, `GET .../rulesets`, `GET .../rulesets/{id}` | Metadata read | yes |
| `GET .../environments`, `GET .../environments/{env}/deployment-branch-policies` | Actions read | yes |
| `GET /orgs/{org}/installations` (verify) | organization Administration read | **no**: verify prints a NOTE |
| `POST /orgs/{org}/repos` | Administration write, or Repository creation write | yes |
| `PATCH /repos/{o}/{r}` | Administration write; `security_and_analysis` also needs the admin role | yes |
| `PUT .../actions/permissions`, `.../workflow`, `.../fork-pr-contributor-approval` | Administration write | yes |
| `PUT .../vulnerability-alerts`, `DELETE .../automated-security-fixes`, `PUT .../private-vulnerability-reporting` | Administration write | yes |
| `PUT .../environments/{env}`, `POST .../deployment-branch-policies` | Administration write | yes |
| `POST .../rulesets`, `PUT .../rulesets/{id}` | Administration write | yes |

The same token can also delete any pulumi-labs repository, change its name,
visibility or archive state (Administration write), create tags and releases
(Contents write), and re-run or dispatch workflows (Actions write). It cannot
transfer a repository (the REST endpoint accepts only GitHub App user tokens) or
approve a deployment (Deployments write is not granted). The skill's limits cover
everything it can do and the plan does not.

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
