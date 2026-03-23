#!/usr/bin/env python3
"""Seed Legal Knowledge Base — 200+ chunks for Clara RAG.

Populates the legal_knowledge_chunks table with curated legal
knowledge across 12 domains. Uses OpenAI text-embedding-3-large (3072 dims).

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_legal_knowledge.py

Requires: ASPIRE_OPENAI_API_KEY env var set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# Knowledge Chunks — 12 domains, 200+ entries
# =============================================================================

LEGAL_KNOWLEDGE: list[dict] = []


def _add(domain: str, chunk_type: str, content: str, **kwargs):
    """Helper to add a knowledge chunk."""
    LEGAL_KNOWLEDGE.append({
        "domain": domain,
        "chunk_type": chunk_type,
        "content": content,
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Domain: pandadoc_api (10 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("PandaDoc Authentication", "PandaDoc API uses OAuth2 bearer tokens or API keys. API keys are passed in the Authorization header as 'API-Key {key}'. OAuth2 tokens expire and must be refreshed. Rate limits: 10 requests/second for most endpoints, 2 requests/second for document creation. Workspace ID is required for multi-workspace accounts. Base URL: https://api.pandadoc.com. API version is specified in the URL path (/public/v1/)."),
    ("PandaDoc Rate Limits", "PandaDoc enforces rate limits per API key: 10 req/sec for reads, 2 req/sec for document creation, 1 req/sec for bulk operations. HTTP 429 returned when exceeded with Retry-After header. Best practice: implement exponential backoff starting at 1 second. Batch document creation should use 500ms delays between requests. Webhooks are preferred over polling for status changes."),
    ("PandaDoc Workspace Model", "PandaDoc organizes resources by workspace. Each workspace has its own templates, contacts, and settings. API key is scoped to a workspace. Multi-workspace accounts need workspace ID in headers. Documents belong to a single workspace. Template sharing between workspaces requires enterprise plan. Folder structure is workspace-specific."),
    ("PandaDoc Webhook Events", "PandaDoc sends webhook notifications for document lifecycle events: document_state_changed (draft, sent, viewed, waiting_approval, approved, completed, declined, voided, expired), recipient_completed, document_updated, document_deleted. Webhooks use HTTPS POST with JSON payload. Verify webhook signature using shared secret. Retry policy: 3 attempts with exponential backoff."),
    ("PandaDoc Error Codes", "Common PandaDoc API errors: 400 (invalid request body/params), 401 (invalid/expired API key), 403 (insufficient permissions), 404 (resource not found), 409 (document locked/conflict), 422 (validation failed), 429 (rate limited), 500 (server error). Always check response.detail for specific error messages. Document creation returns 201, not 200."),
    ("PandaDoc Content Library", "PandaDoc Content Library stores reusable blocks: text snippets, pricing tables, images, and e-signature fields. Content items have categories and tags for organization. API: GET /content-library-items, POST to create. Content items can be inserted into documents during creation. Tokens in content items auto-populate from document variables."),
    ("PandaDoc Pricing Tables", "PandaDoc pricing tables support line items with name, description, price, quantity, discount, and tax. Tables can be simple (flat) or grouped (sections). API: include pricing object in document creation payload. Supports custom columns, formulas, and subtotals. Currency is set per-document. Discount types: percentage or fixed amount."),
    ("PandaDoc Contacts", "PandaDoc contacts store recipient information: email, first_name, last_name, company, phone, address. Contacts are linked to documents as recipients. API: GET/POST /contacts. Contact merge creates duplicate prevention. Contacts can be imported via CSV. Contact custom fields store additional metadata."),
    ("PandaDoc Folders", "PandaDoc folder API organizes documents hierarchically. Folders support nesting up to 10 levels. API: GET /documents/folders, POST to create. Documents can be moved between folders. Folder permissions follow workspace roles. Shared folders visible to all workspace members. Personal folders are user-scoped."),
    ("PandaDoc API Pagination", "PandaDoc list endpoints use cursor-based pagination. Default page size: 50, max: 100. Response includes 'results' array and 'next' cursor URL. Use 'page' and 'count' query params. Sort options vary by endpoint. Filter by status, date range, folder, template. Always handle pagination for production integrations."),
]:
    _add("pandadoc_api", "reference", content.strip())


# ---------------------------------------------------------------------------
# Domain: document_creation (20 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Template-Based Document Creation", "Create documents from PandaDoc templates using POST /documents with template_uuid. Templates define layout, fields, and styling. Pass recipients, tokens (variables), and pricing data in the request body. Template fields are pre-positioned — API fills values. Supports conditional sections based on roles or variables."),
    ("Document From Scratch", "Create blank documents using POST /documents without template_uuid. Include name, recipients, and content array. Content elements: text, image, table, signature, date, initials, checkbox. Each element has position, page, and formatting. More flexible but requires precise layout specification."),
    ("Document Tokens", "PandaDoc tokens are template variables like {{client.name}} or {{contract.start_date}}. Pass token values in the 'tokens' array during creation: [{name: 'client.name', value: 'Acme Inc'}]. Tokens auto-fill in the rendered document. Unpopulated tokens show placeholder text. Custom tokens defined in template editor."),
    ("Document Recipients", "Recipients are parties who receive the document. Types: signer (must sign), CC (copy only), approver (internal approval). Each recipient needs: email, first_name, last_name, role. Role maps to template signing order. Multiple signers supported with sequential or parallel signing. Reassignment allowed before completion."),
    ("Document Fields API", "Document fields are interactive elements: signature, initials, text_field, date, checkbox, dropdown, payment. Fields are assigned to recipients by role. API: GET /documents/{id}/fields lists all fields. Pre-fill fields during creation with the 'fields' object. Required fields must be completed before signing."),
    ("Signature Fields", "E-signature fields support typed, drawn, and uploaded signatures. Each signer gets their own signature field(s). Signature captures: image, timestamp, IP address, browser info. Certificate of completion generated automatically. Signature audit trail includes signer identity verification steps."),
    ("Date Fields", "Date fields in PandaDoc auto-populate with signing date or can be pre-set. Format options: MM/DD/YYYY, DD/MM/YYYY, YYYY-MM-DD. Date fields can be required or optional. Validation: future dates only, past dates only, or any date. Date ranges supported with start/end field pairs."),
    ("Checkbox and Dropdown Fields", "Checkbox fields: single or grouped (radio button behavior). Default checked/unchecked state configurable. Dropdown fields: predefined options list, single or multi-select. Both field types can be required. Validation: minimum selections for multi-select. Options can include descriptions."),
    ("Payment Fields", "PandaDoc payment fields integrate with Stripe, PayPal, Square. Payment captured upon document completion. Supports one-time and recurring payments. Amount can be fixed or variable (from pricing table). Payment failure doesn't void the document — retry mechanism available."),
    ("Document Variables", "Variables in PandaDoc dynamically insert data: {{sender.first_name}}, {{recipient.email}}, {{document.name}}, {{date.created}}. Custom variables defined per template. Variables resolve at send time. Unresolved variables show as placeholder. Variables work in content, headers, and footers."),
    ("Content Sections", "Document content sections: header, body, footer, sidebar. Sections can be conditional (show/hide based on variables). Section ordering defines page flow. Content blocks within sections: text, image, table, field, page_break. Sections support custom styling: fonts, colors, margins."),
    ("Document Naming", "Document naming supports variables: 'Contract - {{client.name}} - {{date.created}}'. Names appear in dashboard and recipient emails. Max length: 255 characters. Special characters allowed but discouraged for file downloads. Rename via API: PATCH /documents/{id} with {name: 'new name'}."),
    ("Document Tags", "Tags organize documents for filtering and search. API: add tags during creation or via PATCH. Multiple tags per document. Tag-based filtering in list endpoints. Tags are workspace-scoped. Common tag patterns: department, client, contract-type, fiscal-year."),
    ("Bulk Document Creation", "Bulk creation: loop POST /documents with 500ms delays between requests. Rate limit: 2 docs/sec. For 100+ documents, use background jobs with status polling. Each document gets unique ID. Error handling: track failed documents, retry individually. Webhook notifications for batch completion."),
    ("Document Cloning", "Clone existing documents via POST /documents with source_document_id. Cloned documents inherit content, fields, and formatting. Recipients and tokens can be overridden in clone request. Useful for recurring contracts with same structure. Clone preserves template link for updates."),
    ("Document Expiration", "Set document expiration with 'expiration_date' field. Expired documents cannot be signed. Warning notifications sent before expiration (configurable days). Expired documents can be renewed by creating new version. Expiration visible in dashboard and recipient view. API: filter expired documents with status=expired."),
    ("Document Versions", "PandaDoc supports document versioning. New versions created on edit after send. Version history tracked with timestamps. Recipients see latest version. Previous versions archived but accessible. Version comparison not available via API. Revisions require re-send to recipients."),
    ("Embedded Signing", "Embedded signing integrates PandaDoc signing into your app. Generate signing session via POST /documents/{id}/session. Returns session_id for iframe URL. Session expires in 60 minutes. Supports custom branding (logo, colors). Redirect URL after completion configurable. Events posted to parent window via postMessage."),
    ("Document Download", "Download completed documents as PDF via GET /documents/{id}/download. Supports: original (with signatures), certificate_of_completion, combined. Download requires document status: completed. Protected documents require access token. Bulk download not available — iterate individually."),
    ("Smart Content", "Smart Content in PandaDoc: conditional blocks that show/hide based on variables or recipient roles. Use cases: multi-jurisdiction contracts (show state-specific clauses), tiered pricing (show relevant tier), role-specific terms. Configure in template editor with conditions. Conditions: equals, not equals, contains, greater than."),
]:
    _add("document_creation", "guide", content.strip())


# ---------------------------------------------------------------------------
# Domain: document_lifecycle (15 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Document Status Flow", "PandaDoc document lifecycle: draft → sent → viewed → waiting_approval → approved → completed. Alternative paths: draft → sent → declined, draft → sent → voided (by sender), draft → sent → expired. Status is immutable — cannot revert to previous status. Each transition generates webhook event and audit trail entry."),
    ("Draft Status", "Draft documents are editable. No notifications sent to recipients. Can be previewed by sender. Fields and content modifiable. Recipients can be added/removed. Once sent, returns to draft only by creating new version. Draft documents don't count toward sending quotas."),
    ("Sent Status", "Sent documents are locked for editing. Recipients receive email notification with signing link. Sender can resend notification, void, or download. Sent timestamp recorded in audit trail. Sequential signing: next signer notified only after previous completes. Parallel signing: all notified simultaneously."),
    ("Viewed Status", "Document transitions to 'viewed' when first recipient opens signing link. View timestamp and IP recorded. Sender notified of first view. Multiple views tracked in audit trail. View does not imply intent to sign. Session duration tracked for compliance."),
    ("Completed Status", "Document completed when all required signers have signed. Certificate of completion auto-generated. All parties receive completed copy via email. Document becomes read-only. Completed timestamp used for legal enforceability. PDF with digital signatures available for download."),
    ("Declined Status", "Recipients can decline to sign with optional reason. Sender notified immediately. Document status changes to 'declined'. Other recipients notified. Declined documents can be voided and re-created. Decline reason stored in audit trail for compliance."),
    ("Voided Status", "Sender can void sent documents to cancel. All recipients notified of voiding. Voided documents cannot be signed. Void reason recorded. Voiding is irreversible. Use case: incorrect terms, wrong recipient, superseded by new version."),
    ("Expired Status", "Documents expire after configured expiration date. Recipients notified before and at expiration. Expired documents cannot be signed. Renewal requires creating new document (clone). Expiration date visible to all parties. Default: no expiration unless explicitly set."),
    ("Approval Workflow", "PandaDoc approval workflow: add approver recipients. Approvers review before document sends to signers. Approval order configurable (sequential or parallel). Rejected approvals return document to draft. Approved documents auto-advance to signing phase. Approval timestamps recorded."),
    ("Reminder Notifications", "Send reminders to unsigned recipients via API: POST /documents/{id}/send. Configurable reminder schedule: manual, daily, every 3 days, weekly. Auto-reminders stop after document completion or voiding. Custom reminder message supported. Reminder count tracked per recipient."),
    ("Audit Trail", "PandaDoc audit trail records all document events: created, sent, viewed, signed, completed, declined, voided. Each entry: timestamp, actor (email), IP address, action, details. GET /documents/{id}/audit-trail returns chronological list. Audit trail is immutable — entries cannot be modified or deleted. Required for legal compliance (ESIGN Act, eIDAS)."),
    ("Document Reassignment", "Reassign documents to different recipients without voiding. API: POST /documents/{id}/recipients/{id}/reassign. New recipient receives notification. Original recipient loses access. Reassignment recorded in audit trail. Useful for: delegation, personnel changes, error correction."),
    ("Document Sharing", "Share document view-only links via API. Shared links have configurable expiration. Viewer activity tracked in audit trail. Sharing doesn't grant signing ability. Internal sharing (workspace members) vs external sharing (link-based). Revoke sharing by deleting link."),
    ("Certificate of Completion", "Auto-generated PDF after all parties sign. Contains: document summary, all signatures with timestamps, signer identification (email, IP), signing order, document hash for integrity verification. Certificate is legally binding evidence of electronic signing process. Download via API separately from signed document."),
    ("Document Retention", "PandaDoc stores completed documents indefinitely on paid plans. Deleted documents enter 30-day recovery window. Permanent deletion irreversible. Compliance retention: configure minimum retention periods per template. Export documents for external archival. GDPR: data subject deletion requests processed within 30 days."),
]:
    _add("document_lifecycle", "workflow", content.strip())


# ---------------------------------------------------------------------------
# Domain: template_management (15 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Template Basics", "PandaDoc templates are reusable document blueprints. Templates define: layout, content, fields, styling, and recipients roles. API: GET /templates lists all, GET /templates/{id} for details. Templates belong to workspace. Shared templates available to all workspace members. Template lock prevents unauthorized edits."),
    ("Template Creation", "Create templates in PandaDoc editor (no API creation). Define: pages, content blocks, fields, tokens, conditional sections, pricing tables. Set default recipients by role (signer_1, signer_2, etc.). Configure field validation, required fields, signing order. Save as draft or active. Template versioning tracks changes."),
    ("Template Variables", "Template tokens/variables: {{variable_name}} syntax. System variables: sender info, recipient info, dates. Custom variables: defined per template. Variable defaults: shown when not populated. Variable validation: text, number, date, email. Variables populate during document creation via API 'tokens' parameter."),
    ("Template Customization", "Customize templates per-document during creation: override default values, show/hide conditional sections, populate pricing tables, set recipient details. Template structure stays fixed — only variable content changes. Custom branding: logo, colors, fonts per workspace. Template categories for organization."),
    ("Template Roles", "Template roles define recipient positions: signer_1, signer_2, approver, cc. Roles map to document recipients during creation. Role-based field assignment: signature fields assigned to specific roles. Role permissions: sign, approve, view-only. Sequential roles define signing order."),
    ("Template Folders", "Organize templates in folders. Folder hierarchy up to 5 levels. API: GET /templates?folder_uuid={id}. Move templates between folders. Folder permissions inherit from workspace roles. Template search within folders. Archive unused templates to reduce clutter."),
    ("Content Library Integration", "Templates reference Content Library items for reusable blocks. Insert library items during template design. Library items update across all referencing templates when modified. Use cases: standard terms, compliance paragraphs, pricing components. Library items support tokens for customization."),
    ("Template Sharing", "Share templates between workspace members. Template permissions: view, edit, use (create documents). Template locking: prevent edits while preserving usage. Cross-workspace template sharing requires enterprise plan. Export templates as JSON for backup or migration."),
    ("Template Analytics", "Track template performance: documents created, completion rate, average time-to-sign, decline rate. API: template usage stats via documents list with template filter. Identify high-performing templates. A/B test template variations by creating variants and comparing completion rates."),
    ("Template Best Practices", "Template design best practices: clear section headings, logical field ordering, minimal required fields, sensible defaults for variables. Mobile-friendly layout: avoid wide tables, use single-column for signing. Test template with sample document before publishing. Version control: archive old versions, document changes."),
    ("Template Categories", "Categories organize templates by type: contracts, proposals, agreements, NDAs, invoices. Assign categories during template creation. Filter templates by category in API. Categories are workspace-scoped. Recommended categories for legal: Employment, NDA, MSA, SOW, Lease, Amendment."),
    ("Template Compliance", "Ensure templates meet legal requirements: include required disclosures, consent language, dispute resolution clauses. Compliance varies by jurisdiction and document type. Legal review recommended before publishing templates. Update templates when regulations change. Track template compliance review dates."),
    ("Conditional Template Sections", "Conditional sections show/hide content based on variables. Use cases: multi-state contracts (state-specific clauses), tiered agreements (tier-specific terms), optional addendums. Conditions: equals, contains, greater than, is empty. Nested conditions supported. Test all condition paths before publishing."),
    ("Template Migration", "Migrate templates between workspaces: export JSON, import in target. Field IDs regenerate during import — update API integrations. Recipient roles preserved. Content Library references may break — verify and re-link. Test migrated templates with sample documents before production use."),
    ("Template Versioning", "Template versions track changes over time. Each save creates a version. Revert to previous versions if needed. Documents created from a template version maintain that version's content. Template updates don't affect already-created documents. Version history accessible in template editor."),
]:
    _add("template_management", "guide", content.strip())


# ---------------------------------------------------------------------------
# Domain: esignature_compliance (15 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("ESIGN Act Overview", "U.S. Electronic Signatures in Global and National Commerce Act (ESIGN, 2000). Grants electronic signatures same legal status as handwritten. Requirements: consent to use e-signatures, ability to retain records, identity verification of signers. Covers: contracts, agreements, notices, disclosures. Exceptions: wills, family law, UCC certain articles, court orders."),
    ("UETA Compliance", "Uniform Electronic Transactions Act (UETA): state-level law adopted by 49 states (not NY, which has own ESIGN law). Requires: parties agree to conduct business electronically, records accurately preserved, identity verification. UETA and ESIGN work together — ESIGN preempts conflicting state laws. PandaDoc compliance: audit trail + certificate of completion."),
    ("eIDAS Regulation", "EU Electronic Identification and Trust Services (eIDAS): defines 3 signature levels: Simple (basic e-signature), Advanced (uniquely linked to signer, capable of identifying signer, under sole control, tamper-evident), Qualified (advanced + qualified certificate + created by qualified device). PandaDoc provides Advanced Electronic Signatures. Qualified requires additional identity verification provider."),
    ("Consent to E-Sign", "Legal requirement: obtain affirmative consent before e-signing. Consent should: describe types of records, explain withdrawal process, specify hardware/software requirements. PandaDoc: consent embedded in signing experience. Consent withdrawal: signers can request paper copies. Consent records maintained in audit trail."),
    ("Identity Verification", "Signer identity verification methods: email verification (default), SMS verification (phone number confirmation), knowledge-based authentication (KBA), government ID verification. PandaDoc supports: email + SMS. Enhanced verification (ID upload) available via integration partners. Verification level should match document sensitivity."),
    ("Record Retention", "E-signature law requires: accurate preservation of signed records, records accessible to all parties, records maintained for legally required periods. PandaDoc retention: indefinite on paid plans. Export capabilities: PDF with embedded signatures, certificate of completion, audit trail. Recommended: maintain external backup copies."),
    ("Tamper Evidence", "PandaDoc provides tamper evidence through: document hash (SHA-256) recorded at completion, hash included in certificate of completion, any modification invalidates hash, audit trail records all access and changes. Digital signature vs electronic signature: digital uses cryptographic certificate, electronic is broader category."),
    ("Cross-Border Validity", "E-signatures valid across borders in most cases. Key frameworks: ESIGN (US), eIDAS (EU), Electronic Commerce Act (UK), IT Act (India). Mutual recognition varies. Best practice: include governing law clause specifying jurisdiction. Some countries require specific signature levels for cross-border validity. PandaDoc operates globally with US/EU compliance."),
    ("Industry-Specific Requirements", "Healthcare (HIPAA): BAA required, PHI handling, access controls. Financial (FINRA): record retention 3-6 years, supervisory review. Real estate: varies by state, some require wet signatures for deeds. Government: varies by agency, often requires higher verification levels. Insurance: state-specific requirements for policy documents."),
    ("Notarization and E-Signatures", "Remote Online Notarization (RON): notary verifies identity via video call. Available in 40+ US states. Requires: identity proofing, video recording, digital certificate. PandaDoc integration: partner with RON providers for high-value documents. Use cases: real estate closings, powers of attorney, certain affidavits."),
    ("Signature Validity Challenges", "E-signatures can be challenged on: consent (party didn't agree to e-sign), identity (signer wasn't who they claimed), intent (accidental clicking), record integrity (document altered after signing). Defense: robust audit trail, identity verification, consent records, tamper-evident technology. PandaDoc provides all defense elements."),
    ("Multi-Party Signing", "Multi-party signing workflows: sequential (ordered), parallel (simultaneous), mixed (some sequential, some parallel). PandaDoc supports all modes. Signing order defined by recipient roles. Sequential: next signer notified after previous completes. Partial completion: track which parties have signed. All parties receive completed document."),
    ("Wet Signature Exceptions", "Documents typically requiring wet signatures: wills and testaments, certain real property deeds, court orders and pleadings, powers of attorney (varies by state), certain insurance cancellations, negotiable instruments (checks). Check state-specific requirements. When in doubt, consult legal counsel for document type."),
    ("Electronic Notarization", "Traditional notarization requires physical presence. Remote Online Notarization (RON) allows video-based notarization. In-person Electronic Notarization (IPEN) uses digital tools with physical presence. Requirements vary by state. Virginia was first state to authorize RON (2012). COVID accelerated RON adoption — temporary and permanent authorizations."),
    ("Audit Trail Best Practices", "Maintain comprehensive audit trails: timestamp all events (UTC), record IP addresses and geolocation, capture browser/device info, log all access (views, downloads, shares), record all status changes, preserve original and signed versions. Audit trail should be: chronological, immutable, complete, accessible. Retain for minimum 7 years (longer for regulated industries)."),
]:
    _add("esignature_compliance", "compliance", content.strip())


# ---------------------------------------------------------------------------
# Domain: common_contracts (25 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Non-Disclosure Agreement (NDA) Basics", "NDAs protect confidential information shared between parties. Types: unilateral (one-way), bilateral (mutual), multilateral (3+ parties). Key clauses: definition of confidential info, obligations of receiving party, exclusions, term, remedies. Duration: typically 2-5 years for obligations. Recommended for: vendor discussions, partnerships, M&A due diligence, employee onboarding."),
    ("NDA Key Clauses", "Essential NDA clauses: 1) Definition scope (broad vs narrow), 2) Permitted disclosures (employees, advisors, legal obligation), 3) Return/destruction of materials, 4) Term and survival (obligations outlast agreement), 5) Remedies (injunctive relief, damages), 6) Non-solicitation (optional). Avoid: overly broad definitions, unlimited duration, unreasonable penalties."),
    ("Master Service Agreement (MSA)", "MSA establishes overarching terms for ongoing business relationship. Covers: payment terms, IP ownership, liability limits, indemnification, termination, dispute resolution. Individual projects governed by Statements of Work (SOWs) referencing the MSA. Benefits: negotiate once, execute quickly via SOWs. Typical duration: 1-3 years with renewal options."),
    ("Statement of Work (SOW)", "SOW defines specific project under an MSA. Includes: scope of work, deliverables, timeline, milestones, acceptance criteria, pricing, resources. SOW should reference MSA for general terms. Change order process for scope modifications. Payment tied to milestones or time-and-materials. SOW termination independent of MSA."),
    ("Employment Agreement", "Employment agreements define: position and duties, compensation and benefits, work schedule, at-will or fixed term, confidentiality obligations, IP assignment, non-compete (where enforceable), non-solicitation, termination conditions, severance. State-specific requirements vary significantly. At-will presumption in most US states. Written agreements recommended even when not required."),
    ("Independent Contractor Agreement", "Contractor agreements must establish: independent relationship (not employment), scope of services, payment terms, IP ownership, confidentiality, insurance requirements, tax obligations (1099), termination rights. Key distinction from employment: contractor controls how/when work is performed. Misclassification risk: IRS factors, state tests (ABC test). Include: no benefits, own tools, multiple clients."),
    ("Commercial Lease Agreement", "Commercial lease key terms: rent amount and escalation, lease term and renewal options, permitted use, common area maintenance (CAM), tenant improvements (TI allowance), security deposit, insurance requirements, assignment/subletting, maintenance responsibilities, default and remedies. Types: gross lease, net lease (NNN), modified gross. Negotiate: rent abatement, early termination, expansion rights."),
    ("Software License Agreement", "Software licensing: perpetual vs subscription, user-based vs site-based, on-premise vs SaaS. Key terms: scope of use, restrictions, support/maintenance, updates/upgrades, data ownership, security obligations, SLA, audit rights, termination and data export, liability limits. SaaS-specific: uptime guarantees, data processing addendum, backup and recovery, API access."),
    ("Partnership Agreement", "Partnership agreements establish: contributions (capital, labor, IP), profit/loss sharing, management authority, decision-making process, new partner admission, partner withdrawal/buyout, dissolution terms, non-compete during and after, dispute resolution. Types: general partnership, limited partnership, LLP. Written agreement essential even if not legally required."),
    ("Consulting Agreement", "Consulting agreements cover: scope of services, deliverables, fees (hourly, fixed, retainer), expenses, timeline, confidentiality, IP assignment, non-compete (limited scope), indemnification, insurance, termination. Distinguish from employment: independence, expertise, limited duration. Include: conflict of interest clause, data protection, liability cap."),
    ("Sales Agreement", "Sales agreements for goods: description and quantity, price and payment terms, delivery terms (FOB, CIF), warranties, inspection and acceptance, risk of loss, title transfer, returns/defects, limitation of liability, force majeure. UCC Article 2 governs (goods over $500). Terms: Net 30/60/90, discount for early payment, late payment penalties."),
    ("Service Level Agreement (SLA)", "SLA defines: service descriptions, performance metrics (uptime, response time), measurement methods, reporting frequency, credits/remedies for breaches, exclusions (maintenance windows, force majeure), escalation procedures, review and amendment process. Common metrics: 99.9% uptime, 4-hour response for P1, 24-hour for P2. Credits: service credits, not cash refunds typically."),
    ("Non-Compete Agreement", "Non-compete agreements restrict post-employment competition. Enforceability varies by state (California bans most). Elements: reasonable scope (geography, duration, activities), consideration (employment or additional payment), protectable interest (trade secrets, relationships). Trends: increasing restrictions on enforceability, FTC proposed ban. Duration: typically 1-2 years. Geographic scope should match actual business footprint."),
    ("Intellectual Property Assignment", "IP assignment transfers ownership from creator to company. Covers: patents, copyrights, trade secrets, trademarks. Key provisions: present and future IP related to business, moral rights waiver (where applicable), cooperation in prosecution, representations of originality. Work-for-hire doctrine: employer owns employee-created IP in course of employment. Contractors: explicit assignment required."),
    ("Data Processing Agreement (DPA)", "DPA required under GDPR for data processors. Includes: scope and purpose of processing, data categories, duration, processor obligations (security, sub-processors, breach notification), controller rights (audit, deletion), cross-border transfer mechanisms (SCCs, adequacy decisions). Include: technical and organizational measures (TOMs), sub-processor list and notification, data subject rights assistance."),
    ("Indemnification Clauses", "Indemnification allocates liability between parties. Types: first-party (direct losses), third-party (claims by others). Scope: narrow (breach of agreement) vs broad (negligence, IP infringement). Cap: often tied to contract value. Survival: typically outlasts agreement by 1-3 years. Mutual vs one-way. Include: defense obligations, control of litigation, settlement approval."),
    ("Limitation of Liability", "Liability caps protect against catastrophic claims. Types: aggregate cap (total liability), per-incident cap, consequential damages exclusion. Common cap: 12 months of fees paid. Carve-outs (uncapped): willful misconduct, indemnification obligations, confidentiality breach, IP infringement. Consequential damages: lost profits, business interruption typically excluded. Essential for service providers."),
    ("Force Majeure", "Force majeure excuses performance during extraordinary events. Events: natural disasters, war, government action, pandemic, strikes, utility failures. Requirements: event beyond reasonable control, unable to perform despite reasonable efforts, timely notice. Duration: suspension of obligations during event. Termination right if event exceeds specified period (typically 30-90 days). Post-COVID: more detailed pandemic language common."),
    ("Dispute Resolution", "Dispute resolution mechanisms: negotiation (required first step), mediation (neutral facilitator), arbitration (binding, limited appeal), litigation (courts). Choice of law and venue clauses. Arbitration: AAA, JAMS, or ICC rules, single or panel, costs allocation. Advantages: arbitration (speed, privacy), litigation (precedent, appeal rights). Include: escalation timeline, interim relief preservation, prevailing party attorney fees."),
    ("Governing Law", "Governing law determines which state/country law applies. Choice of law clause: 'This agreement shall be governed by the laws of [state/country].' Venue clause: where disputes are heard. Consider: where parties are located, where services performed, favorable legal framework. Delaware popular for corporate agreements. New York for financial contracts. Separate from jurisdiction (court authority over parties)."),
    ("Amendment and Waiver", "Amendment clauses: require written agreement signed by both parties. No oral modifications. Waiver: failure to enforce a provision doesn't waive future enforcement. Written waiver required for specific instances. Entire agreement clause: contract supersedes all prior discussions. Severability: invalid provisions don't void entire agreement."),
    ("Termination Clauses", "Termination types: for convenience (with notice period, typically 30 days), for cause (material breach with cure period), automatic (at end of term), mutual agreement. Effects: surviving obligations (confidentiality, IP, indemnification), wind-down period, data return/destruction, final payments. Notice requirements: written, specified delivery method."),
    ("Warranty Clauses", "Warranties: express (stated in contract) vs implied (UCC: merchantability, fitness). Warranty disclaimer: 'AS IS' language. Limited warranty: scope, duration, remedy (repair, replace, refund). Representations: statements of fact at signing. Warranties: ongoing obligations. Breach: damages, termination right. Include: mutual representations (authority, non-infringement)."),
    ("Assignment Clauses", "Assignment clauses control transfer of contract rights. Options: freely assignable, consent required (not unreasonably withheld), non-assignable. Change of control provision: assignment triggered by acquisition. Anti-assignment: contract voidable if assigned without consent. Exceptions: assignment to affiliates, successors in merger. Notice requirement for permitted assignments."),
    ("Confidentiality in Contracts", "Confidentiality clauses in commercial contracts: scope of confidential information, permitted disclosures, obligations (reasonable care, same as own), exceptions (public knowledge, independently developed, legally compelled), duration (2-5 years or perpetual for trade secrets), return/destruction obligations, injunctive relief available. Broader than standalone NDA — embedded in main agreement."),
]:
    _add("common_contracts", "template", content.strip())


# ---------------------------------------------------------------------------
# Domain: legal_best_practices (20 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Contract Review Checklist", "Before signing any contract: 1) Verify parties and authority, 2) Check scope and deliverables, 3) Review payment terms and penalties, 4) Assess liability caps and indemnification, 5) Verify IP ownership, 6) Check confidentiality obligations, 7) Review termination rights, 8) Assess governing law, 9) Check insurance requirements, 10) Review dispute resolution. Involve counsel for contracts over $50K or unusual terms."),
    ("Document Retention Policy", "Implement retention schedules by document type: tax records (7 years), employment records (4 years after termination), contracts (6 years after expiration), corporate records (permanent), financial statements (permanent), insurance policies (permanent), real estate (permanent). State requirements may extend periods. Destruction: secure shredding, certificate of destruction. Digital: verified deletion."),
    ("Small Business Legal Priorities", "Legal priorities for small businesses: 1) Entity formation and operating agreement, 2) EIN and state registrations, 3) Employment agreements and handbook, 4) IP protection (trademarks, trade secrets), 5) Standard contract templates (NDA, MSA, SOW), 6) Privacy policy and terms of service, 7) Insurance coverage, 8) Compliance program (industry-specific). Budget: $2-5K startup, $1-3K/year ongoing."),
    ("Vendor Management Legal", "Vendor management legal requirements: due diligence before engagement, written agreements with all vendors, insurance verification (COI), data processing agreements (if accessing data), regular compliance audits, incident response obligations, termination and transition planning, subcontractor flow-down requirements. Track: contract dates, renewal deadlines, insurance expiration."),
    ("Employment Law Basics", "Employment law fundamentals: at-will employment (most states), anti-discrimination (Title VII, ADA, ADEA), wage and hour (FLSA), family leave (FMLA), workplace safety (OSHA), workers' compensation, unemployment insurance, I-9 verification. State laws may provide additional protections. Key documents: offer letter, employment agreement, employee handbook, I-9, W-4."),
    ("Intellectual Property Protection", "IP protection strategy: 1) Trademark: register brand name, logo, slogans (USPTO), 2) Copyright: automatic upon creation, register for enforcement, 3) Patents: file provisional within 1 year of public disclosure, 4) Trade secrets: NDAs, access controls, employee training. Cost: trademark $250-1K, copyright $45-65, patent $5-15K. Monitor and enforce: cease & desist, DMCA takedowns."),
    ("Privacy Compliance Basics", "Privacy compliance for small businesses: 1) Privacy policy (website, app), 2) Cookie consent (if serving EU users), 3) Data inventory (what you collect, where stored), 4) Data security (encryption, access controls), 5) Breach notification plan, 6) Vendor data processing agreements, 7) Employee privacy training. Key laws: CCPA/CPRA (California), GDPR (EU), state privacy laws. Budget: $1-5K for initial compliance."),
    ("Insurance for Small Businesses", "Essential insurance coverage: 1) General liability ($1M-2M), 2) Professional liability/E&O ($1M+), 3) Workers' compensation (required in most states), 4) Commercial property, 5) Cyber liability ($1M+), 6) Business interruption, 7) Directors & Officers (if incorporated), 8) Employment practices liability. Review annually. Certificate of Insurance (COI) for vendor requirements."),
    ("Contract Negotiation Tips", "Contract negotiation best practices: 1) Start with your template (control baseline terms), 2) Identify must-haves vs nice-to-haves, 3) Propose balanced risk allocation, 4) Use redlines and track changes, 5) Escalate dealbreakers early, 6) Document all negotiations in writing, 7) Set deadline for execution, 8) Review final version carefully (clean vs redline). Don't: accept first draft without review, agree verbally without written confirmation."),
    ("Regulatory Compliance Framework", "Build a compliance framework: 1) Identify applicable regulations (federal, state, industry), 2) Document compliance requirements, 3) Assign compliance owners, 4) Implement controls and procedures, 5) Train employees, 6) Monitor and audit, 7) Report and remediate, 8) Review annually. Common frameworks: SOC 2 (SaaS), HIPAA (healthcare), PCI DSS (payments), GDPR (EU data). Consider fractional compliance officer for SMBs."),
    ("Risk Management for Contracts", "Contract risk management: identify risk categories (financial, operational, legal, reputational), assess likelihood and impact, implement mitigations (caps, insurance, escrow, warranties), document risk acceptance, monitor ongoing obligations, review at renewal. High-risk indicators: uncapped liability, unlimited indemnification, broad IP grants, auto-renewal, most-favored-nation clauses."),
    ("Legal Entity Selection", "Entity types for small businesses: Sole proprietorship (simplest, unlimited liability), LLC (liability protection, tax flexibility), S-Corp (salary + distributions, tax savings above $50K profit), C-Corp (investors, unlimited shareholders, double taxation), Partnership (multiple owners, pass-through tax). Consider: liability protection, tax efficiency, growth plans, investment needs. Most SMBs start LLC, convert to S-Corp at $80-100K net income."),
    ("Terms of Service Essentials", "Terms of Service for digital products: 1) Acceptance mechanism (clickwrap > browsewrap), 2) Service description, 3) User obligations and restrictions, 4) Payment terms, 5) Intellectual property rights, 6) Limitation of liability, 7) Indemnification, 8) Dispute resolution (arbitration clause), 9) Termination rights, 10) Modification process. DMCA safe harbor provision for user-generated content."),
    ("Client Engagement Letters", "Professional service engagement letters: scope of services (specific deliverables), fees (hourly/fixed/retainer), payment terms, timeline, client responsibilities (information, access), confidentiality, conflict check, termination rights, file retention. Industries: accounting, legal, consulting, architecture. Engagement letter = contract. Get signed before starting work. Amendment for scope changes."),
    ("Mergers and Acquisitions Basics", "M&A process for small businesses: 1) Valuation (multiples, DCF), 2) Letter of Intent (non-binding terms), 3) Due diligence (legal, financial, operational), 4) Purchase agreement (asset vs stock purchase), 5) Closing conditions, 6) Post-closing integration. Key legal issues: representations and warranties, indemnification, escrow holdback, employee transition, customer contract assignment, IP transfer."),
    ("Lease Negotiation for Small Business", "Commercial lease negotiation priorities: 1) Total occupancy cost (rent + CAM + taxes + insurance), 2) Lease term and renewal options, 3) Rent escalation caps (2-3% annual), 4) Tenant improvement allowance, 5) Personal guarantee scope and limit, 6) Subletting rights, 7) Early termination option, 8) Exclusive use clause, 9) Signage rights, 10) Maintenance responsibilities. Hire commercial real estate attorney for leases over $100K total value."),
    ("Franchise Law Basics", "Franchise regulation: FTC Franchise Rule requires Franchise Disclosure Document (FDD) 14+ days before signing. FDD contains 23 items including: fees, territory, obligations, financial performance. State registration required in 14+ states. Key negotiable terms: territory exclusivity, renewal terms, transfer rights. Legal costs: $5-15K for FDD review. Franchisee associations provide advocacy."),
    ("Business Succession Planning", "Succession planning elements: 1) Buy-sell agreement (triggers: death, disability, retirement, divorce), 2) Valuation method (agreed formula or independent appraisal), 3) Funding mechanism (life insurance, installment payments), 4) Key person insurance, 5) Management succession plan, 6) Estate planning integration, 7) Tax planning (Section 1042, installment sale). Review buy-sell agreement annually. Update after major business changes."),
    ("Government Contracting Basics", "Government contracting for small businesses: SAM.gov registration, NAICS code selection, small business certifications (SBA 8(a), HUBZone, WOSB, SDVOSB). Contract types: fixed-price, time-and-materials, cost-reimbursement. Compliance: FAR (Federal Acquisition Regulation), DFAR (Defense), CAS (Cost Accounting Standards). Set-aside programs reserve contracts for small businesses. Subcontracting opportunities with prime contractors."),
    ("International Business Legal", "International business legal considerations: 1) Entity structure (branch vs subsidiary), 2) Tax planning (transfer pricing, tax treaties), 3) Employment law (local labor laws, works councils), 4) IP protection (country-specific registration), 5) Export controls (EAR, ITAR), 6) Anti-corruption (FCPA, UK Bribery Act), 7) Data transfer (GDPR, SCCs), 8) Currency and payment risks, 9) Dispute resolution (international arbitration — ICC, LCIA)."),
]:
    _add("legal_best_practices", "best_practice", content.strip())


# ---------------------------------------------------------------------------
# Domain: aspire_governance (10 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Clara Risk Tiers", "Clara operates at YELLOW and RED risk tiers in Aspire. YELLOW operations: creating document drafts, sending documents for review, checking document status, listing templates. RED operations: sending documents for signature, executing e-signatures on behalf of user, voiding signed documents. All operations require capability tokens and produce receipts."),
    ("Contract Approval Flow", "Aspire contract approval flow: 1) User requests contract via Ava, 2) Ava routes to Clara (legal desk), 3) Clara creates draft in PandaDoc (YELLOW — requires confirmation), 4) User reviews draft in UI, 5) User approves sending for signature (RED — requires explicit authority), 6) Clara sends via PandaDoc, 7) Receipt generated with document ID and status. Each step produces immutable receipt."),
    ("Legal Receipt Requirements", "Every Clara action produces a receipt containing: receipt_id, correlation_id, suite_id, action_type (legal.*), risk_tier, document_id (PandaDoc), template_id, recipient_list (redacted emails), status, created_at. RED operations additionally include: authority_evidence (approval ID), capability_token_id, expiry verification. Receipts are append-only — no updates or deletes."),
    ("Document Access Control", "Document access in Aspire follows tenant isolation (Law #6): documents belong to a suite_id, RLS policies prevent cross-tenant access, capability tokens scoped to specific document operations, audit trail tracks all access. Clara cannot access documents belonging to other tenants. PandaDoc workspace isolation provides additional boundary."),
    ("Legal Capability Tokens", "Capability tokens for Clara operations: minted by orchestrator, scoped to specific action + document + tenant, expires in <60 seconds, server-verified before PandaDoc API call. Token claims: suite_id, action (legal.create_draft, legal.send_for_signature, legal.void), document_id, timestamp. Token rejection produces denial receipt."),
    ("Contract Template Governance", "Aspire manages contract templates through Clara: template selection from approved library, template customization within defined parameters, no unauthorized template creation (governance control). Templates categorized by risk: NDA (YELLOW), Employment (YELLOW), Service Agreement (YELLOW), Equity Agreement (RED). Template changes require admin approval."),
    ("Legal Audit Trail", "Aspire legal audit trail: combines PandaDoc audit trail with Aspire receipt chain. Every document operation cross-referenced: PandaDoc event → Aspire receipt → correlation_id link. Audit queries: all documents for a tenant, all actions on a document, all RED operations by an actor. Audit trail immutable — stored in receipts table with hash chain verification."),
    ("E-Signature Governance", "E-signature governance in Aspire: RED risk tier (highest authority required). Flow: 1) Clara prepares document, 2) User reviews in UI, 3) User provides explicit authority (approval record), 4) Clara mints capability token for signature action, 5) PandaDoc API sends for signature, 6) Receipt with authority_evidence, 7) Webhook monitors completion. No autonomous signing — human authority always required."),
    ("Legal Data Privacy", "Clara handles sensitive legal data: PII redaction on all logged content (Presidio DLP), document content never stored in Aspire DB (lives in PandaDoc), only metadata and status in Aspire, recipient emails partially redacted in receipts, no document content in logs. GDPR compliance: data processing agreement with PandaDoc, right to deletion supported."),
    ("Legal Desk Limitations", "Clara's current limitations in Aspire: no contract analysis/comparison (future capability), no legal advice (informational only), no automated clause negotiation, no integration with legal research databases (future), no multi-jurisdiction compliance checking (future). Clara is a contract management tool, not a legal advisor. Users should consult legal counsel for complex matters."),
]:
    _add("aspire_governance", "governance", content.strip())


# ---------------------------------------------------------------------------
# Domain: recipient_management (10 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Recipient Types", "PandaDoc recipient types: Signer (must complete signature fields), Approver (internal review before sending to signers), CC (receives copy, no action required), Viewer (can view but not sign). Each recipient has: email, first_name, last_name, role (maps to template position). Recipient order defines signing sequence in sequential workflows."),
    ("Signing Order", "Signing order options: sequential (one at a time, in order), parallel (all notified simultaneously), mixed (groups sign in parallel, groups in sequence). Configure per-document. Sequential: receipt notification triggers next signer. Parallel: all receive simultaneously. Mixed: define signing groups with order between groups."),
    ("Recipient Notifications", "Notification types: initial send (signing invitation), reminder (configurable schedule), viewed (optional sender notification), completed (all parties), declined (sender + other recipients), voided (all parties), expired (all parties). Customize: email subject, message body, branding. Disable specific notifications per recipient if needed."),
    ("CC Recipients", "CC recipients receive completed document copy. No signing required. Added during document creation or after sending. CC recipients see final document only (not intermediate states). Use cases: legal department copy, file manager copy, compliance record. CC recipients tracked in audit trail."),
    ("Approver Workflow", "Approver recipients review documents before signers. Approval flow: document created → approver notified → approver reviews → approve/reject. Rejection returns to draft. Multiple approvers: sequential or parallel. Approval tracked in audit trail. Approver can add comments. Approved documents auto-advance to signing phase."),
    ("Recipient Fields Assignment", "Fields assigned to recipients by role. Each signer has specific fields to complete. Field types per recipient: signature, initials, date, text, checkbox. Required vs optional per field. Assignment via template (pre-positioned) or API (runtime positioning). Unassigned required fields block document completion."),
    ("Recipient Authentication", "Authentication levels: email (default — recipient verifies by accessing email), SMS (additional phone verification code), password (recipient-specific password), knowledge-based (identity quiz). Set per-recipient. Higher authentication for sensitive documents. Authentication events recorded in audit trail with method and timestamp."),
    ("Delegation and Reassignment", "Recipients can be reassigned to different individuals. API: POST /documents/{id}/recipients/{id}/reassign with new recipient details. Original recipient loses access. New recipient receives notification. Reassignment recorded in audit trail. Use cases: vacation coverage, role changes, error correction. Restrictions: cannot reassign after recipient has signed."),
    ("Bulk Recipient Management", "Managing recipients across multiple documents: use contacts API for consistent recipient data, template roles for standardized assignments, webhooks for tracking recipient actions. Bulk operations: create documents with same recipient list, update recipient details across pending documents. Contact merge prevents duplicates."),
    ("Recipient Experience", "Recipient signing experience: email notification → click link → view document → complete fields → sign → confirmation. Mobile-responsive signing page. No PandaDoc account required for recipients. Signing session timeout: configurable (default 60 minutes). Session can be resumed from notification link. Progress saved if session interrupted."),
]:
    _add("recipient_management", "guide", content.strip())


# ---------------------------------------------------------------------------
# Domain: audit_logging (10 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Audit Trail Structure", "PandaDoc audit trail entries: timestamp (ISO 8601), actor (email or system), action (created, sent, viewed, signed, etc.), IP address, user agent, document_id, recipient_id (if applicable). GET /documents/{id}/audit-trail returns chronological list. Events are immutable — cannot be modified or deleted after creation."),
    ("Document Creation Events", "Audit events for document creation: document.created (who, when, from template or scratch), document.updated (content changes before sending), document.fields_updated (field modifications), document.recipients_added, document.recipients_removed. All events before send are draft-phase events."),
    ("Signing Events", "Signing-related audit events: document.sent (to recipients), document.viewed (per recipient, with IP), document.form_fields_completed (per field), document.signed (per recipient), document.completed (all recipients done). Each event includes: signer identity, timestamp, IP, verification method used."),
    ("Administrative Events", "Administrative audit events: document.voided (reason, actor), document.declined (reason, recipient), document.reassigned (old → new recipient), document.downloaded (who, format), document.shared (link created, expiration), document.deleted (soft delete, recoverable 30 days)."),
    ("Webhook Event Integration", "Integrate PandaDoc webhooks with Aspire audit: document_state_changed → store receipt with new status, recipient_completed → store per-signer receipt, document_updated → track modifications. Webhook payload includes: event, data (document details), timestamp. Verify webhook signature before processing. Idempotent processing: use event ID to prevent duplicates."),
    ("Compliance Reporting", "Generate compliance reports from audit trail: all documents signed in date range, documents pending beyond SLA, declined/voided documents with reasons, recipient response times, documents by status distribution. API: filter documents by status and date, then fetch audit trails. Aggregate for compliance dashboards."),
    ("Audit Trail Export", "Export audit trails for external archival: GET /documents/{id}/audit-trail for per-document export. Batch export: iterate document list, fetch each trail. Format: JSON (API native), convert to CSV for spreadsheet analysis. Include: document metadata, all events, certificate of completion. Recommended: daily automated export for compliance."),
    ("Event Timestamps", "All PandaDoc events use ISO 8601 timestamps in UTC. Timestamp precision: seconds. Events ordered chronologically. Time zone conversion for display: use client-side conversion. Timestamp integrity: events cannot be backdated or modified. Consecutive events may share timestamp (same-second operations)."),
    ("Access Logging", "Track who accesses documents: view events with IP and user agent, download events with format requested, share link access events, API access events (API key identification). Access patterns help detect: unauthorized access attempts, unusual viewing patterns, compliance with need-to-know policies. Monitor: frequency of access, geographic anomalies."),
    ("Retention and Archival", "Audit trail retention: PandaDoc retains indefinitely on paid plans. Regulatory requirements may mandate external archival. Best practices: export and archive monthly, store in immutable storage (S3 with Object Lock), maintain chain of custody documentation, test restoration process quarterly. Deletion: removing document removes audit trail — export first."),
]:
    _add("audit_logging", "reference", content.strip())


# ---------------------------------------------------------------------------
# Domain: error_handling (10 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("HTTP Status Codes", "PandaDoc API HTTP codes: 200 (success), 201 (created), 204 (deleted), 400 (bad request — check body), 401 (auth failed — check API key), 403 (forbidden — check permissions), 404 (not found — check ID), 409 (conflict — document locked), 422 (validation — check fields), 429 (rate limited — back off), 500 (server error — retry). Always check response.detail for specific error message."),
    ("Rate Limit Handling", "When receiving 429: read Retry-After header, implement exponential backoff (1s, 2s, 4s, 8s, max 30s), add jitter (random 0-1s). Proactive: track request rate, pre-throttle before hitting limits. Batch operations: 500ms between requests. Document creation: max 2/sec. Read operations: max 10/sec. Consider webhook-based architecture to reduce polling."),
    ("Authentication Errors", "401 errors: API key invalid (check for extra whitespace), API key expired (regenerate), API key revoked (contact admin), wrong workspace (check workspace ID). OAuth2: token expired (refresh), invalid scope (check permissions). Fix: verify key in PandaDoc settings, test with curl, check environment variable injection."),
    ("Document Not Found", "404 errors for documents: document ID incorrect (check format — UUID), document deleted (check trash), document in different workspace, insufficient permissions (403 possible). Debugging: list documents and search, check workspace context, verify API key workspace scope. Deleted documents: 30-day recovery window."),
    ("Validation Errors", "422 validation errors: missing required fields, invalid field format (email, date), recipient missing required info, template token not found, pricing table validation failure, field value exceeds max length. Response includes: field name, error message, expected format. Fix: validate input before API call, check template field definitions."),
    ("Conflict Errors", "409 conflict errors: document is locked (another user editing), document status prevents action (can't edit sent document), concurrent modification (version conflict). Resolution: retry after delay, check document status before action, implement optimistic locking pattern. Sent documents: create new version instead of editing."),
    ("Webhook Delivery Failures", "Webhook failures: endpoint unreachable (check URL, SSL), timeout (respond within 10 seconds), non-2xx response (check handler logic). PandaDoc retry: 3 attempts with exponential backoff. Debugging: check webhook logs in PandaDoc dashboard, verify SSL certificate, test endpoint with curl. Implement: dead letter queue for failed events."),
    ("Timeout Handling", "API timeouts: default 30 seconds for most operations, document creation may take longer (complex templates). Implement: client-side timeout (35s), retry with idempotency key, async pattern for long operations (create → poll status). Network timeouts: check proxy settings, DNS resolution, SSL handshake. Circuit breaker: stop requests after 3 consecutive timeouts."),
    ("Bulk Operation Errors", "Errors during bulk operations: track per-item success/failure, continue processing remaining items, collect error details for retry, implement idempotency (content hash prevents duplicates). Error aggregation: group by error type, prioritize fixes. Partial failure handling: log successful IDs, retry failed items individually."),
    ("Error Recovery Patterns", "Error recovery for PandaDoc integrations: 1) Transient errors (429, 500, timeout): retry with backoff, 2) Validation errors (400, 422): fix input, don't retry, 3) Auth errors (401, 403): refresh credentials, alert admin, 4) Not found (404): verify resource exists, 5) Conflict (409): check state, resolve conflict. Implement: health check endpoint, circuit breaker, fallback behavior."),
]:
    _add("error_handling", "troubleshooting", content.strip())


# =============================================================================
# Seeding Logic
# =============================================================================

async def seed_knowledge():
    """Embed and insert all legal knowledge chunks."""
    from aspire_orchestrator.services.legal_embedding_service import embed_batch, compute_content_hash
    from aspire_orchestrator.services.supabase_client import supabase_insert

    total = len(LEGAL_KNOWLEDGE)
    logger.info("Seeding %d legal knowledge chunks...", total)

    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = LEGAL_KNOWLEDGE[i:i + batch_size]
        texts = [c["content"] for c in batch]

        try:
            embeddings = await embed_batch(texts, suite_id="system")
        except Exception as e:
            logger.error("Embedding batch %d failed: %s", i // batch_size + 1, e)
            continue

        rows = []
        for j, chunk in enumerate(batch):
            content_hash = compute_content_hash(chunk["content"])
            row = {
                "id": str(uuid.uuid4()),
                "content": chunk["content"],
                "content_hash": content_hash,
                "embedding": f"[{','.join(str(x) for x in embeddings[j])}]",
                "domain": chunk["domain"],
                "chunk_type": chunk.get("chunk_type"),
                "is_active": True,
                "ingestion_receipt_id": f"seed-{uuid.uuid4().hex[:12]}",
            }
            rows.append(row)

        try:
            result = await supabase_insert("legal_knowledge_chunks", rows)
            inserted += len(rows)
            logger.info(
                "Batch %d/%d: inserted %d chunks (total: %d/%d)",
                i // batch_size + 1,
                (total + batch_size - 1) // batch_size,
                len(rows), inserted, total,
            )
        except Exception as e:
            err_msg = str(e)
            if "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
                skipped += len(rows)
                logger.info("Batch %d: %d chunks already exist (dedup)", i // batch_size + 1, len(rows))
            else:
                logger.error("Insert batch %d failed: %s", i // batch_size + 1, e)

    logger.info(
        "Seeding complete: %d inserted, %d skipped (dedup), %d total",
        inserted, skipped, total,
    )


if __name__ == "__main__":
    asyncio.run(seed_knowledge())
