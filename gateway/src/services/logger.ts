/**
 * Structured Logger for Aspire Gateway
 * 
 * Production: Outputs JSON lines matching the Orchestrator format.
 * Dev: Outputs human-readable colored text.
 */

const IS_JSON = process.env.ASPIRE_LOG_FORMAT === 'json' || process.env.NODE_ENV === 'production';

export interface LogFields extends Record<string, any> {
  correlation_id?: string;
  suite_id?: string;
}

function format(level: string, message: string, fields: LogFields = {}) {
  if (IS_JSON) {
    return JSON.stringify({
      timestamp: new Date().toISOString(),
      level,
      logger: 'gateway',
      message,
      ...fields,
    });
  }
  
  const ts = new Date().toLocaleTimeString();
  const cid = fields.correlation_id ? ` [${fields.correlation_id.substring(0, 8)}]` : '';
  return `${ts} [${level}] gateway${cid} ${message} ${Object.keys(fields).length > 0 ? JSON.stringify(fields) : ''}`;
}

export const logger = {
  info: (msg: string, fields?: LogFields) => console.log(format('INFO', msg, fields)),
  warn: (msg: string, fields?: LogFields) => console.warn(format('WARNING', msg, fields)),
  error: (msg: string, fields?: LogFields) => console.error(format('ERROR', msg, fields)),
  debug: (msg: string, fields?: LogFields) => {
    if (process.env.ASPIRE_LOG_LEVEL === 'DEBUG') {
      console.log(format('DEBUG', msg, fields));
    }
  },
};
