# Toolchain

`scripts/bootstrap.py` installs everything the export procedure needs that a
bare workstation lacks: Go, the Pulumi CLI, `jq`, `gitleaks` and GNU `make`.
It needs only python3 (3.8+), network access and a writable `$HOME`; no root,
no compiler. Node, dotnet and java are not installed: the carved repo's CI
builds those SDKs.

```
python3 scripts/bootstrap.py                  # install, then print the PATH line
eval "$(python3 scripts/bootstrap.py)"        # or install and apply in one go
python3 scripts/bootstrap.py --check          # report only, no downloads; exit 1 if anything is missing
python3 scripts/bootstrap.py --only gitleaks  # a single tool (go, pulumi, jq, gitleaks, make)
```

Each shell session needs the PATH line (`export PATH="<prefix>/bin:$PATH"`):
stdout carries that line first, then `# tool: version` comments; progress is
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
| make     | system, else Debian bookworm 4.3-4.1 | see below                      | pinned in `bootstrap.py` for the .deb          |

linux/amd64 and linux/arm64 are supported. Every Pulumi binary is linked into
`bin/` (the CLI and the `pulumi-language-*` hosts), so `gen-sdk` works for all
five languages. A checksum mismatch aborts that tool before anything is
unpacked.

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

Downloads need go.dev, github.com (release assets) and, for the make
fallback, deb.debian.org. A firewall refusal shows as `HTTP 403` with the
policy text in the error. Do not work around it; report the host.
