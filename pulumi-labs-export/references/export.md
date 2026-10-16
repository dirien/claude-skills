# Export: readiness, identity switch, initial export

The mirror model: the monorepo provider (`lib/providers/<p>/`) is the source of
truth and is never deleted; `pulumi-labs/pulumi-<p>` is a generated mirror that
`scripts/export.py` overwrites. `lib/providers/modal/` is the first target.

## What export.py does

```
python3 scripts/export.py --monorepo <autoregistry checkout> --provider <p> --out <mirror dir> [--generate]
```

Same monorepo commit plus same skill version gives byte-identical output,
except `schema.json` and `sdk/`, which `make` produces. It reads the commit
from git (`git archive` of HEAD), never the working tree, and never touches the
network or `gh`. `--generate` then runs `make schema` and `make sdks
VERSION=0.0.1-dev` in `--out` (this downloads Go modules). The committed SDKs
carry the dev version, not `version.txt`: the release workflow regenerates them
with the tag (`make sdks VERSION=<tag>`), exactly as in the archetype
(`pulumi-nvidia-aicr` commits `0.0.1-dev`). After generating, `export.py` checks
that `sdk/nodejs/package.json` and `sdk/dotnet/version.txt` carry it and refuses
otherwise, naming the cause (see "SDK version stamping" below).

Preconditions, each a clear error:

- `carved-providers.json` lists the provider, `mode: "mirror"`, `repo` and
  `goModule` equal to the names from `names.py`.
- The monorepo checkout is clean; its HEAD is the recorded source SHA. Use a
  fresh worktree of `origin/main`.
- `provider/go.mod` has no relative `replace` (unsupported for now).
- `docs/_index.md` and `docs/installation-configuration.md` exist (add them in
  the identity-switch PR; model: the archetype's docs, `references/archetype.md`).
- `version.txt` is `0.x.y`; `skills/SKILLS.md` exists.
- The copy already has the pulumi-labs identity. Any `pulumi/autoregistry`,
  `github.com/pulumi/pulumi-<p>`, `@pulumi/<p>`, `pulumi_<p>`, `Pulumi.<Pascal>`,
  `com.pulumi.<p>` or `WithPublisher("Pulumi")` left in the files to be copied
  fails with file:line and a pointer to the identity-switch PR.
- `--out` has a `.git` and does not overlap the monorepo.

The export is staged in a temp dir and validated first, so a refusal leaves
`--out` untouched. Then every file in `--out` except `.git/` is removed and the
export written: leftovers are impossible.

Transform: copy `provider/cmd`, `provider/pkg`, `examples/`, `templates/`,
`docs/`, `README.md`, `CHANGELOG.md` and, if present, `.gitleaks.toml` (the
reviewed secret-scan allowlist, `verify.md`); move `provider/go.mod`/`go.sum` to the
root with the module line `github.com/pulumi-labs/pulumi-<p>`; rewrite
`github.com/pulumi/autoregistry/lib/providers/<p>/provider/` to
`github.com/pulumi-labs/pulumi-<p>/provider/` in every copied `.go` file;
`CLAUDE.md` is `skills/SKILLS.md` under a short generated header; `render.py`
writes the archetype files (it replaces the monorepo `provider/pkg/version/version.go`).
Never copied: `version.txt`, the monorepo `Makefile` and `.gitignore`,
`provider/go.mod`, `skills/`. `.pulumi-labs-export.json` records provider,
display name, excluded platforms, archetype repo and SHA, `source_repo`,
`source_sha`, `source_version` (from `version.txt`), `skill_commit` (or null)
and is reused on the next export (display name, excluded platforms). Pass
`--exclude-platform windows/amd64 --reason "..."` only if
`GOOS=windows go build ./provider/cmd/...` fails (verify.py `--monorepo`
reports it).

## b. The identity-switch PR (monorepo)

One PR to `pulumi/autoregistry`, one provider, Reviewer clearance on the head
SHA, green checks (`tools/provider-metadata -check` enforces the identity for
listed providers). Use the helper, in this order:

```
python3 scripts/identity.py --check --monorepo <checkout> --provider <p>   # every remaining hit: file:line [category]
python3 scripts/identity.py --apply --monorepo <checkout> --provider <p>   # the mechanical rewrites only; idempotent
# ... the manual items below ...
python3 scripts/identity.py --check --monorepo <checkout> --provider <p>   # repeat until it prints "no old-identity hits"
```

`--check` categories: Go import path `github.com/pulumi/pulumi-<p>`, npm
`@pulumi/<p>`, Python `pulumi_<p>`, NuGet/C# `Pulumi.<Pascal>` (not
`Pulumi.Labs.`), Java `com.pulumi.<p>` and Maven/Gradle `com.pulumi:<p>` (not
`com.pulumi.labs`), repository URL `github.com/pulumi/autoregistry`, publisher
`"Pulumi"`, builder metadata (missing `WithPluginDownloadURL` or
`WithLanguageMap`, `WithLanguageMap` ordered after `WithGoImportPath`),
missing copyright headers, missing `docs/` pages and the
`carved-providers.json` entry. `--apply` rewrites, string for string and without
parsing, only package names and import paths in `examples/`, `templates/`,
`README.md` and the provider's `*_test.go` (a publisher assertion in a test
becomes `"Pulumi Labs"`). It never edits the provider's non-test Go code (the
builder chain, and the monorepo import paths that `export.py` rewrites itself),
`docs/`, `CHANGELOG.md` or `carved-providers.json`; those are the manual items,
which it lists after rewriting.

Contents of the PR, from the first real run on modal:

1. `carved-providers.json` entry: `name`, `repo`, `goModule`, `mode: "mirror"`,
   `registryName`, `listed: false`, and `exportedAt`, `lastSyncedCommit`,
   `lastSyncedVersion` all `null`. (Manual.)
1a. `lib/providers/<p>/version.txt` = `0.1.0` if the provider has none (every
   bridged provider today). `export.py` refuses without it, and
   `provider-metadata` requires it for every listed provider afterwards.
   From then on only the Publisher's release-cut PR changes it
   (`lifecycle.md`).
2. Builder metadata in `provider/pkg/provider/provider.go`: `WithPublisher("Pulumi Labs")`,
   `WithRepository`, `WithGoImportPath`, `WithPluginDownloadURL`
   (`github://api.github.com/pulumi-labs/pulumi-<p>`), `WithLanguageMap`
   (nodejs `packageName` and `packageDescription`, python `packageName`,
   csharp `rootNamespace` `Pulumi.Labs`, java `basePackage` `com.pulumi.labs`)
   and "experimental" in `WithDescription`. Values come from `names.py`. (Manual.)
   **`WithLanguageMap` replaces the whole map, so two rules follow:**
   - Call it BEFORE `WithGoImportPath`. `WithGoImportPath` adds `importBasePath`
     to the `go` entry that is there; without a map the framework derives
     `importBasePath` from the repository URL (`https://github...`), which
     breaks the Go SDK.
   - It must carry the framework defaults the builder starts with, or
     `gen-sdk --version` is ignored and the .NET SDK does not build (below):
     `respectSchemaVersion: true` for `go`, `nodejs`, `python` and `csharp`,
     `generateResourceContainerTypes: true` for `go`, and
     `pyproject.enabled: true` for `python`. The shape (nvidia-aicr's, modal's):

     ```go
     WithLanguageMap(map[string]any{
         "go": map[string]any{"generateResourceContainerTypes": true, "respectSchemaVersion": true},
         "nodejs": map[string]any{"packageName": "@pulumi-labs/<p>", "packageDescription": "Experimental: ...", "respectSchemaVersion": true},
         "python": map[string]any{"packageName": "pulumi_labs_<p>", "respectSchemaVersion": true, "pyproject": map[string]any{"enabled": true}},
         "csharp": map[string]any{"rootNamespace": "Pulumi.Labs", "respectSchemaVersion": true},
         "java":   map[string]any{"basePackage": "com.pulumi.labs", "respectSchemaVersion": true},
     }).
     WithGoImportPath("github.com/pulumi-labs/pulumi-<p>/sdk/go/<gopkg>").
     ```

     Dropping `generateResourceContainerTypes` would also change the Go SDK API
     against the schema the monorepo already ships, so compare the old and new
     `language` sections of `pulumi package get-schema` (only the names and
     `importBasePath` may differ).
3. Everything the export would otherwise carry with the old names, all
   languages: README (experimental banner in the first 15 lines, install
   lines), `examples/` and `templates/` (Go `go.mod` module lines and requires,
   imports; npm, Python, .NET and Java names), tests that assert the old values
   (modal: `metadata_test.go` asserts publisher and Go path). Mostly done by
   `identity.py --apply`.
4. `// Copyright ... Pulumi Corporation` headers on every `provider/**/*.go`
   (modal had none; `make check_copyright` fails without). (Manual; the block is
   the archetype's `provider/pkg/version/version.go`.)
5. The two `docs/` Registry pages. (Manual.)
6. Keep example credentials out of gitleaks' patterns: placeholders such as
   `sk_test_placeholder` (flagged as a Stripe key) become `sk_test_xxx_placeholder`
   style values. A literal that is already in git history stays there; the only
   remedy is a reviewed `lib/providers/<p>/.gitleaks.toml` allowlist (`verify.md`,
   "Secret scan"), which `export.py` copies to the mirror root.

Run `export.py` in a scratch dir to see what is still missing before asking
for review; the failure list is exact.

### SDK version stamping

`pulumi package gen-sdk --version <v>` writes `sdk/dotnet/version.txt` (the
generated csproj embeds it, so `dotnet build` fails without it), sets
`<Version>` in the csproj and the version in `sdk/nodejs/package.json` only for
languages whose schema settings carry `respectSchemaVersion: true`. Without it
the SDK keeps the `${VERSION}` placeholder and has no `version.txt`. (Dropping
`pyproject.enabled` likewise loses the Python SDK's `pyproject.toml`.) The
framework's builder defaults set these; a custom `WithLanguageMap` that omits
them silently turns them off. That was the
modal finding: not a Makefile or `verify.py` defect (the archetype Makefile is
unchanged and `sdk_fixups` does not write `version.txt`, because patching it
there would ship an unstamped Python SDK and hide the builder bug). `check.py`
fails a schema without the flag and SDKs not stamped `0.0.1-dev`;
`verify.py` has an `sdk_version` step; `export.py --generate` refuses.

## c. Initial export

1. Full, non-shallow, unfiltered clone of `pulumi/autoregistry` (about 12 MB;
   no `--depth`, no `--filter`) at the merged identity-switch commit. Never push
   from it.
2. `git subtree split --prefix=lib/providers/<p> -b export/<p>`; in the empty
   mirror clone: `git pull <clone> export/<p>`. This keeps the history.
3. A clean checkout of the same commit (worktree). `python3 scripts/export.py
   --monorepo <checkout> --provider <p> --out . --generate`, then
   `git add -A && git commit -m "Export pulumi/autoregistry@<sha>"`.
4. Gate: `python3 scripts/secret_scan.py --repo .` (gitleaks over the full
   history and the tree; if gitleaks is not installed it exits 2: say "history
   was not scanned for secrets" and stop; any finding stops for a human),
   `python3 scripts/verify.py --repo .`, then
   `python3 scripts/verify.py --assert-pushable --repo .` immediately before the
   push (it needs the passing verify report and the clean secret-scan report for
   the same HEAD).
5. `git push origin main`: the one allowed direct push. Wait for the `test`
   workflow to go green.
6. Monorepo PR (Reviewer clearance, green checks) setting `exportedAt`,
   `lastSyncedCommit` (the source SHA), `lastSyncedVersion`. `listed` stays
   `false` until a human says otherwise.

## d. Onboarding issues

`scripts/onboarding.py`, see `references/onboarding.md`. No tag request until
they are closed.
