# Pulumi Neo API Reference

## Base URL

```
https://api.pulumi.com/api/preview/agents/
```

## Authentication

All requests require:
```
Authorization: token $PULUMI_ACCESS_TOKEN
Accept: application/vnd.pulumi+8
Content-Type: application/json
```

## Endpoints

### Create Task

**POST** `/api/preview/agents/{orgName}/tasks`

Creates a new Neo agent task.

**Request Body:**
```json
{
  "message": {
    "type": "user_message",
    "content": "Help me optimize my Pulumi stack",
    "timestamp": "2025-01-15T10:30:00Z",
    "entity_diff": {
      "add": [
        {"type": "stack", "name": "my-stack", "project": "my-project"}
      ],
      "remove": []
    }
  }
}
```

**Response (201 Created):**
```json
{
  "taskId": "task_abc123"
}
```

### Get Task Metadata

**GET** `/api/preview/agents/{orgName}/tasks/{taskID}`

Retrieves task details and current status.

**Response (200 OK):**
```json
{
  "id": "task_abc123",
  "name": "Task name",
  "status": "running",
  "createdAt": "2025-01-15T00:00:00Z",
  "entities": [
    {"type": "stack", "id": "my-stack"}
  ]
}
```

### List Tasks

**GET** `/api/preview/agents/{orgName}/tasks`

Lists all tasks for an organization.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pageSize` | int | 100 | Results per page (1-1000) |
| `continuationToken` | string | - | Pagination token |

**Response (200 OK):**
```json
{
  "tasks": [
    {
      "id": "task_abc123",
      "name": "Task name",
      "status": "completed",
      "createdAt": "2025-01-15T00:00:00Z"
    }
  ],
  "continuationToken": "next_page_token"
}
```

### Respond to Task

**POST** `/api/preview/agents/{orgName}/tasks/{taskID}`

Sends user input to an ongoing task.

**User Message:**
```json
{
  "event": {
    "type": "user_message",
    "content": "Yes, please proceed with the changes",
    "timestamp": "2025-01-15T10:35:00Z",
    "entity_diff": {"add": [], "remove": []}
  }
}
```

**User Confirmation (Approval):**
```json
{
  "event": {
    "type": "user_confirmation",
    "approval_request_id": "req_123",
    "timestamp": "2025-01-15T10:35:00Z"
  }
}
```

**User Cancel:**
```json
{
  "event": {
    "type": "user_cancel",
    "timestamp": "2025-01-15T10:35:00Z"
  }
}
```

**Response:** `202 Accepted`

### List Task Events

**GET** `/api/preview/agents/{orgName}/tasks/{taskID}/events`

Retrieves the event stream for a task.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pageSize` | int | 100 | Results per page (1-1000) |
| `continuationToken` | string | - | Pagination token |

**Response (200 OK):**
```json
{
  "events": [
    {
      "id": "event_123",
      "type": "agentResponse",
      "eventBody": {
        "content": "I'll analyze your infrastructure...",
        "timestamp": "2025-01-15T10:31:00Z"
      }
    },
    {
      "id": "event_124",
      "type": "userInput",
      "eventBody": {
        "content": "Please proceed",
        "timestamp": "2025-01-15T10:35:00Z"
      }
    }
  ],
  "continuationToken": "next_page_token"
}
```

## Entity Types

### Stack
```json
{
  "type": "stack",
  "name": "my-stack",
  "project": "my-project"
}
```

### Repository
```json
{
  "type": "repository",
  "name": "my-repo",
  "org": "my-org",
  "forge": "github"
}
```

Supported forges: `github`, `gitlab`, `bitbucket`

### Pull Request
```json
{
  "type": "pull_request",
  "number": 123,
  "merged": false,
  "repository": {
    "name": "my-repo",
    "org": "my-org",
    "forge": "github"
  }
}
```

### Policy Issue
```json
{
  "type": "policy_issue",
  "id": "issue_123"
}
```

## Event Types

### Agent Response
Neo's response to user input:
```json
{
  "type": "agentResponse",
  "eventBody": {
    "content": "Analysis complete. I found 3 security issues...",
    "timestamp": "2025-01-15T10:31:00Z"
  }
}
```

### Approval Request
Neo requesting permission to proceed:
```json
{
  "type": "approvalRequest",
  "eventBody": {
    "approval_request_id": "req_123",
    "description": "Create pull request with infrastructure changes",
    "timestamp": "2025-01-15T10:32:00Z"
  }
}
```

### User Input
User messages in the event stream:
```json
{
  "type": "userInput",
  "eventBody": {
    "content": "Please proceed with the changes",
    "timestamp": "2025-01-15T10:35:00Z"
  }
}
```

## Error Codes

| Code | Description |
|------|-------------|
| 400 | Invalid request format or missing required fields |
| 401 | Authentication token invalid or missing |
| 403 | Insufficient permissions for the organization |
| 404 | Task or organization not found |
| 409 | Cannot respond while a previous request is pending |

## Rate Limits

- API calls are rate-limited per organization
- Implement exponential backoff for 429 responses
- Poll events with reasonable intervals (5-10 seconds)

## Pagination

Use `continuationToken` for paginating through results:

```python
token = None
all_events = []

while True:
    params = {"pageSize": 100}
    if token:
        params["continuationToken"] = token

    response = requests.get(url, headers=headers, params=params)
    data = response.json()

    all_events.extend(data.get("events", []))
    token = data.get("continuationToken")

    if not token:
        break
```
