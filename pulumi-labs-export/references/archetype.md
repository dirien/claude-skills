# The archetype

`pulumi-labs/pulumi-nvidia-aicr`, vendored at commit
`1d262e3db83fd120e8454b9162e762eaf743de8d` (see `archetype/manifest.json`).
Every vendored file is stored with a `.tmpl` suffix so that Go, GitHub
Actions, linters and license detection never mistake it for a real file.
The manifest records, per file, the template path, the target path, the mode
and the sha256 of the upstream original.

- `verbatim`: no provider values; copied byte for byte.
- `render`: provider values substituted. Tokens: `__PROVIDER__`, `__GO_PKG__`,
  `__PY_PKG__`, `__PASCAL__`, `__DISPLAY_NAME__`, plus the whole-line
  `__PLATFORM_MATRIX__`. These never collide with GitHub Actions `${{ }}`.
  render.py fails on any leftover `__[A-Z][A-Z0-9_]*__`.

check.py re-renders from `.pulumi-labs-export.json` and compares bytes, so a
rendered or verbatim file that differs in any way is reported as hand-edited.

## Files

| Target                                        | Mode     | Why                                                                                         |
|-----------------------------------------------|----------|---------------------------------------------------------------------------------------------|
| `LICENSE`                                     | verbatim | Apache-2.0 text, no provider values.                                                        |
| `mise.toml`                                   | verbatim | Pins the Pulumi CLI to match the workflows.                                                 |
| `.gitignore`                                  | verbatim | Generic.                                                                                    |
| `scripts/fixup_python_input_optional.py`      | verbatim | Used by `make sdk_fixups`; generic.                                                         |
| `provider/pkg/version/version.go`             | verbatim | `var Version string`, set by ldflags. Carries the Pulumi copyright header.                  |
| `Makefile`                                    | render   | Provider name, ldflags path, Go SDK dir, Python package dir, csproj name, POM name.         |
| `.github/workflows/release.yml`               | render   | Binary name, ldflags path, Go SDK dir, platform matrix.                                     |
| `.github/workflows/test.yml`                  | render   | Binary name, platform matrix.                                                               |
| `.vscode/launch.json`                         | render   | Provider name.                                                                              |
| `CONTRIBUTING.md`                             | render   | Display name, repo name, binary path; AICR-specific text made generic (below).              |

Provider-authored, not templated, but required by check.py: `README.md`,
`CLAUDE.md`, `docs/_index.md`, `docs/installation-configuration.md`,
`examples/` (non-empty), root `go.mod` and `go.sum`,
`provider/cmd/pulumi-resource-<p>/main.go` and `schema.json`, and non-empty
`sdk/{dotnet,go,java,nodejs,python}`.

The two docs files are the Pulumi Registry pages. Front matter shape (from the
archetype):

```
---
title: <Display name> Provider
meta_desc: <one sentence>
layout: package
---
```

Read the archetype's pages at the pinned SHA as the model:

```
gh api "repos/pulumi-labs/pulumi-nvidia-aicr/contents/docs/_index.md?ref=1d262e3db83fd120e8454b9162e762eaf743de8d" -H 'Accept: application/vnd.github.raw'
```

## AICR-specific content dropped from the templates

1. **windows/amd64 matrix exclusion and its NVIDIA comment**, in both
   workflows (the NVIDIA AICR SDK does not compile for GOOS=windows). The
   templates build windows/amd64 by default. When a provider cannot, pass
   `render.py --exclude-platform windows/amd64 --reason "..."` (repeatable,
   one `--reason` per platform); the leg is dropped and the reason is written
   as a comment (test.yml also keeps a commented-out entry, as upstream does).
2. **test.yml step "Verify binary embeds a resolvable AICR SDK version"**
   (greps `go version -m` for `github.com/NVIDIA/aicr`). Removed entirely.
3. **CONTRIBUTING.md**: the "Updating the AICR recipe data" section, the
   "Version rule" paragraph about recipe data and the README compatibility
   table, the AICR-specific layout lines (`pkg/provider`, `pkg/aicr`), the
   AICR test-path sentence and the `accelerator/service/intent` bug-report
   hint are replaced by generic text or removed.

## Archetype defects fixed in the templates (report upstream)

- **csproj name in `set_version`.** The Makefile's `set_version` target
  references `sdk/dotnet/Pulumi.NvidiaAicr.csproj` but every other target uses
  `Pulumi.Labs.NvidiaAicr.csproj` (the file the generator actually writes). As
  upstream the stamp step silently does nothing. The template uses
  `Pulumi.Labs.__PASCAL__.csproj` everywhere. Report it to the archetype repo.
- **Stale release step in CONTRIBUTING.md.** It says SDK publishing is
  "disabled by default; re-enable by removing `if: false`", but `release.yml`
  has no `if: false` and publishes to all registries. The template describes
  what the workflow does. Report it upstream as well.

## Archetype quirks worth knowing

- `release.yml`, `test.yml` and `mise.toml` each pin the Pulumi CLI version
  (3.241.0), and both workflows pin Go 1.27; they must move together, which is
  one reason the files are vendored and not edited.
- `test.yml` runs `make schema` and fails if the committed `schema.json`
  differs, so the schema must be committed and reproducible with the pinned CLI.
- `make go_sdk` runs `go mod init` inside `sdk/go/<go_pkg>`, so the Go SDK is
  its own module. A `go.mod` under `sdk/` is expected; only `provider/` must be
  free of `go.mod`.
- The generated .NET project targets net6.0; `publish-dotnet` installs 6.0.x
  and 8.0.x for that reason.
- **SDK versions.** The Makefile defaults `VERSION ?= 0.0.1-dev`, `release.yml` regenerates the
  SDKs with the tag (`make sdks VERSION=<tag>`, then `set_version` as a fallback), and
  `pulumi-nvidia-aicr` commits `0.0.1-dev` (including `sdk/dotnet/version.txt`). The skill does the
  same: `export.py --generate` passes `VERSION=0.0.1-dev`, and `check.py` requires it. No
  Makefile deviation was needed for the .NET `version.txt` finding: `gen-sdk --version` writes the
  file (and stamps `package.json`, the csproj and `pyproject.toml`) when the schema's language
  settings carry `respectSchemaVersion`, which a custom `WithLanguageMap` drops (`export.md`,
  "SDK version stamping"). `sdk_fixups` stays byte-identical to upstream apart from the deviations
  above, so the manifest's upstream-diff tests are unchanged.
- `make check_copyright` (run in CI) requires `Copyright .* Pulumi Corporation`
  in every `provider/**/*.go`; check.py enforces the same.
- Maven `name` in the generated POM is blank unless the Makefile fills it, which
  `sdk_fixups` does with `pulumi-labs-<p>`.

## Drift

`python3 scripts/archetype_drift.py` compares the sha256 of every vendored
upstream original with the archetype's `main` and lists changed files and new
files in scaffolding locations. It exits 2 on drift and updates nothing.
Give the list to a human. Re-vendoring is a PR to this skill (new SHA, new
templates, updated manifest and tests), never a silent change. After it
merges, check.py reports `ARCHETYPE CHANGED` on carved repos until they are
re-rendered.
