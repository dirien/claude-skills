<!-- FOR AI AGENTS - Human readability is a side effect, not a goal -->
<!-- Managed by agent: keep sections and order; edit content, not structure -->

# AGENTS.md

**Precedence:** the **closest `AGENTS.md`** to the files you're changing wins. Root holds global defaults only.

## Overview

Pulumi IaC and Flux CD GitOps skills for Claude Code. Seven skills: TypeScript, Go, Python, Neo (AI agent), CLI reference, Flux CLI, Flux Operator CLI.
Distributed via Tessl tile (`tile.json`) and `.claude-plugin/marketplace.json`.

## File Map

| Path | Purpose |
|------|---------|
| `pulumi-{typescript,go,python}/` | Language-specific Pulumi skills (`SKILL.md` + `references/` + `evals/`) |
| `pulumi-neo/` | Pulumi Neo AI agent skill (`SKILL.md` + `references/` + `scripts/` + `evals/`) |
| `pulumi-cli/` | Pulumi CLI reference (`SKILL.md` + `references/` + `evals/`) |
| `flux-cli/` | Flux CD CLI reference (`SKILL.md` + `references/`) |
| `flux-operator-cli/` | Flux Operator CLI reference (`SKILL.md` + `references/`) |
| `tile.json` | Tessl tile config (name, version, skill paths) |
| `.claude-plugin/marketplace.json` | Plugin marketplace manifest |
| `.github/workflows/publish-tile.yml` | Publishes tile on push to `main` via `tesslio/publish` |
| `CLAUDE.md` | Project instructions for Claude Code |

## Conventions

- Keep `SKILL.md` concise — detailed content goes in `references/`
- "When to use" belongs in frontmatter `description`, not the body
- No auxiliary files (README.md, CHANGELOG.md) inside skill folders
- Sync versions across `tile.json` and `marketplace.json`

## Boundaries

| | Rule |
|---|------|
| **Always** | Keep SKILL.md focused; detailed docs in `references/` |
| **Always** | Sync versions in `tile.json` and `marketplace.json` |
| **Ask first** | Adding new skills, changing versions, modifying CI |
| **Never** | Commit secrets in skill content |
| **Never** | Add auxiliary files inside skill folders |
| **Never** | Push directly to main |

## When instructions conflict
The nearest `AGENTS.md` wins. Explicit user prompts override files. See also `CLAUDE.md`.
