# Onboarding asks (Linear PVD)

`python3 scripts/onboarding.py --provider <p> [--repo <dir>] [--out-dir <dir>] [--json]`

The asks are derived by parsing `release.yml` (the rendered one, or the one in
`--repo`). The script fails, naming the problem, if the expected jobs,
environments or ESC values are not there; that means the archetype changed, so
stop and tell a human instead of filing from memory.

All asks are Linear issues in team PVD (https://linear.app/pulumi/team/PVD/),
created with the Linear tools, one ask per issue, with exact names and the
exact command if there is one. Run the printed duplicate checks first: search
Linear PVD for the exact title, and for older asks run

```
gh issue list -R pulumi/platform-providers-team --state all --search "<title> in:title"
```

If an issue exists in either place, add evidence there (comment) instead of
filing a duplicate. Nothing new is filed on `pulumi/platform-providers-team`;
issues already there stay until engineering closes them, and you answer on
them. Every Linear issue and comment ends with the footer line (verbatim, from
the draft):

```
🤖 Autoregistry Publisher · Workprentice
```

A Linear issue counts as closed in Done, Canceled or Duplicate.

## What the archetype release.yml does

| Registry     | Job               | Mechanism                                                                                      | Needs from the team                                                                 |
|--------------|-------------------|------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| Go           | `publish-go-sdk`  | `pulumi/publish-go-sdk-action` pushes the `sdk/go/<go_pkg>/vX.Y.Z` tag with `GITHUB_TOKEN`     | nothing (no issue)                                                                  |
| npm          | `publish-nodejs`  | trusted publishing over OIDC, environment `publish-npm`                                        | package `@pulumi-labs/<p>` with a trusted publisher for `release.yml`               |
| PyPI         | `publish-python`  | `pypa/gh-action-pypi-publish`, OIDC, environment `publish-pypi`                                | pending trusted publisher for `pulumi-labs-<p>` bound to `release.yml` + environment |
| NuGet        | `publish-dotnet`  | `NuGet/login` exchanges the OIDC token for a short-lived key; environment `publish-nuget`; user `NUGET_USER` read from ESC | trusted publishing policy for `Pulumi.Labs.<Pascal>`; `NUGET_USER` in ESC |
| Maven Central| `publish-java`    | `gradle publishToSonatype ...` with four secrets read from ESC                                 | `MAVEN_CENTRAL_SIGNING_KEY`, `MAVEN_CENTRAL_SIGNING_PASSWORD`, `MAVEN_CENTRAL_OSSRH_USERNAME`, `MAVEN_CENTRAL_OSSRH_PASSWORD` in ESC; `com.pulumi.labs:<p>` allowed under the namespace |
| ESC          | `publish-dotnet`, `publish-java` | `pulumi/esc-action` with OIDC org `pulumi`, environment `github-secrets/pulumi-labs-pulumi-<p>` (`github-secrets/${{ github.repository_owner }}-${{ github.event.repository.name }}`) | the environment exists and the repo may read it |

## Correction to the engineering draft

The draft says "Go, npm and PyPI publish via OIDC; NuGet and Maven use secrets
from ESC". The workflow does something more specific:

- NuGet is also trusted publishing. There is no NuGet API key secret. The only
  value read from ESC is `NUGET_USER`, the nuget.org policy-owner profile slug.
- Only Maven Central uses stored secrets (signing key and password, OSSRH
  username and password), read from ESC.
- Go needs no onboarding at all.

## Order and unblocking

ESC first (NuGet and Maven jobs need it), then npm, PyPI, NuGet, Maven in any
order. Do not tag a release until every Linear issue is closed. If the first
Linear issue call fails, Linear access is not available to this session:
report it and stop (see the access check in SKILL.md).
