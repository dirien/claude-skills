# Claude Code Skills

Custom [Claude Code](https://claude.ai/claude-code) skills for infrastructure as code development.

## Available Skills

| Skill | Description |
|-------|-------------|
| [pulumi-typescript](./pulumi-typescript/) | Pulumi IaC with TypeScript, Pulumi Cloud & ESC |
| [pulumi-go](./pulumi-go/) | Pulumi IaC with Go, Pulumi Cloud & ESC |
| [pulumi-python](./pulumi-python/) | Pulumi IaC with Python, Pulumi Cloud & ESC |

## Features

All Pulumi skills include:

- **Pulumi ESC Integration** - Centralized secrets and configuration management
- **OIDC Authentication** - Dynamic credentials for AWS, Azure, and GCP
- **Multi-Language Components** - Create components consumable from any Pulumi language
- **Cloud Best Practices** - AWS, Azure (azure-native first), and GCP patterns
- **Stack References** - Cross-stack dependency management

## Installation

### Option 1: Plugin Marketplace (Recommended)

Add the marketplace and install skills:

```bash
# Add the marketplace
/plugin marketplace add dirien/claude-skills

# Install desired skills
/plugin install pulumi-typescript@pulumi-skills
/plugin install pulumi-go@pulumi-skills
/plugin install pulumi-python@pulumi-skills
```

### Option 2: Symlink to ~/.claude/skills

```bash
# Clone the repo
git clone https://github.com/dirien/claude-skills.git

# Symlink skills to Claude's skills directory
mkdir -p ~/.claude/skills
ln -s $(pwd)/claude-skills/pulumi-typescript ~/.claude/skills/pulumi-typescript
ln -s $(pwd)/claude-skills/pulumi-go ~/.claude/skills/pulumi-go
ln -s $(pwd)/claude-skills/pulumi-python ~/.claude/skills/pulumi-python
```

### Option 3: Project-level skills

Copy skills to your project's `.claude/skills/` directory to share with your team.

## Skill Structure

Each skill follows this structure:

```
pulumi-{language}/
├── SKILL.md                           # Main skill definition
└── references/
    ├── pulumi-esc.md                  # ESC patterns and commands
    ├── pulumi-patterns.md             # Infrastructure patterns
    ├── pulumi-{language}.md           # Language-specific guidance
    ├── pulumi-best-practices-aws.md   # AWS best practices
    ├── pulumi-best-practices-azure.md # Azure best practices (azure-native)
    └── pulumi-best-practices-gcp.md   # GCP best practices
```

## Usage

Once installed, the skills automatically activate when you work on Pulumi projects. For example:

> "Create a Pulumi TypeScript project with ESC OIDC authentication to AWS"

> "Set up a secure S3 bucket with encryption using Pulumi Python"

> "Create a multi-language component for a VPC in Go"

## License

MIT
