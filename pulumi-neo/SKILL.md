---
name: pulumi-neo
description: This skill should be used when the user asks to "create Neo task", "use Pulumi Neo", "analyze infrastructure with Neo", "automate infrastructure with AI", or mentions conversational infrastructure management through natural language.
version: 1.3.0
---

# Pulumi Neo Skill

Pulumi Neo is an AI agent for platform engineers that enables conversational infrastructure management through natural language.

## Prerequisites

- **Pulumi Cloud account** with Neo access
- **PULUMI_ACCESS_TOKEN** environment variable set with your Personal Access Token
- **Organization**: Required for all Neo API calls

## Detecting Organization

```bash
# Get current Pulumi organization from CLI
pulumi org get-default

# If no default org or using self-managed backend, ask user for organization name
```

If `pulumi org get-default` returns an error or shows a non-cloud backend, prompt the user for their Pulumi Cloud organization name.

## Quick Start

```bash
# 1. Set your token
export PULUMI_ACCESS_TOKEN=<your-token>

# 2. Get your organization
pulumi org get-default
# Or specify manually: --org your-org-name

# 3. Ask Neo something (non-blocking)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TASK=$(curl -s -X POST "https://api.pulumi.com/api/preview/agents/YOUR_ORG/tasks" \
  -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  -H "Accept: application/vnd.pulumi+8" \
  -H "Content-Type: application/json" \
  -d "{\"message\":{\"type\":\"user_message\",\"content\":\"How many stacks do I have?\",\"timestamp\":\"$TIMESTAMP\",\"entity_diff\":{\"add\":[],\"remove\":[]}}}")
echo $TASK

# 4. Get the response (wait a few seconds first)
TASK_ID=$(echo $TASK | jq -r '.taskId')
curl -s "https://api.pulumi.com/api/preview/agents/YOUR_ORG/tasks/$TASK_ID/events" \
  -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  -H "Accept: application/vnd.pulumi+8" | jq '.events[-1].eventBody.content'
```

## Claude Code Integration

### Option 1: Direct API Calls (Always Available)

Use curl to interact with the Neo API directly:

```bash
# Set your token
export PULUMI_ACCESS_TOKEN=<your-token>

# Create a task
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
curl -s -X POST "https://api.pulumi.com/api/preview/agents/<org>/tasks" \
  -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  -H "Accept: application/vnd.pulumi+8" \
  -H "Content-Type: application/json" \
  -d "{
    \"message\": {
      \"type\": \"user_message\",
      \"content\": \"Your message here\",
      \"timestamp\": \"$TIMESTAMP\",
      \"entity_diff\": {\"add\": [], \"remove\": []}
    }
  }"

# Get task events
curl -s "https://api.pulumi.com/api/preview/agents/<org>/tasks/<task-id>/events" \
  -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  -H "Accept: application/vnd.pulumi+8"
```

### Option 2: Python Script

**IMPORTANT:** Always use `--no-poll` in Claude Code to prevent blocking.

```bash
python <skill-base-directory>/scripts/neo_task.py --org <org> --message "Your message" --no-poll
```

In Claude Code, use the full absolute path provided in the skill context.

### Option 3: MCP Tools (Requires Pulumi MCP Server)

If you have the Pulumi MCP server installed and configured:
- `mcp__pulumi__neo-bridge`
- `mcp__pulumi__neo-get-tasks`
- `mcp__pulumi__neo-continue-task`

**WARNING:** Never run the Python script without `--no-poll` in Claude Code - the polling loop will block indefinitely.

## Using the Python Script

The script handles Neo task creation, polling, and management:

```bash
# Create a task and poll for updates (interactive/terminal use)
python scripts/neo_task.py --org <org-name> --message "Help me optimize my Pulumi stack"

# Create task without polling (CI/CD or programmatic use)
python scripts/neo_task.py --org <org-name> --message "Analyze this" --no-poll

# Create task with stack context
python scripts/neo_task.py --org <org-name> \
  --message "Analyze this stack" \
  --stack-name prod --stack-project my-infra --no-poll

# Create task with repository context
python scripts/neo_task.py --org <org-name> \
  --message "Review this infrastructure code" \
  --repo-name my-repo --repo-org my-github-org --no-poll

# List existing tasks
python scripts/neo_task.py --org <org-name> --list

# Fetch current events (single request, no polling)
python scripts/neo_task.py --org <org-name> --task-id <task-id> --get-events

# Poll an existing task for updates (interactive)
python scripts/neo_task.py --org <org-name> --task-id <task-id>

# Send approval for a pending request
python scripts/neo_task.py --org <org-name> --task-id <task-id> --approve

# Cancel a pending request
python scripts/neo_task.py --org <org-name> --task-id <task-id> --cancel
```

## Neo Task Workflow

### 1. Creating Tasks

Tasks are created with a natural language message describing what you want Neo to do:

- **Infrastructure analysis**: "Analyze my production stack for security issues"
- **Maintenance operations**: "Help me upgrade my Kubernetes cluster"
- **Configuration changes**: "Add monitoring to my Lambda functions"
- **Multi-step workflows**: "Set up a complete CI/CD pipeline for this project"

### 2. Entity Context

Provide context to Neo by attaching entities:

| Entity Type | Use Case |
|-------------|----------|
| `stack` | Reference a Pulumi stack (name + project) |
| `repository` | Reference a code repository (name + org + forge) |
| `pull_request` | Reference a PR (number + merged status + repository) |
| `policy_issue` | Reference a governance policy issue (id) |

### 3. Task Status

| Status | Description |
|--------|-------------|
| `running` | Neo is actively processing the task |
| `idle` | Task is waiting for input or has finished processing |

**Note:** Task completion and approval requests are determined by examining events, not task status.

### 4. Approval Flow

When Neo requires confirmation for an operation:

1. Task status remains `running` or transitions to `idle`
2. An `agentResponse` event contains `tool_calls` with an `approval_request` tool
3. The `approval_request_id` is found in the tool call parameters
4. User reviews the proposed changes
5. Send approval or cancellation via the API

**Detecting approvals:** Check events for `eventBody.tool_calls` containing approval requests rather than relying on task status.

## Common Workflows

### Analyze Infrastructure

```bash
python scripts/neo_task.py --org myorg \
  --message "What security improvements can I make to my AWS infrastructure?" \
  --stack-name prod --stack-project aws-infra
```

### Fix Policy Violations

```bash
python scripts/neo_task.py --org myorg \
  --message "Help me fix the policy violations in my production stack"
```

### Generate Pulumi Code

```bash
python scripts/neo_task.py --org myorg \
  --message "Create a new Pulumi TypeScript project for a containerized web app on AWS ECS"
```

### Review Pull Request

```bash
python scripts/neo_task.py --org myorg \
  --message "Review the infrastructure changes in this PR" \
  --repo-name infra --repo-org myorg --repo-forge github
```

## Best Practices

### Clear Instructions
- Be specific about what you want Neo to do
- Include relevant context (stack names, regions, requirements)
- Specify constraints (budget, compliance requirements)

### Entity Context
- Always attach relevant stack or repository entities
- This helps Neo understand the scope of your request

### Approval Workflow
- Review Neo's proposed changes carefully before approving
- Neo generates pull requests for infrastructure changes
- Use the preview feature to understand impact before deployment

## Error Recovery

### Token Issues
If the Python script reports token not set but it is:
```bash
# Verify token is exported (not just set)
export PULUMI_ACCESS_TOKEN="$PULUMI_ACCESS_TOKEN"

# Test with curl directly
curl -s -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  https://api.pulumi.com/api/user
```

### API Errors
| Error | Cause | Solution |
|-------|-------|----------|
| 401 | Invalid/missing token | Check `PULUMI_ACCESS_TOKEN` is set and valid |
| 404 | Wrong org or endpoint | Verify org name with `pulumi org get-default` |
| 409 | Task busy | Wait for current operation to complete |

### Script Hangs
If the script hangs, you forgot `--no-poll`. Kill it with Ctrl+C and add `--no-poll`:
```bash
python neo_task.py --org myorg --message "..." --no-poll
```

## Troubleshooting

### Authentication Errors (401)
```bash
# Verify token is set
echo $PULUMI_ACCESS_TOKEN

# Test authentication
curl -H "Authorization: token $PULUMI_ACCESS_TOKEN" \
  https://api.pulumi.com/api/user
```

### Organization Not Found (404)
- Verify organization name with `pulumi org get-default`
- Ensure your token has access to the organization

### Cannot Respond While Request Pending (409)
- Wait for Neo to finish processing before sending new messages
- Poll for status updates before responding

## API Reference

Base URL: `https://api.pulumi.com/api/preview/agents`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/{org}/tasks` | POST | Create task |
| `/{org}/tasks` | GET | List tasks |
| `/{org}/tasks/{id}` | GET | Get task |
| `/{org}/tasks/{id}/events` | GET | Get events |
| `/{org}/tasks/{id}` | POST | Send message/approval |

Required headers:
- `Authorization: token $PULUMI_ACCESS_TOKEN`
- `Accept: application/vnd.pulumi+8`
- `Content-Type: application/json`

See [references/pulumi-neo-api.md](references/pulumi-neo-api.md) for full details.
