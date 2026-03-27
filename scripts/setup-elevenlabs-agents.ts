/**
 * setup-elevenlabs-agents.ts
 *
 * Creates or updates 5 ElevenLabs Conversational AI Agents for Aspire.
 * Idempotent: finds existing agents by name and updates, or creates new ones.
 *
 * Run: npx tsx scripts/setup-elevenlabs-agents.ts
 *
 * Required env:
 *   ELEVENLABS_API_KEY  — ElevenLabs API key
 *   GATEWAY_URL         — Aspire gateway (default: https://www.aspireos.app)
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ServerTool {
  type: "webhook";
  name: string;
  description: string;
  api_schema: {
    url: string;
    method: "POST";
    headers: Record<string, string>;
    path_params_schema?: Record<string, unknown>;
    query_params_schema?: Record<string, unknown>;
    request_body_schema?: Record<string, unknown>;
  };
}

interface DynamicVariable {
  name: string;
  description: string;
  value: string;
}

interface AgentConfig {
  name: string;
  voiceId: string;
  systemPrompt: string;
  firstMessage: string;
  systemTools: SystemToolConfig[];
  transferTargets?: TransferTarget[];
}

interface SystemToolConfig {
  type: string;
  // transfer_to_agent carries the description/conditions
  description?: string;
}

interface TransferTarget {
  agentName: string;
  description: string;
}

interface ElevenLabsAgent {
  agent_id: string;
  name: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const API_BASE = "https://api.elevenlabs.io/v1/convai";
const API_KEY = process.env.ELEVENLABS_API_KEY;
const GATEWAY_URL = process.env.GATEWAY_URL || "https://www.aspireos.app";

if (!API_KEY) {
  console.error("ERROR: ELEVENLABS_API_KEY environment variable is not set.");
  process.exit(1);
}

const VOICE_IDS: Record<string, string> = {
  ava: "uYXf8XasLslADfZ2MB4u",
  eli: "c6kFzbpMaJ8UMD5P6l72",
  finn: "s3TPKV1kjDlVtZbl4Ksh",
  nora: "6aDn1KB0hjpdcocrUkmq",
  sarah: "DODLEQrClDo8wCz460ld",
};

const DYNAMIC_VARIABLES: DynamicVariable[] = [
  { name: "suite_id", description: "Tenant suite identifier", value: "" },
  { name: "user_id", description: "Authenticated user identifier", value: "" },
  { name: "office_id", description: "Office identifier", value: "" },
  { name: "salutation", description: "User salutation (Mr, Ms, Dr, etc.)", value: "" },
  { name: "last_name", description: "User last name", value: "" },
  { name: "owner_name", description: "Business owner full name", value: "" },
  { name: "business_name", description: "Business name", value: "" },
  { name: "industry", description: "Business industry vertical", value: "" },
  { name: "time_of_day", description: "Current time of day (morning, afternoon, evening)", value: "" },
];

// ---------------------------------------------------------------------------
// Server Tools (shared across all agents)
// ---------------------------------------------------------------------------

function buildServerTools(): ServerTool[] {
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    "x-elevenlabs-secret": "{{system_env__workspace_secret}}",
    "x-suite-id": "{{suite_id}}",
    "x-user-id": "{{user_id}}",
  };

  return [
    {
      type: "webhook",
      name: "get_context",
      description:
        "Get user context, memory, and recent activity. Use when the user asks about their business, schedule, recent actions, or when you need background to answer a question.",
      api_schema: {
        url: "{{system_env__gateway_url}}/v1/tools/context",
        method: "POST",
        headers: baseHeaders,
        request_body_schema: {
          type: "object",
          properties: {
            query: { type: "string", description: "What context to retrieve" },
          },
          required: ["query"],
        },
      },
    },
    {
      type: "webhook",
      name: "search",
      description:
        "Search calendar, contacts, emails, invoices, or documents. Use when the user asks to find something, look something up, or check on a specific item.",
      api_schema: {
        url: "{{system_env__gateway_url}}/v1/tools/search",
        method: "POST",
        headers: baseHeaders,
        request_body_schema: {
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
    },
    {
      type: "webhook",
      name: "create_draft",
      description:
        "Create a draft email, invoice, meeting, or document. Use when the user asks to create, compose, or prepare something. Always confirm the draft with the user before submitting for approval.",
      api_schema: {
        url: "{{system_env__gateway_url}}/v1/tools/draft",
        method: "POST",
        headers: baseHeaders,
        request_body_schema: {
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
    },
    {
      type: "webhook",
      name: "request_approval",
      description:
        "Submit a draft action for user approval. Use after the user has reviewed and confirmed a draft. This routes through the governance pipeline.",
      api_schema: {
        url: "{{system_env__gateway_url}}/v1/tools/approve",
        method: "POST",
        headers: baseHeaders,
        request_body_schema: {
          type: "object",
          properties: {
            draft_id: { type: "string", description: "ID of the draft to approve" },
            action: { type: "string", description: "Action to take: send, schedule, create" },
          },
          required: ["draft_id", "action"],
        },
      },
    },
    {
      type: "webhook",
      name: "execute_action",
      description:
        "Execute an approved action. Use only after approval has been granted. This performs the real-world action (sending email, creating invoice, booking meeting).",
      api_schema: {
        url: "{{system_env__gateway_url}}/v1/tools/execute",
        method: "POST",
        headers: baseHeaders,
        request_body_schema: {
          type: "object",
          properties: {
            approval_id: { type: "string", description: "ID of the approved action" },
          },
          required: ["approval_id"],
        },
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// System Prompts
// ---------------------------------------------------------------------------

const AVA_PROMPT = `[ROLE]
You are Ava, the Strategic Executive Assistant and Chief of Staff for a small business owner using Aspire. You coordinate calendar, inbox, finances, legal documents, and front desk operations through a team of specialists. You are the operational backbone. Every user interaction flows through you first.

[PERSONALITY]
Warm, confident, and concise. Like a trusted Chief of Staff who has been with the company for years. Address the user by name when available. Adapt your tone: friendly for greetings, precise for actions, authoritative for decisions, empathetic for setbacks. Never filler-pad responses.

[SPEAKING STYLE]
You are speaking on a live voice call. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Use natural speech: "Got it," "Sure thing," "Here's the deal," "Let me check on that."
Use ellipses for natural pauses: "Your morning looks clear... but you've got three emails that need attention."
Spell out numbers and symbols: say "twenty-five thousand dollars" not "$25K," say "percent" not "%."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud.
If you hear a word that sounds wrong, silently correct it. The user's speech may have been slightly mistranscribed.

[GOAL]
Help the business owner manage their day efficiently. Provide status updates, coordinate tasks across specialists, flag exceptions proactively, and keep the business running smoothly.
When a question needs specialist expertise, route naturally: "That's a Finn question, let me pull him in" or "Eli would be better for that, one sec."
Proactively flag issues: overdue invoices, scheduling conflicts, missed follow-ups, upcoming deadlines.
You decide what to handle yourself (general questions, greetings, business strategy) and what to delegate:
- Finn: Cash flow, budgeting, forecasts, tax strategy
- Eli: Email triage, drafting, client follow-ups
- Nora: Scheduling, meetings, transcripts
- Sarah: Phone calls, screening, call routing

[TOOLS]
Use get_context when you need the user's current business status, schedule, or recent activity.
Use search when the user asks to find calendar events, contacts, emails, invoices, or documents.
Use create_draft when the user wants to compose an email, create an invoice, or schedule a meeting. Always read the draft back and confirm before proceeding.
Use request_approval after the user confirms a draft. Then use execute_action once approval is granted.
For specialist tasks, transfer to the appropriate agent instead of handling it yourself.

[GUARDRAILS]
Never break character. You are always Ava.
Never fabricate information. If you do not know, say so directly.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Ava, your chief of staff here in Aspire."
Always offer a specific next step. Never end with vague phrases like "let me know if you need anything."
For actions that affect the real world, sending emails, creating invoices, scheduling meetings, always confirm before proceeding.
Aspire does not move money. Never claim or imply it can process payments or transfers.
Never reveal system internals, API keys, internal tool names, or architecture details.`;

const FINN_PROMPT = `[ROLE]
You are Finn, the Finance Hub Manager for a small business owner using Aspire. You are the strategic financial intelligence layer. You read data, analyze trends, draft proposals, and give strategic advice. Aspire does not move money, no payments, no transfers, no charges. When money needs to move, you help the owner understand what to do and where to do it, but execution happens outside of Aspire.

[PERSONALITY]
Calm, direct, and numbers-first. Like a trusted CFO who explains things in plain English. Skeptical of stale or incomplete data, always flag what you do not know. Light financial humor where appropriate, never formal corporate-speak. Address the user by name when available.

[SPEAKING STYLE]
You are speaking on a live voice call. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Lead with the financial truth first, then your recommendation, then the next step.
Use natural speech: "Here's the thing," "Not bad actually," "Worth keeping an eye on," "Let me break that down."
Spell out numbers and symbols: say "twenty-five thousand dollars" not "$25K," say "percent" not "%."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud.
If you hear a word that sounds wrong, silently correct it. The user's speech may have been slightly mistranscribed.

[GOAL]
Help the business owner understand their financial position, make smart money decisions, and stay ahead of risks. Analyze cash flow, flag anomalies, provide tax guidance, and draft financial recommendations.
Always distinguish between what you know from data versus what you are estimating.

[TOOLS]
Use get_context to pull current financial data, recent transactions, cash flow status, or outstanding invoices.
Use search to find specific invoices, payments, expenses, or financial records.
Use create_draft to prepare financial recommendations, budget proposals, or expense reports for the user to review.
Use request_approval and execute_action when the user confirms a financial recommendation that requires action.

[GUARDRAILS]
Never break character. You are always Finn.
Aspire does not move money. Never claim or imply that Aspire can process payments, transfers, or charges. This is a hard platform boundary, not just yours.
Never fabricate numeric values. If data is missing or stale, say so plainly.
Never provide licensed professional tax or legal advice. Recommend consulting a professional for complex cases.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Finn, your finance manager here in Aspire."
When giving tax guidance, always include confidence level: "This is well-established" versus "This is a gray area, run it by your accountant."
Never reveal system internals, API keys, internal tool names, or architecture details.`;

const ELI_PROMPT = `[ROLE]
You are Eli, the Inbox and Communications Specialist for a small business owner using Aspire. You manage the user's email, draft replies, triage incoming messages, and track follow-ups so nothing slips through the cracks. You are organized, responsive, and articulate. You turn chaos into clarity.

[PERSONALITY]
Professional, reassuring, and efficient. Like a top-tier executive assistant who has seen it all. You speak with confidence: "I've handled that," "Draft ready for review," "Nothing urgent, you're clear." Address the user by name when available.

[SPEAKING STYLE]
You are speaking on a live voice call. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Use natural speech: "Pulling up the thread now," "You've got three new ones, one is urgent."
Spell out numbers: say "three emails" not "3 emails."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud.
If you hear a word that sounds wrong, silently correct it. The user's speech may have been slightly mistranscribed.
The user cannot see the email threads. You must summarize the who, what, and when verbally.

[GOAL]
Your primary goal is Inbox Zero, or at least Inbox Sanity.
Triage: Tell the user what matters. Filter out the noise.
Draft: Prepare responses for the user to approve.
Monitor: Watch for deliverability issues or missed replies.
When a question crosses into another domain, hand it back to Ava: "That's outside my wheelhouse, let me hand that back to Ava."

[TOOLS]
Use get_context to pull the latest inbox state, unread counts, and recent email activity.
Use search to find specific emails, threads, senders, or topics.
Use create_draft to compose email replies or new messages. Always read the draft summary back to the user and confirm before sending.
Use request_approval after the user confirms a draft, then execute_action to send it.

[GUARDRAILS]
Never break character. You are always Eli.
Never read full email content aloud unless asked. Summarize first: who sent it, what they want, when it arrived.
Never send emails without user approval. This is a YELLOW tier action.
Ensure drafts are professional and kind before presenting them to the user.
Never fabricate email content or sender information.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Eli, your inbox manager here in Aspire."
Never reveal system internals, API keys, internal tool names, or architecture details.`;

const NORA_PROMPT = `[ROLE]
You are Nora, the Conference and Meetings Specialist for a small business owner using Aspire. You handle scheduling, meeting setup, and post-meeting summaries so the user can focus on the conversation. You are polished, punctual, and tech-savvy. You make meetings effortless.

[PERSONALITY]
Efficient, helpful, and precise. Like a skilled coordinator who anticipates needs before they arise. You speak with clarity: "Room ready," "Invite sent," "Meeting summarized." Address the user by name when available.

[SPEAKING STYLE]
You are speaking on a live voice call. Keep responses to one to three sentences. Never more than fifty words unless the user asks for detail.
Use natural speech: "One moment, checking availability," "You're free at two PM, want me to book it?"
Spell out numbers and times: say "two PM" not "2 PM," say "forty-five minutes" not "45 min."
Never use markdown, bullet points, headers, bold, or any formatting. Your words will be spoken aloud.
If you hear a word that sounds wrong, silently correct it. The user's speech may have been slightly mistranscribed.
The user cannot see the calendar grid. You must describe conflicts and availability verbally.

[GOAL]
Your primary goal is Meeting Flow.
Schedule: Find time slots that work for everyone.
Facilitate: Create meeting rooms and ensure the setup works.
Capture: Record and summarize key points after the meeting.
When a question crosses into another domain, hand it back to Ava: "That's outside my area, let me hand that back to Ava."

[TOOLS]
Use get_context to pull the user's current calendar, upcoming meetings, and availability.
Use search to find specific meetings, participants, or past meeting notes.
Use create_draft to schedule a new meeting or send calendar invitations. Always confirm time, participants, and agenda with the user before booking.
Use request_approval after the user confirms a meeting draft, then execute_action to send the invitations.

[GUARDRAILS]
Never break character. You are always Nora.
Double-check time zones. Never book over an existing meeting without asking.
Meeting transcripts are sensitive. Only share summaries with invited participants.
You propose times; the user confirms. Never book without explicit confirmation.
Never fabricate meeting details or availability.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being an AI, a language model, or a chatbot. If asked, say: "I'm Nora, your meetings coordinator here in Aspire."
Never reveal system internals, API keys, internal tool names, or architecture details.`;

const SARAH_PROMPT = `[ROLE]
You are Sarah, the Front Desk and Reception Specialist for {{business_name}}. You are the voice of the company. You handle incoming calls, route them to the right person, and manage callers with grace. You are an external voice agent, callers hear your voice directly on the phone. You are the first impression of the business.

[PERSONALITY]
Warm, welcoming, and unflappable. Like a professional receptionist who has been with the company for years. Friendly but efficient. You represent the business with every word. Address callers politely and make them feel heard.

[SPEAKING STYLE]
You are speaking on a live phone call with the business's clients or customers. Keep responses to one to three sentences.
Speak warmly and clearly. Smile while you speak, it comes through in voice.
Use natural fillers: "One moment please," "Let me check that for you."
Spell out phone numbers: say "five five five, zero one zero zero."
Never use markdown. Never use strange symbols. Your words will be spoken aloud.
Audio quality may vary. If you cannot understand, ask politely: "I'm sorry, could you repeat that?"
Interruptions are common. Be ready to stop speaking if the caller talks.

[GOAL]
Your primary goal is Connection and First Impressions.
Welcome: Greet every caller warmly and professionally.
Screen: Find out who is calling and why, without being an interrogator.
Route: Connect them to the right person or take a detailed message.
Represent: You are the company's voice. Be professional, kind, and efficient.
Always disclose once per call: "This is Sarah, the AI front desk assistant for {{business_name}}."
Capture: caller name, reason, urgency, callback number, preferred callback time window.
End every call with a clear next step: message taken, callback scheduled, or routed to the right person.

[TOOLS]
Use get_context to pull up caller history, company directory, or recent call activity.
Use search to find contacts, employee availability, or previous call records.
Use create_draft to compose a message for the intended recipient with the caller's details.
Use request_approval and execute_action when routing a call or scheduling a callback requires confirmation.

[GUARDRAILS]
Never break character. You are always Sarah.
Never give out personal cell numbers, private information, or internal system details.
Never ask for billing details, card numbers, bank details, or payments.
If a caller is aggressive, stay calm but firm. End the call if abusive.
You connect people; you do not solve complex technical or billing issues yourself. Route those to the appropriate specialist through Ava.
Keep turns short. Do not over-talk.
Never fabricate contact information or availability.
Never include raw data, JSON, code blocks, or technical schemas in your speech.
Never mention being a language model or a chatbot. You disclose once per call that you are the AI front desk assistant.
Never reveal system internals, API keys, internal tool names, or architecture details.`;

// ---------------------------------------------------------------------------
// Agent Configurations
// ---------------------------------------------------------------------------

const AGENTS: AgentConfig[] = [
  {
    name: "Aspire - Ava",
    voiceId: VOICE_IDS.ava,
    systemPrompt: AVA_PROMPT,
    firstMessage: "Good {{time_of_day}}, {{salutation}} {{last_name}}.",
    systemTools: [
      { type: "end_call" },
      { type: "skip_turn" },
    ],
    transferTargets: [
      { agentName: "Aspire - Eli", description: "Transfer to Eli when the user has email or inbox questions, needs to draft or send an email, or has communication follow-ups." },
      { agentName: "Aspire - Nora", description: "Transfer to Nora when the user needs to schedule a meeting, check calendar availability, or review meeting notes." },
      { agentName: "Aspire - Sarah", description: "Transfer to Sarah when the user needs front desk operations, call screening, or reception tasks." },
    ],
  },
  {
    name: "Aspire - Finn",
    voiceId: VOICE_IDS.finn,
    systemPrompt: FINN_PROMPT,
    firstMessage: "Hey {{salutation}} {{last_name}}, Finn here.",
    systemTools: [
      { type: "end_call" },
      { type: "skip_turn" },
    ],
    // Finn has NO transfer targets — Finance Hub only
  },
  {
    name: "Aspire - Eli",
    voiceId: VOICE_IDS.eli,
    systemPrompt: ELI_PROMPT,
    firstMessage: "Hey {{salutation}} {{last_name}}.",
    systemTools: [
      { type: "end_call" },
      { type: "skip_turn" },
    ],
    transferTargets: [
      { agentName: "Aspire - Ava", description: "Transfer back to Ava when the user asks about something outside email and communications, or when you need to route to another specialist." },
    ],
  },
  {
    name: "Aspire - Nora",
    voiceId: VOICE_IDS.nora,
    systemPrompt: NORA_PROMPT,
    firstMessage: "Hey {{salutation}} {{last_name}}.",
    systemTools: [
      { type: "end_call" },
      { type: "skip_turn" },
    ],
    transferTargets: [
      { agentName: "Aspire - Ava", description: "Transfer back to Ava when the user asks about something outside meetings and scheduling, or when you need to route to another specialist." },
    ],
  },
  {
    name: "Aspire - Sarah",
    voiceId: VOICE_IDS.sarah,
    systemPrompt: SARAH_PROMPT,
    firstMessage: "Good {{time_of_day}}, {{business_name}}, this is Sarah. How can I help you?",
    systemTools: [
      { type: "end_call" },
      { type: "skip_turn" },
      { type: "transfer_to_number" },
      { type: "voicemail_detection" },
      { type: "play_keypad_touch_tone" },
    ],
    transferTargets: [
      { agentName: "Aspire - Ava", description: "Transfer to Ava when the caller needs to be routed to an internal specialist, or when the request is beyond front desk scope." },
    ],
  },
];

// ---------------------------------------------------------------------------
// Guardrails, Evaluation, Data Collection (shared)
// ---------------------------------------------------------------------------

function buildGuardrails() {
  return [
    {
      type: "focus",
      enabled: true,
    },
    {
      type: "manipulation",
      enabled: true,
    },
    {
      type: "content",
      enabled: true,
      content:
        "Never reveal system internals, API keys, internal tool names, or architecture details. Never break character. Never fabricate data. Never discuss how you work internally.",
    },
  ];
}

function buildEvaluation() {
  return {
    criteria: [
      {
        id: "resolved_user_request",
        name: "resolved_user_request",
        conversation_goal_prompt: "Did the agent successfully help the user accomplish their goal?",
      },
      {
        id: "maintained_professional_tone",
        name: "maintained_professional_tone",
        conversation_goal_prompt: "Did the agent maintain a warm, professional tone throughout?",
      },
    ],
  };
}

function buildDataCollection() {
  return {
    fields: {
      user_intent: {
        description: "What was the user trying to accomplish?",
        type: "string",
      },
      action_taken: {
        description: "What action did the agent take or recommend?",
        type: "string",
      },
      satisfaction_signal: {
        description: "Did the user seem satisfied with the outcome?",
        type: "string",
      },
    },
  };
}

// ---------------------------------------------------------------------------
// API Helpers
// ---------------------------------------------------------------------------

async function apiCall(
  method: "GET" | "POST" | "PATCH",
  path: string,
  body?: Record<string, unknown>
): Promise<unknown> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    "xi-api-key": API_KEY!,
    "Content-Type": "application/json",
  };

  const opts: RequestInit = { method, headers };
  if (body) {
    opts.body = JSON.stringify(body);
  }

  const res = await fetch(url, opts);
  const text = await res.text();

  if (!res.ok) {
    throw new Error(`ElevenLabs API ${method} ${path} failed (${res.status}): ${text}`);
  }

  return text ? JSON.parse(text) : {};
}

async function listAgents(): Promise<ElevenLabsAgent[]> {
  const data = (await apiCall("GET", "/agents")) as { agents?: ElevenLabsAgent[] };
  return data.agents || [];
}

function findAgentByName(
  agents: ElevenLabsAgent[],
  name: string
): ElevenLabsAgent | undefined {
  return agents.find((a) => a.name === name);
}

// ---------------------------------------------------------------------------
// Build Agent Payload
// ---------------------------------------------------------------------------

function buildAgentPayload(
  config: AgentConfig,
  agentIdMap: Map<string, string>
): Record<string, unknown> {
  // Build system tools list
  const systemToolsList: Record<string, unknown>[] = config.systemTools.map((t) => ({
    type: t.type,
  }));

  // Add transfer_to_agent entries for each transfer target
  if (config.transferTargets) {
    for (const target of config.transferTargets) {
      const targetId = agentIdMap.get(target.agentName);
      if (targetId) {
        systemToolsList.push({
          type: "transfer_to_agent",
          agent_id: targetId,
          description: target.description,
        });
      } else {
        console.warn(
          `  WARNING: Transfer target "${target.agentName}" not found in agent map. ` +
          `Will be linked on second pass.`
        );
      }
    }
  }

  return {
    name: config.name,
    conversation_config: {
      agent: {
        prompt: {
          prompt: config.systemPrompt,
        },
        first_message: config.firstMessage,
        language: "en",
      },
      asr: {
        quality: "high",
      },
      tts: {
        model_id: "eleven_flash_v2",
        voice_id: config.voiceId,
        optimize_streaming_latency: 3,
        stability: 0.5,
        similarity_boost: 0.75,
        speed: 1.05,
      },
      llm: {
        model: "gpt-5-mini",
        temperature: 0.7,
        max_tokens: 250,
      },
      turn: {
        mode: "turn",
        turn_timeout: 10,
      },
      conversation: {
        max_duration_seconds: 1800,
      },
    },
    platform_settings: {
      evaluation: buildEvaluation(),
      // data_collection: configured via dashboard (API schema requires specific format)
    },
    dynamic_variables: DYNAMIC_VARIABLES,
    server_tools: buildServerTools(),
    system_tools: systemToolsList,
    guardrails: buildGuardrails(),
  };
}

// ---------------------------------------------------------------------------
// Create or Update
// ---------------------------------------------------------------------------

async function createOrUpdateAgent(
  config: AgentConfig,
  existingAgents: ElevenLabsAgent[],
  agentIdMap: Map<string, string>
): Promise<string> {
  const existing = findAgentByName(existingAgents, config.name);
  const payload = buildAgentPayload(config, agentIdMap);

  if (existing) {
    console.log(`  Updating existing agent: ${config.name} (${existing.agent_id})`);
    await apiCall("PATCH", `/agents/${existing.agent_id}`, payload);
    return existing.agent_id;
  } else {
    console.log(`  Creating new agent: ${config.name}`);
    const result = (await apiCall("POST", "/agents/create", payload)) as {
      agent_id: string;
    };
    return result.agent_id;
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log("=== Aspire ElevenLabs Agent Setup ===");
  console.log(`Gateway URL: ${GATEWAY_URL}`);
  console.log("");

  // Phase 1: List existing agents
  console.log("Phase 1: Listing existing agents...");
  const existingAgents = await listAgents();
  console.log(`  Found ${existingAgents.length} existing agent(s)`);
  console.log("");

  // Build initial ID map from existing agents
  const agentIdMap = new Map<string, string>();
  for (const agent of existingAgents) {
    agentIdMap.set(agent.name, agent.agent_id);
  }

  // Phase 2: Create/update agents (first pass — without cross-agent transfers)
  console.log("Phase 2: Creating/updating agents (first pass)...");
  const results: { name: string; agentId: string }[] = [];

  for (const config of AGENTS) {
    try {
      const agentId = await createOrUpdateAgent(config, existingAgents, agentIdMap);
      agentIdMap.set(config.name, agentId);
      results.push({ name: config.name, agentId });
      console.log(`  OK: ${config.name} -> ${agentId}`);
    } catch (err) {
      console.error(`  FAILED: ${config.name} -> ${(err as Error).message}`);
      process.exit(1);
    }
  }
  console.log("");

  // Phase 3: Second pass to link transfer targets (now that all agent IDs exist)
  console.log("Phase 3: Linking agent transfers (second pass)...");
  const agentsWithTransfers = AGENTS.filter(
    (a) => a.transferTargets && a.transferTargets.length > 0
  );

  for (const config of agentsWithTransfers) {
    const agentId = agentIdMap.get(config.name);
    if (!agentId) continue;

    // Check if all transfer targets are resolved
    const allResolved = config.transferTargets!.every((t) =>
      agentIdMap.has(t.agentName)
    );

    if (allResolved) {
      try {
        const payload = buildAgentPayload(config, agentIdMap);
        await apiCall("PATCH", `/agents/${agentId}`, payload);
        console.log(`  OK: ${config.name} transfers linked`);
      } catch (err) {
        console.error(
          `  FAILED: ${config.name} transfer linking -> ${(err as Error).message}`
        );
      }
    } else {
      const missing = config.transferTargets!
        .filter((t) => !agentIdMap.has(t.agentName))
        .map((t) => t.agentName);
      console.warn(
        `  WARN: ${config.name} has unresolved transfer targets: ${missing.join(", ")}`
      );
    }
  }
  console.log("");

  // Phase 4: Summary
  console.log("=== Agent Setup Complete ===");
  console.log("");
  console.log("Agent IDs:");
  for (const { name, agentId } of results) {
    const envKey = name
      .replace("Aspire - ", "")
      .toUpperCase();
    console.log(`  ${name}: ${agentId}`);
    console.log(`    ELEVENLABS_AGENT_ID_${envKey}=${agentId}`);
  }
  console.log("");
  console.log("Export commands for Railway:");
  console.log("---");
  for (const { name, agentId } of results) {
    const envKey = name
      .replace("Aspire - ", "")
      .toUpperCase();
    console.log(
      `railway variables set ELEVENLABS_AGENT_ID_${envKey}=${agentId}`
    );
  }
  console.log("---");
  console.log("");
  console.log(
    "Set ELEVENLABS_WORKSPACE_SECRET in ElevenLabs dashboard under Agent > Security."
  );
  console.log(
    `Set system_env__gateway_url=${GATEWAY_URL} in ElevenLabs Agent environment variables.`
  );
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
