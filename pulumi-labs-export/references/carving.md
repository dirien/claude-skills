# Carving a provider out of the monorepo

How a monorepo provider differs from the archetype layout, and what each
export step does about it. `lib/providers/modal/` (main) is the first real
target and the worked example. Always re-audit the provider you are given; do
not assume it looks like modal.

## Monorepo shape today

| Monorepo (`lib/providers/<p>/`)                              | Public repo                                                   |
|--------------------------------------------------------------|---------------------------------------------------------------|
| `provider/go.mod`, module `github.com/pulumi/autoregistry/lib/providers/<p>/provider`, plus `provider/go.sum` | root `go.mod`/`go.sum`, module `github.com/pulumi-labs/pulumi-<p>`; nothing under `provider/` |
| `version.txt` (modal: `0.1.0`)                                | deleted; the version is the git tag (`v0.x.y`)                |
| own `Makefile` (`build`, `gen-sdk`, `tidy`, local Go toolchain path) | rendered archetype `Makefile` (`provider`, `schema`, `sdks`, `set_version`, `check_copyright`) |
| `templates/`                                                  | human decision: not part of the archetype, ask before keeping or dropping |
| `skills/SKILLS.md`                                            | `CLAUDE.md` (dev guide); then remove `skills/`                |
| `README.md`, `CHANGELOG.md`, `examples/`                      | keep, with the namespace rewrite applied                      |
| no committed `schema.json`; SDKs generated on demand          | `provider/cmd/pulumi-resource-<p>/schema.json` and `sdk/{dotnet,go,java,nodejs,python}` committed (`make schema`, `make sdks`) |
| metadata set by builder calls in `provider/pkg/provider/provider.go` | same place, new values (below)                         |
| CI is repo level (`build.yml`, `lint.yml`, ...)               | `.github/workflows/test.yml` and `release.yml` only           |
| `LICENSE`, `mise.toml`, `.gitignore`, `CONTRIBUTING.md`, `docs/` absent | `LICENSE`, `mise.toml`, `.gitignore`, `CONTRIBUTING.md` from render.py; `docs/` written by you |

## Step by step

**1. Audit.** In a fresh `main`: build, vet, generate SDKs. Grep for imports of
other autoregistry packages (none allowed; there is no `replace` to follow in
the public repo). List every hit of the old namespaces
(`pulumi/autoregistry`, `github.com/pulumi/pulumi-<p>`, `@pulumi/<p>`,
`pulumi_<p>`, `Pulumi.<Pascal>`, `com.pulumi.<p>`) with file and line. For
modal the hits include `provider/go.mod`, `provider/cmd/pulumi-resource-modal/main.go`,
`provider/pkg/provider/provider.go` (`WithRepository`, `WithGoImportPath`),
`examples/modal-go*/go.mod`, example `package.json`/`requirements.txt`/
`__main__.py`, templates, and the README.

**2. Split.**

```
git -C <autoregistry> subtree split --prefix=lib/providers/<p> -b export/<p>
git init -b main <work>/pulumi-<p>
git -C <work>/pulumi-<p> pull <autoregistry> export/<p>
```

**3. Restructure commit.** `git mv provider/go.mod go.mod` and
`provider/go.sum go.sum`; `git rm version.txt Makefile`; keep the rest.
Imports still use the old module path at this point.

**4. Namespace rewrite commit** (nothing else in it). Use `names.py` for every
value.
- `go.mod` module line, and every import
  `github.com/pulumi/autoregistry/lib/providers/<p>/provider/...` to
  `github.com/pulumi-labs/pulumi-<p>/provider/...`.
- Examples and templates: Go module/imports, npm `@pulumi/<p>` to
  `@pulumi-labs/<p>`, Python `pulumi_<p>` to `pulumi_labs_<p>` (and the
  requirements pin `pulumi-labs-<p>`), .NET `Pulumi.<Pascal>` to
  `Pulumi.Labs.<Pascal>`, Java `com.pulumi.<p>` to `com.pulumi.labs.<package>` (the generated
  `sdk/java` sources are the authority for the exact package).
- Provider metadata in `provider.go`. Modal today sets
  `WithRepository("https://github.com/pulumi/autoregistry")`,
  `WithGoImportPath("github.com/pulumi/pulumi-modal/sdk/go/modal")` and
  `WithPublisher("Pulumi")`. They become `repo_url`, `go_import_base_path`
  and `Pulumi Labs`; also set the plugin download URL
  (`github://api.github.com/pulumi-labs/pulumi-<p>`) and the language names the
  schema needs (`nodejs.packageName`, `python.packageName`,
  `csharp.rootNamespace` = `Pulumi.Labs`, `java.basePackage` = `com.pulumi.labs`)
  through the builder's metadata options for the pulumi-go-provider version in
  `go.mod`. The archetype's `provider/pkg/provider/provider.go` shows the
  resulting schema. If the builder cannot express one of them, stop and report
  it; do not post-process the schema by hand.
- Update tests that assert the old values (modal: `metadata_test.go`).

**5. Scaffolding commit.**

```
python3 scripts/render.py --repo-dir <repo> --provider <p> --display-name "<Name>"
```

Add `--exclude-platform windows/amd64 --reason "<why>"` only if
`GOOS=windows go build ./provider/cmd/...` fails. Then write `README.md`,
`CLAUDE.md` (`git mv skills/SKILLS.md CLAUDE.md`, then fix paths and names),
`docs/_index.md` and `docs/installation-configuration.md` (shape in
`archetype.md`; install lines for npm, PyPI, Go, .NET and Java come from
`names.py`). `render.py` overwrites any file of the same name, which is the
point.

**6. Experimental commit.** README banner in the first 15 lines, for example
`> **Experimental:** this provider is experimental. Expect changes before 1.0.`
The provider description (`WithDescription`) and the Node.js
`packageDescription` must contain "experimental" so that the generated schema
and every SDK carry it. Then regenerate: `make schema`, `make sdks
VERSION=<0.x.y>`, commit `schema.json` and `sdk/`. Pick the version from the
monorepo's `version.txt` if it is already `0.x.y` (modal: `0.1.0`), else `0.1.0`;
the first tag is `v<that>`, created only after the onboarding issues close.

**7. Gate, scan, issues, push.** See SKILL.md. First push to an empty repo is
`git remote add origin https://github.com/pulumi-labs/pulumi-<p>.git && git push origin main`.

What happens to `lib/providers/<p>/` in the monorepo afterwards is a human
decision. Do not delete or edit it as part of the export.
