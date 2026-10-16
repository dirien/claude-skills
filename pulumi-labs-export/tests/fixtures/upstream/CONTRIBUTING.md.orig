# Contributing

Thanks for your interest in contributing to the Pulumi NVIDIA AICR provider.

## Development setup

You will need:

- [Go](https://go.dev/) 1.27+
- [Pulumi CLI](https://www.pulumi.com/docs/install/) 3.165+
- For SDK generation/build: Node.js 18+, Python 3.9+, .NET 8+, JDK 11+

Clone, build, and test:

```bash
git clone https://github.com/pulumi-labs/pulumi-nvidia-aicr.git
cd pulumi-nvidia-aicr
make provider          # build the provider binary into ./bin/
make test              # run unit tests
make schema            # regenerate provider/cmd/.../schema.json
make sdks              # regenerate all 5 language SDKs
```

## Repository layout

```
provider/
  cmd/pulumi-resource-nvidia-aicr/  # provider binary entry point + schema.json
  pkg/provider/                     # ClusterStack component implementation
  pkg/aicr/                         # adapter over the NVIDIA AICR Go SDK
  pkg/version/                      # version string injected via ldflags
sdk/
  nodejs/ python/ go/ dotnet/ java/ # generated SDKs (do not edit by hand)
examples/                           # one directory per scenario × language
.github/workflows/                  # CI (test.yml) and release (release.yml)
```

## Pull request flow

1. Open an issue first for non-trivial changes so we can align on direction.
2. Make your changes on a topic branch off `main`.
3. Run `make test` and, if you touched the schema or component API,
   `make schema && make sdks` and commit the regenerated files.
4. Open a PR. CI runs the full test + SDK build matrix.
5. A maintainer reviews and merges.

## Updating the AICR recipe data

Recipe resolution and data come from the official
[NVIDIA AICR Go SDK](https://github.com/NVIDIA/aicr) (`pkg/client/v1`),
pinned in `go.mod`; the recipe YAML is embedded in that module. To pick up
new upstream recipes:

1. `go get github.com/NVIDIA/aicr@<release-tag> && go mod tidy`.
2. If the new data adds criteria values (services, accelerators, OSes,
   platforms), update the `supported*` allowlists, `validateCompatibility`,
   and `Annotate` descriptions in `provider/pkg/provider/clusterstack.go`,
   the README's tables, and `examples/README.md`.
3. Update the README's "AICR Version Compatibility" table with the
   provider minor version that will ship the new SDK (see Releasing).
4. Run `make test` — adapter resolution tests will catch most drift.

Never vendor or hand-edit NVIDIA recipe YAML in this repo.

## Releasing

Releases are tag-driven. Pushing a `v*.*.*` tag fires
`.github/workflows/release.yml`, which:

1. Cross-compiles the provider for `linux/amd64`, `linux/arm64`,
   `darwin/amd64`, and `darwin/arm64`.
2. Generates SDKs in all five languages with the tag's version baked in.
3. Uploads provider tarballs and SDK source archives to the GitHub Release.
4. Optionally publishes SDKs to npm/PyPI/NuGet (disabled by default;
   re-enable by removing `if: false` and setting the relevant secrets).

**Version rule:** a release that bumps the AICR SDK (and therefore the
embedded recipe data) must bump the provider's minor version, never just
the patch. Recipe data changes what an unchanged program deploys, and the
README's compatibility table maps provider minor versions to SDK versions,
so a patch release with new recipe data makes that table wrong. Before
tagging, confirm the tag's minor version matches the table's row for the
SDK pinned in `go.mod`.

## Code style

- Go: standard `go fmt` + `go vet`. Match existing patterns; no unrelated
  refactors in feature PRs.
- Comments on exported symbols only when the WHY is non-obvious.
- Keep tests focused: the SDK-adapter layer has thorough coverage in
  `provider/pkg/aicr/`; the component layer has smoke tests in
  `provider/pkg/provider/clusterstack_test.go`.

## Reporting bugs

Open an issue with the bug template. Please include the provider version,
your `accelerator`/`service`/`intent` inputs, and the relevant `pulumi up`
output.

## Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/).
