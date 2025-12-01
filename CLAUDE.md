# Claude Code Skills Repository

This repository contains Pulumi infrastructure-as-code skills for Claude Code.

## Repository Structure

```
claude-skills/
├── .claude-plugin/
│   └── marketplace.json      # Plugin marketplace definition
├── pulumi-typescript/        # TypeScript Pulumi skill
├── pulumi-go/                # Go Pulumi skill
├── pulumi-python/            # Python Pulumi skill
├── pulumi-neo/               # Pulumi Neo AI agent skill
└── CLAUDE.md                 # This file
```

## Skills Overview

Each skill provides Pulumi IaC guidance with:
- Pulumi ESC (Environments, Secrets, Configuration) integration
- OIDC authentication patterns for AWS, Azure, GCP
- Multi-language component development
- Cloud provider best practices

## Skill Structure

Each skill follows this pattern:
```
pulumi-{language}/
├── SKILL.md              # Main skill with frontmatter (name, description)
└── references/           # Detailed reference documentation
    ├── pulumi-esc.md
    ├── pulumi-patterns.md
    ├── pulumi-{language}.md
    └── pulumi-best-practices-{cloud}.md
```

## Development Guidelines

When modifying skills:
1. Keep SKILL.md concise - move detailed content to `references/`
2. "When to use" info belongs in the frontmatter `description`, not the body
3. Use `references/` for cloud-specific patterns and detailed documentation
4. Do not add auxiliary files (README.md, CHANGELOG.md) inside skill folders

## Marketplace Distribution

Skills are distributed via the `.claude-plugin/marketplace.json` file. Users install via:
```bash
/plugin marketplace add <owner>/claude-skills
/plugin install pulumi-typescript@pulumi-skills
```
