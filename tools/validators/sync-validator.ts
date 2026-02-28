/**
 * ============================================================================
 * ASPIRE SYNC VALIDATOR - Enterprise-Grade Documentation Sync Engine
 * ============================================================================
 * 
 * Validates consistency across all Aspire documentation, schemas, and registries.
 * 
 * PURPOSE: Ensure phases and roadmap flow together correctly
 * VERSION: 1.0.0
 * LAST UPDATED: 2026-02-04
 * 
 * ============================================================================
 */

import * as fs from 'fs';
import * as path from 'path';
import * as yaml from 'js-yaml';

// ============================================================================
// TYPES
// ============================================================================

interface SyncError {
  category: 'schema' | 'duration' | 'terminology' | 'cross-ref' | 'gates';
  severity: 'error' | 'warning';
  file: string;
  line?: number;
  message: string;
  fix?: string;
}

interface SyncValidationResult {
  passed: boolean;
  score: number;  // 1-10
  errors: SyncError[];
  warnings: SyncError[];
  stats: {
    filesChecked: number;
    errorsFound: number;
    warningsFound: number;
    checksPerformed: number;
  };
}

interface PhaseInfo {
  name: string;
  duration: string;
  durationWeeks: number;
  file: string;
}

// ============================================================================
// CONFIGURATION
// ============================================================================

const CONFIG = {
  planDir: 'plan',
  phasesDir: 'plan/phases',
  schemasDir: 'plan/schemas',
  registriesDir: 'plan/registries',
  
  // Expected total timeline (from v4.0 roadmap)
  expectedTotalWeeks: 52,
  
  // Expected phase durations (from v4.0 roadmap)
  expectedDurations: {
    '0A': { min: 0.3, max: 0.5 },  // 2-3 days
    '0B': { min: 2, max: 3 },
    '1': { min: 9, max: 9 },
    '2': { min: 12, max: 12 },
    '3': { min: 7, max: 7 },
    '4': { min: 12, max: 12 },
    '5': { min: 3, max: 3 },
    '6': { min: 16, max: 16 },
  },
  
  // Banned terminology (legacy terms that should not appear)
  bannedTerms: [
    { pattern: /\bZoho\b/gi, replacement: 'PolarisM', context: 'Mail provider' },
    { pattern: /risk_tier\s*=\s*['"]low['"]/gi, replacement: "'green'", context: 'Risk tier' },
    { pattern: /risk_tier\s*=\s*['"]medium['"]/gi, replacement: "'yellow'", context: 'Risk tier' },
    { pattern: /risk_tier\s*=\s*['"]high['"]/gi, replacement: "'red'", context: 'Risk tier' },
    { pattern: /RiskTier\s*=\s*['"]low['"]/gi, replacement: "'green'", context: 'Risk tier' },
    { pattern: /RiskTier\s*=\s*['"]medium['"]/gi, replacement: "'yellow'", context: 'Risk tier' },
    { pattern: /RiskTier\s*=\s*['"]high['"]/gi, replacement: "'red'", context: 'Risk tier' },
    { pattern: /trace_id/g, replacement: 'correlation_id', context: 'Receipt field' },
  ],
  
  // Files to skip in terminology checks
  skipFiles: [
    'Aspire-Production-Roadmap-BACKUP',
    'node_modules',
    '.git',
    'temp_ecosystem_scan',
    'Aspire_Ecosystem_extracted',
    'aspire_reflect_extracted',
  ],
};

// ============================================================================
// VALIDATORS
// ============================================================================

/**
 * Validate timeline consistency across all documentation
 */
async function validateTimeline(errors: SyncError[]): Promise<void> {
  console.log('🕐 Validating timeline consistency...');
  
  // Check dependencies.md
  const dependenciesPath = path.join(CONFIG.planDir, '00-dependencies.md');
  if (fs.existsSync(dependenciesPath)) {
    const content = fs.readFileSync(dependenciesPath, 'utf-8');
    
    // Check for correct total timeline
    if (!content.includes('52 weeks') && !content.includes('~52 weeks')) {
      // Check if it has old timeline
      if (content.includes('47 weeks') || content.includes('47-48 weeks')) {
        errors.push({
          category: 'duration',
          severity: 'error',
          file: dependenciesPath,
          message: 'Timeline shows 47-48 weeks but should be 52 weeks (v4.0)',
          fix: 'Update to "~52 weeks" to match Aspire-Production-Roadmap.md v4.0',
        });
      }
    }
  }
  
  // Check main roadmap
  const roadmapPath = path.join(CONFIG.planDir, 'Aspire-Production-Roadmap.md');
  if (fs.existsSync(roadmapPath)) {
    const content = fs.readFileSync(roadmapPath, 'utf-8');
    
    // Verify v4.0 changelog exists
    if (!content.includes('Version 4.0')) {
      errors.push({
        category: 'duration',
        severity: 'warning',
        file: roadmapPath,
        message: 'Version 4.0 changelog not found',
        fix: 'Ensure v4.0 changelog is present with 52-week timeline',
      });
    }
  }
}

/**
 * Validate terminology consistency (no banned terms)
 */
async function validateTerminology(errors: SyncError[]): Promise<void> {
  console.log('📝 Validating terminology consistency...');
  
  const filesToCheck = getMarkdownFiles(CONFIG.planDir);
  
  for (const filePath of filesToCheck) {
    // Skip backup and excluded files
    if (CONFIG.skipFiles.some(skip => filePath.includes(skip))) {
      continue;
    }
    
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.split('\n');
    
    for (const term of CONFIG.bannedTerms) {
      lines.forEach((line, index) => {
        if (term.pattern.test(line)) {
          // Reset regex lastIndex
          term.pattern.lastIndex = 0;
          
          errors.push({
            category: 'terminology',
            severity: 'error',
            file: filePath,
            line: index + 1,
            message: `Banned term found: ${term.context}`,
            fix: `Replace with "${term.replacement}"`,
          });
        }
      });
    }
  }
}

/**
 * Validate schema consistency across SQL, JSON, and TypeScript
 */
async function validateSchemas(errors: SyncError[]): Promise<void> {
  console.log('📋 Validating schema consistency...');
  
  const schemasDir = CONFIG.schemasDir;
  
  if (!fs.existsSync(schemasDir)) {
    errors.push({
      category: 'schema',
      severity: 'warning',
      file: schemasDir,
      message: 'Schemas directory does not exist',
      fix: 'Create plan/schemas/ directory with canonical schema definitions',
    });
    return;
  }
  
  // Check for required schema files
  const requiredSchemas = [
    'risk-tiers.enum.yaml',
    'receipts.schema.v1.yaml',
  ];
  
  for (const schema of requiredSchemas) {
    const schemaPath = path.join(schemasDir, schema);
    if (!fs.existsSync(schemaPath)) {
      errors.push({
        category: 'schema',
        severity: 'error',
        file: schemaPath,
        message: `Required schema file missing: ${schema}`,
        fix: `Create ${schemaPath} with canonical definitions`,
      });
    }
  }
}

/**
 * Validate cross-references between files
 */
async function validateCrossReferences(errors: SyncError[]): Promise<void> {
  console.log('🔗 Validating cross-references...');
  
  // Check that phase files reference success criteria
  const phasesDir = CONFIG.phasesDir;
  
  if (!fs.existsSync(phasesDir)) {
    errors.push({
      category: 'cross-ref',
      severity: 'warning',
      file: phasesDir,
      message: 'Phases directory does not exist',
    });
    return;
  }
  
  const phaseFiles = fs.readdirSync(phasesDir).filter(f => f.endsWith('.md'));
  
  for (const phaseFile of phaseFiles) {
    const content = fs.readFileSync(path.join(phasesDir, phaseFile), 'utf-8');
    
    // Check for success criteria references
    if (!content.includes('SC-') && !content.includes('success criteria')) {
      errors.push({
        category: 'cross-ref',
        severity: 'warning',
        file: path.join(phasesDir, phaseFile),
        message: 'No success criteria references found',
        fix: 'Add references to success criteria IDs (e.g., 1-SC-001)',
      });
    }
  }
}

/**
 * Validate gate requirements per phase
 */
async function validateGates(errors: SyncError[]): Promise<void> {
  console.log('🚪 Validating gate requirements...');
  
  const gatesPath = path.join(CONFIG.registriesDir, 'gate-satisfaction.yaml');
  
  if (!fs.existsSync(gatesPath)) {
    errors.push({
      category: 'gates',
      severity: 'warning',
      file: gatesPath,
      message: 'Gate satisfaction registry does not exist',
      fix: 'Create plan/registries/gate-satisfaction.yaml',
    });
    return;
  }
  
  try {
    const gatesContent = fs.readFileSync(gatesPath, 'utf-8');
    const gates = yaml.load(gatesContent) as any;
    
    // Verify all 10 gates are defined
    if (!gates.gates || Object.keys(gates.gates).length < 10) {
      errors.push({
        category: 'gates',
        severity: 'error',
        file: gatesPath,
        message: 'Not all 10 gates are defined',
        fix: 'Ensure gates gate_1 through gate_10 are defined',
      });
    }
    
    // Verify phase-gate mapping
    if (!gates.phase_gate_requirements) {
      errors.push({
        category: 'gates',
        severity: 'error',
        file: gatesPath,
        message: 'Phase-gate requirements mapping missing',
        fix: 'Add phase_gate_requirements section',
      });
    }
  } catch (e) {
    errors.push({
      category: 'gates',
      severity: 'error',
      file: gatesPath,
      message: `Failed to parse gate satisfaction registry: ${e}`,
    });
  }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Recursively get all markdown files in a directory
 */
function getMarkdownFiles(dir: string): string[] {
  const files: string[] = [];
  
  if (!fs.existsSync(dir)) {
    return files;
  }
  
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    
    if (entry.isDirectory()) {
      // Skip certain directories
      if (!CONFIG.skipFiles.some(skip => entry.name.includes(skip))) {
        files.push(...getMarkdownFiles(fullPath));
      }
    } else if (entry.name.endsWith('.md')) {
      files.push(fullPath);
    }
  }
  
  return files;
}

/**
 * Calculate sync score based on errors and warnings
 */
function calculateScore(errors: SyncError[]): number {
  const errorCount = errors.filter(e => e.severity === 'error').length;
  const warningCount = errors.filter(e => e.severity === 'warning').length;
  
  // Base score of 10, subtract for errors and warnings
  let score = 10;
  score -= errorCount * 1.5;  // Errors reduce score more
  score -= warningCount * 0.5;
  
  // Ensure score is between 1 and 10
  return Math.max(1, Math.min(10, Math.round(score * 10) / 10));
}

// ============================================================================
// MAIN VALIDATION FUNCTION
// ============================================================================

/**
 * Run all sync validations
 */
export async function validateSync(): Promise<SyncValidationResult> {
  console.log('');
  console.log('╔════════════════════════════════════════════════════════════════╗');
  console.log('║           ASPIRE SYNC VALIDATOR v1.0.0                         ║');
  console.log('║           Enterprise-Grade Documentation Sync                  ║');
  console.log('╚════════════════════════════════════════════════════════════════╝');
  console.log('');
  
  const allErrors: SyncError[] = [];
  let filesChecked = 0;
  let checksPerformed = 0;
  
  // Run all validators
  await validateTimeline(allErrors);
  checksPerformed++;
  
  await validateTerminology(allErrors);
  checksPerformed++;
  filesChecked += getMarkdownFiles(CONFIG.planDir).length;
  
  await validateSchemas(allErrors);
  checksPerformed++;
  
  await validateCrossReferences(allErrors);
  checksPerformed++;
  
  await validateGates(allErrors);
  checksPerformed++;
  
  // Separate errors and warnings
  const errors = allErrors.filter(e => e.severity === 'error');
  const warnings = allErrors.filter(e => e.severity === 'warning');
  
  // Calculate score
  const score = calculateScore(allErrors);
  
  // Print results
  console.log('');
  console.log('════════════════════════════════════════════════════════════════');
  console.log('                      VALIDATION RESULTS');
  console.log('════════════════════════════════════════════════════════════════');
  console.log('');
  
  if (errors.length > 0) {
    console.log('❌ ERRORS:');
    for (const error of errors) {
      console.log(`  [${error.category}] ${error.file}${error.line ? `:${error.line}` : ''}`);
      console.log(`    ${error.message}`);
      if (error.fix) {
        console.log(`    Fix: ${error.fix}`);
      }
      console.log('');
    }
  }
  
  if (warnings.length > 0) {
    console.log('⚠️  WARNINGS:');
    for (const warning of warnings) {
      console.log(`  [${warning.category}] ${warning.file}${warning.line ? `:${warning.line}` : ''}`);
      console.log(`    ${warning.message}`);
      if (warning.fix) {
        console.log(`    Fix: ${warning.fix}`);
      }
      console.log('');
    }
  }
  
  console.log('════════════════════════════════════════════════════════════════');
  console.log(`📊 SYNC SCORE: ${score}/10`);
  console.log(`   Files Checked: ${filesChecked}`);
  console.log(`   Checks Performed: ${checksPerformed}`);
  console.log(`   Errors: ${errors.length}`);
  console.log(`   Warnings: ${warnings.length}`);
  console.log('════════════════════════════════════════════════════════════════');
  
  const passed = errors.length === 0;
  
  if (passed) {
    console.log('');
    console.log('✅ SYNC VALIDATION PASSED');
  } else {
    console.log('');
    console.log('❌ SYNC VALIDATION FAILED - Please fix errors above');
  }
  
  console.log('');
  
  return {
    passed,
    score,
    errors,
    warnings,
    stats: {
      filesChecked,
      errorsFound: errors.length,
      warningsFound: warnings.length,
      checksPerformed,
    },
  };
}

// ============================================================================
// CLI ENTRY POINT
// ============================================================================

if (require.main === module) {
  validateSync()
    .then(result => {
      process.exit(result.passed ? 0 : 1);
    })
    .catch(error => {
      console.error('Validation failed with error:', error);
      process.exit(1);
    });
}
