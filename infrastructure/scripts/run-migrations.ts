/**
 * Aspire Trust Spine Migration Runner
 *
 * Applies SQL migration files to Supabase via Session Pooler connection.
 * No psql required — uses the pg library directly.
 *
 * Usage:
 *   npx tsx infrastructure/scripts/run-migrations.ts
 *
 * Environment:
 *   DATABASE_URL - Session Pooler connection string (required)
 *   DRY_RUN     - Set to "true" to print SQL without executing
 */

import { Pool } from 'pg';
import * as fs from 'fs';
import * as path from 'path';

const MIGRATIONS_DIR = path.resolve(__dirname, '..', 'migrations');

const MIGRATION_FILES = [
  'trust-spine-bundle.sql',
  '20260210_desktop_tables.sql',
];

async function run() {
  const databaseUrl = process.env.DATABASE_URL;
  const dryRun = process.env.DRY_RUN === 'true';

  if (!databaseUrl) {
    console.error('ERROR: DATABASE_URL environment variable is required.');
    console.error('Expected: postgresql://postgres.qtuehjqlcmfcascqjjhc:[PASSWORD]@aws-0-1-us-east-1.pooler.supabase.com:5432/postgres');
    process.exit(1);
  }

  console.log('='.repeat(70));
  console.log('Aspire Trust Spine Migration Runner');
  console.log('='.repeat(70));
  console.log(`Mode: ${dryRun ? 'DRY RUN (no changes)' : 'LIVE EXECUTION'}`);
  console.log(`Migrations dir: ${MIGRATIONS_DIR}`);
  console.log(`Files to apply: ${MIGRATION_FILES.length}`);
  console.log('');

  // Verify all files exist before starting
  for (const file of MIGRATION_FILES) {
    const filePath = path.join(MIGRATIONS_DIR, file);
    if (!fs.existsSync(filePath)) {
      console.error(`ERROR: Migration file not found: ${filePath}`);
      process.exit(1);
    }
    const stats = fs.statSync(filePath);
    console.log(`  [OK] ${file} (${(stats.size / 1024).toFixed(1)} KB)`);
  }
  console.log('');

  if (dryRun) {
    console.log('DRY RUN — printing SQL sizes only, no database changes.');
    for (const file of MIGRATION_FILES) {
      const filePath = path.join(MIGRATIONS_DIR, file);
      const sql = fs.readFileSync(filePath, 'utf-8');
      const stmtCount = sql.split(';').filter(s => s.trim().length > 0).length;
      console.log(`  ${file}: ${stmtCount} statements, ${sql.length} chars`);
    }
    console.log('\nDry run complete. Set DRY_RUN=false or remove it to execute.');
    process.exit(0);
  }

  const pool = new Pool({
    connectionString: databaseUrl,
    ssl: { rejectUnauthorized: false },
    connectionTimeoutMillis: 30000,
    statement_timeout: 300000, // 5 min per statement
  });

  try {
    // Test connection
    console.log('Testing database connection...');
    const client = await pool.connect();
    const versionResult = await client.query('SELECT version()');
    console.log(`Connected: ${(versionResult.rows[0].version as string).split(',')[0]}`);
    client.release();
    console.log('');

    // Apply each migration file
    for (let i = 0; i < MIGRATION_FILES.length; i++) {
      const file = MIGRATION_FILES[i];
      const filePath = path.join(MIGRATIONS_DIR, file);
      const sql = fs.readFileSync(filePath, 'utf-8');

      console.log('-'.repeat(70));
      console.log(`[${i + 1}/${MIGRATION_FILES.length}] Applying: ${file}`);
      console.log(`  Size: ${(sql.length / 1024).toFixed(1)} KB`);
      console.log('-'.repeat(70));

      const startTime = Date.now();

      try {
        // Execute the entire SQL file as a single query
        // Most migration files use BEGIN/COMMIT internally
        const migrationClient = await pool.connect();
        try {
          await migrationClient.query(sql);
          const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
          console.log(`  [SUCCESS] ${file} applied in ${elapsed}s`);
        } finally {
          migrationClient.release();
        }
      } catch (err: any) {
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        console.error(`  [FAILED] ${file} after ${elapsed}s`);
        console.error(`  Error: ${err.message}`);
        if (err.position) {
          // Show context around the error position
          const pos = parseInt(err.position, 10);
          const context = sql.substring(Math.max(0, pos - 100), pos + 100);
          console.error(`  Near: ...${context}...`);
        }
        console.error('');
        console.error('Migration halted. Fix the error and re-run.');
        console.error('Previous migrations in this run may have been applied.');
        process.exit(1);
      }

      console.log('');
    }

    // Post-migration verification
    console.log('='.repeat(70));
    console.log('Post-migration verification');
    console.log('='.repeat(70));

    const verifyClient = await pool.connect();
    try {
      // Check Trust Spine core tables
      const coreTablesResult = await verifyClient.query(`
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('app', 'public')
          AND table_name IN ('suites', 'offices', 'receipts', 'receipt_items', 'capability_tokens',
                            'approval_requests', 'approval_events', 'inbox', 'outbox',
                            'suite_profiles', 'services', 'bookings', 'finance_connections',
                            'finance_events', 'finance_snapshots')
        ORDER BY table_schema, table_name
      `);
      console.log(`\nTables verified: ${coreTablesResult.rowCount}`);
      for (const row of coreTablesResult.rows) {
        console.log(`  [OK] ${row.table_schema}.${row.table_name}`);
      }

      // Check RLS is enabled
      const rlsResult = await verifyClient.query(`
        SELECT schemaname, tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN ('suite_profiles', 'services', 'bookings', 'finance_connections',
                           'finance_events', 'finance_snapshots', 'receipts')
        ORDER BY tablename
      `);
      console.log(`\nRLS status:`);
      for (const row of rlsResult.rows) {
        const status = row.rowsecurity ? 'ENABLED' : 'DISABLED';
        console.log(`  ${row.tablename}: ${status}`);
      }

      // Check receipts table columns (Trust Spine format)
      const colsResult = await verifyClient.query(`
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'receipts' AND table_schema = 'public'
        ORDER BY ordinal_position
      `);
      console.log(`\nReceipts table columns: ${colsResult.rowCount}`);
      for (const row of colsResult.rows) {
        console.log(`  ${row.column_name}: ${row.data_type}`);
      }
    } finally {
      verifyClient.release();
    }

    console.log('\n' + '='.repeat(70));
    console.log('ALL MIGRATIONS APPLIED SUCCESSFULLY');
    console.log('='.repeat(70));

  } catch (err: any) {
    console.error('Fatal error:', err.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

run().catch((err) => {
  console.error('Unhandled error:', err);
  process.exit(1);
});
