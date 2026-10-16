# Naming

Every name is derived from the provider slug by `scripts/names.py`. Do not
type names by hand and do not derive them in your head: the Go SDK directory,
the Python package and the csproj name each have a rule that is easy to get
wrong.

```
python3 scripts/names.py <slug>                  # whole table as JSON
python3 scripts/names.py <slug> --field go_pkg   # one value
```

The slug matches `^[a-z][a-z0-9-]*[a-z0-9]$` and contains no `--`.

| Field                  | Rule                                             | nvidia-aicr                                                  | modal                                          | better-uptime                                   |
|------------------------|--------------------------------------------------|--------------------------------------------------------------|------------------------------------------------|-------------------------------------------------|
| repo                   | `pulumi-labs/pulumi-<p>`                         | `pulumi-labs/pulumi-nvidia-aicr`                             | `pulumi-labs/pulumi-modal`                     | `pulumi-labs/pulumi-better-uptime`              |
| go_module              | `github.com/pulumi-labs/pulumi-<p>`              | `github.com/pulumi-labs/pulumi-nvidia-aicr`                  | `github.com/pulumi-labs/pulumi-modal`          | `github.com/pulumi-labs/pulumi-better-uptime`   |
| go_pkg (SDK dir)       | `<p>` with hyphens removed                       | `nvidiaaicr`                                                 | `modal`                                        | `betteruptime`                                  |
| go_import_base_path    | `<go_module>/sdk/go/<go_pkg>`                    | `.../pulumi-nvidia-aicr/sdk/go/nvidiaaicr`                   | `.../pulumi-modal/sdk/go/modal`                | `.../pulumi-better-uptime/sdk/go/betteruptime`  |
| npm_package            | `@pulumi-labs/<p>`                               | `@pulumi-labs/nvidia-aicr`                                   | `@pulumi-labs/modal`                           | `@pulumi-labs/better-uptime`                    |
| python_package         | `pulumi_labs_<p with _>`                         | `pulumi_labs_nvidia_aicr`                                    | `pulumi_labs_modal`                            | `pulumi_labs_better_uptime`                     |
| pypi_name              | `pulumi-labs-<p>`                                | `pulumi-labs-nvidia-aicr`                                    | `pulumi-labs-modal`                            | `pulumi-labs-better-uptime`                     |
| dotnet_pascal          | each hyphen part capitalised, joined             | `NvidiaAicr`                                                 | `Modal`                                        | `BetterUptime`                                  |
| dotnet_root_namespace  | fixed                                            | `Pulumi.Labs`                                                | `Pulumi.Labs`                                  | `Pulumi.Labs`                                   |
| dotnet_package / csproj| `Pulumi.Labs.<Pascal>`                           | `sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj`                   | `sdk/dotnet/Pulumi.Labs.Modal.csproj`          | `sdk/dotnet/Pulumi.Labs.BetterUptime.csproj`    |
| java_base_package      | fixed                                            | `com.pulumi.labs`                                            | `com.pulumi.labs`                              | `com.pulumi.labs`                               |
| maven_coordinates      | `com.pulumi.labs:<p>`                            | `com.pulumi.labs:nvidia-aicr`                                | `com.pulumi.labs:modal`                        | `com.pulumi.labs:better-uptime`                 |
| pom_name               | `pulumi-labs-<p>`                                | `pulumi-labs-nvidia-aicr`                                    | `pulumi-labs-modal`                            | `pulumi-labs-better-uptime`                     |
| binary                 | `pulumi-resource-<p>`                            | `pulumi-resource-nvidia-aicr`                                | `pulumi-resource-modal`                        | `pulumi-resource-better-uptime`                 |
| ldflags_version_var    | `<go_module>/provider/pkg/version.Version`       |                                                              |                                                |                                                 |
| publisher              | fixed                                            | `Pulumi Labs`                                                |                                                |                                                 |
| plugin_download_url    | `github://api.github.com/pulumi-labs/pulumi-<p>` |                                                              |                                                |                                                 |
| esc_environment        | `github-secrets/pulumi-labs-pulumi-<p>`          | `github-secrets/pulumi-labs-pulumi-nvidia-aicr`              |                                                |                                                 |

`names.py` also prints `repo_url`, `cmd_dir`, `schema_path`, `go_sdk_dir` and
`maven_artifact`.

## What check.py enforces from this table

Committed `provider/cmd/pulumi-resource-<p>/schema.json` must have: `name`,
`publisher` (`Pulumi Labs`), `repository` (`https://github.com/pulumi-labs/pulumi-<p>`),
`pluginDownloadURL`, `language.go.importBasePath`, `language.nodejs.packageName`,
`language.python.packageName`, `language.csharp.rootNamespace` (`Pulumi.Labs`)
and `language.java.basePackage` (`com.pulumi.labs`). The generated SDKs must
then exist as `sdk/dotnet/Pulumi.Labs.<Pascal>.csproj` and `sdk/go/<go_pkg>/`.

Old names that fail the namespace grep (matched with word boundaries, so
`pulumi_labs_<p>`, `Pulumi.Labs.<Pascal>` and `com.pulumi.labs` are fine):
`pulumi/autoregistry`, `github.com/pulumi/pulumi-<p>`, `@pulumi/<p>`,
`pulumi_<p>`, `Pulumi.<Pascal>` (not preceded by `Labs.`), `com.pulumi.<p>`
(also with hyphens removed or turned into underscores).

The pattern is deliberately strict. If a historical mention (for example in
CHANGELOG.md) trips it, reword the mention rather than weakening the check.
