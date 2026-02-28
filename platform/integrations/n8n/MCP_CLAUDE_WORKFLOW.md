# n8n MCP Integration for Claude Code

Claude Code uses n8n-mcp + n8n-skills for workflow orchestration.

---

## GitHub Repositories

| Repository | URL | Purpose |
|------------|-----|---------|
| **n8n-mcp** | https://github.com/czlonkowski/n8n-mcp | MCP server enabling Claude to read/modify/test n8n workflows |
| **n8n-skills** | https://github.com/czlonkowski/n8n-skills | Skill pack integration patterns for n8n workflows |

---

## Installation

### 1. Install n8n-mcp

```bash
# Clone the repository
git clone https://github.com/czlonkowski/n8n-mcp
cd n8n-mcp

# Install dependencies
npm install

# Build (if required)
npm run build
```

### 2. Configure Environment

Create a `.env` file or set environment variables:

```bash
N8N_HOST=http://localhost:5678
N8N_API_KEY=your_n8n_api_key_here
```

---

## Claude Code Configuration

Add the n8n MCP server to your `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "n8n": {
      "command": "npx",
      "args": ["n8n-mcp"],
      "env": {
        "N8N_HOST": "http://localhost:5678",
        "N8N_API_KEY": "${N8N_API_KEY}"
      }
    }
  }
}
```

**Alternative: Node direct execution**

```json
{
  "mcpServers": {
    "n8n": {
      "command": "node",
      "args": ["/path/to/n8n-mcp/dist/index.js"],
      "env": {
        "N8N_HOST": "http://localhost:5678",
        "N8N_API_KEY": "${N8N_API_KEY}"
      }
    }
  }
}
```

---

## Available MCP Operations

Once configured, Claude Code can:

| Operation | Description |
|-----------|-------------|
| `list_workflows` | List all n8n workflows |
| `get_workflow` | Get workflow details by ID |
| `execute_workflow` | Trigger workflow execution |
| `create_workflow` | Create new workflow |
| `update_workflow` | Modify existing workflow |
| `activate_workflow` | Enable workflow |
| `deactivate_workflow` | Disable workflow |

---

## n8n-skills Integration

The [n8n-skills](https://github.com/czlonkowski/n8n-skills) repository provides:

1. **Workflow Templates** - Pre-built patterns for common automation tasks
2. **Skill Pack Patterns** - Integration patterns for Aspire skill packs
3. **Best Practices** - Security and governance patterns for n8n workflows

### Applying Skill Patterns

Reference the n8n-skills repository when creating workflows for:

- Finance synchronization (FIN_DAILY_SYNC)
- Mail monitoring (MAIL_DELIVERABILITY_MONITOR)
- DNS verification (MAIL_DNS_CHECK_SCHEDULE)
- IMAP synchronization (MAIL_IMAP_SYNC_SCHEDULE)
- Incident escalation (MAIL_INCIDENT_ESCALATION)

---

## Governance

**CRITICAL: n8n is request-only plumbing - it NEVER decides.**

Per Aspire Law #7 (Tools Are Hands, Not Brains):

- n8n workflows are **request-triggered only**
- No autonomous execution without orchestrator approval
- All decisions are made by the LangGraph orchestrator
- Webhooks require authentication
- All workflow executions generate receipts

### Compliance Checklist

Before deploying any n8n workflow:

- [ ] Workflow is request-triggered (not autonomous)
- [ ] No decision logic in n8n (orchestrator decides)
- [ ] Webhook endpoints require authentication
- [ ] Error handling configured (no silent failures)
- [ ] Execution generates receipt via orchestrator callback

---

## Verification

Test the MCP connection:

```bash
# In Claude Code, ask:
"List all n8n workflows"

# Expected: Claude uses mcp__n8n__list_workflows and returns workflow list
```

---

## Related Documentation

- **n8n Setup:** `platform/integrations/n8n/SETUP_SELF_HOSTED.md`
- **Security Model:** `platform/integrations/n8n/SECURITY_MODEL.md`
- **Workflow Templates:** `platform/integrations/n8n/templates/workflows/`
- **Hardening Checklist:** `platform/integrations/n8n/templates/WORKFLOW_HARDENING_CHECKLIST.md`

---

## Phase 0B Tasks

| Task ID | Description | Status |
|---------|-------------|--------|
| PHASE0B-TASK-N8N-006 | Install n8n-mcp for Claude Code | Pending |
| PHASE0B-TASK-N8N-007 | Review n8n-skills Patterns | Pending |

---

**Last Updated:** 2026-02-04
