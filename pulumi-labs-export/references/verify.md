# The verify gate

`scripts/verify.py` proves, by code, that a provider may be pushed to
`pulumi-labs/pulumi-<provider>`:

1. SDK generation works for all five languages.
2. There are no errors during SDK generation, and the generated Go SDK compiles.
3. The provider follows the default-provider pattern: it works with no explicit
   `Provider` resource, and credentials come from environment variables (or
   per-resource attributes).

Three modes. Prerequisite for all: `eval "$(python3 scripts/bootstrap.py)"`
(profile `full`). A missing tool is a FAIL, never a skip.

```
python3 scripts/verify.py --monorepo <autoregistry checkout> --provider <p>   # readiness, before the carve
python3 scripts/verify.py --repo <repo>                                       # the gate, on the exact commit to push
python3 scripts/verify.py --assert-pushable --repo <repo>                     # right before git push
```

Reports: `$HOME/.local/pulumi-labs-export/reports/<repo>-<HEAD sha>.{json,md}`
(monorepo: `<provider>-monorepo-<sha>`), full logs in `<same base>.logs/`. The
report holds HEAD SHA, tree hash, `git status --porcelain` (must be empty),
the skill clone SHA, tool versions, and each step's PASS/FAIL with a log tail.
Exit 0 only if every step passes. Failures in a monorepo run are fixed in the
monorepo; failures in a repo run are fixed in the carved repo, then re-run.

## How the gate runs

It makes a clean local clone of HEAD in a scratch dir and builds there, so the
checkout never changes (the last step proves it) and the proof is for the
committed tree, not for stray files. Only local `git` is used; no `gh`, no
network `git`. Dependencies still download from Go, npm, PyPI, NuGet and Maven.

## Steps (carved repo)

| Step id | What | Why |
|---|---|---|
| `identity` | HEAD, tree, worktree clean, provider read from `.pulumi-labs-export.json` | the report is only valid for this commit |
| `toolchain` | go, pulumi, make, jq, node, npm, python3 (a real `venv` with pip), dotnet, java, gradle | the `build_*` Make targets skip silently when a tool is missing; the gate must not |
| `make_provider`, `make_lint`, `make_test`, `make_check_copyright` | the archetype targets | same checks CI runs. `make test` uses `-race`, which needs a C compiler; without one the gate runs `go test` without `-race`, records a warning |
| `make_schema`, `schema_diff` | regenerate, then `git diff --exit-code` on schema.json | a stale committed schema means stale SDKs |
| `make_sdks` | all five generators, `sdk_fixups`, `go mod tidy`. FAIL on non-zero exit or any error-pattern line (`error:`, `panic:`, `failed to`, `go: ... cannot find / missing go.sum / reading ...`, tracebacks). Warnings are collected into the report. Warns if the regenerated `sdk/` differs from the committed one | requirement 1 and 2 |
| `go_sdk_build`, `go_sdk_vet` | `go build ./...` and `go vet ./...` in `sdk/go/<gopkg>` | requirement 2 |
| `go_module_paths` | `go list -m` equals `github.com/pulumi-labs/pulumi-<p>`; every `go list ./...` package starts with it | the Go path is permanent once released |
| `go_consumer_smoke` | fresh module outside the repo: `go mod init example.com/consumer`, require plus `replace` to the local SDK, import `github.com/pulumi-labs/pulumi-<p>/sdk/go/<gopkg>`, reference one exported symbol, `go build` | proves the exact import path users write compiles |
| `go_examples` | every Go example and template (`examples/*`, `templates/*` with a `go.mod` or `.go` files), copied to scratch, `replace` to the local SDK, `go mod tidy`, `go build`. `${PROJECT}` placeholders are replaced in the copy | examples must compile against the SDK about to be published, not a released one |
| `build_nodejs_sdk`, `build_python_sdk`, `build_dotnet_sdk`, `build_java_sdk` | the Make targets; FAIL on non-zero exit or any "skipping" in the output | requirement 1 |
| `dp_*` | the five default-provider checks below | requirement 3 |
| `check_py` | `check.py` (structure, naming, byte-identical renders, namespace grep, positive Go module paths) | one command is the whole gate |
| `worktree_clean_after` | HEAD unchanged, porcelain empty | the gate must not dirty the checkout |

Toolchain notes: the .NET 8 SDK builds the generated `net6.0` csproj through
the targeting pack NuGet restores automatically (needs `api.nuget.org`; a
`NETSDK1138` end-of-support warning is expected). Without libicu, bootstrap
exports `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`. Gradle 9 runs on JDK 17; the
`sdk_fixups` target already moves the generated Java toolchain to 17.

## Default-provider and credential checks (static, reported per item)

| Step | Rule | Why |
|---|---|---|
| `dp_env_defaults` | every `config.variables` entry that is `secret: true` or whose name matches `token\|key\|secret\|password\|credential` (case-insensitive) has a non-empty `defaultInfo.environment` | without it the default provider cannot read the credential from the environment |
| `dp_no_required_config` | `config.defaults` and `provider.requiredInputs` are empty | any required config breaks the "no Provider resource, just env vars" path |
| `dp_no_env_tags` | no `env:"..."` tag on a `pulumi:"..."` struct field in non-test provider Go code (`shim/`, `sdk/`, `vendor/` skipped) | infer ignores `env` tags; use `a.SetDefault(&c.Field, nil, "ENV_VAR")` in `Annotate`. Mirrors the `native-provider-env-tags` workflow |
| `dp_no_explicit_provider` | examples and templates never instantiate a provider: `new <x>.Provider(` (TS/JS), `<x>.Provider(` (Python), `.NewProvider(ctx` (Go), `new <X>.Provider(` or `new Provider(` (C#), `new Provider(` (Java), `pulumi:providers:` (YAML). Comment lines are ignored | examples must show the default-provider path |
| `dp_no_credential_require` | examples and templates never read (`require*`, `get*`, `try*`, any case style) a config key matching one of the provider's own credential variables: the schema's `config.variables` that are `secret: true` or credential-named (modal: `tokenId`, `tokenSecret`), compared case-insensitively ignoring `_`, `-` and a `<ns>:` prefix | provider credentials come from the environment (or per-resource attributes), not stack config the user must set up. App secrets stored in a Secret resource (e.g. `hfToken`, `workerApiKey`) are not provider credentials and pass |

Limits: the credential check looks at code, not at `config:` blocks in
`Pulumi.yaml`. `templates/` must follow the repo's template
rules (project-namespace config keys) in AGENTS.md; if a template can only work
with a read of a provider credential key, stop and ask a human.

## Positive Go module-path checks in `check.py`

- `sdk/go/<gopkg>/go.mod` declares exactly
  `module github.com/pulumi-labs/pulumi-<p>/sdk/go/<gopkg>`.
- A Go example or template `go.mod` that requires something ending in
  `/sdk/go/<gopkg>` requires exactly that path.
- A committed `replace` is allowed only as that module to the local
  `sdk/go/<gopkg>`; anything else fails.

## `--assert-pushable`

Exits 0 only if the report for the current HEAD exists, `mode` is `repo`, its
HEAD and tree match now, its recorded porcelain was empty, every required step
is present and PASS, and the worktree is clean right now. Any new commit
invalidates the report. Run it immediately before every push, including later
PR branches.

## Monorepo readiness (`--monorepo`)

Before any carve. For `lib/providers/<p>/provider`: `go build`, `go vet`,
`go test`; an informational `GOOS=windows` build (a failure means pass
`--exclude-platform windows/amd64 --reason ...` to `render.py`); provider
binary; `pulumi package get-schema`; `pulumi package gen-sdk` for nodejs,
python, go, dotnet and java into a temp dir; Go SDK `go mod init`/`tidy`,
build, vet; the consumer smoke test with the schema's `importBasePath`; Go
examples/templates against that SDK; `sdk_fixups` and the four SDK builds using
the rendered archetype Makefile; the same `dp_*` checks on the monorepo code,
examples and templates. A WARN-only step `metadata_pulumi_labs` reports whether
the builder already sets the pulumi-labs Go import path, repository, publisher
and plugin download URL; today the monorepo metadata contract keeps the
`pulumi/` values, so expect warnings that the carve's namespace commit resolves.
