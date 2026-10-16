# Toolchain

`scripts/bootstrap.py` installs everything the export procedure needs that a
bare workstation lacks: Go, the Pulumi CLI, `jq`, `gitleaks` and GNU `make`, and (profile `full`, the default) Node.js 22
LTS, the .NET 8 SDK, Temurin JDK 17 and Gradle 9, so `verify.py` can build every
SDK locally. It needs only python3 (3.8+), network access and a writable
`$HOME`; no root, no compiler. `--profile minimal` installs only the first five.
`python3` itself must provide `venv` with `pip` (the Python SDK build uses it);
bootstrap does not install Python.

```
python3 scripts/bootstrap.py                  # install, then print the PATH line
eval "$(python3 scripts/bootstrap.py)"        # or install and apply in one go
python3 scripts/bootstrap.py --check          # report only, no downloads; exit 1 if anything is missing
python3 scripts/bootstrap.py --only gitleaks  # a single tool (go, pulumi, jq, gitleaks, make, node, dotnet, java, gradle)
python3 scripts/bootstrap.py --profile minimal  # skip node, dotnet, java, gradle
```

Each shell session needs the PATH line (`export PATH="<prefix>/bin:$PATH"`) and
the extra exports that follow it (`JAVA_HOME`, `DOTNET_ROOT`,
`DOTNET_CLI_TELEMETRY_OPTOUT=1`, `DOTNET_NOLOGO=1`, and
`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` only when no libicu is installed):
stdout carries those lines first, then `# tool: version` comments; progress is
on stderr. Tools live in `$HOME/.local/pulumi-labs-export/` (`opt/` holds the
unpacked versions, `bin/` the symlinks). Re-running skips tools already at the
pinned version. Any failure exits 1 with the tool and reason; stop and report
it. Nothing prints environment variables or tokens.

## Pins and verification

| Tool     | Version | Source                                              | SHA-256 from                                   |
|----------|---------|-----------------------------------------------------|------------------------------------------------|
| Go       | 1.27.2  | `go.dev/dl` tarball (= `.go-version` in autoregistry) | pinned in `bootstrap.py` (from go.dev JSON)    |
| Pulumi   | read from `archetype/mise.toml.tmpl` | GitHub release `pulumi-v<ver>-linux-<x64\|arm64>.tar.gz` | `pulumi-<ver>-checksums.txt` in the release |
| jq       | 1.8.2   | jqlang/jq release binary                            | `sha256sum.txt` in the release                 |
| gitleaks | 8.30.1  | gitleaks/gitleaks release tarball                   | `gitleaks_<ver>_checksums.txt` in the release  |
| Node.js  | 22.23.3 (22 LTS) | `nodejs.org/dist/v22.23.3/node-v22.23.3-linux-<x64\|arm64>.tar.gz` | `SHASUMS256.txt` in the same dir |
| .NET SDK | 8.0.426 | `builds.dotnet.microsoft.com/dotnet/Sdk/8.0.426/dotnet-sdk-8.0.426-linux-<x64\|arm64>.tar.gz` | **sha512** in `release-metadata/8.0/releases.json` |
| Temurin JDK | 17.0.20.1+1 | `github.com/adoptium/temurin17-binaries` release asset `OpenJDK17U-jdk_<x64\|aarch64>_linux_hotspot_17.0.20.1_1.tar.gz` | `<asset>.sha256.txt` next to it |
| Gradle   | 9.8.1 | `services.gradle.org/distributions/gradle-9.8.1-bin.zip` (extracted with `zipfile`) | `<asset>.sha256` (bare hash) |
| make     | system, else Debian bookworm 4.3-4.1 | see below                      | pinned in `bootstrap.py` for the .deb          |

linux/amd64 and linux/arm64 are supported. Every Pulumi binary is linked into
`bin/` (the CLI and the `pulumi-language-*` hosts), so `gen-sdk` works for all
five languages. A checksum mismatch aborts that tool before anything is
unpacked.

`dotnet build` of the generated `net6.0` csproj works with the 8.0 SDK: NuGet
restores the net6.0 targeting pack on demand (an end-of-support warning is
expected). Downloads use concrete file URLs; the bare CDN roots answer HTTP 400,
which is normal. An unreachable host fails with the URL, host and HTTP status.

## make without root or a compiler

1. `make` already on PATH (or in `bin/`): used as is.
2. `apt-get download make` in a temp dir (works without root when the apt
   lists exist), then the binary is unpacked from the .deb.
3. The pinned Debian bookworm .deb from `deb.debian.org`.

The .deb is read with the standard library (own `ar` parser plus `tarfile`).
`data.tar.xz`, `.gz` and `.bz2` work; `data.tar.zst` cannot be read by the
standard library, so route 2 falls through to route 3 with that reason.

## Bumping a pin

Edit the constants at the top of `bootstrap.py` (Go also needs the new
hashes from `https://go.dev/dl/?mode=json&include=all`; the Pulumi version
changes only in `archetype/mise.toml.tmpl`), then run the tests. A test fails
when the Go pin differs from `.go-version` in pulumi/autoregistry.

## Blocked hosts

Downloads need go.dev, github.com (release assets, including Temurin),
nodejs.org, builds.dotnet.microsoft.com, services.gradle.org and, for the make
fallback, deb.debian.org. Builds then need api.nuget.org, registry.npmjs.org,
pypi.org, repo.maven.apache.org, plugins.gradle.org and proxy.golang.org. A firewall refusal shows as `HTTP 403` with the
policy text in the error. Do not work around it; report the host.
