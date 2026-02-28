---
phase: "0B"
name: "Trust Spine Deployment + Desktop Integration"
status: "complete"
status_date: "2026-02-12"
blocking_phase: "0A"
blocks_phases: ["1"]
duration_estimate: "2-3 days"
handoff_provides: "Trust Spine substrate (49 migrations, 5 Edge Functions, RLS-enforced tenant isolation) + Desktop server on Trust Spine"
layer_spec: "reference/layer-specs/trust-spine-spec.md"
implementation_readiness: "100% — cloud deployed + local dev environment complete"
gates_satisfied: ["Gate 6: Receipts Immutable (partial)", "Gate 7: RLS Isolation (27/27 tests pass)"]
priority: "high"
cost: "$0/mo (Supabase free tier + Railway)"
---

# PHASE 0B: Trust Spine Deploy + Desktop Integration — COMPLETE (2026-02-12)

> **Cloud half** completed 2026-02-10. **Local dev half** (Skytech Tower) completed 2026-02-12.

## Objective

Deploy complete Trust Spine governance substrate to Supabase AND connect Aspire Desktop (Express + Expo web on Railway) as the first Trust Spine client with governed receipts, RLS-enforced tenant isolation, and the foundation for mobile + orchestrator integration.

## Completion Summary

**Deployed to Supabase:**
- 42 core Trust Spine migrations + 7 A2A addon migrations = 49 total
- 5 Edge Functions: approval-events, inbox, outbox-executor, outbox-worker, policy-eval
- 40 tables with RLS + FORCE ROW LEVEL SECURITY
- Splinter lint: 0 WARNs, 149 INFOs (all expected — unused indexes + stripe FKs)

**Desktop Server Refactored:**
- Removed `initDatabase()` — tables now come from Supabase migrations
- Added RLS context middleware (`app.current_suite_id` set on every request)
- Receipt format upgraded to Trust Spine 15-column (hash-chained, UUID suite_id, JSONB action/result)
- All 14 server files updated for UUID suite_id + office_id
- PR #1 merged to `swayz032/Aspire-Desktop` (SHA: `952cfb4`)

**Live at www.aspireos.app:**
- Health endpoint: 200 OK
- Finance routes: Stripe connected, Plaid/Gusto/QuickBooks sandbox ready
- Bootstrap suite: `d97291ee-d458-4a8a-97b3-21bc78019364`
- Bootstrap office: `52fffe76-2d4c-4513-847f-521bf08a2e05`

**Testing (Gate 1):**
- 27/27 RLS isolation + evil tests PASS
- Cross-tenant reads: 0 rows (isolated)
- Cross-tenant writes: denied by RLS
- Receipt immutability: UPDATE/DELETE blocked by triggers
- Fail-closed: empty/fake/injected suite_id all denied

**Security Fixes Applied:**
- CRITICAL: Gusto webhook secret redacted from logs
- CRITICAL: RLS middleware moved BEFORE all webhook routes
- HIGH: x-suite-id header injection removed
- HIGH: TOKEN_ENCRYPTION_KEY hardcoded fallback removed (fail-closed)

**Local Dev Environment (Skytech Tower) — Completed 2026-02-12:**
- High Performance power plan active
- WSL2 Ubuntu 22.04 (23GB RAM, 8GB swap, 12 processors)
- Git configured + SSH key registered on GitHub (swayz032)
- Postgres 16.11 + pgvector 0.8.0 on :5432 (aspire_dev database)
- Redis 7 on :6379 (AOF enabled, 2GB maxmemory)
- CUDA Toolkit 12.6 (RTX 5060, 8151 MiB VRAM, driver 591.74)
- Python 3.11.14 (venv at ~/venvs/aspire)
- Node.js v20.20.0 via nvm + pnpm 10.29.3
- Ollama: llama3:8b (4.7GB) + qwen3-coder:30b (18GB)
- Docker Desktop 29.2.0 + Compose 5.0.2
- n8n via Docker on :5678 (n8n-db on :5433)
- OTEL Collector + Prometheus + Grafana via Docker (observability stack)
- n8n-mcp configured in .claude/mcp.json (API key set)
- n8n-skills (7 skills) installed at ~/.claude/skills/
- SLI/SLO definitions documented
- n8n hardening audit documented (Law #1/#7 compliance)

**Deferred to Phase 1:**
- Receipt coverage gap: 16/28 operations missing receipts (booking CRUD, service CRUD, authority queue, token ops)
- Auth-based RLS tests for Trust Spine tables (requires Supabase Auth integration)
- Go receipt hash verifier (not needed yet — hash computation done in Postgres triggers)
- N8N-005: Wire n8n to Trust Spine (requires LangGraph orchestrator)

---

## 🔗 API WIRING TASKS (v4.2)

**This phase establishes the infrastructure foundation for all 19 API services:**

| API Service | Wiring Task | Verification |
|-------------|-------------|--------------|
| **Supabase** | Apply Trust Spine canonical migrations per MIGRATION_ORDER_ADDON.md (see CANONICAL_PATHS.md for exact paths and counts). Deploy 5 core Edge Functions + optional A2A addon (7 migrations + 3 Edge Functions). | Tables created, RLS active |
| **AWS S3** | Configure bucket + CORS | Upload/download works |
| **Upstash** | Install Redis, configure queue | `redis-cli ping` returns PONG |
| **OpenAI** | Store API key in .env | API call succeeds |

**Schema Files to Deploy:**
- `plan/schemas/risk-tiers.enum.yaml` → Canonical risk tier definition
- `plan/schemas/receipts.schema.v1.yaml` → Receipt schema (SQL/TS/JSON)
- `plan/registries/skill-pack-registry.yaml` → Track 15 skill packs

**Environment Files:**
```
.env                    # All 19 API keys (gitignored, never committed)
.env.example            # Template without real values (committed)
backend/.env            # Backend-specific subset
mobile/.env             # Mobile-specific subset (Expo)
```

---

## Trust Spine Deployment (NEW)

**Critical Addition:** Deploy complete Trust Spine governance substrate before tower development work.

### Database Migrations (see CANONICAL_PATHS.md for exact paths and counts)
- Deploy Claude Handoff 4.0 base migrations (baseline schema)
- Deploy 43 Trust Spine core migrations (see CANONICAL_PATHS.md) (suite/office identity, receipts v1, outbox, inbox)
- Deploy 7 A2A addon migrations (agent-to-agent task queue)
- Verify RLS policies on all tables

### Edge Functions Deployment (8 functions: 5 main + 3 A2A addon)

**Main Edge Functions (5):**
- `policy-eval` - Policy evaluation engine (ALLOW/DENY/REQUIRE_APPROVAL)
- `outbox-worker` - Durable job executor (FOR UPDATE SKIP LOCKED concurrency)
- `outbox-executor` - Executes outbox jobs
- `inbox` - Intent ingestion endpoint
- `approval-events` - Approval routing (Yellow/Red tier gates)

**A2A Addon Edge Functions (3 - optional):**
- `a2a-inbox-claim` - Claim agent-to-agent tasks
- `a2a-inbox-enqueue` - Enqueue A2A tasks
- `a2a-inbox-transition` - Transition A2A task states

**Note:** Receipt creation and hash chain verification are handled by the Go verification service (receiptsverifier), not separate Edge Functions.

### Go Verification Service
- Deploy `backend/verification/receipt_verify.go` microservice
- Verify hash chain integrity via `POST /v1/receipts/verify-run`
- E2E test: Create 100 receipts → verify hash chain unbroken

---

## n8n Workflow Engine Setup (NEW)

**Source:** `platform/integrations/n8n/`

**Doctrine:** n8n is **request-only plumbing** (timers/retries/batch) - NEVER decides. All decisions are made by the LangGraph orchestrator.

### n8n Deployment Tasks

- [x] **PHASE0B-TASK-N8N-001** Deploy n8n Docker Container
  - Follow `platform/integrations/n8n/SETUP_SELF_HOSTED.md`
  - Configure persistent storage for workflows
  - Set up authentication (basic auth minimum)
  - Test: n8n accessible at http://localhost:5678
  - **Verification:** n8n UI loads successfully

- [x] **PHASE0B-TASK-N8N-002** Configure n8n Security Model
  - Review `platform/integrations/n8n/SECURITY_MODEL.md`
  - n8n CANNOT make autonomous decisions
  - All workflows are request-triggered (no autonomous execution)
  - Webhooks require authentication
  - **Verification:** Security checklist complete

- [x] **PHASE0B-TASK-N8N-003** Deploy Workflow Templates
  - Import 5 workflow templates from `platform/integrations/n8n/templates/workflows/`:
    - `finance/FIN_DAILY_SYNC.json` - Daily finance reconciliation
    - `mail/MAIL_DELIVERABILITY_MONITOR.json` - Email deliverability checks
    - `mail/MAIL_DNS_CHECK_SCHEDULE.json` - DNS verification (SPF/DKIM/DMARC)
    - `mail/MAIL_IMAP_SYNC_SCHEDULE.json` - IMAP inbox sync scheduling
    - `mail/MAIL_INCIDENT_ESCALATION.json` - Mail incident escalation
  - Test each workflow in isolation
  - **Verification:** All 5 workflows imported and validated

- [x] **PHASE0B-TASK-N8N-004** Complete Workflow Hardening Checklist
  - Follow `platform/integrations/n8n/templates/WORKFLOW_HARDENING_CHECKLIST.md`
  - Verify no autonomous decision-making in any workflow
  - All external actions require orchestrator approval
  - Error handling configured (no silent failures)
  - **Verification:** Hardening checklist complete

- [ ] **PHASE0B-TASK-N8N-005** Wire n8n to Trust Spine
  - Configure n8n webhook endpoints for orchestrator triggers
  - Set up MCP bridge if using `MCP_CLAUDE_WORKFLOW.md` pattern
  - Test: Orchestrator can trigger n8n workflows
  - **Verification:** End-to-end workflow trigger working

- [x] **PHASE0B-TASK-N8N-006** Install n8n-mcp for Claude Code
  - Clone: `git clone https://github.com/czlonkowski/n8n-mcp`
  - Install: `cd n8n-mcp && npm install`
  - Configure `.claude/mcp.json` with n8n server settings
  - Test: Claude can list n8n workflows via MCP
  - **Verification:** MCP connection to n8n operational

- [x] **PHASE0B-TASK-N8N-007** Review n8n-skills Patterns
  - Reference: https://github.com/czlonkowski/n8n-skills
  - Apply skill pack patterns to 5 workflow templates
  - Document integration patterns in `MCP_CLAUDE_WORKFLOW.md`
  - **Verification:** Skill patterns documented and applied

### n8n Success Criteria

- [x] `0B-SC-N8N-001` n8n accessible at http://localhost:5678
- [x] `0B-SC-N8N-002` All 5 workflow templates imported
- [x] `0B-SC-N8N-003` Workflow hardening checklist complete
- [~] `0B-SC-N8N-004` n8n-to-orchestrator trigger working

---

## Observability Stack Foundation (NEW)

**Source:** `OBSERVABILITY_DEFAULTS.md`, `platform/observability/`

### Observability Deployment Tasks

- [x] **PHASE0B-TASK-OBS-001** Deploy OpenTelemetry Collector
  - Install OTEL collector (Docker or native)
  - Configure exporters (Jaeger/Zipkin for traces, Prometheus for metrics)
  - Test: Traces visible in collector UI
  - **Verification:** OTEL collector receiving data

- [x] **PHASE0B-TASK-OBS-002** Configure Base Alerting Rules
  - Set up alerting for critical failures (orchestrator down, DB unreachable)
  - Configure notification channels (email, Slack webhook)
  - Test: Alert fires on simulated failure
  - **Verification:** Alerting working end-to-end

- [x] **PHASE0B-TASK-OBS-003** Setup Grafana Dashboard Templates
  - Import base dashboard templates from `platform/observability/dashboards/`
  - Configure Postgres datasource
  - Configure Redis datasource (if available)
  - Test: Dashboard shows live metrics
  - **Verification:** Grafana dashboards operational

- [x] **PHASE0B-TASK-OBS-004** Define Baseline Metrics
  - Document SLI definitions (latency, error rate, throughput)
  - Set up metric collection endpoints
  - Test: Metrics scraping working
  - **Verification:** Baseline metrics defined and collecting

### Observability Success Criteria

- [x] `0B-SC-OBS-001` OTEL collector receiving traces
- [x] `0B-SC-OBS-002` Alerting rules configured
- [x] `0B-SC-OBS-003` Grafana dashboards showing data
- [x] `0B-SC-OBS-004` Baseline SLI metrics defined

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Comprehensive deployment documentation exists in the Trust Spine package:**

### Quick Start Resources
- **Getting Started:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/00_START_HERE/README.md` for deployment order and environment variables
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for complete file index and reading order
- **Phase 1 Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_1_TRUST_SPINE/RUNBOOKS/RUNBOOK.md` for step-by-step deployment
- **Definition of Done:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/PHASE_1_TRUST_SPINE/DEFINITION_OF_DONE/DEFINITION_OF_DONE.md` for success gates

### Testing Resources
- **E2E Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/` for SQL tests (tenant_isolation.sql, outbox_concurrency.sql, idempotency_replay.sql, receipt_hash_verify.sql)
- **Test Execution Guide:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/README.md` for test order and expected results
- **Stress Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS/` for k6 load tests

### API & Security Resources
- **API Contracts:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/02_CANONICAL/openapi.unified.yaml` for capability token API + all Trust Spine endpoints
- **Security Documentation:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/security/THREAT_MODEL.md` for threat analysis and evil tests

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` for a complete navigation guide to all Trust Spine documentation.

---

## Dependencies

**Requires (Blocking):**
- Phase 0A: Laptop-Compatible Prep (cloud accounts operational, schemas designed, manifests created)

**Blocks (Downstream):**
- Phase 1: Core Orchestrator + Safety Systems (needs local Postgres, Redis, CUDA)

---

## Tasks

### 1. Trust Spine Infrastructure Deployment

- [ ] **PHASE0B-TASK-001-TS** Deploy Database Migrations
  - Copy Trust Spine canonical migration files (see CANONICAL_PATHS.md for exact paths and counts) from Trust Spine handoff to `supabase/migrations/`
  - Run `supabase db push` to apply all migrations
  - Verify tables exist: `supabase db remote ls`
  - Test RLS policies: Run `tests/substrate/rls_isolation_test.py`
  - **Verification:** All migrations applied, zero cross-tenant leakage

- [ ] **PHASE0B-TASK-002-TS** Deploy Edge Functions
  - Copy 7 Edge Function directories from Trust Spine handoff
  - Deploy each function: `supabase functions deploy <function-name>`
  - Health check all 7 functions (expect 200 OK)
  - Test policy-eval: Send sample intent, verify ALLOW/DENY/REQUIRE_APPROVAL
  - **Verification:** All 8 Edge Functions operational (5 main + 3 A2A addon)

- [ ] **PHASE0B-TASK-003-TS** Deploy Go Verification Service
  - Copy `receipt_verify.go` from Trust Spine handoff to `backend/verification/`
  - Build Docker image: `docker build -t receipt-verify:latest backend/verification/`
  - Deploy container: `docker run -p 8080:8080 receipt-verify:latest`
  - Test verification: `curl http://localhost:8080/v1/receipts/verify-run`
  - **Verification:** Hash chain verification working

- [ ] **PHASE0B-TASK-004-TS** Run Substrate Validation Tests
  - Execute `tests/substrate/rls_isolation_test.py` → MUST pass
  - Execute `tests/substrate/outbox_concurrency_test.py` → MUST pass
  - Execute `tests/substrate/hash_chain_test.py` → MUST pass
  - Create 100 test receipts → verify hash chain via Go service
  - **Verification:** All substrate tests pass, hash chain integrity 100%

---

### 2. Hardware Optimization

- [x] `PHASE0B-TASK-001` **BIOS Configuration**
  - Enable XMP/EXPO for RAM (unlock full DDR5 speed)
  - Enable Virtualization (VT-x/AMD-V) for WSL2
  - Verify boot order (NVMe SSD first)
  - Update BIOS to latest stable version (if needed)
  - Document BIOS version → `docs/hardware/bios-config.md`

- [x] `PHASE0B-TASK-002` **Windows 11 Optimization**
  - Remove bloatware (uninstall unnecessary pre-installed apps)
  - Disable Windows telemetry (privacy settings)
  - Set High Performance power mode (avoid CPU throttling)
  - Disable fast startup (conflicts with dual-boot/WSL2)
  - Install Windows updates (latest stable)

- [x] `PHASE0B-TASK-003` **NVIDIA Drivers**
  - Install NVIDIA Studio Drivers (latest stable, NOT Game Ready)
  - Verify installation: `nvidia-smi` (should show RTX 5060)
  - Configure NVIDIA Control Panel (Manage 3D Settings → Prefer Maximum Performance)
  - Documentation: Driver version → `docs/hardware/gpu-config.md`

---

### 3. WSL2 Installation

- [x] `PHASE0B-TASK-004` **Enable WSL2 Feature**
  - PowerShell (admin): `wsl --install`
  - Reboot system
  - Verify WSL2 version: `wsl --list --verbose`

- [x] `PHASE0B-TASK-005` **Install Ubuntu 22.04 LTS**
  - Install from Microsoft Store: Ubuntu 22.04 LTS
  - Create user account (username: aspire, strong password)
  - Update packages: `sudo apt update && sudo apt upgrade -y`
  - Documentation: WSL2 username → `docs/development-environment.md`

- [x] `PHASE0B-TASK-006` **Configure `.wslconfig`**
  - Create `C:\Users\<YourUser>\.wslconfig`:
    ```
    [wsl2]
    memory=24GB           # Allocate 24GB RAM (leaving 8GB for Windows)
    processors=12         # Use all cores (Ryzen 7 7700: 8 cores / 16 threads)
    swap=8GB              # Swap space
    localhostForwarding=true
    ```
  - Restart WSL: `wsl --shutdown`
  - Verify allocation: `free -h` (inside WSL2)

- [x] `PHASE0B-TASK-007` **Windows Terminal Preview Setup**
  - Install Windows Terminal Preview from Microsoft Store
  - Set Ubuntu 22.04 as default profile
  - Configure color scheme (One Dark Pro recommended)
  - Set font: Cascadia Code NF (Nerd Font for icons)
  - Documentation: Terminal config → `docs/development-environment.md`

---

### 4. Native Data Layer (WSL2)

- [x] `PHASE0B-TASK-008` **Postgres 16 Installation**
  - Add PostgreSQL APT repository:
    ```bash
    sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
    wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
    sudo apt update
    sudo apt install postgresql-16 -y
    ```
  - Start Postgres: `sudo service postgresql start`
  - Create database user: `sudo -u postgres createuser -s aspire`
  - Create database: `createdb aspire_dev`
  - Test connection: `psql -h localhost -U aspire -d aspire_dev`
  - Documentation: Connection string → `.env.local`

- [x] `PHASE0B-TASK-009` **pgvector Extension Installation**
  - Install build dependencies: `sudo apt install postgresql-server-dev-16 git build-essential -y`
  - Clone pgvector: `git clone https://github.com/pgvector/pgvector.git`
  - Build and install:
    ```bash
    cd pgvector
    make
    sudo make install
    ```
  - Enable in database: `CREATE EXTENSION vector;`
  - Verify: `SELECT * FROM pg_extension WHERE extname = 'vector';`

- [x] `PHASE0B-TASK-010` **Redis 7 Installation**
  - Install Redis: `sudo apt install redis-server -y`
  - Configure: Edit `/etc/redis/redis.conf`:
    - Enable AOF: `appendonly yes`
    - Set maxmemory: `maxmemory 2gb`
    - Set eviction policy: `maxmemory-policy allkeys-lru`
  - Start Redis: `sudo service redis-server start`
  - Test connection: `redis-cli ping` (should return PONG)
  - Documentation: Redis config → `docs/development-environment.md`

- [x] `PHASE0B-TASK-011` **Test Database Connections**
  - PostgreSQL: `psql -h localhost -U aspire -d aspire_dev -c "SELECT version();"`
  - Redis: `redis-cli INFO server`
  - Document connection strings → `.env.local`

---

### 5. Runtime & Inference

- [x] `PHASE0B-TASK-012` **NVIDIA CUDA Toolkit (RTX 5060 Local Inference)**
  - Install CUDA Toolkit 12.x:
    ```bash
    wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
    sudo dpkg -i cuda-keyring_1.1-1_all.deb
    sudo apt update
    sudo apt install cuda-toolkit-12-3 -y
    ```
  - Add to PATH (edit `~/.bashrc`):
    ```bash
    export PATH=/usr/local/cuda-12.3/bin:$PATH
    export LD_LIBRARY_PATH=/usr/local/cuda-12.3/lib64:$LD_LIBRARY_PATH
    ```
  - Source: `source ~/.bashrc`
  - Verify: `nvcc --version`
  - Test: `nvidia-smi` (should show CUDA Version)

- [x] `PHASE0B-TASK-013` **Python 3.11 (LangGraph + AI)**
  - Install Python 3.11:
    ```bash
    sudo add-apt-repository ppa:deadsnakes/ppa -y
    sudo apt update
    sudo apt install python3.11 python3.11-venv python3.11-dev -y
    ```
  - Create virtual environment: `python3.11 -m venv ~/venvs/aspire`
  - Activate: `source ~/venvs/aspire/bin/activate`
  - Upgrade pip: `pip install --upgrade pip`
  - Documentation: Venv path → `docs/development-environment.md`

- [x] `PHASE0B-TASK-014` **Node.js 20 (n8n + Frontend)**
  - Install Node.js 20 via nvm:
    ```bash
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
    source ~/.bashrc
    nvm install 20
    nvm use 20
    nvm alias default 20
    ```
  - Verify: `node --version` (should show v20.x.x)
  - Install pnpm: `npm install -g pnpm`
  - Documentation: Node version → `docs/development-environment.md`

- [x] `PHASE0B-TASK-015` **Llama 3 (8B) Model Download + Test Inference**
  - Install Ollama: `curl https://ollama.ai/install.sh | sh`
  - Download Llama 3 8B: `ollama pull llama3:8b`
  - Test inference: `ollama run llama3:8b "What is the capital of France?"`
  - Measure response time (target: <2s)
  - Documentation: Model path + performance → `docs/hardware/gpu-inference.md`

---

### 6. Development Tools

- [x] `PHASE0B-TASK-016` **VS Code + Remote WSL Extension**
  - Install VS Code for Windows (https://code.visualstudio.com/)
  - Install Remote - WSL extension (ms-vscode-remote.remote-wsl)
  - Open WSL project: `code ~/Projects/aspire` (from WSL terminal)
  - Install extensions:
    - Python (ms-python.python)
    - ESLint (dbaeumer.vscode-eslint)
    - Prettier (esbenp.prettier-vscode)
    - GitLens (eamodio.gitlens)
  - Configure settings.json (format on save, linting)

- [x] `PHASE0B-TASK-017` **Git Configuration**
  - Set global config:
    ```bash
    git config --global user.name "Your Name"
    git config --global user.email "your.email@example.com"
    git config --global init.defaultBranch main
    git config --global core.editor "code --wait"
    ```
  - Generate SSH key: `ssh-keygen -t ed25519 -C "your.email@example.com"`
  - Add to GitHub: `cat ~/.ssh/id_ed25519.pub` (copy to GitHub SSH keys)
  - Test: `ssh -T git@github.com`

- [x] `PHASE0B-TASK-018` **n8n Global Install**
  - Install n8n: `npm install n8n -g`
  - Start n8n: `n8n` (runs on http://localhost:5678)
  - Verify: Open browser → http://localhost:5678
  - Create initial account
  - Stop n8n: Ctrl+C
  - Documentation: n8n setup → `docs/integrations/n8n-setup.md`

- [x] `PHASE0B-TASK-019` **Claude Code CLI Setup**
  - Install Claude Code CLI (if not already installed)
  - Configure API key (if needed)
  - Test: `claude --version`
  - Documentation: Claude Code config → `docs/development-environment.md`

---

## Success Criteria

### Trust Spine Success Criteria
- [ ] `0B-SC-TS-001` All Trust Spine canonical migrations applied successfully (see CANONICAL_PATHS.md for exact counts)
- [ ] `0B-SC-TS-002` All 8 Edge Functions deployed and returning 200 OK (5 main: policy-eval, outbox-worker, outbox-executor, inbox, approval-events + 3 A2A: a2a-inbox-claim, a2a-inbox-enqueue, a2a-inbox-transition)
- [ ] `0B-SC-TS-003` Go verification service operational
- [ ] `0B-SC-TS-004` RLS isolation test passes (zero cross-tenant leakage)
- [ ] `0B-SC-TS-005` Hash chain verification test passes (100 receipts chained)
- [ ] `0B-SC-TS-006` OpenAPI contract validated (Postman collection green)

### Implementation Success Criteria

- [x] `0B-SC-001` Postgres running locally (`psql -h localhost` works)
- [x] `0B-SC-002` CUDA active (`nvidia-smi` shows RTX 5060)
- [x] `0B-SC-003` Llama 3 inference works (<2s response time)
- [x] `0B-SC-004` n8n accessible (http://localhost:5678)
- [x] `0B-SC-005` Ready to begin Phase 1 implementation

### Memory System Success Criteria

- [x] `0B-MEM-001` Knowledge Graph: 10+ entities (infrastructure setup patterns documented)

**Source:** `plan/00-success-criteria-index.md`

---

## Related Artifacts

**Created in This Phase:**
- WSL2 Ubuntu 22.04 LTS environment
- Postgres 16 + pgvector (local database)
- Redis 7 (local cache/queue)
- NVIDIA CUDA Toolkit (GPU inference)
- Python 3.11 virtual environment
- Node.js 20 + pnpm
- Llama 3 (8B) model
- n8n workflow engine (http://localhost:5678)

**Used in Later Phases:**
- Phase 1: Postgres (deploy schemas), Redis (queue), CUDA (local inference), Python (LangGraph), Node.js (n8n)
- Phase 2: n8n (workflow automation), Postgres (skill pack state)
- Phase 3: Node.js (Expo app build)

---

## Related Gates

**No gates required for Phase 0B** (infrastructure setup phase only)

**Gates Enabled:**
- Phase 1 can now deploy Gate 6 (Receipts Immutable) → Postgres ready
- Phase 1 can now deploy Gate 7 (RLS Isolation) → Postgres RLS ready

---

## Estimated Duration

**Full-time:** 2-3 DAYS (hardware already available, just configuration needed)

**Timeline:**
- Day 1: WSL2 + CUDA + Postgres + Redis setup (6-8 hours)
- Day 2: Trust Spine deployment (canonical migrations per CANONICAL_PATHS.md + Edge Functions) + .env consolidation (6-8 hours)
- Day 3: Local LLM testing (Llama 3) + verification (4-6 hours)
- **Buffer:** Day 4 if any troubleshooting needed

---

## Cost

**$0/mo** - Local development environment (no cloud costs)

**Hardware Cost (One-Time):**
- Skytech Shadow: ~$900-1,000 (already purchased)

---

## Notes

**Hardware Specs (Skytech Shadow):**
- CPU: AMD Ryzen 7 7700 (8 cores, 16 threads, 5.3 GHz boost)
- GPU: NVIDIA GeForce RTX 5060 (8GB GDDR6)
- RAM: 32GB DDR5 (24GB allocated to WSL2, 8GB for Windows)
- Storage: 1TB NVMe SSD (PCIe 4.0)

**Performance Targets:**
- Llama 3 (8B) inference: <2s response time
- Postgres queries: <50ms (local SSD)
- Redis operations: <1ms (in-memory)
- n8n workflow execution: <500ms startup

**Blocking Notes:**
- Phase 1 cannot start until Postgres + Redis are operational
- CUDA required for local Llama 3 inference (optional, but recommended for cost savings)
- All schemas from Phase 0A will be deployed to Postgres in Phase 1

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Success Criteria:** [00-success-criteria-index.md](../00-success-criteria-index.md)
- **Dependencies:** [00-dependencies.md](../00-dependencies.md)
- **Previous Phase:** [phase-0a-laptop-prep.md](phase-0a-laptop-prep.md)
- **Next Phase:** [phase-1-orchestrator.md](phase-1-orchestrator.md)

---

**Last Updated:** 2026-02-12
**Status:** COMPLETE (Cloud: Trust Spine deployed, Desktop integrated, 27/27 RLS tests pass. Local Dev: Skytech Tower fully configured — WSL2, Postgres 16, Redis 7, CUDA 12.6, Docker, n8n, observability stack, Git/SSH)
