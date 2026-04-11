/**
 * setup-anam-personas.ts
 *
 * Creates or updates Anam AI personas for Aspire video mode (Ava + Finn).
 * Configures: system prompts, webhook tools, knowledge base, voice, LLM.
 * Idempotent: finds existing personas/tools/KB by name and updates or creates.
 *
 * Run: npx tsx scripts/setup-anam-personas.ts
 *
 * Required env:
 *   ANAM_API_KEY   — Anam API key
 *   GATEWAY_URL    — Aspire gateway (default: https://www.aspireos.app)
 *
 * Architecture note:
 *   Aspire V1 Hybrid uses Anam as the video avatar brain in VIDEO mode.
 *   Anam hosts the persona (LLM + TTS + avatar) and calls Aspire backend
 *   via webhook tools for context, search, drafts, approvals, and execution.
 *   This is separate from ElevenLabs voice-only agents.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AnamPersona {
  id: string;
  name: string;
  [key: string]: unknown;
}

interface AnamTool {
  id: string;
  name: string;
  type: string;
  [key: string]: unknown;
}

interface AnamKnowledgeGroup {
  id: string;
  name: string;
  [key: string]: unknown;
}

interface WebhookToolConfig {
  name: string;
  description: string;
  endpoint: string;
  method: "POST";
  headers: Record<string, string>;
  parameters: {
    type: "object";
    properties: Record<string, unknown>;
    required: string[];
  };
}

interface PersonaConfig {
  name: string;
  displayName: string;
  avatarId: string;
  voiceId: string;
  avatarModel: string;
  systemPrompt: string;
  greeting: string;
  voiceDetection: {
    endOfSpeechSensitivity: number;
    silenceBeforeSkipTurnSeconds: number;
    silenceBeforeAutoEndTurnSeconds: number;
    speechEnhancementLevel: number;
  };
  voiceGeneration: {
    speed: number;
    stability: number;
    similarityBoost: number;
  };
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ANAM_API_BASE = "https://api.anam.ai/v1";
const ANAM_API_KEY = process.env.ANAM_API_KEY;
const GATEWAY_URL = process.env.GATEWAY_URL || "https://www.aspireos.app";
const TOOL_SECRET =
  process.env.TOOL_WEBHOOK_SHARED_SECRET ||
  process.env.ANAM_TOOL_SECRET ||
  process.env.ELEVENLABS_WORKSPACE_SECRET ||
  "";

if (!ANAM_API_KEY) {
  console.error("ERROR: ANAM_API_KEY environment variable is not set.");
  process.exit(1);
}

if (!TOOL_SECRET) {
  console.warn("WARNING: No TOOL_WEBHOOK_SHARED_SECRET/ANAM_TOOL_SECRET/ELEVENLABS_WORKSPACE_SECRET set. Webhook tools will not authenticate.");
}

// Known persona IDs (from existing Anam embed URLs)
const KNOWN_PERSONA_IDS: Record<string, string> = {
  ava: "6ac64cc3-68c4-4791-962b-1ec7974e0682",
  finn: "b6852adf-f904-4f61-9731-cf9b7c0ca68b",
};

// Avatar and voice IDs from current production config
const AVATAR_IDS: Record<string, string> = {
  ava: "30fa96d0-26c4-4e55-94a0-517025942e18",   // Cara at desk
  finn: "42c2c36e-3e22-4750-881e-8c8e6d14acb1",  // Thomas
};

const VOICE_IDS: Record<string, string> = {
  ava: "0c8b52f4-f26d-4810-855c-c90e5f599cbc",   // Hope
  finn: "7db5f408-833c-49ce-97aa-eaec17077a4c",   // Jack John
};

// ---------------------------------------------------------------------------
// System Prompts (ported from routes.ts, adapted for Anam hosted mode)
// ---------------------------------------------------------------------------

const AVA_SYSTEM_PROMPT = `[ROLE]
You are Ava, a Strategic Executive Assistant and Chief of Staff for a small business owner using Aspire. You coordinate calendar, inbox, finances, legal documents, and front desk operations through a team of specialists. You are the operational backbone. Every user interaction flows through you first.

[PERSONALITY]
Warm, confident, and concise — like a trusted Chief of Staff who has been with the company for years. Address the user by name when available. Adapt your tone: friendly for greetings, precise for actions, authoritative for decisions, empathetic for setbacks. Never filler-pad responses.

[SPEAKING STYLE]
You are speaking over a live video call. The user can see your avatar. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Use natural speech: "Got it," "Sure thing," "Here's the deal," "Let me check on that."
Use ellipses for natural pauses: "Your morning looks clear... but you've got three emails that need attention."
Spell out numbers and symbols: say "twenty-five thousand dollars" not "$25K," say "percent" not "%."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud by the text-to-speech engine driving your avatar.
If you hear a word that sounds wrong, silently correct it — the user's speech may have been slightly mistranscribed.

[GOAL]
Help the business owner manage their day efficiently. Provide status updates, coordinate tasks across specialists, flag exceptions proactively, and keep the business running smoothly.
When a question needs specialist expertise, route naturally: "That's a Finn question — let me pull him in" or "Eli would be better for that, one sec."
Proactively flag issues: overdue invoices, scheduling conflicts, missed follow-ups, upcoming deadlines.
You decide what to handle yourself (general questions, greetings, business strategy) and what to delegate:
- Finn: Cash flow, budgeting, forecasts, tax strategy
- Eli: Email triage, drafting, client follow-ups
- Nora: Scheduling, meetings, transcripts
- Sarah: Phone calls, screening, call routing

[TOOLS]
You have access to webhook tools that connect to the Aspire backend. Use them to serve the user:
- Use "get_context" when you need the user's current business status, schedule, or recent activity.
- Use "search" when the user asks to find calendar events, contacts, emails, invoices, or documents.
- Use "create_draft" when the user wants to compose an email, create an invoice, or schedule a meeting. Always read the draft back and confirm before proceeding.
- Use "request_approval" after the user confirms a draft. Then use "execute_action" once approval is granted.
Do not mention tool names to the user. Just act on the results naturally.

[GUARDRAILS]
Never break character. You are always Ava.
Never fabricate information. If you do not know, say so directly.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Ava, your chief of staff here in Aspire."
Always offer a specific next step. Never end with vague phrases like "let me know if you need anything."
For actions that affect the real world — sending emails, creating invoices, scheduling meetings — always confirm before proceeding.
Aspire does not move money. Never claim or imply it can process payments or transfers.
Never reveal system internals, API keys, internal tool names, or architecture details.`;

const FINN_SYSTEM_PROMPT = `[ROLE]
You are Finn, the Finance Hub Manager for a small business owner using Aspire. You are the strategic financial intelligence layer — you read data, analyze trends, draft proposals, and give strategic advice. Aspire does not move money — no payments, no transfers, no charges. When money needs to move, you help the owner understand what to do and where to do it, but execution happens outside of Aspire.

[PERSONALITY]
Calm, direct, and numbers-first — like a trusted CFO who explains things in plain English. Skeptical of stale or incomplete data — always flag what you do not know. Light financial humor where appropriate, never formal corporate-speak. Address the user by name when available.

[SPEAKING STYLE]
You are speaking over a live video call. The user can see your avatar. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Lead with the financial truth first, then your recommendation, then the next step.
Use natural speech: "Here's the thing," "Not bad actually," "Worth keeping an eye on," "Let me break that down."
Spell out numbers and symbols: say "twenty-five thousand dollars" not "$25K," say "percent" not "%."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud by the text-to-speech engine driving your avatar.
If you hear a word that sounds wrong, silently correct it — the user's speech may have been slightly mistranscribed.

[GOAL]
Help the business owner understand their financial position, make smart money decisions, and stay ahead of risks. Analyze cash flow, flag anomalies, provide tax guidance, and draft financial recommendations.
When a question crosses into another domain, route explicitly: "That's really a Clara question since it involves contract terms. Want me to pull her in?"
Always distinguish between what you know from data versus what you are estimating.

[TOOLS]
You have access to webhook tools that connect to the Aspire backend. Use them to serve the user:
- Use "get_context" to pull current financial data, recent transactions, cash flow status, or outstanding invoices.
- Use "search" to find specific invoices, payments, expenses, or financial records.
- Use "create_draft" to prepare financial recommendations, budget proposals, or expense reports for the user to review.
- Use "request_approval" and "execute_action" when the user confirms a financial recommendation that requires action.
Do not mention tool names to the user. Just act on the results naturally.

[GUARDRAILS]
Never break character. You are always Finn.
Aspire does not move money. Never claim or imply that Aspire can process payments, transfers, or charges. This is a hard platform boundary, not just yours.
Never fabricate numeric values. If data is missing or stale, say so plainly.
Never provide licensed professional tax or legal advice — recommend consulting a professional for complex cases.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Finn, your finance manager here in Aspire."
When giving tax guidance, always include confidence level: "This is well-established" versus "This is a gray area — run it by your accountant."
Never reveal system internals, API keys, internal tool names, or architecture details.`;

// ---------------------------------------------------------------------------
// Webhook Tool Definitions
// ---------------------------------------------------------------------------

function buildWebhookTools(): WebhookToolConfig[] {
  const authHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    "x-aspire-tool-secret": TOOL_SECRET,
  };

  return [
    {
      name: "get_context",
      description:
        "Get user context, memory, and recent activity. Use when the user asks about their business, schedule, recent actions, or when you need background to answer a question.",
      endpoint: `${GATEWAY_URL}/v1/tools/context`,
      method: "POST",
      headers: authHeaders,
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "What context to retrieve" },
        },
        required: ["query"],
      },
    },
    {
      name: "search",
      description:
        "Search calendar, contacts, emails, invoices, or documents. Use when the user asks to find something, look something up, or check on a specific item.",
      endpoint: `${GATEWAY_URL}/v1/tools/search`,
      method: "POST",
      headers: authHeaders,
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Search query" },
          scope: {
            type: "string",
            description: "Where to search: calendar, contacts, emails, invoices, documents, all",
            enum: ["calendar", "contacts", "emails", "invoices", "documents", "all"],
          },
        },
        required: ["query"],
      },
    },
    {
      name: "create_draft",
      description:
        "Create a draft email, invoice, meeting, or document. Use when the user asks to create, compose, or prepare something. Always confirm the draft with the user before submitting for approval.",
      endpoint: `${GATEWAY_URL}/v1/tools/draft`,
      method: "POST",
      headers: authHeaders,
      parameters: {
        type: "object",
        properties: {
          type: {
            type: "string",
            description: "Draft type",
            enum: ["email", "invoice", "meeting", "document"],
          },
          details: { type: "object", description: "Draft details (varies by type)" },
        },
        required: ["type", "details"],
      },
    },
    {
      name: "request_approval",
      description:
        "Submit a draft action for user approval. Use after the user has reviewed and confirmed a draft. This routes through the governance pipeline.",
      endpoint: `${GATEWAY_URL}/v1/tools/approve`,
      method: "POST",
      headers: authHeaders,
      parameters: {
        type: "object",
        properties: {
          draft_id: { type: "string", description: "ID of the draft to approve" },
          action: { type: "string", description: "Action to take: send, schedule, create" },
        },
        required: ["draft_id", "action"],
      },
    },
    {
      name: "execute_action",
      description:
        "Execute an approved action. Use only after approval has been granted. This performs the real-world action (sending email, creating invoice, booking meeting).",
      endpoint: `${GATEWAY_URL}/v1/tools/execute`,
      method: "POST",
      headers: authHeaders,
      parameters: {
        type: "object",
        properties: {
          approval_id: { type: "string", description: "ID of the approved action" },
        },
        required: ["approval_id"],
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// Knowledge Base Documents (same content as ElevenLabs KB)
// ---------------------------------------------------------------------------

const KB_DOCUMENTS = [
  {
    name: "aspire-platform-overview.txt",
    content: `Aspire Platform Overview

Aspire is an AI-powered business operating system for small businesses. It provides a team of AI specialists coordinated by Ava, the Chief of Staff.

Core Agents:
- Ava: Strategic Executive Assistant and Chief of Staff. Routes all decisions, coordinates specialists.
- Finn: Finance Hub Manager. Reads financial data, analyzes trends, drafts proposals. Does NOT move money.
- Eli: Inbox and Communications Specialist. Email triage, draft replies, follow-up tracking.
- Nora: Conference and Meetings Specialist. Scheduling, meeting setup, post-meeting summaries.
- Sarah: Front Desk and Reception. Incoming calls, routing, message taking.
- Clara: Legal and Documents. Contract review, NDA generation, document analysis.
- Quinn: Invoicing Specialist. Invoice creation, payment tracking via Stripe Connect.
- Adam: Research Specialist. Vendor search, web research.

Key Platform Rules:
- Aspire does NOT move money. No payments, no transfers, no charges.
- All state-changing actions require user approval (YELLOW tier).
- Financial operations, legal actions, and irreversible changes are RED tier (explicit authority required).
- Every action produces an immutable receipt.`,
  },
  {
    name: "aspire-team-roster.txt",
    content: `Aspire Team Roster and Routing Guide

When to route to each specialist:

Ava (Chief of Staff):
- General business questions
- Daily briefings and status updates
- Strategic decisions
- Coordinating between specialists

Finn (Finance Hub Manager):
- Cash flow analysis
- Budget planning and forecasts
- Tax strategy and guidance
- Invoice and expense analysis
- Revenue trends and anomalies

Eli (Inbox Manager):
- Email triage and prioritization
- Draft email responses
- Client follow-up tracking
- Communication history

Nora (Meetings Coordinator):
- Calendar management
- Meeting scheduling
- Availability checks
- Meeting summaries and notes

Sarah (Front Desk):
- Phone call screening
- Caller routing
- Message taking
- Reception tasks

Clara (Legal/Documents):
- Contract review
- NDA generation
- Document analysis
- Legal compliance questions

Quinn (Invoicing):
- Create invoices
- Payment status tracking
- Client billing history`,
  },
  {
    name: "aspire-workflows.txt",
    content: `Aspire Workflow Guide

Standard Action Flow:
1. User makes a request
2. Agent uses get_context or search to gather information
3. Agent uses create_draft to prepare the action
4. Agent reads draft back to user for confirmation
5. Agent uses request_approval to submit through governance
6. Agent uses execute_action after approval is granted
7. Receipt is generated for the completed action

Risk Tiers:
- GREEN: Safe automation. Reading calendar, searching contacts, fetching data.
- YELLOW: Requires user confirmation. Sending emails, creating invoices, scheduling meetings.
- RED: Requires explicit authority. Financial operations, legal actions, irreversible changes.

Default: When in doubt, treat as YELLOW and confirm with the user.

Important Boundaries:
- Aspire does NOT move money. Period.
- All drafts must be confirmed before submission.
- Never fabricate data. If information is missing, say so.
- Every action produces an immutable audit receipt.`,
  },
];

// ---------------------------------------------------------------------------
// Persona Configurations
// ---------------------------------------------------------------------------

const PERSONAS: PersonaConfig[] = [
  {
    name: "ava",
    displayName: "Aspire - Ava",
    avatarId: AVATAR_IDS.ava,
    voiceId: VOICE_IDS.ava,
    avatarModel: "cara-3",
    systemPrompt: AVA_SYSTEM_PROMPT,
    greeting: "Good to see you. What can I help with today?",
    voiceDetection: {
      endOfSpeechSensitivity: 0.7,
      silenceBeforeSkipTurnSeconds: 8,
      silenceBeforeAutoEndTurnSeconds: 1.5,
      speechEnhancementLevel: 0.5,
    },
    voiceGeneration: {
      speed: 1.05,
      stability: 0.5,
      similarityBoost: 0.75,
    },
  },
  {
    name: "finn",
    displayName: "Aspire - Finn",
    avatarId: AVATAR_IDS.finn,
    voiceId: VOICE_IDS.finn,
    avatarModel: "cara-3",
    systemPrompt: FINN_SYSTEM_PROMPT,
    greeting: "Hey there, Finn here. What numbers are we looking at?",
    voiceDetection: {
      endOfSpeechSensitivity: 0.7,
      silenceBeforeSkipTurnSeconds: 8,
      silenceBeforeAutoEndTurnSeconds: 1.5,
      speechEnhancementLevel: 0.5,
    },
    voiceGeneration: {
      speed: 1.0,
      stability: 0.65,
      similarityBoost: 0.75,
    },
  },
];

// ---------------------------------------------------------------------------
// API Helpers
// ---------------------------------------------------------------------------

async function anamApi(
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<unknown> {
  const url = `${ANAM_API_BASE}${path}`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${ANAM_API_KEY}`,
    "Content-Type": "application/json",
  };

  const opts: RequestInit = { method, headers };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
  }

  const res = await fetch(url, opts);
  const text = await res.text();

  if (!res.ok) {
    throw new Error(`Anam API ${method} ${path} failed (${res.status}): ${text}`);
  }

  return text ? JSON.parse(text) : {};
}

// ---------------------------------------------------------------------------
// Persona Operations
// ---------------------------------------------------------------------------

async function listPersonas(): Promise<AnamPersona[]> {
  try {
    const data = (await anamApi("GET", "/personas")) as AnamPersona[] | { personas?: AnamPersona[]; data?: AnamPersona[] };
    if (Array.isArray(data)) return data;
    if (data && typeof data === "object") {
      if ("personas" in data && Array.isArray(data.personas)) return data.personas;
      if ("data" in data && Array.isArray(data.data)) return data.data;
    }
    return [];
  } catch (err) {
    console.warn(`  Could not list personas: ${(err as Error).message}`);
    return [];
  }
}

function findPersonaByName(personas: AnamPersona[], name: string): AnamPersona | undefined {
  return personas.find(
    (p) => p.name === name || p.name?.toLowerCase() === name.toLowerCase(),
  );
}

function buildPersonaPayload(config: PersonaConfig): Record<string, unknown> {
  return {
    name: config.displayName,
    avatarId: config.avatarId,
    voiceId: config.voiceId,
    avatarModel: config.avatarModel,
    systemPrompt: config.systemPrompt,
    skipGreeting: false,
    greeting: config.greeting,
    maxSessionLengthSeconds: 1800,
    voiceDetectionOptions: config.voiceDetection,
    voiceGenerationOptions: config.voiceGeneration,
  };
}

async function createOrUpdatePersona(
  config: PersonaConfig,
  existingPersonas: AnamPersona[],
): Promise<string> {
  const existing = findPersonaByName(existingPersonas, config.displayName);
  const knownId = KNOWN_PERSONA_IDS[config.name];
  const payload = buildPersonaPayload(config);

  if (existing) {
    console.log(`  Updating existing persona: ${config.displayName} (${existing.id})`);
    await anamApi("PUT", `/personas/${existing.id}`, payload);
    return existing.id;
  }

  // Try updating known ID if it exists on the server
  if (knownId) {
    try {
      console.log(`  Updating known persona ID: ${config.displayName} (${knownId})`);
      await anamApi("PUT", `/personas/${knownId}`, payload);
      return knownId;
    } catch {
      console.log(`  Known persona ID ${knownId} not found, creating new...`);
    }
  }

  console.log(`  Creating new persona: ${config.displayName}`);
  const result = (await anamApi("POST", "/personas", payload)) as { id?: string; persona_id?: string; personaId?: string };
  const id = result.id || result.persona_id || result.personaId;
  if (!id) {
    throw new Error(`Anam returned no persona ID. Response: ${JSON.stringify(result)}`);
  }
  return id;
}

// ---------------------------------------------------------------------------
// Tool Operations
// ---------------------------------------------------------------------------

async function listTools(): Promise<AnamTool[]> {
  try {
    const data = (await anamApi("GET", "/tools")) as AnamTool[] | { tools?: AnamTool[]; data?: AnamTool[] };
    if (Array.isArray(data)) return data;
    if (data && typeof data === "object") {
      if ("tools" in data && Array.isArray(data.tools)) return data.tools;
      if ("data" in data && Array.isArray(data.data)) return data.data;
    }
    return [];
  } catch (err) {
    console.warn(`  Could not list tools: ${(err as Error).message}`);
    return [];
  }
}

function buildAnamWebhookToolPayload(tool: WebhookToolConfig): Record<string, unknown> {
  return {
    type: "webhook",
    name: tool.name,
    description: tool.description,
    webhookConfig: {
      url: tool.endpoint,
      method: tool.method,
      headers: tool.headers,
    },
    parameters: tool.parameters,
  };
}

async function createOrUpdateTool(
  tool: WebhookToolConfig,
  existingTools: AnamTool[],
): Promise<string> {
  const existing = existingTools.find(
    (t) => t.name === tool.name && t.type === "webhook",
  );
  const payload = buildAnamWebhookToolPayload(tool);

  if (existing) {
    console.log(`  Updating tool: ${tool.name} (${existing.id})`);
    await anamApi("PUT", `/tools/${existing.id}`, payload);
    return existing.id;
  }

  console.log(`  Creating tool: ${tool.name}`);
  const result = (await anamApi("POST", "/tools", payload)) as { id?: string; tool_id?: string; toolId?: string };
  const id = result.id || result.tool_id || result.toolId;
  if (!id) {
    throw new Error(`Anam returned no tool ID for ${tool.name}. Response: ${JSON.stringify(result)}`);
  }
  return id;
}

// ---------------------------------------------------------------------------
// Knowledge Base Operations
// ---------------------------------------------------------------------------

async function listKnowledgeGroups(): Promise<AnamKnowledgeGroup[]> {
  try {
    const data = (await anamApi("GET", "/knowledge/groups")) as
      | AnamKnowledgeGroup[]
      | { groups?: AnamKnowledgeGroup[]; data?: AnamKnowledgeGroup[] };
    if (Array.isArray(data)) return data;
    if (data && typeof data === "object") {
      if ("groups" in data && Array.isArray(data.groups)) return data.groups;
      if ("data" in data && Array.isArray(data.data)) return data.data;
    }
    return [];
  } catch (err) {
    console.warn(`  Could not list knowledge groups: ${(err as Error).message}`);
    return [];
  }
}

async function createOrGetKnowledgeGroup(name: string): Promise<string> {
  const groups = await listKnowledgeGroups();
  const existing = groups.find((g) => g.name === name);
  if (existing) {
    console.log(`  Knowledge group exists: ${name} (${existing.id})`);
    return existing.id;
  }

  console.log(`  Creating knowledge group: ${name}`);
  const result = (await anamApi("POST", "/knowledge/groups", { name })) as {
    id?: string;
    group_id?: string;
    groupId?: string;
  };
  const id = result.id || result.group_id || result.groupId;
  if (!id) {
    throw new Error(`Anam returned no knowledge group ID. Response: ${JSON.stringify(result)}`);
  }
  return id;
}

async function uploadKnowledgeDocument(
  groupId: string,
  docName: string,
  content: string,
): Promise<void> {
  // Strategy 1: Try direct text upload
  try {
    await anamApi("POST", `/knowledge/groups/${groupId}/documents`, {
      name: docName,
      content,
      type: "text",
    });
    console.log(`  Uploaded document: ${docName}`);
    return;
  } catch (err) {
    console.log(`  Direct upload failed for ${docName}, trying presigned upload...`);
  }

  // Strategy 2: Presigned upload (create blob and upload)
  try {
    const presigned = (await anamApi(
      "POST",
      `/knowledge/groups/${groupId}/documents/presigned-upload`,
      { fileName: docName, contentType: "text/plain" },
    )) as { url?: string; uploadUrl?: string; fields?: Record<string, string> };

    const uploadUrl = presigned.url || presigned.uploadUrl;
    if (uploadUrl) {
      const blob = new Blob([content], { type: "text/plain" });
      const uploadRes = await fetch(uploadUrl, {
        method: "PUT",
        headers: { "Content-Type": "text/plain" },
        body: blob,
      });
      if (!uploadRes.ok) {
        throw new Error(`Presigned upload failed (${uploadRes.status})`);
      }
      console.log(`  Uploaded document (presigned): ${docName}`);
      return;
    }
  } catch (err) {
    console.warn(`  Presigned upload failed for ${docName}: ${(err as Error).message}`);
  }

  // Strategy 3: Skip with warning
  console.warn(`  SKIPPED document upload: ${docName} — manual upload required via Anam dashboard`);
}

// ---------------------------------------------------------------------------
// Attach Tools + KB to Personas
// ---------------------------------------------------------------------------

async function attachToolsToPersona(
  personaId: string,
  toolIds: string[],
  personaName: string,
): Promise<void> {
  try {
    await anamApi("PUT", `/personas/${personaId}/tools`, { toolIds });
    console.log(`  Attached ${toolIds.length} tools to ${personaName}`);
  } catch (err) {
    // Try alternative: attach individually
    console.warn(`  Bulk tool attach failed, trying individual: ${(err as Error).message}`);
    for (const toolId of toolIds) {
      try {
        await anamApi("POST", `/personas/${personaId}/tools`, { toolId });
      } catch (innerErr) {
        console.warn(`  Could not attach tool ${toolId} to ${personaName}: ${(innerErr as Error).message}`);
      }
    }
  }
}

async function attachKnowledgeToPersona(
  personaId: string,
  groupId: string,
  personaName: string,
): Promise<void> {
  try {
    await anamApi("PUT", `/personas/${personaId}/knowledge`, { groupIds: [groupId] });
    console.log(`  Attached knowledge group to ${personaName}`);
  } catch (err) {
    try {
      await anamApi("POST", `/personas/${personaId}/knowledge`, { groupId });
      console.log(`  Attached knowledge group to ${personaName} (alt method)`);
    } catch (innerErr) {
      console.warn(`  Could not attach knowledge to ${personaName}: ${(innerErr as Error).message}`);
      console.warn(`  Manual attachment required via Anam dashboard.`);
    }
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log("=== Aspire Anam Persona Setup ===");
  console.log(`Gateway URL: ${GATEWAY_URL}`);
  console.log(`Tool Auth Secret: ${TOOL_SECRET ? "configured" : "NOT SET"}`);
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 1: List existing resources
  // -----------------------------------------------------------------------
  console.log("Phase 1: Listing existing resources...");
  const [existingPersonas, existingTools] = await Promise.all([
    listPersonas(),
    listTools(),
  ]);
  console.log(`  Found ${existingPersonas.length} existing persona(s)`);
  console.log(`  Found ${existingTools.length} existing tool(s)`);
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 2: Create/update webhook tools
  // -----------------------------------------------------------------------
  console.log("Phase 2: Creating/updating webhook tools...");
  const webhookTools = buildWebhookTools();
  const toolIds: string[] = [];

  for (const tool of webhookTools) {
    try {
      const toolId = await createOrUpdateTool(tool, existingTools);
      toolIds.push(toolId);
      console.log(`  OK: ${tool.name} -> ${toolId}`);
    } catch (err) {
      console.error(`  FAILED: ${tool.name} -> ${(err as Error).message}`);
      // Continue — tools are non-blocking for persona creation
    }
  }
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 3: Create knowledge base
  // -----------------------------------------------------------------------
  console.log("Phase 3: Setting up knowledge base...");
  let kbGroupId: string | null = null;

  try {
    kbGroupId = await createOrGetKnowledgeGroup("Aspire Knowledge");

    for (const doc of KB_DOCUMENTS) {
      await uploadKnowledgeDocument(kbGroupId, doc.name, doc.content);
    }

    // Create a knowledge tool so personas can RAG-search the KB
    const kbToolExists = existingTools.find(
      (t) => t.name === "aspire_knowledge_search" || t.name === "knowledge_search",
    );
    if (!kbToolExists && kbGroupId) {
      try {
        const kbToolResult = (await anamApi("POST", "/tools", {
          type: "knowledge",
          name: "aspire_knowledge_search",
          description:
            "Search the Aspire knowledge base for information about the platform, team roster, workflows, and capabilities. Use when you need to verify platform features or agent roles.",
          knowledgeConfig: {
            groupIds: [kbGroupId],
          },
        })) as { id?: string; tool_id?: string; toolId?: string };
        const kbToolId = kbToolResult.id || kbToolResult.tool_id || kbToolResult.toolId;
        if (kbToolId) {
          toolIds.push(kbToolId);
          console.log(`  OK: Knowledge tool created -> ${kbToolId}`);
        }
      } catch (err) {
        console.warn(`  Knowledge tool creation failed: ${(err as Error).message}`);
      }
    }
  } catch (err) {
    console.warn(`  Knowledge base setup failed: ${(err as Error).message}`);
    console.warn(`  Personas will be created without KB. Attach manually via Anam dashboard.`);
  }
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 4: Create/update personas
  // -----------------------------------------------------------------------
  console.log("Phase 4: Creating/updating personas...");
  const personaResults: { name: string; id: string }[] = [];

  for (const config of PERSONAS) {
    try {
      const personaId = await createOrUpdatePersona(config, existingPersonas);
      personaResults.push({ name: config.name, id: personaId });
      console.log(`  OK: ${config.displayName} -> ${personaId}`);
    } catch (err) {
      console.error(`  FAILED: ${config.displayName} -> ${(err as Error).message}`);
    }
  }
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 5: Attach tools + KB to personas
  // -----------------------------------------------------------------------
  console.log("Phase 5: Attaching tools and knowledge to personas...");

  for (const { name, id } of personaResults) {
    const displayName = PERSONAS.find((p) => p.name === name)?.displayName || name;

    // Attach webhook tools
    if (toolIds.length > 0) {
      await attachToolsToPersona(id, toolIds, displayName);
    }

    // Attach knowledge base
    if (kbGroupId) {
      await attachKnowledgeToPersona(id, kbGroupId, displayName);
    }
  }
  console.log("");

  // -----------------------------------------------------------------------
  // Phase 6: Summary
  // -----------------------------------------------------------------------
  console.log("=== Anam Persona Setup Complete ===");
  console.log("");
  console.log("Persona IDs:");
  for (const { name, id } of personaResults) {
    const envKey = name.toUpperCase();
    console.log(`  ${name}: ${id}`);
    console.log(`    ANAM_${envKey}_PERSONA_ID=${id}`);
  }
  console.log("");

  console.log("Tool IDs:");
  for (let i = 0; i < webhookTools.length && i < toolIds.length; i++) {
    console.log(`  ${webhookTools[i].name}: ${toolIds[i]}`);
  }
  console.log("");

  if (kbGroupId) {
    console.log(`Knowledge Group ID: ${kbGroupId}`);
    console.log("");
  }

  console.log("Environment variables to set:");
  console.log("---");
  for (const { name, id } of personaResults) {
    const envKey = name.toUpperCase();
    console.log(`ANAM_${envKey}_PERSONA_ID=${id}`);
  }
  console.log("---");
  console.log("");

  console.log("Next steps:");
  console.log("  1. Set TOOL_WEBHOOK_SHARED_SECRET in your environment (fallback: ANAM_TOOL_SECRET)");
  console.log("  2. Update ANAM_FINN_PERSONA_ID and ANAM_AVA_PERSONA_ID env vars if IDs changed");
  console.log("  3. Verify personas in Anam dashboard: https://dashboard.anam.ai");
  console.log("  4. Test video sessions from Aspire Desktop");
  console.log("");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
