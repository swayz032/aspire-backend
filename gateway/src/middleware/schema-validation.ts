/**
 * Schema Validation Middleware — AJV-based Request Validation (Law #3: Fail Closed)
 *
 * Validates AvaOrchestratorRequest body at the gateway edge.
 * Invalid requests are rejected before reaching the orchestrator.
 *
 * Uses ajv with ajv-formats for proper date-time/uuid validation.
 */

import type { Request, Response, NextFunction } from 'express';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';

type AjvErrorObject = { instancePath?: string; message?: string };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const AjvDefault = (Ajv as any).default ?? Ajv;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const addFormatsDefault = (addFormats as any).default ?? addFormats;

const ajv = new AjvDefault({ allErrors: true, removeAdditional: false });
addFormatsDefault(ajv);

/**
 * AvaOrchestratorRequest JSON Schema.
 * Per plan/contracts/ava-user/ava_orchestrator_request.schema.json
 */
const AvaOrchestratorRequestSchema = {
  type: 'object',
  required: [
    'schema_version',
    'suite_id',
    'office_id',
    'request_id',
    'correlation_id',
    'timestamp',
    'task_type',
    'payload',
  ],
  properties: {
    schema_version: { type: 'string', const: '1.0' },
    suite_id: { type: 'string', minLength: 1 },
    office_id: { type: 'string', minLength: 1 },
    request_id: { type: 'string', format: 'uuid' },
    correlation_id: { type: 'string', format: 'uuid' },
    timestamp: { type: 'string', format: 'date-time' },
    task_type: { type: 'string', minLength: 1, maxLength: 100 },
    payload: { type: 'object' },
  },
  additionalProperties: false,
} as const;

const validateRequest = ajv.compile(AvaOrchestratorRequestSchema);

/**
 * AvaResult JSON Schema for egress validation.
 * Per plan/contracts/ava-user/ava_result.schema.json
 */
const AvaResultSchema = {
  type: 'object',
  required: ['schema_version', 'request_id', 'correlation_id', 'route', 'risk', 'governance', 'plan'],
  properties: {
    schema_version: { type: 'string' },
    request_id: { type: 'string' },
    correlation_id: { type: 'string' },
    route: { type: 'object' },
    risk: {
      type: 'object',
      required: ['tier'],
      properties: {
        tier: { type: 'string', enum: ['green', 'yellow', 'red'] },
      },
    },
    governance: {
      type: 'object',
      required: ['approvals_required', 'presence_required', 'capability_token_required', 'receipt_ids'],
      properties: {
        approvals_required: { type: 'array', items: { type: 'string' } },
        presence_required: { type: 'boolean' },
        capability_token_required: { type: 'boolean' },
        receipt_ids: { type: 'array', items: { type: 'string' } },
      },
    },
    plan: { type: 'object' },
  },
} as const;

const validateResult = ajv.compile(AvaResultSchema);

/**
 * Middleware: validate AvaOrchestratorRequest body.
 */
export function schemaValidationMiddleware(req: Request, res: Response, next: NextFunction): void {
  if (!req.body || typeof req.body !== 'object') {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Request body must be a JSON object',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  const valid = validateRequest(req.body);
  if (!valid) {
    const errors: AjvErrorObject[] = validateRequest.errors ?? [];
    const details = errors.map((e: AjvErrorObject) => `${e.instancePath || '/'}: ${e.message ?? 'unknown'}`).join('; ');
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Request validation failed: ${details}`,
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  next();
}

/**
 * Validate AvaResult before returning to client (egress validation).
 * Per spec: "Validate AvaResult schema before returning"
 *
 * Returns true if valid, false with error details if invalid.
 */
export function validateAvaResult(result: unknown): { valid: boolean; errors?: string } {
  const valid = validateResult(result);
  if (!valid) {
    const errors: AjvErrorObject[] = validateResult.errors ?? [];
    const details = errors.map((e: AjvErrorObject) => `${e.instancePath || '/'}: ${e.message ?? 'unknown'}`).join('; ');
    return { valid: false, errors: details };
  }
  return { valid: true };
}
