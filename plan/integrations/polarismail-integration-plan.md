# PolarisMail Integration Plan — Enterprise Grade
## Aspire Governed Execution Platform

**Version:** 1.0
**Date:** 2026-02-11
**Author:** Aspire Engineering
**Status:** APPROVED FOR IMPLEMENTATION
**Phase:** Phase 2 (Founder MVP) — Tasks PHASE2-TASK-005 through PHASE2-TASK-008 + PHASE2-TASK-041 through PHASE2-TASK-044

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Overview](#2-architecture-overview)
3. [PolarisMail API Reference](#3-polarismail-api-reference)
4. [Skill Pack Design: Eli (Inbox)](#4-skill-pack-eli-inbox)
5. [Skill Pack Design: mail_ops_desk (Admin)](#5-skill-pack-mail_ops_desk-admin)
6. [Database Schema](#6-database-schema)
7. [Authentication & Credential Management](#7-authentication--credential-management)
8. [Execution Pipeline (Per-Action)](#8-execution-pipeline-per-action)
9. [Risk Tier Classification](#9-risk-tier-classification)
10. [Receipt Specifications](#10-receipt-specifications)
11. [Error Handling & Circuit Breakers](#11-error-handling--circuit-breakers)
12. [Tenant Isolation (RLS)](#12-tenant-isolation-rls)
13. [State Machine Definitions](#13-state-machine-definitions)
14. [Testing Strategy](#14-testing-strategy)
15. [Production Gates Checklist](#15-production-gates-checklist)
16. [Deployment Sequence](#16-deployment-sequence)
17. [File Structure](#17-file-structure)
18. [Appendix: Raw API Payloads](#18-appendix-raw-api-payloads)

---

## 1. Executive Summary

### What
Direct integration between Aspire and PolarisMail's JSON API — bypassing WHMCS entirely. PolarisMail provides white-label email hosting (IMAP/SMTP/Webmail). Aspire provisions and manages mailboxes as part of its bundled subscription.

### Why
- Aspire already has billing (Stripe) — WHMCS is redundant middleware
- Direct API integration means fewer moving parts, lower cost, tighter governance
- Every email operation flows through Aspire's Trust Spine (receipts, capability tokens, RLS)

### Business Model
- Aspire pays PolarisMail as a reseller (wholesale pricing)
- Customers get email included in their Aspire Suite subscription
- Each Suite gets a custom domain; each Office (human) gets a mailbox

### Two Skill Packs
1. **Eli (Inbox)** — User-facing email operations (read, draft, send, triage)
2. **mail_ops_desk** — Internal admin operations (domain provisioning, mailbox lifecycle, DNS verification)

### Governance
- All 7 Immutable Laws enforced
- Every API call produces a receipt
- Capability tokens required for every operation
- RLS ensures zero cross-tenant data leakage
- Credential secrets never exposed to users or logs

---

## 2. Architecture Overview

### 3-Layer Stack Integration

```
┌──────────────────────────────────────────────────────────────────┐
│                    INTELLIGENCE LAYER                             │
│                                                                  │
│  LangGraph Orchestrator (Single Brain)                           │
│       │                                                          │
│       ├── Eli Subgraph (Inbox skill pack)                        │
│       │     └── email.read, email.draft, email.send, email.triage│
│       │                                                          │
│       └── mail_ops_desk Subgraph (Admin skill pack)              │
│             └── domain.add, domain.verify, mailbox.create, etc.  │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│                    TRUST SPINE                                    │
│                                                                  │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────┐ │
│  │ Capability   │  │ Policy       │  │ Receipt Generation      │ │
│  │ Token Mint   │  │ Engine       │  │ (append-only ledger)    │ │
│  │ (<60s, scoped│  │ (risk tier   │  │                         │ │
│  │  per-tenant) │  │  enforcement)│  │ Receipts on SUCCESS,    │ │
│  └──────┬──────┘  └──────┬───────┘  │ DENIAL, and FAILURE     │ │
│         │                │          └─────────────────────────┘ │
│         │                │                                       │
│  ┌──────┴────────────────┴──────────────────────────────────┐   │
│  │              PII Redaction (Presidio DLP)                 │   │
│  │  Email addresses → <EMAIL_REDACTED>                       │   │
│  │  Passwords → <PASSWORD_REDACTED>                          │   │
│  │  Domain credentials → <CREDENTIAL_REDACTED>               │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│                    STATE LAYER                                    │
│                                                                  │
│  Supabase (Postgres + RLS)                                       │
│  ┌──────────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │ mail_domains      │  │ mailboxes    │  │ mail_credentials   │ │
│  │ (suite_id, domain │  │ (office_id,  │  │ (encrypted, never  │ │
│  │  status, dns)     │  │  email, type)│  │  exposed in logs)  │ │
│  └──────────────────┘  └──────────────┘  └────────────────────┘ │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────┐                         │
│  │ receipts          │  │ capability_  │                         │
│  │ (immutable,       │  │ tokens       │                         │
│  │  hash-chained)    │  │ (<60s TTL)   │                         │
│  └──────────────────┘  └──────────────┘                         │
│                                                                  │
│  Redis/Upstash: Token cache (5min session), rate limiting        │
│  Vault: PolarisMail admin credentials (encrypted at rest)        │
└──────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│              POLARISMAIL EXTERNAL API                             │
│                                                                  │
│  Primary:  https://cfcp.emailarray.com/admin/json.php            │
│  Fallback: https://cp.emailarray.com/admin/json.php              │
│  User API: https://cp.emailarray.com/json.php                    │
│  Webmail:  https://al.emailarray.com/                            │
│                                                                  │
│  Auth: username/password → session token (5min TTL)              │
│  Protocol: HTTP POST, form-encoded body, JSON response           │
│  Server IP: 69.28.212.201                                        │
└──────────────────────────────────────────────────────────────────┘
```

### Data Flow: New Customer Signup

```
Customer subscribes to Aspire (Stripe)
    │
    ▼
LangGraph Orchestrator receives "suite.created" event
    │
    ├── 1. mint capability_token (tool: polarismail.domain.add, suite_id)
    ├── 2. mail_ops_desk → addDomain(customer-domain.com)
    ├── 3. receipt: domain_provisioned (outcome: success/failed)
    ├── 4. mail_ops_desk → getDomainVerification(customer-domain.com)
    ├── 5. Return DNS records to customer (SPF, DKIM, DMARC, MX)
    ├── 6. receipt: dns_verification_requested
    │
    ▼
Customer configures DNS (async — polled or webhook)
    │
    ▼
Domain verified → mail_ops_desk provisions default mailboxes
    │
    ├── For each Office (human) in the Suite:
    │   ├── mint capability_token (tool: polarismail.mailbox.create)
    │   ├── addUser(username, domain, quota, type)
    │   ├── receipt: mailbox_provisioned
    │   └── Store encrypted credentials in mail_credentials table
    │
    ▼
Eli (Inbox) is now active for the Suite
    ├── email.read (GREEN) — No approval needed
    ├── email.draft (YELLOW) — Draft created, needs approval to send
    └── email.send (YELLOW) — Requires user confirmation
```

---

## 3. PolarisMail API Reference

### Connection Details

| Property | Value |
|----------|-------|
| **Admin API (Primary)** | `https://cfcp.emailarray.com/admin/json.php` |
| **Admin API (Fallback)** | `https://cp.emailarray.com/admin/json.php` |
| **User API** | `https://cp.emailarray.com/json.php` |
| **User Login URL** | `https://cp.emailarray.com/processlogin.php` |
| **Webmail Login URL** | `https://al.emailarray.com/` |
| **Server IP** | `69.28.212.201` |
| **Protocol** | HTTP POST, `application/x-www-form-urlencoded` |
| **Response Format** | JSON: `{ returncode: 0|1, returndata: any, message?: string }` |
| **Auth Type** | Username + password → session token |
| **Token TTL** | ~5 minutes (cached client-side) |
| **Failover** | If primary returns HTTP 5xx, retry on fallback URL |

### Authentication Flow

```
POST /admin/json.php
Content-Type: application/x-www-form-urlencoded

action=login&username={admin_user}&password={admin_pass}

Response (success):
{ "returncode": 1, "returndata": "session_token_string" }

Response (failure):
{ "returncode": 0, "returndata": "Invalid credentials" }
```

**Token caching strategy:**
- Cache token in Redis with 4-minute TTL (refresh before 5-minute expiry)
- Key: `polarismail:session:{suite_id}` (tenant-scoped cache)
- On auth failure: clear cache, retry once, then fail closed

### Complete API Action Catalog

#### Domain Management (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `addDomain` | token, newdomain | `{ returncode, returndata }` | YELLOW |
| `removeDomain` | token, domain | `{ returncode, returndata }` | RED |
| `isDomainAvailable` | token, domain | `{ returncode, returndata }` | GREEN |
| `getAllDomains` | token | `{ returncode, returndata: Domain[] }` | GREEN |
| `getDomainVerification` | token, domain | `{ returncode, returndata: DnsRecord }` | GREEN |
| `getDomainAssignedQuota` | token, domain | `{ returncode, returndata: { totalquota } }` | GREEN |
| `updateDomain` | token, domain, editdomainactive (0\|1) | `{ returncode, returndata }` | YELLOW |

#### Mailbox Management (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `addUser` | token, username, domain, account_type (1=Basic,2=Enhanced), password, quota, uname, twofa_allowed, user_language | `{ returncode, returndata }` | YELLOW |
| `removeUser` | token, username, domain | `{ returncode, returndata }` | RED |
| `enableUser` | token, username, domain | `{ returncode, returndata }` | YELLOW |
| `disableUser` | token, username, domain | `{ returncode, returndata }` | YELLOW |
| `updateUser` | token, editusername, editdomain, editquota, editpassword?, edituname?, editaccount_type? | `{ returncode, returndata }` | YELLOW |
| `getUserInfo` | token, username, domain | `{ returncode, returndata: UserInfo }` | GREEN |
| `getAllUsersDomain` | token, domain, slicing?, offset?, limit?, getOTPAndToken?, showQuota? | `{ returncode, returndata: User[], meta: { all } }` | GREEN |
| `getTotalUsersDomain` | token, domain, atype ("B"\|"E") | `{ returncode, returndata: [basicCount, enhancedCount] }` | GREEN |
| `searchUsers` | token, domain, search, slicing?, offset?, limit? | `{ returncode, returndata: User[] }` | GREEN |
| `getOTPass` | token, username, domain | `{ returncode, returndata: "one_time_password" }` | GREEN |
| `getOTPassAndToken` | token, username, domain | `{ returncode, returndata: { otp, token, url } }` | GREEN |

#### Alias Management (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `addAlias` | token, alias, domain, forward | `{ returncode, returndata }` | YELLOW |
| `removeAlias` | token, alias, domain | `{ returncode, returndata }` | YELLOW |
| `getAllAliasesDomain` | token, domain | `{ returncode, returndata: Alias[] }` | GREEN |

#### Distribution List Management (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `addList` | token, newlname, domain, newlisttype | `{ returncode, returndata }` | YELLOW |
| `removeList` | token, lname, domain | `{ returncode, returndata }` | YELLOW |
| `getAllListsDomain` | token, domain | `{ returncode, returndata: List[] }` | GREEN |
| `getListMembers` | token, lname, domain | `{ returncode, returndata: Member[] }` | GREEN |
| `addListMember` | token, editlname, editdomain, newmember | `{ returncode, returndata }` | YELLOW |
| `removeListMember` | token, lname, domain, member | `{ returncode, returndata }` | YELLOW |

#### Forwarding (User API — requires user-level auth via OTP)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `addForward` | token (user), newforward | `{ returncode, returndata }` | YELLOW |
| `removeForward` | token (user), forward | `{ returncode, returndata }` | YELLOW |
| `getForwards` | token (user) | `{ returncode, returndata: Forward[] }` | GREEN |
| `getAllForwardsDomain` | token (admin), domain | `{ returncode, returndata: Forward[] }` | GREEN |

#### DKIM Management (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `getDKIM` | token, domain | `{ returncode, returndata: { dkim_enabled, dkim_host, dkim_key } }` | GREEN |
| `enableDKIM` | token, domain | `{ returncode, returndata }` | YELLOW |
| `disableDKIM` | token, domain | `{ returncode, returndata }` | YELLOW |

#### Branding (Admin API)

| Action | Parameters | Response | Risk Tier |
|--------|-----------|----------|-----------|
| `getDomainBrandInfo` | token, domain | `{ returncode, returndata: { brandname, supportemail, brandcolor, basic_logo_href } }` | GREEN |
| `setDomainBrandingV2` | token, domain, brand: { brandname, supportemail, brandcolor, newlogo } | `{ returncode, returndata }` | YELLOW |
| `resetDomainBranding` | token, domain | `{ returncode, returndata }` | YELLOW |

---

## 4. Skill Pack: Eli (Inbox)

### Manifest

```json
{
  "pack_id": "eli_inbox",
  "name": "Eli — Inbox",
  "description": "User-facing email operations: read, draft, send, triage, forward management",
  "version": "1.0.0",
  "tier": "founder_quarter",
  "agent": "Eli",
  "provider": "polarismail",

  "permissions": {
    "allow": [
      "email.read",
      "email.draft",
      "email.send",
      "email.triage",
      "email.search",
      "forward.list",
      "forward.add",
      "forward.remove",
      "alias.list",
      "webmail.login"
    ],
    "deny": [
      "email.delete_all",
      "email.forward_bulk",
      "email.export_all",
      "mailbox.create",
      "mailbox.delete",
      "domain.add",
      "domain.remove"
    ]
  },

  "risk_tiers": {
    "green": [
      "email.read",
      "email.search",
      "email.triage",
      "forward.list",
      "alias.list",
      "webmail.login"
    ],
    "yellow": [
      "email.draft",
      "email.send",
      "forward.add",
      "forward.remove"
    ],
    "red": []
  },

  "approvals": {
    "required_for": ["email.send", "forward.add", "forward.remove"],
    "approval_type": {
      "email.send": "standard",
      "forward.add": "standard",
      "forward.remove": "standard"
    },
    "timeout_seconds": 300
  },

  "receipts": {
    "required_fields": [
      "receipt_id", "correlation_id", "suite_id", "office_id",
      "action_type", "risk_tier", "tool_used", "capability_token_id",
      "created_at", "executed_at", "outcome", "redacted_inputs", "redacted_outputs"
    ],
    "custom_fields": [
      "polarismail_domain",
      "polarismail_username",
      "email_subject_hash",
      "recipient_count"
    ],
    "pii_redaction": {
      "enabled": true,
      "fields_to_redact": [
        "email_body",
        "email_addresses",
        "mailbox_password",
        "otp_token"
      ]
    }
  },

  "integrations": {
    "primary": {
      "name": "PolarisMail API",
      "api_base_url": "https://cfcp.emailarray.com/admin/json.php",
      "api_fallback_url": "https://cp.emailarray.com/admin/json.php",
      "api_user_url": "https://cp.emailarray.com/json.php",
      "auth_type": "session_token",
      "auth_config": {
        "token_ttl_seconds": 300,
        "cache_ttl_seconds": 240,
        "cache_key_pattern": "polarismail:session:{suite_id}"
      },
      "rate_limits": {
        "requests_per_second": 5,
        "requests_per_minute": 100
      }
    }
  },

  "failure_handling": {
    "tool_failure": {
      "max_retries": 3,
      "backoff_strategy": "exponential_with_jitter",
      "initial_delay_ms": 1000,
      "max_delay_ms": 15000,
      "escalate_to_human": true
    },
    "auth_failure": {
      "action": "clear_cache_and_retry_once",
      "fallback": "pause_workflow",
      "notify_user": true
    },
    "timeout": {
      "tool_call_timeout_ms": 5000,
      "orchestrator_timeout_ms": 30000,
      "action": "generate_failure_receipt"
    }
  }
}
```

### Eli Tool Functions

| Function | API Actions Used | Description |
|----------|-----------------|-------------|
| `eli.email.read` | `getOTPassAndToken` → webmail SSO | Generate webmail login URL for user |
| `eli.email.triage` | `getAllUsersDomain`, `getUserInfo` | Classify and route incoming mail |
| `eli.email.draft` | (internal — stored in Aspire DB, not sent) | AI-generated draft response |
| `eli.email.send` | `getOTPass` → user API → SMTP relay | Send approved email via user session |
| `eli.email.search` | `searchUsers` | Search mailboxes/contacts |
| `eli.forward.list` | `getAllForwardsDomain` or `getForwards` (user) | List email forwards |
| `eli.forward.add` | `getOTPass` → `userLogin` → `addForward` | Create email forward |
| `eli.forward.remove` | `getOTPass` → `userLogin` → `removeForward` | Remove email forward |
| `eli.alias.list` | `getAllAliasesDomain` | List email aliases |
| `eli.webmail.login` | `getOTPassAndToken` | Generate SSO URL for webmail access |

---

## 5. Skill Pack: mail_ops_desk (Admin)

### Manifest

```json
{
  "pack_id": "mail_ops_desk",
  "name": "Mail Ops Desk — Internal Admin",
  "description": "Internal PolarisM admin: domain provisioning, mailbox lifecycle, DNS verification, branding",
  "version": "1.0.0",
  "tier": "founder_quarter",
  "agent": "mail_ops_desk",
  "provider": "polarismail",
  "internal_only": true,

  "permissions": {
    "allow": [
      "mail_admin.add_domain",
      "mail_admin.remove_domain",
      "mail_admin.verify_domain",
      "mail_admin.check_domain_available",
      "mail_admin.list_domains",
      "mail_admin.enable_domain",
      "mail_admin.disable_domain",
      "mail_admin.create_mailbox",
      "mail_admin.remove_mailbox",
      "mail_admin.enable_mailbox",
      "mail_admin.disable_mailbox",
      "mail_admin.update_mailbox",
      "mail_admin.get_mailbox_info",
      "mail_admin.list_mailboxes",
      "mail_admin.get_quota",
      "mail_admin.add_alias",
      "mail_admin.remove_alias",
      "mail_admin.list_aliases",
      "mail_admin.add_list",
      "mail_admin.remove_list",
      "mail_admin.list_distribution_lists",
      "mail_admin.manage_list_members",
      "mail_admin.get_dkim",
      "mail_admin.enable_dkim",
      "mail_admin.disable_dkim",
      "mail_admin.get_branding",
      "mail_admin.set_branding",
      "mail_admin.reset_branding",
      "incidents.open",
      "authority_queue.propose"
    ],
    "deny": [
      "user_content_access",
      "sending_email",
      "reading_user_mail",
      "credential_export"
    ]
  },

  "risk_tiers": {
    "green": [
      "mail_admin.check_domain_available",
      "mail_admin.list_domains",
      "mail_admin.verify_domain",
      "mail_admin.get_mailbox_info",
      "mail_admin.list_mailboxes",
      "mail_admin.get_quota",
      "mail_admin.list_aliases",
      "mail_admin.list_distribution_lists",
      "mail_admin.get_dkim",
      "mail_admin.get_branding"
    ],
    "yellow": [
      "mail_admin.add_domain",
      "mail_admin.enable_domain",
      "mail_admin.disable_domain",
      "mail_admin.create_mailbox",
      "mail_admin.enable_mailbox",
      "mail_admin.disable_mailbox",
      "mail_admin.update_mailbox",
      "mail_admin.add_alias",
      "mail_admin.remove_alias",
      "mail_admin.add_list",
      "mail_admin.remove_list",
      "mail_admin.manage_list_members",
      "mail_admin.enable_dkim",
      "mail_admin.disable_dkim",
      "mail_admin.set_branding",
      "mail_admin.reset_branding"
    ],
    "red": [
      "mail_admin.remove_domain",
      "mail_admin.remove_mailbox"
    ]
  },

  "hard_rules": [
    "NO user content access — cannot read user emails",
    "NO sending email — cannot send on behalf of users",
    "Credential secrets NEVER returned in responses or logged",
    "100% receipted — all actions generate receipts",
    "Passwords generated server-side, stored encrypted, never exposed"
  ]
}
```

### mail_ops_desk Tool Functions

| Function | API Action | Parameters | Description |
|----------|-----------|------------|-------------|
| `ops.domain.add` | `addDomain` | domain | Provision new domain |
| `ops.domain.remove` | `removeDomain` | domain | Delete domain (RED) |
| `ops.domain.check` | `isDomainAvailable` | domain | Check availability |
| `ops.domain.list` | `getAllDomains` | — | List all suite domains |
| `ops.domain.verify` | `getDomainVerification` | domain | Get DNS verification records |
| `ops.domain.quota` | `getDomainAssignedQuota` | domain | Check quota usage |
| `ops.domain.enable` | `updateDomain` | domain, active=1 | Enable domain |
| `ops.domain.disable` | `updateDomain` | domain, active=0 | Disable domain |
| `ops.mailbox.create` | `addUser` | username, domain, type, quota | Create mailbox |
| `ops.mailbox.remove` | `removeUser` | username, domain | Delete mailbox (RED) |
| `ops.mailbox.enable` | `enableUser` | username, domain | Enable mailbox |
| `ops.mailbox.disable` | `disableUser` | username, domain | Disable mailbox |
| `ops.mailbox.update` | `updateUser` | username, domain, quota?, password? | Update mailbox |
| `ops.mailbox.info` | `getUserInfo` | username, domain | Get mailbox details |
| `ops.mailbox.list` | `getAllUsersDomain` | domain, pagination | List mailboxes |
| `ops.mailbox.count` | `getTotalUsersDomain` | domain, type | Count by type |
| `ops.mailbox.search` | `searchUsers` | domain, query | Search mailboxes |
| `ops.alias.add` | `addAlias` | alias, domain, forward | Create alias |
| `ops.alias.remove` | `removeAlias` | alias, domain | Remove alias |
| `ops.alias.list` | `getAllAliasesDomain` | domain | List aliases |
| `ops.list.add` | `addList` | name, domain, type | Create distribution list |
| `ops.list.remove` | `removeList` | name, domain | Remove distribution list |
| `ops.list.getAll` | `getAllListsDomain` | domain | List all distribution lists |
| `ops.list.members` | `getListMembers` | name, domain | Get list members |
| `ops.list.addMember` | `addListMember` | name, domain, email | Add member |
| `ops.list.removeMember` | `removeListMember` | name, domain, email | Remove member |
| `ops.dkim.get` | `getDKIM` | domain | Get DKIM status |
| `ops.dkim.enable` | `enableDKIM` | domain | Enable DKIM signing |
| `ops.dkim.disable` | `disableDKIM` | domain | Disable DKIM signing |
| `ops.brand.get` | `getDomainBrandInfo` | domain | Get branding config |
| `ops.brand.set` | `setDomainBrandingV2` | domain, name, email, color, logo | Set branding |
| `ops.brand.reset` | `resetDomainBranding` | domain | Reset to default |

---

## 6. Database Schema

### New Tables

```sql
-- ============================================================================
-- MAIL DOMAINS — Tracks PolarisMail domains per suite
-- ============================================================================

CREATE TABLE mail_domains (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL REFERENCES suites(id),
    domain TEXT NOT NULL UNIQUE,

    -- PolarisMail state
    status TEXT NOT NULL DEFAULT 'pending_verification'
        CHECK (status IN (
            'pending_verification',  -- DNS not yet configured
            'verification_sent',     -- DNS records provided to customer
            'verified',              -- Domain verified and active
            'suspended',             -- Temporarily disabled
            'terminated'             -- Permanently removed
        )),

    -- DNS verification
    dns_verification_record JSONB,  -- { type, host, value } from getDomainVerification
    dns_verified_at TIMESTAMPTZ,

    -- DKIM
    dkim_enabled BOOLEAN NOT NULL DEFAULT false,
    dkim_host TEXT,
    dkim_key TEXT,

    -- Branding
    brand_name TEXT,
    brand_color TEXT,
    brand_support_email TEXT,
    brand_logo_url TEXT,

    -- Quota tracking
    total_quota_gb INTEGER NOT NULL DEFAULT 0,
    used_quota_gb INTEGER NOT NULL DEFAULT 0,

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID,  -- office_id of creator

    -- Tenant isolation
    CONSTRAINT fk_suite FOREIGN KEY (suite_id) REFERENCES suites(id)
);

-- RLS
ALTER TABLE mail_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_domains FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_mail_domains ON mail_domains
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- Indexes
CREATE INDEX idx_mail_domains_suite ON mail_domains (suite_id);
CREATE UNIQUE INDEX idx_mail_domains_domain ON mail_domains (domain);
CREATE INDEX idx_mail_domains_status ON mail_domains (status);


-- ============================================================================
-- MAILBOXES — Tracks individual mailboxes per office
-- ============================================================================

CREATE TABLE mailboxes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL REFERENCES suites(id),
    office_id UUID NOT NULL REFERENCES offices(id),
    mail_domain_id UUID NOT NULL REFERENCES mail_domains(id),

    -- Mailbox identity
    username TEXT NOT NULL,       -- local part (before @)
    domain TEXT NOT NULL,         -- full domain
    email TEXT GENERATED ALWAYS AS (username || '@' || domain) STORED,

    -- PolarisMail config
    account_type INTEGER NOT NULL DEFAULT 1 CHECK (account_type IN (1, 2)),
        -- 1 = Basic, 2 = Enhanced
    quota_gb INTEGER NOT NULL DEFAULT 5,
    twofa_allowed BOOLEAN NOT NULL DEFAULT true,

    -- Status
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'suspended', 'terminated')),

    -- Metadata
    display_name TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    UNIQUE (username, domain)
);

-- RLS
ALTER TABLE mailboxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE mailboxes FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_mailboxes ON mailboxes
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- Indexes
CREATE INDEX idx_mailboxes_suite ON mailboxes (suite_id);
CREATE INDEX idx_mailboxes_office ON mailboxes (office_id);
CREATE INDEX idx_mailboxes_domain ON mailboxes (mail_domain_id);
CREATE INDEX idx_mailboxes_email ON mailboxes (email);
CREATE INDEX idx_mailboxes_status ON mailboxes (status);


-- ============================================================================
-- MAIL CREDENTIALS — Encrypted storage (NEVER expose in logs/receipts)
-- ============================================================================

CREATE TABLE mail_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL REFERENCES suites(id),

    -- Credential type
    credential_type TEXT NOT NULL
        CHECK (credential_type IN ('admin', 'mailbox')),

    -- Reference
    mailbox_id UUID REFERENCES mailboxes(id),  -- NULL for admin credentials

    -- Encrypted values (using TOKEN_ENCRYPTION_KEY from environment)
    encrypted_username BYTEA NOT NULL,
    encrypted_password BYTEA NOT NULL,
    encryption_key_version INTEGER NOT NULL DEFAULT 1,

    -- Lifecycle
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,  -- NULL = no expiry (rotated on schedule)

    -- Status
    is_active BOOLEAN NOT NULL DEFAULT true
);

-- RLS
ALTER TABLE mail_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_credentials FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_mail_credentials ON mail_credentials
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- Indexes
CREATE INDEX idx_mail_creds_suite ON mail_credentials (suite_id);
CREATE INDEX idx_mail_creds_mailbox ON mail_credentials (mailbox_id);
CREATE INDEX idx_mail_creds_type ON mail_credentials (credential_type, is_active);


-- ============================================================================
-- MAIL ALIASES — Tracks aliases per domain
-- ============================================================================

CREATE TABLE mail_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL REFERENCES suites(id),
    mail_domain_id UUID NOT NULL REFERENCES mail_domains(id),
    alias_name TEXT NOT NULL,
    forward_to TEXT NOT NULL,
    domain TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE mail_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_aliases FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_mail_aliases ON mail_aliases
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);


-- ============================================================================
-- MAIL DISTRIBUTION LISTS — Tracks distribution lists per domain
-- ============================================================================

CREATE TABLE mail_distribution_lists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL REFERENCES suites(id),
    mail_domain_id UUID NOT NULL REFERENCES mail_domains(id),
    list_name TEXT NOT NULL,
    list_type INTEGER NOT NULL DEFAULT 1,
    domain TEXT NOT NULL,
    members JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE mail_distribution_lists ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_distribution_lists FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_mail_dist_lists ON mail_distribution_lists
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);
```

---

## 7. Authentication & Credential Management

### PolarisMail Admin Credentials

```
Storage:     Supabase mail_credentials table (encrypted with TOKEN_ENCRYPTION_KEY)
Encryption:  AES-256-GCM via pgcrypto
Key Source:  TOKEN_ENCRYPTION_KEY environment variable (Railway)
Key Rotation: Quarterly (encryption_key_version tracks which key encrypted which row)
```

### Session Token Management

```typescript
// Redis cache pattern
interface PolarisMailSession {
  token: string;
  authenticatedAt: number;  // epoch ms
  expiresAt: number;        // epoch ms (authenticatedAt + 240_000)
}

// Cache key: polarismail:session:{suite_id}
// TTL: 240 seconds (refresh before 300s PolarisMail expiry)
// On 401/auth error: delete cache key, re-authenticate once, then fail closed
```

### Credential Flow

```
1. Orchestrator needs to call PolarisMail API
2. Check Redis cache for valid session token
3. If cache miss or expired:
   a. Read encrypted admin credentials from mail_credentials (RLS-scoped)
   b. Decrypt using TOKEN_ENCRYPTION_KEY
   c. POST login to PolarisMail API
   d. Cache session token in Redis (4min TTL)
   e. NEVER log the decrypted password
4. Use cached session token for API call
5. If API returns auth error: clear cache, retry once (step 3)
6. If retry fails: fail closed, generate denial receipt
```

### Security Rules (NON-NEGOTIABLE)

- Passwords are generated server-side (10 chars, mixed case + digits + symbols)
- Passwords are encrypted at rest (AES-256-GCM)
- Passwords are NEVER included in receipts, logs, or API responses
- OTP tokens are ephemeral — used once and discarded
- Admin credentials are per-suite (use PolarisMail sub-admin per suite)
- Sub-admin IP restriction: locked to Aspire's Railway IP range

---

## 8. Execution Pipeline (Per-Action)

Every PolarisMail operation follows the Prime Directive:

```
Intent → Context → Plan → Policy Check → Approval → Execute → Receipt → Summary
```

### Example: Create Mailbox

```
Step 1: INTENT
  User says: "Create an email for john@mybusiness.com"
  Orchestrator identifies: mail_admin.create_mailbox

Step 2: CONTEXT
  - Suite: suite_abc (mybusiness.com)
  - Office: office_john
  - Domain: mybusiness.com (verified? check mail_domains table)
  - Quota available? (check getDomainAssignedQuota)
  - Mailbox count within limit? (check getTotalUsersDomain)

Step 3: PLAN
  - Tool: ops.mailbox.create
  - Params: username=john, domain=mybusiness.com, type=1, quota=5GB
  - Risk tier: YELLOW
  - Requires approval: YES

Step 4: POLICY CHECK
  - Capability token: mint for polarismail.mailbox.create, suite_abc, <60s
  - Risk tier YELLOW: requires user confirmation
  - Domain verified: YES (mail_domains.status = 'verified')
  - Quota sufficient: YES (available > 5GB)

Step 5: APPROVAL
  - Present to user: "Create email john@mybusiness.com (5GB Basic)?"
  - Interaction state: WARM (voice confirmation) or COLD (text confirmation)
  - User approves → record approval_evidence

Step 6: EXECUTE
  - Decrypt admin credentials from mail_credentials
  - Get/refresh PolarisMail session token
  - POST addUser to PolarisMail API
  - Generate random password (server-side)
  - Store encrypted password in mail_credentials
  - Insert row in mailboxes table

Step 7: RECEIPT
  receipt = {
    receipt_id: uuid,
    correlation_id: uuid,
    suite_id: suite_abc,
    office_id: office_john,
    action_type: "polarismail.mailbox.create",
    risk_tier: "yellow",
    tool_used: "polarismail_api",
    capability_token_id: token_xyz,
    created_at: now,
    approved_at: approval_timestamp,
    executed_at: now,
    approval_evidence: { approver_id, method: "voice", timestamp },
    outcome: "success",
    redacted_inputs: {
      username: "john",
      domain: "mybusiness.com",
      account_type: "Basic",
      quota_gb: 5,
      password: "<PASSWORD_REDACTED>"
    },
    redacted_outputs: {
      email: "john@mybusiness.com",
      polarismail_returncode: 1
    },
    previous_receipt_hash: "sha256_of_previous",
    receipt_hash: "sha256_of_this"
  }

Step 8: SUMMARY
  "Created email john@mybusiness.com — 5GB Basic mailbox.
   They can access webmail at al.emailarray.com."
```

---

## 9. Risk Tier Classification

| Operation | Risk Tier | Approval Required | Rationale |
|-----------|-----------|-------------------|-----------|
| Check domain availability | GREEN | No | Read-only, no state change |
| List domains/mailboxes | GREEN | No | Read-only, no state change |
| Get DNS verification | GREEN | No | Read-only |
| Get quota/info | GREEN | No | Read-only |
| Get DKIM status | GREEN | No | Read-only |
| Get branding info | GREEN | No | Read-only |
| List aliases/lists | GREEN | No | Read-only |
| Generate webmail SSO URL | GREEN | No | Read-only (OTP is ephemeral) |
| **Add domain** | **YELLOW** | **Yes** | Creates external resource |
| **Create mailbox** | **YELLOW** | **Yes** | Creates external resource, generates credentials |
| **Enable/disable mailbox** | **YELLOW** | **Yes** | Changes access state |
| **Enable/disable domain** | **YELLOW** | **Yes** | Affects all mailboxes in domain |
| **Update mailbox** | **YELLOW** | **Yes** | Changes quota/password |
| **Add/remove alias** | **YELLOW** | **Yes** | Changes email routing |
| **Add/remove list** | **YELLOW** | **Yes** | Changes email distribution |
| **Enable/disable DKIM** | **YELLOW** | **Yes** | Changes email authentication |
| **Set/reset branding** | **YELLOW** | **Yes** | Changes user-visible branding |
| **Send email** | **YELLOW** | **Yes** | External communication |
| **Add/remove forward** | **YELLOW** | **Yes** | Changes email routing |
| **Remove domain** | **RED** | **Explicit authority** | Destroys all mailboxes, irreversible |
| **Remove mailbox** | **RED** | **Explicit authority** | Destroys email data, irreversible |

---

## 10. Receipt Specifications

### Receipt Fields for PolarisMail Operations

```typescript
interface PolarisMailReceipt {
  // Standard Aspire receipt fields
  receipt_id: string;           // UUID
  correlation_id: string;       // UUID — groups related operations
  suite_id: string;             // UUID — tenant isolation
  office_id: string;            // UUID — individual human
  action_type: string;          // e.g., "polarismail.mailbox.create"
  risk_tier: "green" | "yellow" | "red";
  tool_used: "polarismail_api";
  capability_token_id: string;  // UUID
  created_at: string;           // ISO8601
  approved_at: string | null;   // ISO8601 (null for GREEN tier)
  executed_at: string;          // ISO8601
  approval_evidence: {
    approver_id: string;
    approval_method: "voice" | "video" | "text";
    approval_timestamp: string;
  } | null;
  outcome: "success" | "denied" | "failed";
  reason_code: string | null;   // e.g., "api_timeout", "auth_failed", "quota_exceeded"

  // PII-redacted (NEVER include raw passwords, OTPs, or session tokens)
  redacted_inputs: {
    action: string;             // PolarisMail API action name
    domain?: string;            // Domain involved
    username?: string;          // Mailbox username (local part only)
    account_type?: string;      // "Basic" | "Enhanced"
    quota_gb?: number;
    password: "<PASSWORD_REDACTED>";  // ALWAYS redacted
    session_token: "<TOKEN_REDACTED>";  // ALWAYS redacted
  };
  redacted_outputs: {
    returncode: number;         // 0 or 1
    returndata_summary?: string;  // Summarized (never raw response)
    email_created?: string;     // e.g., "john@mybusiness.com"
    error_message?: string;     // PolarisMail error text (if failed)
  };

  // Hash chain
  previous_receipt_hash: string | null;
  receipt_hash: string;

  // Custom PolarisMail fields
  polarismail_domain: string;
  polarismail_api_url: string;   // Which endpoint was called
  polarismail_response_time_ms: number;
}
```

### Failure Receipt Example

```json
{
  "receipt_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "correlation_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "suite_id": "suite_abc",
  "office_id": "office_john",
  "action_type": "polarismail.mailbox.create",
  "risk_tier": "yellow",
  "tool_used": "polarismail_api",
  "capability_token_id": "token_xyz",
  "created_at": "2026-02-11T12:00:00Z",
  "approved_at": "2026-02-11T12:00:01Z",
  "executed_at": "2026-02-11T12:00:02Z",
  "outcome": "failed",
  "reason_code": "quota_exceeded",
  "redacted_inputs": {
    "action": "addUser",
    "domain": "mybusiness.com",
    "username": "john",
    "quota_gb": 50,
    "password": "<PASSWORD_REDACTED>"
  },
  "redacted_outputs": {
    "returncode": 0,
    "error_message": "Quota exceeded"
  }
}
```

---

## 11. Error Handling & Circuit Breakers

### Error Categories

| Error | Retry? | Circuit Breaker? | Action |
|-------|--------|-----------------|--------|
| Auth failure (bad credentials) | Once (clear cache) | No | Fail closed, generate denial receipt |
| Auth failure (2FA enabled) | No | No | Fail closed, alert admin |
| API timeout (>5s) | Yes (3x, exponential) | Yes (5 failures in 60s) | Generate failure receipt |
| HTTP 5xx from primary | Yes (failover to fallback URL) | Yes | Try fallback, then fail |
| HTTP 5xx from fallback | Yes (3x, exponential) | Yes | Generate failure receipt |
| `returncode: 0` (business error) | No | No | Generate failure receipt with error message |
| Quota exceeded | No | No | Generate denial receipt, notify user |
| Domain not verified | No | No | Generate denial receipt, return DNS records |
| Rate limited | Yes (respect Retry-After) | Yes | Backoff and retry |
| Network error | Yes (3x, exponential) | Yes | Generate failure receipt |

### Circuit Breaker Configuration

```typescript
interface CircuitBreakerConfig {
  name: "polarismail_api";
  failureThreshold: 5;          // Open circuit after 5 failures
  failureWindowMs: 60_000;      // Within 60 seconds
  resetTimeoutMs: 30_000;       // Try half-open after 30 seconds
  halfOpenMaxAttempts: 1;       // Allow 1 request in half-open state

  // Metrics emitted
  metrics: {
    "polarismail.circuit.open": "counter";
    "polarismail.circuit.half_open": "counter";
    "polarismail.circuit.closed": "counter";
    "polarismail.api.latency_ms": "histogram";
    "polarismail.api.success_rate": "gauge";
  };
}
```

### Retry Strategy

```typescript
interface RetryConfig {
  maxRetries: 3;
  backoffStrategy: "exponential_with_jitter";
  initialDelayMs: 1000;
  maxDelayMs: 15000;
  jitterRange: 0.25;  // ±25% randomization

  // Retry sequence: ~1s, ~2s, ~4s (with jitter)
  // Total max wait: ~7s before final failure

  retryableErrors: [
    "ETIMEDOUT",
    "ECONNRESET",
    "HTTP_5XX",
    "RATE_LIMITED"
  ];

  nonRetryableErrors: [
    "AUTH_FAILED",
    "INVALID_PARAMS",
    "QUOTA_EXCEEDED",
    "DOMAIN_NOT_FOUND",
    "RETURNCODE_0"  // Business logic errors
  ];
}
```

### Idempotency

| Operation | Idempotent? | Strategy |
|-----------|------------|----------|
| `addDomain` | Yes (if domain exists, returns existing) | Safe to retry |
| `addUser` | No (creates duplicate if username differs) | Check before create |
| `removeUser` | Yes (if already removed, returns error) | Safe to retry |
| `enableUser` | Yes | Safe to retry |
| `disableUser` | Yes | Safe to retry |
| `addAlias` | No | Check before create |
| `addList` | No | Check before create |
| `updateUser` | Yes (last write wins) | Safe to retry |

**Pre-check pattern for non-idempotent operations:**
```
1. Check if resource exists (getAllUsersDomain / getAllAliasesDomain)
2. If exists: skip creation, log "already_exists" receipt
3. If not: proceed with creation
4. Handle race conditions: if creation fails with "already exists", treat as success
```

---

## 12. Tenant Isolation (RLS)

### RLS Policies (Applied to ALL mail tables)

Every mail table has RLS ENABLED + FORCED with this policy pattern:

```sql
CREATE POLICY tenant_isolation_{table} ON {table}
    FOR ALL
    USING (suite_id = current_setting('app.current_suite_id', true)::uuid);
```

### Evil Test Matrix

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `EVIL-MAIL-001` | Suite A tries to read Suite B's mail_domains | 0 rows returned |
| `EVIL-MAIL-002` | Suite A tries to read Suite B's mailboxes | 0 rows returned |
| `EVIL-MAIL-003` | Suite A tries to read Suite B's mail_credentials | 0 rows returned |
| `EVIL-MAIL-004` | Suite A tries to INSERT into Suite B's mail_domains | INSERT fails (RLS) |
| `EVIL-MAIL-005` | Suite A tries to UPDATE Suite B's mailbox status | 0 rows affected |
| `EVIL-MAIL-006` | Suite A tries to DELETE Suite B's mail_aliases | 0 rows affected |
| `EVIL-MAIL-007` | No suite_id set, try to read mail_domains | 0 rows returned (fail closed) |
| `EVIL-MAIL-008` | SQL injection in domain name parameter | Parameterized query blocks it |
| `EVIL-MAIL-009` | Try to read mail_credentials without RLS context | 0 rows returned |
| `EVIL-MAIL-010` | Try to UPDATE receipts table (immutability check) | UPDATE denied |

---

## 13. State Machine Definitions

### mail_ops_desk: Domain Provisioning Flow

```yaml
name: domain_provisioning
type: langgraph_subgraph
trigger: suite.created OR admin.add_domain

nodes:
  - check_availability:
      tool: ops.domain.check
      risk_tier: green
      next:
        available: add_domain
        unavailable: generate_denial_receipt

  - add_domain:
      tool: ops.domain.add
      risk_tier: yellow
      requires_approval: true
      next:
        success: get_verification
        failure: generate_failure_receipt

  - get_verification:
      tool: ops.domain.verify
      risk_tier: green
      next:
        records_returned: notify_customer_dns
        error: generate_failure_receipt

  - notify_customer_dns:
      action: return DNS records to customer
      next: wait_for_verification

  - wait_for_verification:
      type: checkpoint
      poll_interval: 300s  # Check every 5 minutes
      max_wait: 72h        # Give customer 72 hours
      next:
        verified: enable_dkim
        timeout: notify_admin

  - enable_dkim:
      tool: ops.dkim.enable
      risk_tier: yellow
      next:
        success: set_branding
        failure: generate_failure_receipt

  - set_branding:
      tool: ops.brand.set
      risk_tier: yellow
      params:
        brandname: "{suite.name}"
        supportemail: "support@{domain}"
        brandcolor: "{suite.brand_color}"
      next:
        success: provision_default_mailboxes
        failure: generate_failure_receipt

  - provision_default_mailboxes:
      type: loop
      for_each: suite.offices
      tool: ops.mailbox.create
      risk_tier: yellow
      next:
        all_success: generate_success_receipt
        any_failure: generate_partial_receipt

  - generate_success_receipt:
      type: receipt
      outcome: success
      next: complete

  - generate_denial_receipt:
      type: receipt
      outcome: denied
      next: complete

  - generate_failure_receipt:
      type: receipt
      outcome: failed
      next: complete

  - generate_partial_receipt:
      type: receipt
      outcome: partial
      next: notify_admin
```

### Eli: Email Send Flow

```yaml
name: email_send
type: langgraph_subgraph
trigger: user.email.send

nodes:
  - validate_input:
      checks:
        - mailbox exists in mailboxes table
        - domain is verified (mail_domains.status = 'verified')
        - recipient is valid email format
      next:
        valid: prepare_draft
        invalid: generate_denial_receipt

  - prepare_draft:
      action: AI-generate draft or use user-provided content
      next: wait_approval

  - wait_approval:
      type: checkpoint
      risk_tier: yellow
      interaction_state: warm  # Voice confirmation
      prompt: "Send email from {sender} to {recipient}? Subject: {subject}"
      timeout: 300s
      next:
        approved: execute_send
        denied: generate_denial_receipt
        timeout: generate_timeout_receipt

  - execute_send:
      steps:
        1. mint_capability_token(tool: polarismail.email.send)
        2. get_otp(admin_token, username, domain)
        3. user_login(username, otp, domain)
        4. send_via_smtp(user_token, recipient, subject, body)
      next:
        success: generate_success_receipt
        failure: generate_failure_receipt

  - generate_success_receipt:
      type: receipt
      outcome: success
      redact: [email_body, recipient_email, otp_token]
      next: complete

  - generate_denial_receipt:
      type: receipt
      outcome: denied
      next: complete

  - generate_failure_receipt:
      type: receipt
      outcome: failed
      next: notify_user
```

---

## 14. Testing Strategy

### Unit Tests

| Test File | Coverage |
|-----------|----------|
| `tests/unit/polarismail/client.test.ts` | API client: auth, token caching, failover, error handling |
| `tests/unit/polarismail/domain-ops.test.ts` | Domain CRUD operations with mock API |
| `tests/unit/polarismail/mailbox-ops.test.ts` | Mailbox CRUD operations with mock API |
| `tests/unit/polarismail/credential-vault.test.ts` | Encryption/decryption, key rotation |
| `tests/unit/polarismail/circuit-breaker.test.ts` | Circuit breaker state transitions |
| `tests/unit/polarismail/receipt-generation.test.ts` | Receipt completeness, PII redaction |

### Integration Tests

| Test File | Coverage |
|-----------|----------|
| `tests/integration/polarismail/api-smoke.test.ts` | Real API calls (test account): login, addDomain, addUser |
| `tests/integration/polarismail/domain-lifecycle.test.ts` | Full domain lifecycle: add → verify → mailbox → remove |
| `tests/integration/polarismail/token-flow.test.ts` | Capability token mint → validate → use → expire |
| `tests/integration/polarismail/receipt-chain.test.ts` | Verify receipt hash chain integrity |

### RLS Isolation Tests

| Test File | Coverage |
|-----------|----------|
| `tests/rls/mail-domains-isolation.test.ts` | Zero cross-tenant domain access |
| `tests/rls/mailboxes-isolation.test.ts` | Zero cross-tenant mailbox access |
| `tests/rls/mail-credentials-isolation.test.ts` | Zero cross-tenant credential access |

### Evil Tests

| Test File | Coverage |
|-----------|----------|
| `tests/evil/polarismail/cross-tenant.test.ts` | EVIL-MAIL-001 through EVIL-MAIL-010 |
| `tests/evil/polarismail/credential-leak.test.ts` | Verify no passwords/OTPs in logs/receipts |
| `tests/evil/polarismail/injection.test.ts` | SQL injection in domain/username params |
| `tests/evil/polarismail/bypass.test.ts` | Attempt to call API without capability token |
| `tests/evil/polarismail/replay.test.ts` | Attempt to reuse expired capability token |

### Performance Tests

| Metric | Target | Test |
|--------|--------|------|
| API call latency (p95) | <800ms | Load test with 100 concurrent ops |
| Token validation latency (p95) | <50ms | Load test with 1000 validations |
| Receipt generation latency (p95) | <100ms | Load test with 500 receipts |
| Circuit breaker trip time | <100ms | Simulate 5 failures in 60s |

---

## 15. Production Gates Checklist

### Gate 1: Testing
- [ ] Unit tests: ≥80% coverage on polarismail client code
- [ ] Integration tests: domain lifecycle, mailbox lifecycle pass
- [ ] RLS isolation tests: 100% pass (zero cross-tenant leakage)
- [ ] Evil tests: all 10 EVIL-MAIL tests pass
- [ ] Replay demo: reconstruct mail domain state from receipts alone

### Gate 2: Observability
- [ ] SLO dashboard: polarismail API latency (p50/p95/p99), success rate, error budget
- [ ] Correlation IDs flow: orchestrator → capability token → API call → receipt
- [ ] Health check: `/health/polarismail` endpoint (tests API connectivity)
- [ ] Alerting: circuit breaker open, auth failure, quota exhaustion

### Gate 3: Reliability
- [ ] Circuit breaker: configured and tested (5 failures / 60s window)
- [ ] Retry with exponential backoff + jitter: implemented and tested
- [ ] Idempotency: pre-check pattern for non-idempotent operations
- [ ] Timeout enforcement: 5s tool timeout, 30s orchestrator timeout
- [ ] Failover: primary URL → fallback URL on 5xx

### Gate 4: Operations
- [ ] Incident runbook: `runbooks/polarismail-incident.md`
- [ ] Credential rotation procedure documented
- [ ] Domain migration procedure documented
- [ ] 24h soak test plan with error rate targets
- [ ] Rollback procedure: disable integration without data loss

### Gate 5: Security
- [ ] No passwords/OTPs in logs or receipts (verified by evil test)
- [ ] Admin credentials encrypted at rest (AES-256-GCM)
- [ ] Session tokens cached in Redis (not persisted to disk)
- [ ] Sub-admin IP restriction configured (Railway IP range only)
- [ ] RLS enforced on all mail tables (ENABLE + FORCE)
- [ ] No hardcoded credentials anywhere in codebase

---

## 16. Deployment Sequence

### Phase 2, Sprint 1: Foundation

```
Week 1:
  1. Sign up for PolarisMail reseller account
  2. Create sub-admin for API access (no 2FA, IP-restricted)
  3. Run database migration: create mail_domains, mailboxes, mail_credentials tables
  4. Implement PolarisMailClient (TypeScript): auth, token caching, failover
  5. Unit tests for PolarisMailClient

Week 2:
  6. Implement mail_ops_desk skill pack tools
  7. Implement credential vault (encrypt/decrypt/rotate)
  8. Integration test: domain lifecycle with real PolarisMail API
  9. RLS isolation tests for all new tables
  10. Evil tests for cross-tenant and credential leakage
```

### Phase 2, Sprint 2: Eli Integration

```
Week 3:
  11. Implement Eli skill pack tools (email.read, draft, send, triage)
  12. Implement email send flow (OTP → user login → SMTP)
  13. Implement webmail SSO (getOTPassAndToken → login URL)
  14. Receipt generation for all operations
  15. PII redaction verification

Week 4:
  16. LangGraph state machine: domain provisioning flow
  17. LangGraph state machine: email send flow
  18. Circuit breaker implementation
  19. Production gates review
  20. Soak test (24h stability)
```

### Environment Variables Required

```
POLARISMAIL_ADMIN_USERNAME    # Sub-admin username (per-suite or global)
POLARISMAIL_ADMIN_PASSWORD    # Sub-admin password (encrypted in vault)
POLARISMAIL_API_PRIMARY_URL   # https://cfcp.emailarray.com/admin/json.php
POLARISMAIL_API_FALLBACK_URL  # https://cp.emailarray.com/admin/json.php
POLARISMAIL_API_USER_URL      # https://cp.emailarray.com/json.php
POLARISMAIL_SERVER_IP         # 69.28.212.201
TOKEN_ENCRYPTION_KEY          # AES-256 key for credential encryption (already in Railway)
REDIS_URL                     # For session token cache (already in Railway)
```

---

## 17. File Structure

```
backend/
  providers/
    polarismail/
      client.ts                    # PolarisMailClient — HTTP client with auth, caching, failover
      types.ts                     # TypeScript interfaces for all API request/response types
      circuit-breaker.ts           # Circuit breaker for API calls
      credential-vault.ts          # Encrypt/decrypt/rotate credentials
      session-cache.ts             # Redis session token cache

  skill-packs/
    eli-inbox/
      manifest.json                # Skill pack manifest (permissions, risk tiers, receipts)
      tools/
        email-read.ts              # eli.email.read
        email-draft.ts             # eli.email.draft
        email-send.ts              # eli.email.send
        email-triage.ts            # eli.email.triage
        email-search.ts            # eli.email.search
        forward-list.ts            # eli.forward.list
        forward-add.ts             # eli.forward.add
        forward-remove.ts          # eli.forward.remove
        alias-list.ts              # eli.alias.list
        webmail-login.ts           # eli.webmail.login
      state-machines/
        email-send-flow.yaml       # LangGraph subgraph definition

    mail-ops-desk/
      manifest.json                # Skill pack manifest
      tools/
        domain-add.ts              # ops.domain.add
        domain-remove.ts           # ops.domain.remove
        domain-check.ts            # ops.domain.check
        domain-list.ts             # ops.domain.list
        domain-verify.ts           # ops.domain.verify
        domain-quota.ts            # ops.domain.quota
        domain-enable.ts           # ops.domain.enable
        domain-disable.ts          # ops.domain.disable
        mailbox-create.ts          # ops.mailbox.create
        mailbox-remove.ts          # ops.mailbox.remove
        mailbox-enable.ts          # ops.mailbox.enable
        mailbox-disable.ts         # ops.mailbox.disable
        mailbox-update.ts          # ops.mailbox.update
        mailbox-info.ts            # ops.mailbox.info
        mailbox-list.ts            # ops.mailbox.list
        mailbox-count.ts           # ops.mailbox.count
        mailbox-search.ts          # ops.mailbox.search
        alias-add.ts               # ops.alias.add
        alias-remove.ts            # ops.alias.remove
        alias-list.ts              # ops.alias.list
        list-add.ts                # ops.list.add
        list-remove.ts             # ops.list.remove
        list-get-all.ts            # ops.list.getAll
        list-members.ts            # ops.list.members
        list-add-member.ts         # ops.list.addMember
        list-remove-member.ts      # ops.list.removeMember
        dkim-get.ts                # ops.dkim.get
        dkim-enable.ts             # ops.dkim.enable
        dkim-disable.ts            # ops.dkim.disable
        brand-get.ts               # ops.brand.get
        brand-set.ts               # ops.brand.set
        brand-reset.ts             # ops.brand.reset
      state-machines/
        domain-provisioning.yaml   # LangGraph subgraph definition

infrastructure/
  migrations/
    050_mail_domains.sql           # mail_domains table + RLS
    051_mailboxes.sql              # mailboxes table + RLS
    052_mail_credentials.sql       # mail_credentials table + RLS
    053_mail_aliases.sql           # mail_aliases table + RLS
    054_mail_distribution_lists.sql # mail_distribution_lists table + RLS

tests/
  unit/polarismail/
    client.test.ts
    domain-ops.test.ts
    mailbox-ops.test.ts
    credential-vault.test.ts
    circuit-breaker.test.ts
    receipt-generation.test.ts
  integration/polarismail/
    api-smoke.test.ts
    domain-lifecycle.test.ts
    token-flow.test.ts
    receipt-chain.test.ts
  rls/
    mail-domains-isolation.test.ts
    mailboxes-isolation.test.ts
    mail-credentials-isolation.test.ts
  evil/polarismail/
    cross-tenant.test.ts
    credential-leak.test.ts
    injection.test.ts
    bypass.test.ts
    replay.test.ts

docs/
  runbooks/
    polarismail-incident.md
    polarismail-credential-rotation.md
    polarismail-domain-migration.md
```

---

## 18. Appendix: Raw API Payloads

### Login

```
Request:
POST https://cfcp.emailarray.com/admin/json.php
Content-Type: application/x-www-form-urlencoded

action=login&username=admin_user&password=admin_pass

Response (success):
{ "returncode": 1, "returndata": "abc123sessiontoken" }
```

### Add Domain

```
Request:
action=addDomain&token=abc123sessiontoken&newdomain=mybusiness.com

Response (success):
{ "returncode": 1, "returndata": "Domain added" }

Response (failure — domain exists):
{ "returncode": 0, "returndata": "Domain already exists" }
```

### Add User (Create Mailbox)

```
Request:
action=addUser&token=abc123&username=john&domain=mybusiness.com&account_type=1&password=SecureP@ss1&quota=5&uname=John&twofa_allowed=1&user_language=en

Response (success):
{ "returncode": 1, "returndata": "User added" }
```

### Get Domain Verification

```
Request:
action=getDomainVerification&token=abc123&domain=mybusiness.com

Response:
{ "returncode": 1, "returndata": { "type": "TXT", "host": "...", "value": "..." }, "message": "Add this DNS record" }
```

### Get All Users (Paginated)

```
Request:
action=getAllUsersDomain&token=abc123&domain=mybusiness.com&slicing=1&offset=0&limit=10&showQuota=1

Response:
{
  "returncode": 1,
  "returndata": [
    {
      "email": "john@mybusiness.com",
      "username": "john",
      "account_type": 1,
      "quota": 5000,
      "usage": 1234,
      "disabled": 0,
      "lastauth_timestamp": "2026-02-10 12:00:00"
    }
  ],
  "meta": { "all": 15 }
}
```

### Get OTP + Webmail Token

```
Request:
action=getOTPassAndToken&token=abc123&username=john&domain=mybusiness.com

Response:
{
  "returncode": 1,
  "returndata": {
    "otp": "one_time_password",
    "token": "webmail_access_token",
    "url": "webmail.emailarray.com"
  }
}
```

### Set Domain Branding

```
Request:
action=setDomainBrandingV2&token=abc123&domain=mybusiness.com&brand[brandname]=MyBusiness&brand[supportemail]=support@mybusiness.com&brand[brandcolor]=2196F3&brand[newlogo]=base64_encoded_image

Response:
{ "returncode": 1, "returndata": "Branding updated" }
```

---

## Document Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-11 | Aspire Engineering | Initial enterprise integration plan |

---

**END OF DOCUMENT**
