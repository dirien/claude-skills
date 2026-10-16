# The bridged archetype (Terraform-bridged providers)

Status: ADOPTED on 2026-10-16 by Engin Diri (repo lead), with the prototype's choices as the decisions (see
"Decisions"). Alberto Pose (the engineering standard owner) is to be informed. The prototype ran end to end for one
provider (clerk, see "End-to-end result"). Nothing here changes how native providers are exported: the bridged archetype is
selected by detection and every native code path keeps its bytes.

## Why a second archetype

The skill was built around one archetype, `pulumi-labs/pulumi-nvidia-aicr`: a native
`pulumi-go-provider` repo with a root `go.mod`, `make schema` (the schema comes out of the built
binary), `make sdks` and vendored `.tmpl` files. A Terraform-bridged provider differs in every one
of those points, and 36 of the 45 providers in `lib/providers/` are bridged (28 on the
terraform-plugin-framework path, "pf", and 8 on terraform-plugin-sdk, "sdkv2"). Measured on
`origin/main` with the prototype's own detection:

| Property of the 36 bridged providers                          | Count | Consequence for the mirror |
|---------------------------------------------------------------|-------|----------------------------|
| Go module is `provider/go.mod`, `github.com/pulumi/pulumi-<p>/provider` | 36 | no root module; the module path is a vanity name, rewritten at export |
| Relative `replace` to the in-repo shim module (`./shim`)      | 36    | `export.py` used to refuse; now kept when it resolves inside the export |
| cmd mains in the plain shape (one `Main` call, no extra code) | 36    | the archetype can own them |
| `PackageSchema = []byte("{}")` placeholder in the resource main | 28 (all pf) | the monorepo binary serves no real schema; the mirror must embed one |
| `tfbridge.Main(..., nil)` (schema generated at runtime)       | 8 (all sdkv2) | same fix |
| `version.txt` present                                         | 3 (clerk, coderd, pinecone) | the mirror trigger needs one; 33 need it added |
| `ProviderInfo.GitHubOrg` set to the upstream org              | 28    | must never be rewritten to a pulumi value |
| `docs/` Registry pages                                        | 0     | 2 pages per provider to write |
| Identity items still on the `pulumi` values (`identity.py --check`, ProviderInfo only) | 9 to 14 per provider | the manual part of the identity-switch PR |

## How the monorepo builds a bridged provider today

Facts read from `lib/providers/clerk`, `pinecone`, `neon`, `AGENTS.md`, `.github/scripts/package-ci.sh`
and `.github/scripts/plan-release.sh`.

- Layout: `provider/{go.mod,resources.go,resources_test.go,cmd/pulumi-tfgen-<p>,cmd/pulumi-resource-<p>,pkg/version,shim/}`.
  `resources.go` owns the whole `tfbridge.ProviderInfo`. `shim/` is a separate module named as a
  sub-path of the upstream Terraform provider module and reached through
  `replace <upstream>/pulumi-bridge-shim => ./shim`.
- Local Makefile (`generate_schema`, `generate_sdks`, `build_*`): `go run ./cmd/pulumi-tfgen-<p>` writes a
  gitignored `schema.json`; `pulumi package gen-sdk` writes a gitignored `sdk/`. Nothing generated is committed.
- Release (`release.yml` + `package-ci.sh`): `gen-sdks` runs `go run ./cmd/pulumi-tfgen-<p> schema --out <tmp>`,
  normalises the schema with `jq` (publisher, repository, language names, plugin URL), runs
  `pulumi package gen-sdk --version`, then patches `${VERSION}` into `package.json` and `setup.py` with `sed`
  (the providers do not set `RespectSchemaVersion`). The binary is built with
  `-X <module>/pkg/version.Version=<v>` and ships with the placeholder schema. The Go SDK is "NOT publishable yet".
- Version source: the git tag `<pkg>-vX.Y.Z` per package (`plan-release.sh`). `version.txt` exists for 3 providers only and
  nothing in the monorepo reads it except the mirror flow.
- Upstream bumps: deliberate PRs (`upstream-drift.yml` reports the gap; dependabot freezes the Terraform family).

## What the official boilerplate does

`pulumi/pulumi-tf-provider-boilerplate` at `c62eff764d69c837605fcc05069e489aa51d88ec` (the SHA the templates pin; read
2026-10-16). `archetype_drift.py --archetype bridged` reports changes to the files the templates track.

| Area | Boilerplate | Prototype |
|------|-------------|-----------|
| Go module | `provider/go.mod`, `github.com/pulumi/pulumi-xyz/provider` | same, with the pulumi-labs path |
| Entry points | `provider/cmd/pulumi-tfgen-xyz`, `provider/cmd/pulumi-resource-xyz` (embeds `schema.json`) | same; rendered, flavor pf or sdkv2 |
| Committed generated files | `provider/cmd/pulumi-resource-xyz/{schema.json,bridge-metadata.json,Pulumi.yaml}` and `sdk/` | same |
| Schema step | `bin/pulumi-tfgen-xyz schema --out provider/cmd/pulumi-resource-xyz` | same, then `jq 'del(.version)'` |
| SDK step | `bin/pulumi-tfgen-xyz <lang> --out sdk/<lang>` (4 languages, Java off) | `pulumi package gen-sdk` from the schema (5 languages), as the native archetype and `package-ci.sh` do |
| Go SDK | one module `…/sdk` at `sdk/go.mod`, import base `…/sdk/go/xyz` | module at `sdk/go/<gopkg>` (`make go_sdk` runs `go mod init`), import base `…/sdk/go/<gopkg>`: the native layout, so `publish-go-sdk-action`, `check.py` and the Go smoke tests are unchanged |
| Tooling | mise + vfox, `pulumictl`, `schema-tools`, `upgrade-provider` | plain `make` and the skill's bootstrap toolchain |
| Version | `PROVIDER_VERSION` (`pulumictl`), `1.0.0-alpha.0+dev` default | `VERSION ?= 0.0.1-dev`, tag `vX.Y.Z` stamps release builds |
| CI | generated by `ci-mgmt` (`.ci-mgmt.yaml`, template `external-bridged-provider`): reusable workflows, Pulumi-internal GitHub App token, long-lived registry tokens through `pulumi-package-publisher`, ESC `imports/github-secrets` | vendored `test.yml` and `release.yml` taken from nvidia-aicr (OIDC npm, PyPI and NuGet, Maven through ESC, `publish-go-sdk-action`) with the bridged build steps |
| Example conversion | `PULUMI_CONVERT := 1` | `PULUMI_CONVERT ?= 0` (see decision 5) |

## Target layout and naming

Names are the native table (`naming.md`), derived by `names.py`; nothing is typed by hand. New derived fields for the
bridged archetype: `provider_module` (`github.com/pulumi-labs/pulumi-<p>/provider`), `tfgen_binary`, `tfgen_cmd_dir`,
`bridge_metadata_path`.

```
.github/workflows/{release,test}.yml     rendered
.gitignore .vscode/launch.json LICENSE mise.toml scripts/fixup_python_input_optional.py   the native archetype's files
CONTRIBUTING.md Makefile                 rendered
CLAUDE.md README.md CHANGELOG.md docs/ examples/ templates/ .gitleaks.toml   copied from the monorepo
provider/
  go.mod go.sum                          module github.com/pulumi-labs/pulumi-<p>/provider
  resources.go resources_test.go         copied (imports rewritten)
  pkg/version/version.go                 copied: the provider's own file keeps its dev default
  shim/{go.mod,go.sum,shim.go}           copied unchanged; reached through the relative replace
  cmd/pulumi-tfgen-<p>/main.go           rendered (flavor pf or sdkv2)
  cmd/pulumi-resource-<p>/main.go        rendered; embeds schema.json
  cmd/pulumi-resource-<p>/{schema.json,bridge-metadata.json,Pulumi.yaml}   generated by `make schema`, committed
sdk/{nodejs,python,go/<gopkg>,dotnet,java}   generated by `make sdks VERSION=0.0.1-dev`, committed
.pulumi-labs-export.json                 records provider, display name, archetype, flavor, source SHA and version
```

There is no root `go.mod` and no root `go.sum` (`check.py` forbids both), no `version.txt` in the mirror (the version
comes from the tag), and the Go modules are `provider`, `provider/shim` and `sdk/go/<gopkg>`.

### Files rendered from templates

| Target | Mode | Template | Provenance |
|--------|------|----------|------------|
| `Makefile` | render | `archetype-bridged/Makefile.tmpl` | boilerplate (names, tfgen schema step, tidy) plus nvidia-aicr (gen-sdk targets, `sdk_fixups`, `build_*_sdk`, `set_version`, `check_copyright`; tail byte-identical to native) |
| `.github/workflows/release.yml` | render | `archetype-bridged/.github/workflows/release.yml.tmpl` | nvidia-aicr; publishing jobs byte-identical; build jobs use `provider/go.sum`; an extra guard fails the release when the committed schema is stale |
| `.github/workflows/test.yml` | render | `archetype-bridged/.github/workflows/test.yml.tmpl` | nvidia-aicr; adds bridge-metadata freshness and a `make tidy` gate |
| `CONTRIBUTING.md` | render | `archetype-bridged/CONTRIBUTING.md.tmpl` | nvidia-aicr, bridged layout, "generated mirror" note |
| `provider/cmd/pulumi-resource-<p>/main.go` | render, per flavor | `archetype-bridged/provider/cmd/resource-main.{pf,sdkv2}.go.tmpl` | boilerplate, with `//go:embed schema.json` |
| `provider/cmd/pulumi-tfgen-<p>/main.go` | render, per flavor | `archetype-bridged/provider/cmd/tfgen-main.{pf,sdkv2}.go.tmpl` | boilerplate |
| `LICENSE`, `mise.toml`, `.gitignore`, `.vscode/launch.json`, `scripts/fixup_python_input_optional.py` | verbatim or render | the native templates | nvidia-aicr, shared on purpose |

`archetype-bridged/manifest.json` records both pinned SHAs and, per file, where it came from (`derived_from`) or the
sha256 of the nvidia-aicr original (`upstream_sha256`, files shared with the native archetype). Re-vendoring is a PR to the
skill, never a silent change; `check.py` reports `ARCHETYPE CHANGED` when a repo was rendered from another SHA.

### The Makefile contract

The target names are the native ones, so `verify.py` and the workflows reuse them: `provider`, `schema`, `test`, `lint`,
`check_copyright`, `sdks`, `<lang>_sdk`, `build_<lang>_sdk`, `sdk_fixups`, `set_version`, `clean`, plus bridged `tfgen` and
`tidy`. `provider` depends on `schema` (the binary embeds the schema, so it can never serve a stale one); `schema` builds
`bin/pulumi-tfgen-<p>` with the version ldflags (`Provider()` panics on an empty version), writes `schema.json`,
`bridge-metadata.json` and `Pulumi.yaml` next to the resource main and strips `.version`; `lint` vets `provider/` and
`provider/shim/`; `test` runs `go test ./...` in `provider/` without `-race` (the unit tests are pure Go and the race detector
would need cgo and a C compiler, which the gate would otherwise have to special-case).

## How export.py transforms a bridged provider

`export.py` detects the archetype: `provider/cmd/pulumi-tfgen-<p>` exists. The flavor comes from the tfgen main's import
(`pkg/pf/tfgen` is pf, `pkg/tfgen` is sdkv2) and is recorded in `.pulumi-labs-export.json`; an existing mirror cannot switch
archetype in place. Preconditions (each a clear error, all before anything in `--out` is touched):

1. `carved-providers.json` entry, clean checkout, `docs/_index.md` and `docs/installation-configuration.md`, `skills/SKILLS.md`: as native.
2. `version.txt` is required and must be `0.x.y`. The message says why (decision 4).
3. `provider/pkg/version/version.go` and `provider/resources.go` exist.
4. The cmd mains have the plain shape: exactly one `pf.Main` / `tfbridge.Main` / `pftfgen.Main` / `tfgen.Main` call whose first
   string is the provider name, plus at most `x := Provider()` and `meta := pf.ProviderMetadata{...}`. Anything else (extra statements,
   flags) is refused with the file and the reason: the archetype owns these two files and would silently drop the code.
5. Every relative `replace` in any exported `go.mod` resolves to a directory whose `go.mod` is part of the export. `./shim`
   passes; `../x` that is not exported, an absolute path or a path out of the repository is refused. (Native still refuses all relative replaces.)
6. The monorepo copy already has the pulumi-labs identity: the namespace patterns of `check.py` plus, for ProviderInfo,
   `Publisher: "Pulumi"`, `Repository: "https://github.com/pulumi/`, `PluginDownloadURL: "github://api.github.com/pulumi/`,
   `ImportBasePath: "github.com/pulumi/` and `GitHubOrg: "pulumi"` / `"pulumi-labs"`, each with file and line.

The transform:

| Step | Detail |
|------|--------|
| Copy | the whole `provider/` tree (go.mod and go.sum stay in `provider/`), `examples/`, `templates/`, `docs/`, `README.md`, `CHANGELOG.md`, `.gitleaks.toml`; not copied: `version.txt`, the monorepo Makefile and .gitignore, `skills/`, the monorepo's cmd mains, any committed `schema.json` |
| Module path | in every `.go` file and every `go.mod`, `github.com/pulumi/pulumi-<p>/provider` becomes `github.com/pulumi-labs/pulumi-<p>/provider` on identifier boundaries; the `module` line may already be the new path |
| Shim replace | untouched: `replace <upstream>/pulumi-bridge-shim => ./shim` keeps working because `provider/shim/` is exported next to `provider/go.mod`; `GitHubOrg` and the upstream module paths are never rewritten |
| cmd mains | rendered from the flavor template; they embed `schema.json` |
| `CLAUDE.md` | `skills/SKILLS.md` under the generated header, as native |
| Archetype files | `render.py` (above) |
| `.pulumi-labs-export.json` | adds `archetype` and `flavor` (native repos have neither key) |
| `--generate` | `make schema`, then `make sdks VERSION=0.0.1-dev`, then the SDK stamp check, as native |

Drift (`check.py --against-source`) re-runs the same transform at the recorded SHA. The paths `make` writes are excluded:
`schema.json`, `bridge-metadata.json`, the cmd `Pulumi.yaml` and `sdk/`.

## check.py, verify.py and identity.py for a bridged repo

`check.py` reads the archetype from `.pulumi-labs-export.json` and, for bridged:

- requires `provider/go.mod`, `provider/go.sum`, `provider/pkg/version/version.go`, both cmd mains, `schema.json` and
  `bridge-metadata.json`; forbids a root `go.mod`, `go.sum` and `version.txt`;
- module `github.com/pulumi-labs/pulumi-<p>/provider`; every relative replace in a `go.mod` under `provider/` must resolve to an
  existing `go.mod` in the repository;
- the archetype files (including the two cmd mains) are byte-identical to `render.py` output;
- `GitHubOrg` in non-test provider code is never `pulumi` or `pulumi-labs`;
- everything schema-, SDK-, namespace-, copyright-, tag- and README-related is shared with native, including the
  `respectSchemaVersion` check on the schema (the bridge writes it from `JavaScript/Python/CSharp/Golang.RespectSchemaVersion`).

`verify.py` runs the same gate with tfgen-based steps. New or changed steps, in report order:

| Step | What | Why |
|------|------|-----|
| `make_provider` | `make provider`: tfgen, schema, build | the binary embeds the regenerated schema |
| `make_test` | `make test` (no `-race`) | |
| `make_schema`, `schema_diff` | regenerate, `git diff --exit-code` on `schema.json` | a stale committed schema means stale SDKs and a stale binary |
| `metadata_diff` | `git diff --exit-code` on `bridge-metadata.json` and `Pulumi.yaml` | tfgen rewrites them; the bridge reads the metadata at runtime |
| `make_sdks`, `sdk_version` | all five SDKs with `VERSION=0.0.1-dev` and the stamp check | `RespectSchemaVersion` gaps show up here |
| `go_sdk_build`, `go_sdk_vet`, `go_consumer_smoke`, `go_examples` | as native | |
| `go_module_paths` | `go list -m` in `provider/` is the provider module, all packages under it | |
| `go_mod_tidy` | `make tidy` (provider and shim) leaves no diff | the monorepo's replace-tidy gate, carried to the mirror |
| `binary_schema` | `pulumi package get-schema bin/pulumi-resource-<p>` equals the committed schema modulo `version` | proves the embed, which is the whole reason the archetype owns the main |
| `build_{nodejs,python,dotnet,java}_sdk` | as native, a "skipping" is a failure | |
| `dp_env_defaults`, `dp_no_required_config` | on the generated schema (`defaultInfo.environment` is the compiled `Config[...].Default.EnvVars`) | |
| `dp_provider_info_defaults` | static read of `ProviderInfo.Config`: a credential-like entry with a `Default` must name upper-case `EnvVars` and carry no literal `Value` | catches a hard-coded credential default and an empty `EnvVars` with the file and line |
| `dp_no_env_tags`, `dp_no_explicit_provider`, `dp_no_credential_require` | as native (`shim/` is skipped) | clerk's six templates failed the last two |
| `check_py`, `worktree_clean_after` | as native | |

`--assert-pushable` requires the bridged step set for a bridged repo (it reads the archetype from the committed options file
and the report), so a native-shaped report never authorises a bridged push.

`verify.py --monorepo` (readiness, before the identity switch) detects a tfgen command and produces the schema from tfgen instead
of the binary; the rest of the readiness run is shared.

`identity.py` detects bridged providers the same way and, instead of the infer builder chain, reads the `tfbridge.ProviderInfo`
literal:

| ProviderInfo field | Wanted | Notes |
|--------------------|--------|-------|
| `Publisher` | `Pulumi Labs` | |
| `Repository` | `https://github.com/pulumi-labs/pulumi-<p>` | |
| `PluginDownloadURL` | `github://api.github.com/pulumi-labs/pulumi-<p>` | |
| `GitHubOrg` | unchanged | the UPSTREAM Terraform provider's organisation; `pulumi` and `pulumi-labs` are flagged because tfgen reads upstream docs through it |
| `JavaScript.PackageName` | `@pulumi-labs/<p>` | plus `RespectSchemaVersion: true` |
| `Python.PackageName` | `pulumi_labs_<p_>` | plus `RespectSchemaVersion: true` and `PyProject` enabled |
| `CSharp.RootNamespace` | `Pulumi.Labs` | plus `RespectSchemaVersion: true` |
| `Java.BasePackage` | `com.pulumi.labs` | |
| `Golang.ImportBasePath` | `github.com/pulumi-labs/pulumi-<p>/sdk/go/<gopkg>` | plus `RespectSchemaVersion: true` |
| `Description` | says "experimental" | becomes the schema description and the npm package description |

It also reports a missing or non-`0.x.y` `version.txt`, a cmd main that is not plain, missing copyright headers (the archetype-owned
mains are exempt), missing docs pages and the `carved-providers.json` entry. `--apply` rewrites only examples, templates, README and
`*_test.go`; it deliberately leaves `github.com/pulumi/pulumi-<p>/provider` alone (the export rewrites it everywhere, so rewriting some
occurrences would break the monorepo build).

## Versioning

Governance for every mirrored provider (AGENTS.md, "Version and CHANGELOG discipline", and `lifecycle.md`) applies unchanged; this is
what is specific to a bridged one.

- A mirrored bridged provider needs `lib/providers/<p>/version.txt` (`0.x.y`; `0.1.0` in its identity-switch PR, `export.md` 1a).
  The monorepo versions bridged providers by git tag (`<pkg>-vX.Y.Z`) and `plan-release.sh` refuses mirrored ones, so without
  `version.txt` the sync trigger has nothing to compare; `provider-metadata -check` requires it for every listed provider and the
  prototype's `export.py` refuses without it and says why. 33 of 36 providers need the file added.
- Only the Publisher's release-cut PR changes `version.txt` afterwards. `source_version` in `.pulumi-labs-export.json` is its value, and
  the mirror's tag `v<version>` is made by a person, never the agent.
- Every build stamps the version with `-X github.com/pulumi-labs/pulumi-<p>/provider/pkg/version.Version`. The provider's own
  `version.go` keeps its dev default (`0.0.1-alpha.0` and the like) because `Provider()` panics on an empty version and the unit tests
  run without ldflags; the gate also builds with `0.0.1-dev`, which is what the committed SDKs carry (`respectSchemaVersion`
  stamps `package.json`, `version.txt`, `pyproject.toml`, the Go plugin metadata and `build.gradle`; Python prints it as `0.0.1+dev`).
- Verified on the prototype: `make provider VERSION=0.1.0` leaves the committed schema and metadata untouched, and
  `make sdks VERSION=0.1.0` stamps all five SDKs, which is exactly what `release.yml` does for a tag.

## Day 2: an upstream Terraform provider bump

1. Engineering bumps the upstream module in the monorepo (shim `go.mod`, token mappings, tests) with the existing reviewed process
   (`upstream-drift.yml` is the signal; `provider-upgrade` skill) and adds an `[Unreleased]` CHANGELOG line, `BREAKING:` for an
   upstream major bump unless the PR shows no schema diff (AGENTS.md). Nobody but the Publisher touches `version.txt`.
2. The Publisher's release cut moves `version.txt` (new resource or breaking change: minor; otherwise patch). That is the trigger
   (`version.txt` greater than `lastSyncedVersion`) and the Publisher runs step e unchanged: drift check on the mirror's `main`, then
   `export.py --generate` on `sync/v<version>`. What changes in the mirror: `provider/go.mod`, `provider/go.sum`, `provider/shim/*`,
   `resources.go`, `schema.json`, `bridge-metadata.json`, `sdk/`, possibly `docs/` and `examples/`.
3. The gate does the review work that nobody does by hand: `go_mod_tidy` (tidy provider and shim), `schema_diff` and `metadata_diff`
   (regeneration is stable), `binary_schema` (binary equals schema), all five SDK builds, `go_examples` (examples still compile against the
   new SDK), `dp_*`. A schema that changed names or shapes shows in the sync PR as a `schema.json` diff; `schema-tools compare` against the
   previous tag is the recommended reviewer aid in the PR description (not wired in). The monorepo side is guarded the same way:
   `provider-readiness.yml` runs `verify.py --monorepo` for changed carved providers (dispatch it with `providers=bridged-all` for
   every bridged provider, `native-all` for the native ones, or `all`).
4. Fixes land in the monorepo first. The tidy and replace rules are the same in both places (`tools/replace-tidy`).
5. Risks not yet exercised: tfgen needs the upstream module in the module cache for docs (`GitHubOrg` must name the upstream org, set for 28 of 36
   providers; clerk, coderd, crusoe, pinecone, posthog, railway, render and zillizcloud lack it, so their schemas carry no upstream
   argument docs: "Unable to find the upstream provider's documentation"), and example conversion changes the schema (decision 5).

## The identity-switch PR for a bridged provider

Same PR, same order as native (`export.md` step b), with these differences. What the clerk simulation needed:

1. `identity.py --check`, then `--apply` (31 files of examples, templates and README), then by hand:
2. `resources.go`: Publisher, Repository, PluginDownloadURL, the four language names, `RespectSchemaVersion` on JavaScript, Python,
   Golang and CSharp, `PyProject`, `Description` starting with "Experimental".
3. Copyright headers on `resources.go`, `resources_test.go`, `shim/shim.go` and `pkg/version/version.go` (`make check_copyright` covers `provider/shim`).
4. Two `docs/` pages, the `carved-providers.json` entry (`listed: false`), `version.txt` (clerk already had `0.1.0`), an experimental banner in the README.
5. Content the gate then rejected, which the monorepo never compiled or linted: the six `templates/clerk-cloud-*` used an explicit
   `Provider` and read `apiKey` from project config (fails `dp_no_explicit_provider` and `dp_no_credential_require`), and the
   `team-auth-*` examples set `redirectUris` on `OauthApplication`, a property that does not exist (the Go example did not compile against
   the generated SDK; the property is `callbackUrl`). Expect a few such fixes per provider; they are monorepo bugs, not archetype bugs.
6. A comment in `shim/go.mod` mentioned `pulumi/autoregistry`; the namespace check is strict by design, so it was reworded.

## End-to-end result (clerk)

Run in scratch copies only: a worktree of `origin/main` (`7cd6e5ea`) with a throwaway local branch for the simulated identity
switch (two local commits, never pushed), an empty `git init` directory as the mirror, and `bootstrap.py --profile full` in a temporary
`HOME`. Nothing was committed to `main` or pushed other than this pull request's branch.

| # | Step | Command (abridged) | Result |
|---|------|--------------------|--------|
| 1 | Simulated identity switch | `identity.py --check` (about 74 hits, 47 of them mechanical), `--apply`, manual edits (ProviderInfo, headers, 2 docs pages, `carved-providers.json`, README banner), then the fixes the gate asked for (6 templates, 6 examples, `version.go` header) | `identity.py --check`: no old-identity hits |
| 2 | Monorepo build and gates | `go build ./...` and `go test ./...` in `lib/providers/clerk/provider`; `go run ./cmd/provider-metadata -root ../.. -check` in `tools/provider-metadata` | 8 tests pass; "45 providers satisfy the required Registry metadata contract" (clerk: advisory only, "language package names not enforced yet") |
| 2b | Readiness | `verify.py --monorepo <sim> --provider clerk` (the new bridged branch: tfgen schema instead of the binary) | tfgen schema, 5 `gen-sdk` runs, Go SDK build and vet, consumer smoke, examples, `sdk_fixups`, Python and Java builds and all `dp_*` steps passed; the Node.js and .NET builds and the windows build hit ENOSPC on this 20 GB host (rerun was not possible); `GOOS=windows go build` of the mirror's resource main was checked by hand: rc 0 |
| 3 | Export | `export.py --monorepo <sim> --provider clerk --out <mirror> --generate` | PASS: archetype `bridged`, flavor `pf`; `make schema` and `make sdks VERSION=0.0.1-dev` generated 5 SDKs, all stamped `0.0.1-dev` |
| 4 | Structure | `check.py --repo <mirror>` | OK |
| 5 | Drift | `check.py --repo <mirror> --against-source <sim>` | OK (generated paths excluded) |
| 6 | Secrets | `secret_scan.py --repo <mirror>` | PASS: 0 history, 0 tree findings, 1 commit |
| 7 | Gate | `verify.py --repo <mirror>` | PASS, 30 of 30 steps (listed above) |
| 8 | Push check | `verify.py --assert-pushable --repo <mirror>` | PUSHABLE |
| 9 | Release behaviour | `make provider VERSION=0.1.0` then `make sdks VERSION=0.1.0` on a copy | schema and metadata untouched; all five SDKs stamped `0.1.0` |
| 10 | Workflows | `actionlint` on the rendered `release.yml` and `test.yml` | clean |

The first gate run failed 7 steps, which is the gate doing its job: `toolchain` and `build_python_sdk` (host Python without `ensurepip`, an
environment issue), `make_test` (the native `version.go` panicked `Provider()`; fixed by exporting the provider's own `version.go`),
`go_examples` (a clerk example that never compiled), `dp_no_explicit_provider` and `dp_no_credential_require` (six clerk templates). The
sdkv2 flavor was exercised on `neon` without a full export: the rendered sdkv2 mains compile against the real module, tfgen produces a
schema, and `pulumi package get-schema` on the built binary equals it.

## Findings worth knowing

- The monorepo's resource mains ship a placeholder schema (`[]byte("{}")`) or none (`nil`). A mirror that published that would serve an
  empty `GetSchema`. The archetype owns the two cmd mains and embeds the generated schema; `binary_schema` proves it.
- `version.go` must keep the provider's dev default. Replacing it with the native `var Version string` made `Provider()` panic in unit tests.
- The sandbox's system Python (3.14) has no `ensurepip`; `toolchain` and `build_python_sdk` fail until a Python with `venv` is first on PATH
  (`uv python install 3.12` works). Not a skill defect, but it blocks the gate on such hosts.
- `PULUMI_CONVERT` changes the committed schema (converted examples differ between 0 and 1); with 0 the output is byte-stable.
- Disk: a bridged build adds roughly 3 GB of Go module and build cache on top of the toolchain; a full verify in a small sandbox needs 6 GB free.

`verify.py --monorepo` runs before the identity switch, so for a bridged provider it applies the switch's schema edits
(`respectSchemaVersion`, a missing Go `importBasePath`) to its scratch schema and reports them as WARNs, and it runs tfgen with
`PULUMI_CONVERT=0`. Everything else it finds is a real provider defect.

## Decisions (2026-10-16, Engin Diri)

Decided by Engin Diri on 2026-10-16 with the prototype's choices as the decisions. Alberto Pose is to be informed.

1. Adopt the `pulumi-tf-provider-boilerplate`-based bridged archetype as implemented: its layout and names, its tfgen schema step and
   committed schema and metadata, SDKs from `gen-sdk`, and the Go SDK module under `sdk/go/<gopkg>` (the native shape).
2. CI is vendored (`test.yml`/`release.yml`, publishing jobs identical to nvidia-aicr), not ci-mgmt. The layout stays
   boilerplate-compatible, so a later swap to ci-mgmt is a file replacement.
3. The cmd mains are archetype-owned in the mirror; the monorepo source stays untouched.
4. Bridged mirrors require `version.txt`, starting at `0.1.0` in the identity-switch PR. Monorepo `<pkg>-v` tags are not used for mirrors.
5. `PULUMI_CONVERT=0`. `GitHubOrg` (the upstream org) is recommended because it brings the upstream docs into the schema, but not
   required for now: `verify.py` reports a WARN for a bridged provider without it (8 of the 36 today).
6. Keep the vanity module path `github.com/pulumi/pulumi-<p>/provider` in the monorepo and rewrite it at export.
7. Enforcing bridged language package names in `tools/provider-metadata` (the `contract.go` TODO) is a follow-up, not a blocker.
   Until then the Publisher's `identity.py` is the only enforcement.
8. Publish Java.

## Remaining work

Estimates for one engineer, excluding review latency.

| Item | Size |
|------|------|
| Second bridged e2e on an sdkv2 provider (neon or stripe) including its identity switch | 0.5 day |
| `tools/provider-metadata`: bridged language names (decision 7, a follow-up) with testdata | 0.5 day |
| Windows exclusion detection for bridged (`GOOS=windows go build`) wired into readiness | 0.25 day |
| Per provider identity-switch PR (mechanical part via `identity.py`, docs, headers, version.txt, template and example fixes the gate finds) | 1 to 3 hours each when the content is clean; clerk took the longest on the six templates and examples |
| First real tag on a bridged mirror to run `release.yml` for real (the publishing jobs are byte-identical to nvidia-aicr, the build jobs are new and untested in Actions) | 0.5 day plus the registry onboarding issues |
| Publisher prompt update (SKILL.md is the delivered text) and a mirror of the skill to `dirien/claude-skills` after the PR merges | 0.25 day |
| If decision 2 becomes ci-mgmt: pinned `provider-ci` step in `export.py`, drift rule, tests | 1 to 2 days |

## Limits of the prototype

- Verified end to end for one pf provider; sdkv2 only by the spike described above. The vendored workflows were parsed and
  compared to the native ones by tests but not run in GitHub Actions.
- `cmd_main_problems` is a statement-level reader, not a Go parser; unusual but valid mains are refused rather than accepted.
- Muxed providers (pf and sdkv2 together) are not modelled; none exists in the monorepo today.
- `verify.py --monorepo` for a bridged provider was exercised only through its shared steps and unit tests of the new branches.
