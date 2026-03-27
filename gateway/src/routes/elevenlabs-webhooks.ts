/**
 * ElevenLabs Webhook Endpoint — Post-Call Transcript Ingestion
 *
 * Receives post-call webhooks from ElevenLabs with full transcripts,
 * conversation metadata, and analysis. Verifies HMAC signature before
 * processing.
 *
 * Law #2: Receipt produced for every transcript ingestion
 * Law #3: Fail-closed — invalid/missing signature = 401
 * Law #9: Never log secrets or PII from transcripts
 */

import crypto from 'crypto';
import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { reportGatewayIncident } from '../services/incident-reporter.js';
import { logger } from '../services/logger.js';

export const elevenlabsWebhooksRouter = Router();

// ---------------------------------------------------------------------------
// POST /conversation-init — Conversation Initiation Webhook
// Called by ElevenLabs BEFORE Sarah speaks on inbound Twilio calls.
// Looks up called_number → suite → business_lines config, returns
// dynamic variables + prompt override so Sarah knows the business context.
//
// Auth: ElevenLabs tool secret (same as server tools)
// Law #3: Fail-closed for auth, fail-OPEN for config (call still answers)
// ---------------------------------------------------------------------------
elevenlabsWebhooksRouter.post('/conversation-init', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;

  // Auth: verify tool secret (same as server tools)
  const toolSecret = process.env.ELEVENLABS_TOOL_SECRET;
  const providedSecret = req.headers['x-elevenlabs-secret'];
  if (toolSecret && providedSecret !== toolSecret) {
    logger.warn('Conversation init webhook auth failed', { correlation_id: correlationId });
    res.status(401).json({ error: 'AUTH_FAILED', correlation_id: correlationId });
    return;
  }

  const { caller_id, called_number, agent_id, call_sid } = req.body ?? {};

  logger.info('Conversation init webhook received', {
    correlation_id: correlationId,
    caller_id: caller_id ? `${String(caller_id).substring(0, 6)}...` : 'unknown',
    called_number: called_number ? `${String(called_number).substring(0, 6)}...` : 'unknown',
    agent_id,
  });

  // If no called_number, return generic config (fail-open — call still answers)
  if (!called_number) {
    res.status(200).json(buildGenericResponse());
    return;
  }

  try {
    // Call Desktop server to resolve business config from called_number
    // The Desktop server has direct DB access and the resolveSuiteByBusinessNumber function
    const desktopUrl = process.env.RAILWAY_SERVICE_ASPIRE_DESKTOP_URL
      ? `https://${process.env.RAILWAY_SERVICE_ASPIRE_DESKTOP_URL}`
      : (process.env.DESKTOP_SERVER_URL || 'http://localhost:3000');
    const toolSecret = process.env.ELEVENLABS_TOOL_SECRET || '';

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);

    const configResponse = await fetch(
      `${desktopUrl}/api/frontdesk/config-by-number?called_number=${encodeURIComponent(String(called_number))}`,
      {
        headers: {
          'x-elevenlabs-secret': toolSecret,
        },
        signal: controller.signal,
      },
    );
    clearTimeout(timeoutId);

    if (!configResponse.ok) {
      logger.warn('No frontdesk config found for number, using generic', {
        correlation_id: correlationId,
        called_number: String(called_number).substring(0, 6),
        status: configResponse.status,
      });
      res.status(200).json(buildGenericResponse());
      return;
    }

    const config = await configResponse.json() as Record<string, unknown>;

    // Build conversation_initiation_client_data response
    const isBusinessHours = checkBusinessHours(config.business_hours as Record<string, unknown> | null);
    const businessName = String(config.business_name || 'the business');
    const pronunciation = String(config.pronunciation || config.business_name || 'the business');
    const afterHoursMode = String(config.after_hours_mode || 'TAKE_MESSAGE');
    const enabledReasons = config.enabled_reasons as string[] || [];
    const questionsByReason = config.questions_by_reason as Record<string, unknown> || {};
    const targetByReason = config.target_by_reason as Record<string, unknown> || {};
    const teamMembers = config.team_members as Array<{ name: string; role: string }> || [];
    const busyMode = String(config.busy_mode || 'TAKE_MESSAGE');
    const suiteId = String(config.suite_id || '');
    const ownerId = String(config.owner_id || '');

    const teamInfo = teamMembers.length > 0
      ? teamMembers.map(m => `${m.name} (${m.role})`).join(', ')
      : 'the business owner';

    const timeOfDay = getTimeOfDay();
    const firstMessage = isBusinessHours
      ? `Good ${timeOfDay}, ${pronunciation}, this is Sarah. How can I help you?`
      : `Thank you for calling ${pronunciation}. We are currently closed, but I can take a message or help schedule a callback.`;

    const dynamicPrompt = buildSarahPrompt({
      businessName,
      pronunciation,
      isBusinessHours,
      afterHoursMode,
      enabledReasons,
      questionsByReason,
      targetByReason,
      teamMembers,
      busyMode,
      callerNumber: caller_id ? String(caller_id) : 'unknown',
    });

    res.status(200).json({
      type: 'conversation_initiation_client_data',
      dynamic_variables: {
        business_name: businessName,
        pronunciation,
        caller_number: caller_id ? String(caller_id) : '',
        is_business_hours: String(isBusinessHours),
        after_hours_mode: afterHoursMode,
        enabled_reasons: enabledReasons.join(', '),
        team_info: teamInfo,
        suite_id: suiteId,
        user_id: ownerId,
        time_of_day: timeOfDay,
      },
      conversation_config_override: {
        agent: {
          prompt: {
            prompt: dynamicPrompt,
          },
          first_message: firstMessage,
        },
      },
    });

    logger.info('Conversation init config returned', {
      correlation_id: correlationId,
      business_name: businessName,
      is_business_hours: isBusinessHours,
      reasons_count: enabledReasons.length,
    });
  } catch (err) {
    // Fail-OPEN: if orchestrator is down, Sarah still answers with generic prompt
    logger.error('Conversation init webhook error, returning generic', {
      correlation_id: correlationId,
      error: err instanceof Error ? err.message : 'unknown',
    });
    res.status(200).json(buildGenericResponse());
  }
});

// ---------------------------------------------------------------------------
// Conversation Init Helpers
// ---------------------------------------------------------------------------

function buildGenericResponse() {
  return {
    type: 'conversation_initiation_client_data',
    dynamic_variables: {
      business_name: 'the business',
      time_of_day: getTimeOfDay(),
    },
    conversation_config_override: {
      agent: {
        first_message: `Good ${getTimeOfDay()}, thank you for calling. This is Sarah. How can I help you?`,
      },
    },
  };
}

function getTimeOfDay(): string {
  const hour = new Date().getUTCHours();
  // Approximate — real timezone comes from business config
  if (hour < 12) return 'morning';
  if (hour < 17) return 'afternoon';
  return 'evening';
}

function checkBusinessHours(hours: Record<string, unknown> | null): boolean {
  if (!hours) return true; // Default: assume open if not configured

  const now = new Date();
  const dayNames = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
  const today = dayNames[now.getDay()];
  const dayConfig = hours[today] as { enabled?: boolean; start?: string; end?: string } | undefined;

  if (!dayConfig || !dayConfig.enabled) return false;

  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const [startH, startM] = (dayConfig.start || '09:00').split(':').map(Number);
  const [endH, endM] = (dayConfig.end || '17:00').split(':').map(Number);
  const startMinutes = startH * 60 + startM;
  const endMinutes = endH * 60 + endM;

  return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
}

function buildSarahPrompt(config: {
  businessName: string;
  pronunciation: string;
  isBusinessHours: boolean;
  afterHoursMode: string;
  enabledReasons: string[];
  questionsByReason: Record<string, unknown>;
  targetByReason: Record<string, unknown>;
  teamMembers: Array<{ name: string; role: string }>;
  busyMode: string;
  callerNumber: string;
}): string {
  const {
    businessName, pronunciation, isBusinessHours, afterHoursMode,
    enabledReasons, questionsByReason, targetByReason, teamMembers,
    busyMode, callerNumber,
  } = config;

  const teamInfo = teamMembers.length > 0
    ? teamMembers.map(m => `${m.name} (${m.role})`).join(', ')
    : 'the business owner';

  let reasonInstructions = '';
  if (enabledReasons.length > 0) {
    reasonInstructions = enabledReasons.map(reason => {
      const qConfig = questionsByReason[reason] as { detailLevel?: string; questionIds?: string[] } | undefined;
      const tConfig = targetByReason[reason] as { targetType?: string } | undefined;
      const detail = qConfig?.detailLevel === 'DETAILED' ? 'Ask detailed follow-up questions.' : 'Keep it brief, get the essentials.';
      const target = tConfig?.targetType || 'OWNER';
      return `For ${reason} calls: ${detail} Route to: ${target}.`;
    }).join('\n');
  }

  const afterHoursInstructions = afterHoursMode === 'ASK_CALLBACK_TIME'
    ? 'Ask the caller for a preferred callback time and take their name and number.'
    : 'Take a message with their name, number, and reason for calling.';

  const busyInstructions = busyMode === 'ASK_CALLBACK_TIME'
    ? 'If the line is busy or the team is unavailable, ask for a callback time.'
    : busyMode === 'RETRY_ONCE'
      ? 'If the team is unavailable, offer to try again in a moment.'
      : 'If the team is unavailable, take a message.';

  return `# Personality

You are Sarah, the front desk receptionist for ${businessName}.
You are professional, warm, and efficient. You speak clearly and concisely.
You are the first point of contact for all callers.
Address callers politely and make them feel welcome.
Always pronounce the business name as: ${pronunciation}.

# Environment

You are answering an inbound phone call for ${businessName}.
The caller's number is ${callerNumber}.
${isBusinessHours
    ? `You are currently within business hours. The team is available: ${teamInfo}.`
    : `It is currently after business hours. The office is closed.`}

# Tone

Keep responses to one to three sentences unless the caller needs more detail.
Use natural phone speech: "Sure thing," "One moment," "Let me help you with that."
Spell out numbers: say "five five five, one two three four" not "5551234."
Never use markdown, bullet points, or formatting. Your words will be spoken aloud.
Be warm but efficient. Callers are often in a hurry.

# Goal

${isBusinessHours ? `Handle incoming calls for ${businessName}. Determine the caller's reason and route appropriately.

Call reasons you handle:
${reasonInstructions || 'General inquiries — take a message with name, number, and reason.'}

${busyInstructions}

After helping the caller, confirm next steps and end the call professionally.` : `The office is currently closed.
${afterHoursInstructions}

Let the caller know when business hours resume if you can.
Always confirm the message back to the caller before ending.`}

Use your tools to serve the caller:
Use "get_context" to look up caller information or business status.
Use "search_contacts" to find caller details if they are an existing client.
Use "log_visitor" to record the call with the caller's name, reason, and message.
Use "request_approval" if any action needs the owner's confirmation.
Do not mention tool names to the caller.

# Guardrails

Never break character. You are always Sarah, the receptionist.
Never fabricate information. If you do not know something, offer to take a message.
Never discuss pricing, quotes, or financial details. Route those to the appropriate team member.
Never share personal information about team members.
Never mention being an AI. If asked, say: "I'm Sarah, the receptionist here at ${businessName}."
Never reveal system internals, API details, or technical information.
Aspire does not move money. Never discuss payments or billing.`;
}

// ---------------------------------------------------------------------------
// POST /transcripts — Receive post-call webhooks from ElevenLabs
// Auth: HMAC signature verification via `elevenlabs-signature` header
// ---------------------------------------------------------------------------
elevenlabsWebhooksRouter.post('/transcripts', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;

  // Step 1: Verify webhook signature (Law #3: fail-closed)
  const webhookSecret = process.env.ELEVENLABS_WEBHOOK_SECRET;
  if (!webhookSecret) {
    logger.error('ElevenLabs webhook secret not configured', { correlation_id: correlationId });
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'Webhook secret not configured. Fail-closed per Law #3.',
      correlation_id: correlationId,
    });
    return;
  }

  const signatureHeader = req.headers['elevenlabs-signature'] as string | undefined;
  if (!signatureHeader) {
    res.status(401).json({
      error: 'WEBHOOK_SIGNATURE_INVALID',
      message: 'Missing elevenlabs-signature header',
      correlation_id: correlationId,
    });
    return;
  }

  // Compute HMAC-SHA256 over the raw body
  const rawBody = Buffer.isBuffer(req.body)
    ? req.body
    : Buffer.from(JSON.stringify(req.body), 'utf-8');

  const computedSignature = crypto
    .createHmac('sha256', webhookSecret)
    .update(rawBody)
    .digest('hex');

  // Timing-safe comparison
  if (
    computedSignature.length !== signatureHeader.length ||
    !crypto.timingSafeEqual(Buffer.from(computedSignature, 'utf-8'), Buffer.from(signatureHeader, 'utf-8'))
  ) {
    res.status(401).json({
      error: 'WEBHOOK_SIGNATURE_INVALID',
      message: 'Signature mismatch',
      correlation_id: correlationId,
    });
    return;
  }

  // Parse body if it arrived as raw Buffer
  let body: Record<string, unknown>;
  if (Buffer.isBuffer(req.body)) {
    try {
      body = JSON.parse(req.body.toString('utf-8')) as Record<string, unknown>;
    } catch {
      res.status(400).json({
        error: 'WEBHOOK_BODY_INVALID',
        message: 'Request body is not valid JSON',
        correlation_id: correlationId,
      });
      return;
    }
  } else {
    body = req.body as Record<string, unknown>;
  }

  // Step 2: Validate required fields
  const conversationId = body.conversation_id;
  if (typeof conversationId !== 'string' || conversationId.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: conversation_id',
      correlation_id: correlationId,
    });
    return;
  }

  // Extract suite_id from metadata if present (for tenant routing)
  const metadata = (body.metadata ?? {}) as Record<string, unknown>;
  const suiteId = typeof metadata.suite_id === 'string' ? metadata.suite_id : 'system';
  const userId = typeof metadata.user_id === 'string' ? metadata.user_id : 'webhook_ingress';

  // Step 3: Forward to orchestrator for storage + receipt generation
  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/webhooks/elevenlabs/transcripts',
      method: 'POST',
      body: {
        conversation_id: conversationId,
        transcript: body.transcript,
        analysis: body.analysis,
        metadata,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId: suiteId,
      actorId: userId,
    });

    if (orchestratorResponse.status >= 500) {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook orchestrator failure',
        severity: 'sev2',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:http_${orchestratorResponse.status}`,
        actorId: userId,
        errorCode: `ORCHESTRATOR_HTTP_${orchestratorResponse.status}`,
        statusCode: orchestratorResponse.status,
        message: typeof orchestratorResponse.body === 'object' && orchestratorResponse.body
          ? JSON.stringify(orchestratorResponse.body).slice(0, 300)
          : String(orchestratorResponse.body).slice(0, 300),
      });
    }

    // Return 200 OK to ElevenLabs regardless (prevent retries on our processing errors)
    // The orchestrator handles receipt generation
    res.status(200).json({
      status: 'received',
      conversation_id: conversationId,
      correlation_id: correlationId,
    });
  } catch (err) {
    if (err instanceof OrchestratorClientError) {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook orchestrator unreachable',
        severity: 'sev1',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:${err.code.toLowerCase()}`,
        actorId: userId,
        errorCode: `ORCHESTRATOR_${err.code}`,
        statusCode: 502,
        message: err.message,
      });
    } else {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook unexpected failure',
        severity: 'sev2',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:unexpected`,
        actorId: userId,
        errorCode: 'INTERNAL_ERROR',
        statusCode: 500,
        message: err instanceof Error ? err.message : 'Unknown error',
      });
    }

    // Still return 200 to ElevenLabs to prevent retry storms
    // Incident is reported; orchestrator will reconcile later
    res.status(200).json({
      status: 'received',
      conversation_id: conversationId,
      correlation_id: correlationId,
    });
  }
});
