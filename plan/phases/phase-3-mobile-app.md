---
phase: "3"
name: "Desktop + Mobile App Integration"
status: "not_started"
blocking_phase: "2"
blocks_phases: ["4"]
duration_estimate: "2-3 weeks (Desktop on Railway, Expokit 43+ screens COMPLETE, backend wiring + mail UI needed)"
gates_targeted: [1, 2, 3, 4, 5]
priority: "high"
hardware_required: "Skytech Shadow + iOS/Android test devices"
cost: "$30-50/mo (LiveKit, OpenAI, Anam)"
desktop_app_status: "DEPLOYED (Aspire-Desktop on Railway at www.aspireos.app, Trust Spine integrated)"
mobile_app_status: "PRODUCTION-READY (43+ screens complete, Expokit)"
admin_portal_status: "PROTOTYPE (import-my-portal-main, Vite + React, 170+ files)"
governance_status: "15% COMPLETE (UI only, enforcement logic required)"
time_savings: "2-3 weeks"
---

# PHASE 3: Desktop + Mobile App Integration

## Platform Inventory

| App | Location | Stack | Deployment | Status |
|-----|----------|-------|------------|--------|
| **Desktop** | `Aspire-Desktop/` | Express + Expo web | Railway (www.aspireos.app) | DEPLOYED, Trust Spine integrated |
| **Mobile** | Expokit (to extract to `mobile/`) | React Native + Expo | TestFlight / Play Store | 43+ screens, 0% backend wired |
| **Admin Portal** | `import-my-portal-main/` | Vite + React + shadcn | TBD (Vercel/Render) | Prototype, 0% backend wired |

## 🔗 MOBILE WIRING TASKS (v4.2)

**This phase wires the 43+ screen Expokit to backend APIs:**

| API Service | Mobile Wiring Task | Component | Verification |
|-------------|-------------------|-----------|--------------|
| **Supabase** | Auth + data binding | All screens | Login/logout works |
| **LiveKit** | Video SDK integration | Call UI, Authority | Video calls work |
| **Deepgram** | Real-time transcription | Call UI | STT works |
| **ElevenLabs** | Voice responses | Ava responses | TTS plays |

**Mobile API Endpoints to Connect:**
- `GET /v1/receipts` → Receipt viewer
- `POST /v1/intents` → Intent input
- `GET /v1/authority/queue` → Authority Dashboard
- `POST /v1/authority/approve` → Approval action
- `GET /v1/skill-packs/{id}/status` → Skill pack UI

**Files to Create:**
- `mobile/src/services/api.ts` → API service layer
- `mobile/src/services/auth.ts` → Supabase auth
- `mobile/src/services/livekit.ts` → Video integration

**Dependencies to Install:**
- `@supabase/supabase-js` → Database/auth
- `@livekit/react-native` → Video SDK
- `crypto-js` → Hash verification
- `@react-native-async-storage/async-storage` → Local storage

**Gates to Satisfy:**
- UI Surfaces (6 screens wired)
- Call State (LiveKit integration)
- Forced Escalation (RED tier → video)
- Degradation Ladder (Video → Audio → Text)

---

## 🚨 CRITICAL DISCOVERY

**Expokit is 100% production-ready** (UI complete), but **governance is 15% complete** (UI mockups only, no enforcement logic).

**⚠️ CORRECTED AFTER ULTRA-DEEP SCAN**: Phase 3 scope changed from BUILD to **INTEGRATION + GOVERNANCE IMPLEMENTATION**.

**What We Have:**
- ✅ 43+ fully implemented screens (React Native + TypeScript strict)
- ✅ 5-tab navigation (Home/Inbox/Mic/Receipts/More)
- ✅ Complete design system + **55 components** (9 Ava, 14 Session UI, 10 Dashboard, 5 Navigation, 11 Primitives, 8 Other)
- ✅ **12 type files + 18 additional types** (30+ total type definitions for API contracts)
- ✅ Session management (LiveKit SDK ready)
- ✅ Authority queue + receipt system + financial dashboard (UI only)
- ✅ AI staff management (6 roles)
- ✅ Calendar + inbox multi-tab

**What We Need (CRITICAL):**
- ❌ Backend API connection (currently 100% mock data, no Supabase client, no API service layer)
- ❌ Governance enforcement logic (capability tokens, hash-chaining, RLS, fail-closed - all 0% implemented)
- ❌ Critical dependencies (@supabase/supabase-js, crypto-js, AsyncStorage - not installed)
- ❌ src/services/api.ts (doesn't exist, must create)

---

## Objective

Integrate production-ready Expokit mobile app with Phase 1-2 backend APIs and add governance compliance (7 Aspire Laws).

**Previous Objective** (OBSOLETE): Build Expo-based mobile app with 6 UI surfaces, 4-tab navigation, LiveKit video integration
**Revised Objective**: Connect existing mobile app to backend + add governance layer

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 API contracts and mobile integration documentation exists in the Trust Spine package:**

### API Integration Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for API contract quick reference
- **API Contracts:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/02_CANONICAL/openapi.unified.yaml` for complete API specification:
  - Receipts API (POST /v1/receipts, GET /v1/receipts/{id}, POST /v1/receipts/verify-run)
  - Policy Evaluation API (POST /v1/policy/evaluate → ALLOW/DENY/REQUIRE_APPROVAL)
  - Capability Token API (POST /v1/capability-tokens/mint, POST /v1/capability-tokens/refresh)
  - Outbox API (POST /v1/outbox, GET /v1/outbox/{id}/status)
- **Integration Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_6_POST_LAUNCH_OPERATIONS/RUNBOOKS/` for mobile-to-backend wiring guide

### Governance Implementation Resources
- **ADR-0001:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0001-canonical-identity.md` for tenant model (suite → office → user)
- **ADR-0002:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0002-receipts-v1.md` for hash-chained receipt implementation
- **ADR-0005:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0005-capability-tokens.md` for token-based authorization

### Testing Resources
- **Hash Chain Verification:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/receipt_hash_verify.sql` for testing hash chain in mobile UI
- **Policy Evaluation Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/http/smoke.http` for API smoke tests

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` → then `02_CANONICAL/openapi.unified.yaml` for API contracts.

---

## Duration: 2-3 WEEKS (Down from 5-6 weeks)

**Time Savings**: **2-3 weeks** (~50% reduction)
**Revised Total Roadmap**: 39-51 weeks (down from 42-54 weeks)

**⚠️ CORRECTED**: Initial estimate was 1-2 weeks, but ultra-deep scan revealed governance layer must be built from scratch (not just connected). Backend foundation + governance implementation requires additional week.

---

## Success Criteria (REVISED)

- [ ] `3-SC-001` Mobile app extracted to `mobile/` directory and runs via `npx expo start`
- [ ] `3-SC-002` All 43 screens render correctly with mock data (baseline test)
- [ ] `3-SC-003` Backend API connection working (receipts, authority queue, sessions, finance)
- [ ] `3-SC-004` JWT authentication functional (Supabase Auth)
- [ ] `3-SC-005` Capability token management implemented (AsyncStorage + expiry + auto-refresh)
- [ ] `3-SC-006` Receipt hash-chaining verified (hash + previous_hash fields + UI integrity indicator)
- [ ] `3-SC-007` Tenant isolation enforced (X-Suite-ID, X-Office-ID headers on all API calls)
- [ ] `3-SC-008` API audit complete (all external calls route through orchestrator)
- [ ] `3-SC-009` Cold start <2.5s achieved
- [ ] `3-SC-010` Production build deployed to TestFlight (iOS) + Play Store (Android)

**REMOVED** (already complete in Expokit):
- ~~LiveKit video connects successfully~~ (SDK already integrated)
- ~~Authority gates functional~~ (UI already implemented)
- ~~Auto-downshift triggers work~~ (degradation ladder already implemented)

---

## Critical Gates Satisfied

**ALREADY SATISFIED BY EXPOKIT:**
- ✅ Gate 1: UI Surfaces (6 surfaces: Authority, Inbox, Receipts, Ava, Call, Settings/Market)
- ✅ Gate 2: Call State (Cold/Warm/Hot functional via session management)
- ✅ Gate 3: Forced Escalation (risk tier UI ready for video escalation)
- ✅ Gate 4: Degradation Ladder (4-level fallback architecture ready)
- ✅ Gate 5: Authority UI Contract (Authority Dashboard + approval buttons implemented)

**GOVERNANCE STATUS: ~15% COMPLETE (UI ONLY, NO ENFORCEMENT LOGIC)**

**⚠️ CRITICAL FINDINGS FROM ULTRA-DEEP SCAN:**

**What Exists (UI Only):**
- ⚠️ Receipt data model (UI exists, **NO hash/previous_hash fields** - types/receipts.ts missing critical fields)
- ⚠️ Risk tiers (UI with low/medium/high labels, **NO policy enforcement** - just display colors)
- ⚠️ Tenant context provider (exists with hardcoded IDs: SUITE_ID='ZEN-014', OFFICE_ID='O-1011', **NO RLS enforcement**)
- ⚠️ Authority queue UI (approval buttons exist, **NO capability token validation** - all mock data)

**What's Missing (0% Implementation):**
- ❌ **Law #1 (Single Brain)**: API audit required - currently 100% mock data (data/mockData.ts, lib/mockDb.ts)
- ❌ **Law #2 (Receipts)**: Hash-chaining completely missing (Receipt interface has NO hash/previous_hash fields)
- ❌ **Law #3 (Fail Closed)**: No token validation logic, no authorization checks
- ❌ **Law #5 (Capability Tokens)**: String references only ("token_123"), no generation/storage/validation/expiry
- ❌ **Law #6 (Tenant Isolation)**: Hardcoded tenant IDs, no RLS headers (X-Suite-ID, X-Office-ID)
- ❌ **Law #7 (Tools Are Hands)**: No API service layer (src/services/api.ts doesn't exist)

**Critical Dependencies Missing:**
- ❌ @supabase/supabase-js (backend connection)
- ❌ crypto-js (SHA-256 hash verification)
- ❌ @react-native-async-storage/async-storage (token/data persistence)

**CONCLUSION**: All governance features are **UI mockups only**. No enforcement logic exists. Phase 3 must build complete governance layer from scratch.

---

## Expokit Mobile App Inventory

### Technology Stack
- **React Native**: 0.81.4
- **Expo**: 54.0.9 (managed workflow)
- **TypeScript**: 5.9.2 (strict mode)
- **Expo Router**: 6.0.7 (typed routes)
- **React Three Fiber**: 9.5.0 + Three.js 0.182.0 (3D Ava animations)
- **React Native Reanimated**: 4.1.0
- **React Navigation**: 7 (bottom tabs)
- **React Native Skia**: 2.4.14 (GPU acceleration)

### 43+ Screens Organized
1. **5 Tab Root Screens**: Home, Inbox, Mic, Receipts, More
2. **10 Session Screens**: voice, video, conference-lobby, conference, conference-live, calls, plan, authority, transcript, start
3. **4 Inbox Detail Screens**: calls/:id, contacts/:id, mail/:id, office/:id
4. **10+ Settings Screens**: appearance, support, help, notifications, office-identity, policies, security, integrations, team, etc.
5. **6 Modal/Overlay Screens**: office-store, calendar, cash-position, roadmap, etc.
6. **8+ Detail/Dynamic Screens**: receipt detail, session detail, etc.

### 55 Components (Categorized by Function)

**Ava AI Avatar (9 components):**
- AvaOrb, AvaOrbVideo, AvaBlobPure, AvaBlobSkia, AvaBlob3D, AvaDock, AvaMiniPlayer, AvaVoiceStrip, AvaWelcome

**Session UI (14 components):**
- ParticipantTile, ParticipantPanel, ConferenceGrid, BottomSheet, ChatDrawer, InviteSheet, SessionControls, VoiceVisualizer, VideoOverlay, TranscriptView, AuthorityQueueWidget, SessionHeader, RecordingIndicator, NetworkStatus

**Dashboard (10 components):**
- OpsSnapshotTabs, TodayPlanTabs, AuthorityQueueCard, CashPositionCard, PipelineCard, BusinessScoreGauge, RevenueChart, ActivityFeed, QuickActions, MetricCard

**Navigation (5 components):**
- HapticTab, PageHeader, TopHeader, BottomTabBar, NavigationHeader

**UI Primitives (11 components):**
- ThemedText, Button, Badge, Card, Avatar, IconButton, ProgressBar, Skeleton, Toast, Modal, Divider

**Other (8 components):**
- OfficeIdentityBar, MiniCalendar, InteractionModePanel, SearchBar, FilterChips, SectionHeader, EmptyState, ErrorBoundary

### 12 Type Files + 18 Additional Types (30+ Total API Contracts)

**Dedicated Type Files (types/ directory):**
1. **common.ts** - Shared types (Tenant, User, Office, Suite)
2. **receipts.ts** - Receipt model (**⚠️ MISSING hash/previous_hash fields**)
3. **inbox.ts** - InboxItem, Message, Thread
4. **session.ts** - Session, Participant, TranscriptEntry, DocumentPreview
5. **team.ts** - TeamMember, Role, Permission
6. **tenant.ts** - Tenant, TenantContext
7. **calls.ts** - CallRecord, CallParticipant
8. **mail.ts** - EmailThread, EmailMessage
9. **contacts.ts** - Contact, ContactGroup
10. **integrations.ts** - Integration, IntegrationConfig
11. **support.ts** - SupportTicket, SupportMessage
12. **index.ts** - Re-exports all types

**Additional Types in index.ts (18+ types):**
- AuthorityItem, CashPosition, StaffRole, CalendarEvent, ConnectedAccount, ReserveAccount, BusinessScore, EvidenceArtifact, PipelineItem, BusinessInsight, NotificationItem, PolicyRule, SecuritySetting, OpsSnapshot, PlanTask, RoadmapMilestone, OfficeStoreItem, InteractionMode

---

## Integration Work (2-3 Weeks)

**⚠️ CORRECTED**: Expanded from 1-2 weeks to 2-3 weeks due to backend foundation requirements (dependencies, API service layer creation, governance implementation from scratch).

### WEEK 1: Backend Foundation (5 days)

**CRITICAL**: Backend integration is 0% complete. Must install dependencies and create API service layer before wiring.

#### Day 1: Dependency Installation & Project Setup
**Tasks:**
1. Install critical missing dependencies:
   ```bash
   npm install @supabase/supabase-js
   npm install crypto-js
   npm install @react-native-async-storage/async-storage
   ```
2. Create environment configuration files:
   - `.env.example` (template with placeholder values)
   - `.env` (actual credentials, gitignored)
   - Add environment variables: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `ORCHESTRATOR_BASE_URL`
3. Create `src/services/api.ts` (doesn't exist, must build from scratch):
   ```typescript
   import axios from 'axios';
   import { ORCHESTRATOR_BASE_URL } from '@/constants/config';

   const api = axios.create({
     baseURL: ORCHESTRATOR_BASE_URL,
     timeout: 30000,
   });

   export default api;
   ```

**Success Criteria:**
- ✅ All dependencies installed successfully
- ✅ Environment files created
- ✅ src/services/api.ts created with base configuration

---

#### Day 2-3: API Infrastructure Setup
**Tasks:**
1. Update `src/services/api.ts` with Phase 1 orchestrator base URL
2. Configure Supabase Auth client (JWT token management)
3. Add API interceptor for tenant context headers:
   ```typescript
   api.interceptors.request.use(async (config) => {
     const tenant = useContext(TenantContext);
     config.headers['X-Suite-ID'] = tenant.suite_id;
     config.headers['X-Office-ID'] = tenant.office_id;
     config.headers['X-Correlation-ID'] = generateCorrelationId();
     return config;
   });
   ```
4. Add JWT token refresh logic
5. Add correlation ID generator for receipts

**Success Criteria:**
- ✅ API client configured with orchestrator URL
- ✅ JWT tokens automatically attached to requests
- ✅ Tenant headers present on all API calls
- ✅ Token refresh working (auto-refresh before expiry)

---

#### Day 4: Core API Wiring (Part 1)
**Tasks:**
1. **Receipts API** (`src/screens/receipts/`)
   - Connect to `GET /api/receipts?suite_id={}&office_id={}`
   - Test list view with real data
   - Test detail screen with real receipt data

2. **Authority Queue API** (`src/screens/authority/`)
   - Connect to `GET /api/authority-queue`
   - Connect approval actions to `POST /api/authority-queue/:id/approve`
   - Test approval workflow (approve/deny/defer)

3. **Session Management API** (`src/screens/session/`)
   - Connect to `POST /api/sessions/start`
   - Connect to `GET /api/sessions/:id`
   - Wire WebSocket for real-time transcript updates
   - Test voice/video/conference flows

4. **Financial Dashboard API** (`src/screens/cash-position/`)
   - Connect to `GET /api/finance/cash-position`
   - Test cash position card + reserve allocations
   - Test business score display

**Success Criteria:**
- ✅ Receipts screen loads real data from backend
- ✅ Authority queue displays pending approvals
- ✅ Approve/deny actions generate receipts
- ✅ Session creation works end-to-end
- ✅ Cash position displays real financial data

---

#### Day 5: Testing & Error Handling
**Tasks:**
1. Test all API connections with real backend
2. Validate error handling (network errors, 401/403/500 responses)
3. Test offline mode (graceful degradation)
4. Test loading states (skeleton screens, spinners)
5. Verify data persistence (AsyncStorage caching)

**Success Criteria:**
- ✅ All API endpoints return expected data shapes
- ✅ Error states display user-friendly messages
- ✅ Offline mode shows cached data
- ✅ No crashes on network failures

---

### WEEK 2: Governance Compliance (5 days)

#### Day 1-2: Capability Token Management
**Tasks:**
1. **Token Storage** (`src/services/capability-tokens.ts`)
   ```typescript
   interface CapabilityToken {
     token_id: string;
     suite_id: string;
     office_id: string;
     tool: string;
     scopes: string[];
     issued_at: string;
     expires_at: string;
     signature: string;
   }

   const storeToken = async (token: CapabilityToken) => {
     await AsyncStorage.setItem(`token_${token.tool}`, JSON.stringify(token));
   };

   const getToken = async (tool: string): Promise<CapabilityToken | null> => {
     const tokenStr = await AsyncStorage.getItem(`token_${tool}`);
     if (!tokenStr) return null;

     const token: CapabilityToken = JSON.parse(tokenStr);

     // Check expiry (<60s)
     if (new Date(token.expires_at) < new Date()) {
       await AsyncStorage.removeItem(`token_${tool}`);
       return null;
     }

     return token;
   };

   const refreshToken = async (tool: string): Promise<CapabilityToken> => {
     const response = await api.post('/api/tokens/refresh', { tool });
     const newToken = response.data;
     await storeToken(newToken);
     return newToken;
   };
   ```

2. **Token Validation Before API Calls**
   - Add token checking before authority queue approvals
   - Add auto-refresh mechanism (refresh if expires in <10s)
   - Add error handling for missing/expired tokens

**Success Criteria:**
- ✅ Capability tokens stored in AsyncStorage
- ✅ Token expiry checking works (<60s enforcement)
- ✅ Auto-refresh prevents token expiry errors
- ✅ API calls blocked if token missing/expired

**Aspire Law Compliance**: Law #3 (Fail Closed), Law #5 (Capability Tokens)

---

#### Day 3: Receipt Hash-Chaining + Trust Spine Integration
**Tasks:**
1. **Update Receipt Type** (`src/types/receipt.ts`)
   ```typescript
   interface Receipt {
     // ... existing fields
     hash: string;              // SHA-256 hash of receipt content
     previous_hash?: string;    // Hash of previous receipt (chain)
   }
   ```

2. **Hash Verification Logic** (`src/services/receipts.ts`)
   ```typescript
   import CryptoJS from 'crypto-js';

   const verifyReceiptHash = (receipt: Receipt): boolean => {
     const { hash, previous_hash, ...content } = receipt;

     const calculatedHash = CryptoJS.SHA256(
       JSON.stringify(content, Object.keys(content).sort())
     ).toString();

     return calculatedHash === hash;
   };

   const verifyHashChain = (receipts: Receipt[]): boolean => {
     for (let i = 1; i < receipts.length; i++) {
       if (receipts[i].previous_hash !== receipts[i - 1].hash) {
         return false; // Chain broken
       }
     }
     return true;
   };
   ```

3. **UI Integrity Indicator** (`src/screens/receipts/ReceiptDetailScreen.tsx`)
   - Add hash verification badge (✅ Valid / ⚠️ Invalid)
   - Display hash chain status
   - Show hash value (truncated, with copy button)

4. **Wire to Trust Spine Receipts API**
   - Update Receipt API calls to use Trust Spine receipts-api Edge Function
   - Integrate with Go verification service (`POST /v1/receipts/verify-run`)
   - Display UI integrity indicator based on hash chain verification status
   - Test hash chain verification with 100 receipts

**Success Criteria:**
- ✅ Receipt type includes hash + previous_hash
- ✅ Hash verification runs on receipt detail view
- ✅ UI displays integrity status (green ✅ if valid)
- ✅ Hash chain verified across multiple receipts
- ✅ Trust Spine receipts-api integration working
- ✅ Go verification service returning VALID status

**Aspire Law Compliance**: Law #2 (No Action Without Receipt) - hash-chain enforcement

**Trust Spine Integration**: Receipts flow through Trust Spine receipts-api Edge Function deployed in Phase 0B

---

#### Day 4: RLS Enforcement & API Audit
**Tasks:**
1. **Verify Tenant Headers** (`src/services/api.ts`)
   - Audit all API calls for X-Suite-ID and X-Office-ID headers
   - Test tenant switching (switch office → verify data changes)
   - Test zero cross-tenant leakage (office A cannot see office B's data)

2. **Orchestrator Routing Audit**
   - Search codebase for direct external API calls (Stripe, Gmail, etc.)
   - Replace with orchestrator proxy pattern:
     ```typescript
     // BEFORE (direct call - VIOLATES Law #1)
     const invoice = await stripeAPI.createInvoice({ ... });

     // AFTER (orchestrator proxy - COMPLIANT)
     const response = await api.post('/api/orchestrator/execute', {
       action: 'stripe.invoice.create',
       parameters: { amount: 100, customer: 'cust_123' },
       capability_token: token.signature
     });
     ```
   - Document all external API calls and their orchestrator equivalents

**Success Criteria:**
- ✅ X-Suite-ID and X-Office-ID headers on ALL API requests
- ✅ Tenant switching verified (data filters correctly)
- ✅ Zero cross-tenant leakage (RLS enforced at API level)
- ✅ No direct external API calls (all route through orchestrator)

**Aspire Law Compliance**: Law #1 (Single Brain), Law #6 (Tenant Isolation), Law #7 (Tools Are Hands)

---

#### Day 5: Final Validation & Production Build
**Tasks:**
1. **7 Aspire Laws Compliance Checklist**
   - [ ] Law #1 (Single Brain): All decisions through orchestrator ✅
   - [ ] Law #2 (Receipts): Hash-chaining implemented ✅
   - [ ] Law #3 (Fail Closed): Token validation blocks execution ✅
   - [ ] Law #4 (Risk Tiers): Risk levels mapped (low→GREEN, medium→YELLOW, high→RED) ✅
   - [ ] Law #5 (Capability Tokens): <60s expiry enforced ✅
   - [ ] Law #6 (Tenant Isolation): RLS headers on all calls ✅
   - [ ] Law #7 (Tools Are Hands): No direct external calls ✅

2. **Production Build**
   - Run `eas build --platform all --profile production`
   - Test production build on iOS device
   - Test production build on Android device
   - Verify cold start <2.5s target

3. **App Store Deployment**
   - Deploy to TestFlight (iOS beta testing)
   - Deploy to Google Play Internal Track (Android beta testing)
   - Submit compliance documentation (privacy policy, permissions rationale)

**Success Criteria:**
- ✅ All 7 Aspire Laws compliant
- ✅ Production build succeeds (no errors)
- ✅ Cold start <2.5s achieved
- ✅ TestFlight build available for iOS
- ✅ Play Store Internal Track available for Android

---

## Visual Assets Integration

### Ava Background Video
**File**: `C:\Users\tonio\Projects\myapp\14898794_1920_1080_30fps 2.mp4`
**Purpose**: Animated background for Ava voice/video sessions
**Integration**:
1. Copy to `mobile/assets/videos/ava-background.mp4`
2. Compress to 720p for mobile bundle size (target <5MB)
3. Use in voice/video session screens:
   ```typescript
   import { Video } from 'expo-av';

   const AvaBackground = () => (
     <Video
       source={require('../../assets/videos/ava-background.mp4')}
       style={StyleSheet.absoluteFill}
       resizeMode="cover"
       shouldPlay
       isLooping
       isMuted
     />
   );
   ```

**Effort**: 0.5 days

---

### Visual Screenshots (19 files)
**Location**: `C:\Users\tonio\Projects\myapp\Aspire visuals`
**Purpose**: Design reference, documentation, integration testing validation
**Organization**:
```
mobile/docs/design/screenshots/
├── 01-receipts-tab.png
├── 02-more-settings.png
├── 03-office-store.png
├── 04-inbox-multi-tab.png
├── 05-home-dashboard.png
├── 06-todays-plan.png
├── 07-business-roadmap.png
├── 08-authority-queue.png
├── 09-roadmap-detail.png
├── 10-cash-position.png
├── 11-session-plan.png
├── 12-calendar-mini.png
├── 13-calendar-full.png
├── 14-voice-session.png
├── 15-conference-live.png
├── 16-conference-lobby.png
├── 17-conference-active.png
├── 18-dialer.png
└── 19-recent-calls.png
```

**Usage**: Reference during integration testing to validate screens match designs

**Effort**: 0.5 days

---

## Backend API Contracts (Phase 1-2 MUST MATCH)

### Session API
```typescript
// Mobile expects this shape from GET /api/sessions/:id
interface Session {
  id: string;
  mode: 'voice' | 'video' | 'conference';
  status: 'scheduled' | 'live' | 'ended';
  participants: Participant[];
  transcript: TranscriptEntry[];
  authorityQueue: AuthorityItem[];
  staff: StaffRole[];
  chat: ChatMessage[];
  context: DocumentPreview[];
  startedAt: Date;
  endedAt?: Date;
}
```

**Endpoints:**
- `POST /api/sessions/start` - Create new session
- `GET /api/sessions/:id` - Fetch session details
- `POST /api/sessions/:id/participants` - Add participant
- `DELETE /api/sessions/:id/participants/:participantId` - Remove participant
- `GET /api/sessions/:id/transcript` - Fetch real-time transcript
- `POST /api/sessions/:id/end` - End session
- **WebSocket**: `/api/sessions/:id/ws` - Real-time transcript/authority queue updates

---

### Authority Queue API
```typescript
// Mobile expects this shape from GET /api/authority-queue
interface AuthorityItem {
  id: string;
  type: 'session' | 'invoice' | 'contract' | 'call' | 'email' | 'approval';
  title: string;
  description: string;
  riskLevel: 'low' | 'medium' | 'high';
  status: 'live' | 'pending' | 'blocked' | 'failed' | 'logged';
  actor: string;
  office: string;
  dueDate?: Date;
  context?: DocumentPreview[];
  actions?: string[];
  receiptId?: string;
}
```

**Endpoints:**
- `GET /api/authority-queue?suite_id={}&office_id={}` - Fetch pending approvals
- `POST /api/authority-queue/:id/approve` - Approve action (generates receipt)
- `POST /api/authority-queue/:id/deny` - Deny action (generates denial receipt)
- `POST /api/authority-queue/:id/defer` - Defer decision

---

### Receipt API
```typescript
// Mobile expects this shape from GET /api/receipts
interface Receipt {
  id: string;
  type: 'allow' | 'deny' | 'fail' | 'success';
  title: string;
  description: string;
  status: 'success' | 'blocked' | 'failed' | 'pending';
  capability: string;
  actor: string;
  office: string;
  timestamp: Date;
  hash: string;              // MUST IMPLEMENT
  previous_hash?: string;    // MUST IMPLEMENT
  evidence?: EvidenceArtifact[];
  tags?: string[];
  approved?: boolean;
  approvedBy?: string;
  approvedAt?: Date;
}
```

**Endpoints:**
- `GET /api/receipts?suite_id={}&office_id={}&limit=50` - Fetch receipts
- `GET /api/receipts/:id` - Fetch single receipt with full evidence
- `POST /api/receipts` - Generate new receipt (orchestrator only)
- `GET /api/receipts/:id/verify-hash` - Verify receipt hash integrity

---

### Financial API
```typescript
// Mobile expects this shape from GET /api/finance/cash-position
interface CashPosition {
  totalAvailable: number;
  expectedIn7Days: number;
  inflows: number;
  outflows: number;
  lastSynced: Date;
  accounts: ConnectedAccount[];
  reserves: ReserveAccount[];
}
```

**Endpoints:**
- `GET /api/finance/cash-position` - Fetch current cash position
- `GET /api/finance/accounts` - Fetch connected accounts
- `GET /api/finance/reserves` - Fetch reserve allocations
- `POST /api/finance/transfer` - Transfer funds between accounts
- `GET /api/business/score` - Calculate business health score

---

### Staff API
```typescript
// Mobile expects this shape from GET /api/staff/available
interface StaffRole {
  id: string;
  name: string;
  role: string;
  internalPackId: string;
  whatTheyDo: string;
  approvalLevel: 'always' | 'conditional' | 'auto_low_risk';
  badges?: string[];
  avatar?: string;
  outputCount?: number;
  taskState?: 'idle' | 'working' | 'waiting_approval';
}
```

**Endpoints:**
- `GET /api/staff/available` - List available AI staff roles
- `POST /api/staff/enable` - Enable staff member for office
- `DELETE /api/staff/:id` - Disable staff member
- `POST /api/staff/:id/command` - Execute staff command (with approval)

---

---

## Mail UI Integration (NEW — Desktop + Mobile)

**Dependencies:** Phase 0C (Domain Rail), Phase 2 (Eli Inbox + mail state machines)

These tasks add mail/domain UI to both the Desktop app (Railway) and Mobile app (Expokit).

### Desktop Mail UI (Aspire-Desktop on Railway)

- [ ] **PHASE3-MAIL-DESKTOP-001** Inbox Mail Thread List
  - Wire Desktop inbox to Aspire mail API (`GET /v1/mail/threads`)
  - NOT direct provider calls — all through orchestrator (Law #1)
  - Display mail threads with sender, subject, timestamp, read/unread status
  - **Verification:** Desktop inbox shows real mail threads from Aspire API

- [ ] **PHASE3-MAIL-DESKTOP-002** Mail Detail Screen
  - Wire `mail/:id` route to Aspire mail API (`GET /v1/mail/:id`)
  - Display full email content, attachments, thread history
  - Reply/forward actions route through orchestrator (YELLOW — user confirm before send)
  - **Verification:** Mail detail loads from API, reply generates receipt

- [ ] **PHASE3-MAIL-DESKTOP-003** Business Email Setup Wizard
  - "Business Email Setup" in Desktop settings
  - Two paths: BYOD (bring existing domain) or Buy Domain (via ResellerClub)
  - Shows 13-state progress indicator (from Phase 2 state machines)
  - DNS verification status with real-time polling
  - **Verification:** BYOD and Buy Domain onboarding flows work end-to-end from Desktop

### Mobile Mail UI (Expokit)

- [ ] **PHASE3-MAIL-MOBILE-001** Inbox Mail Thread List (Mobile)
  - Same API as Desktop: `GET /v1/mail/threads`
  - Mobile-optimized thread list in Inbox tab
  - Pull-to-refresh, infinite scroll
  - **Verification:** Mobile inbox shows real mail threads

- [ ] **PHASE3-MAIL-MOBILE-002** Mail Detail Screen (Mobile)
  - `mail/:id` route in Expokit
  - Mobile-optimized mail reader
  - Reply/forward with approval flow (YELLOW)
  - **Verification:** Mail detail screen loads from API

- [ ] **PHASE3-MAIL-MOBILE-003** Business Email Setup Wizard (Mobile)
  - Same flows as Desktop but mobile-optimized
  - BYOD + Buy Domain onboarding
  - Push notifications for DNS verification status changes
  - **Verification:** Onboarding flows work on mobile

### Mail UI Success Criteria
- [ ] `3-SC-MAIL-001` Desktop inbox loads real mail from Aspire API
- [ ] `3-SC-MAIL-002` Mobile inbox loads real mail from Aspire API
- [ ] `3-SC-MAIL-003` Email setup wizard (BYOD + Buy Domain) works on both Desktop and Mobile
- [ ] `3-SC-MAIL-004` All mail actions route through orchestrator (zero direct provider calls)
- [ ] `3-SC-MAIL-005` Reply/forward generates receipts (YELLOW tier approval enforced)

---

## 🚨 ADMIN PORTAL PROTOTYPE STATUS (CRITICAL)

**Source:** `import-my-portal-main/` (170+ files)

### ⚠️ ADMIN PORTAL IS A PROTOTYPE - NOT PRODUCTION-READY

The Admin Portal React application is a **UI prototype only** that requires significant work before production use:

**What Exists (Prototype Only):**
- ✅ Complete UI shell (Vite + TypeScript + React + shadcn-ui + Tailwind)
- ✅ Staff Catalog UI (10 staff definitions)
- ✅ Tools Catalog UI (10 tool definitions)
- ✅ Providers Catalog UI (8 provider definitions)
- ✅ TypeScript contracts (`ecosystem.ts`, `control-plane.ts`)
- ✅ Page shells: Approvals, Receipts, Rollouts, Incidents, Control Plane

**What's Missing (REQUIRED FOR PRODUCTION):**
- ❌ **Backend API Connection** - All data is static JSON snapshots, no API calls
- ❌ **Authentication** - No Supabase Auth integration, no JWT handling
- ❌ **Real-time Data** - No WebSocket connections, no live updates
- ❌ **State Persistence** - No database writes, no mutations working
- ❌ **Error Handling** - No API error states, no loading states
- ❌ **Production Build** - Not deployed, no CI/CD pipeline
- ❌ **Testing** - No unit tests, no E2E tests

### Admin Portal Integration Tasks (Phase 3)

- [ ] **PHASE3-TASK-AP-001** Setup Backend Connection
  - Install `@supabase/supabase-js`
  - Create `src/services/apiClient.ts` with proper auth
  - Configure environment variables (SUPABASE_URL, SUPABASE_ANON_KEY)
  - Wire to Trust Spine backend APIs
  - **Effort:** 2-3 days

- [ ] **PHASE3-TASK-AP-002** Wire Approvals Page
  - Connect to Authority Queue API (`GET /api/authority-queue`)
  - Implement approve/deny actions (`POST /api/authority-queue/:id/approve`)
  - Add real-time updates via WebSocket
  - **Effort:** 2 days

- [ ] **PHASE3-TASK-AP-003** Wire Receipts Page
  - Connect to Receipts API (`GET /api/receipts`)
  - Add domain filtering (deploy, slo, rbac, etc.)
  - Implement detail drawer with full payload
  - **Effort:** 1-2 days

- [ ] **PHASE3-TASK-AP-004** Wire Control Plane Pages
  - Connect Registry page to Control Plane registry API
  - Connect Rollouts page to canary deployment API
  - Implement percentage controls and rollback
  - **Effort:** 2-3 days

- [ ] **PHASE3-TASK-AP-005** Wire Incidents Page
  - Connect to Robot test results
  - Link to LLMOpsDesk for AI analysis
  - Add recommended actions
  - **Effort:** 1-2 days

- [ ] **PHASE3-TASK-AP-006** Production Deployment
  - Configure build pipeline
  - Deploy to hosting (Vercel/Netlify/Render)
  - Setup CI/CD for automatic deployments
  - **Effort:** 1 day

### Admin Portal Success Criteria
- [ ] `3-SC-AP-001` All pages load with real backend data (not JSON snapshots)
- [ ] `3-SC-AP-002` Authentication working with Supabase Auth
- [ ] `3-SC-AP-003` Approval actions generate receipts in database
- [ ] `3-SC-AP-004` Rollout controls actually affect deployments
- [ ] `3-SC-AP-005` Production build deployed and accessible

### Admin Portal → Phase 6 Continuation

The Admin Portal will receive additional features in Phase 6:
- Meeting of Minds (Council) UI
- LLM Observability dashboards
- Multi-operator management
- Evolution Doctrine controls

---

## Channel Faces Integration (NEW)

**Source:** `platform/CLAUDE_HANDOFF/05_CHANNEL_FACES.md`

Channel Faces provides multi-channel agent wiring for unified communication across voice, text, video, and email.

### Channel Face Types

| Channel | Interaction Mode | Description |
|---------|------------------|-------------|
| **Voice** | Warm | Primary phone/voice interactions |
| **Video** | Hot | High-stakes authority moments (RED tier) |
| **Text/Chat** | Cold | Async messaging, low-bandwidth fallback |
| **Email** | Cold | Formal external communications |

### Integration Tasks

- [ ] **PHASE3-TASK-CF-001** Channel Router Implementation
  - Route intents to appropriate channel face
  - Pattern: Intent → Channel Router → Face Handler → Response
  - Test: "Schedule meeting" → Voice channel, "Sign contract" → Video channel

- [ ] **PHASE3-TASK-CF-002** Degradation Ladder
  - Implement automatic downshift on technical failures
  - Ladder: Video → Audio → Async Voice → Text
  - Test: Video failure → graceful downshift to audio

- [ ] **PHASE3-TASK-CF-003** Channel-Specific Routing Rules
  - RED tier actions → Force Video (Hot) channel
  - YELLOW tier actions → Voice (Warm) channel
  - GREEN tier actions → Any channel (user preference)
  - Test: RED payment → video escalation required

- [ ] **PHASE3-TASK-CF-004** Cross-Channel Context Persistence
  - Maintain conversation context across channel switches
  - Store channel transitions in receipts
  - Test: Start on voice → switch to video → context preserved

### Success Criteria (Channel Faces)
- [ ] `3-SC-CF-001` All 4 channel types routing correctly
- [ ] `3-SC-CF-002` Degradation ladder working (auto-downshift on failure)
- [ ] `3-SC-CF-003` RED tier forces Hot (video) channel
- [ ] `3-SC-CF-004` Cross-channel context persistence verified

---

## UI Handoff Specifications (NEW - P2 Gap Fix)

**Source:** `ui_handoff/`

UI Handoff documents provide detailed specifications for UI components that need custom implementation beyond the base Expokit screens.

### Lovable UI Specs (Admin/Internal)

**Location:** `ui_handoff/lovable/`

| File | Purpose | Target |
|------|---------|--------|
| `ACCOUNTANT_ROLE_ADMIN.md` | Read-only accountant interface for auditors | Admin Portal |
| `FINANCE_OFFICE_ADMIN_CONTROLS.md` | Finance office admin panel for money ops | Admin Portal |
| `PROVIDER_CONTROL_CENTER_ADMIN.md` | Provider health monitoring dashboard | Admin Portal |

- [ ] **PHASE3-TASK-UIH-001** Implement Accountant Role Admin View
  - Read-only access to financial data (Teressa's view)
  - Export capabilities for auditors
  - No modification permissions (GREEN tier only)
  - Source: `ui_handoff/lovable/ACCOUNTANT_ROLE_ADMIN.md`

- [ ] **PHASE3-TASK-UIH-002** Implement Finance Office Admin Controls
  - Money movement admin interface (Finn oversight)
  - Transfer monitoring and emergency stop
  - Source: `ui_handoff/lovable/FINANCE_OFFICE_ADMIN_CONTROLS.md`

- [ ] **PHASE3-TASK-UIH-003** Implement Provider Control Center
  - Provider health status dashboard
  - Connection status monitoring (Gusto, QBO, Moov, Plaid)
  - Source: `ui_handoff/lovable/PROVIDER_CONTROL_CENTER_ADMIN.md`

### Replit UI Specs (Customer-Facing)

**Location:** `ui_handoff/replit/`

| File | Purpose | Target |
|------|---------|--------|
| `AUTHORITY_QUEUE_RESEARCH_PACKET.md` | Research packet display in Authority Queue | Mobile App |
| `AUTHORITY_QUEUE_VENDOR_OUTREACH.md` | Vendor outreach approval UI | Mobile App |
| `BUSINESS_GOOGLE_WAR_ROOM_PANEL.md` | Business Google war room feature | Mobile App |
| `FINANCE_OFFICE_PLAIN_ENGLISH_COPY.md` | Plain English copy for finance UI | Mobile App |
| `FINANCE_OFFICE_UI_PROMPT.md` | Finance office UI design prompts | Mobile App |
| `RESEARCH_OFFICE_UI_PROMPT.md` | Research office UI design prompts | Mobile App |

- [ ] **PHASE3-TASK-UIH-004** Implement Research Packet View
  - Display Adam's research packets in Authority Queue
  - Evidence citations and vendor comparisons
  - Source: `ui_handoff/replit/AUTHORITY_QUEUE_RESEARCH_PACKET.md`

- [ ] **PHASE3-TASK-UIH-005** Implement Vendor Outreach Approval
  - Vendor outreach approval workflow UI
  - RFQ tracking and status
  - Source: `ui_handoff/replit/AUTHORITY_QUEUE_VENDOR_OUTREACH.md`

- [ ] **PHASE3-TASK-UIH-006** Implement Business Google War Room
  - Real-time research war room panel
  - Multi-source aggregation display
  - Source: `ui_handoff/replit/BUSINESS_GOOGLE_WAR_ROOM_PANEL.md`

### Success Criteria (UI Handoff)
- [ ] `3-SC-UIH-001` Accountant role view operational (read-only)
- [ ] `3-SC-UIH-002` Finance admin controls functional
- [ ] `3-SC-UIH-003` Provider control center showing health status
- [ ] `3-SC-UIH-004` Research packets displaying in Authority Queue
- [ ] `3-SC-UIH-005` Vendor outreach approvals working

---

## Trust Spine Integration Points (Phase 3)

**Flow:** Mobile → Phase 1B Backend APIs → Trust Spine Edge Functions

**Integration Paths:**
- **Receipts:** Mobile Receipt UI → Backend API → Trust Spine receipts-api → Go verification
- **Policy:** Mobile Authority Queue → Backend API → Trust Spine policy-eval → Risk tier display
- **Approvals:** Mobile Approval buttons → Backend API → Trust Spine approvals-workflow → Receipt logged
- **Hash Verification:** Mobile Receipt detail → Backend API → Go service → Integrity badge
- **Channel Routing:** Channel Faces router → Trust Spine policy-eval → Risk tier → Channel selection

**Validation Tasks:**
- [ ] **PHASE3-TASK-TS-001** Validate Trust Spine receipts-api integration
  - Receipts flow through Trust Spine receipts-api Edge Function
  - Hash chain verification via Go service (`POST /v1/receipts/verify-run`)
  - UI displays integrity status (✅ VALID / ⚠️ INVALID)

- [ ] **PHASE3-TASK-TS-002** Validate Trust Spine policy-eval integration
  - Authority Queue calls Trust Spine policy-eval Edge Function
  - Risk tiers mapped correctly (ALLOW=Green, REQUIRE_APPROVAL=Yellow, DENY=Red)
  - Policy decisions displayed in mobile UI

- [ ] **PHASE3-TASK-TS-003** Validate Trust Spine approvals-workflow integration
  - Approval buttons route through Trust Spine approvals-workflow Edge Function
  - Receipts logged for all approval actions (approve, deny, defer)
  - Correlation IDs flow through entire stack

---

## Task Breakdown (10-15 Days Total)

**⚠️ CORRECTED**: Updated from 10-13 days to 10-15 days due to backend foundation requirements.

### Immediate Actions (Day 0)
- [ ] Extract Expokit ZIP to `mobile/` directory
- [ ] Run `npm install` in `mobile/` directory (will fail - missing dependencies)
- [ ] Test `npx expo start` (verify 43 screens render with mock data)
- [ ] Organize visual assets into `mobile/docs/design/screenshots/`
- [ ] Copy Ava background video to `mobile/assets/videos/`

**Estimated Time**: 1 day

---

### Week 1: Backend Foundation (Days 1-5)
- [ ] **Day 1**: Dependency installation (@supabase, crypto-js, AsyncStorage) + create src/services/api.ts
- [ ] **Day 2-3**: API infrastructure setup (Supabase client, JWT auth, tenant headers, correlation IDs)
- [ ] **Day 4**: Core API wiring Part 1 (receipts, authority queue)
- [ ] **Day 5**: Core API wiring Part 2 (sessions, finance) + testing & error handling

**⚠️ CRITICAL**: Backend integration is 0% complete. Must build API service layer from scratch before wiring APIs.

**Estimated Time**: 5 days

---

### Week 2: Governance Compliance (Days 6-10)
- [ ] **Day 6-7**: Capability token management (create src/services/capability-tokens.ts, storage, expiry, auto-refresh)
- [ ] **Day 8**: Receipt hash-chaining (add hash/previous_hash to Receipt type, verification logic, UI integrity indicator)
- [ ] **Day 9**: RLS enforcement + API audit (tenant headers on all calls, orchestrator routing audit, replace direct external calls)
- [ ] **Day 10**: Final validation + production build (7 Laws checklist, TestFlight/Play Store deployment)

**Estimated Time**: 5 days

---

### Optional: Polish & Optimization (Days 11-15)
- [ ] Ava background video integration + compression
- [ ] Visual assets organization + documentation
- [ ] Performance optimization (cold start tuning, bundle size reduction)
- [ ] Additional testing (edge cases, error scenarios, stress testing)
- [ ] Production soak testing (24h stability test)

**Estimated Time**: 0-5 days (optional, allows for 2-3 week range)

---

## Risks & Mitigations

### Risk 1: Backend API Contracts Mismatch
**Likelihood**: Medium (30%)
**Impact**: High (blocks integration)
**Mitigation**:
- Phase 1 uses mobile TypeScript types as source of truth
- OpenAPI spec generated from mobile types (validation)
- API contract tests run before Phase 3 starts

---

### Risk 2: Capability Token Implementation Complexity
**Likelihood**: Low (20%)
**Impact**: Medium (delays governance compliance)
**Mitigation**:
- Token management pattern already proven (AsyncStorage + JWT pattern)
- Backend token minting API straightforward (Phase 1 deliverable)
- <60s expiry simplifies refresh logic (no complex TTL management)

---

### Risk 3: Hash-Chaining Performance Impact
**Likelihood**: Low (15%)
**Impact**: Low (minor UX delay)
**Mitigation**:
- Hash calculation offloaded to Web Worker (non-blocking)
- Only verify hash on receipt detail screen (not list view)
- Cache verification results in memory (avoid recalculation)

---

### Risk 4: API Audit Uncovers Many Direct External Calls
**Likelihood**: Medium (40%)
**Impact**: Medium (extends Week 2 timeline)
**Mitigation**:
- Grep search for `fetch(`, `axios(`, `stripeAPI`, `gmailAPI` patterns
- Orchestrator proxy pattern is straightforward replacement
- Can defer non-critical external calls to Phase 4 if needed

---

## Phase 3 Complete When...

- ✅ Mobile app runs with real backend data (not mock data)
- ✅ All 7 Aspire Laws enforced (capability tokens, receipts, RLS, risk tiers)
- ✅ Receipt hash-chaining implemented + verified
- ✅ Tenant isolation tested (zero cross-office data leakage)
- ✅ Session management working (voice/video/conference with real LiveKit)
- ✅ Authority queue functional (approve/deny generates receipts)
- ✅ Cold start <2.5s achieved
- ✅ Production build deployed to TestFlight (iOS) + Play Store (Android)

---

## Strategic Impact

**⚠️ CORRECTED AFTER ULTRA-DEEP SCAN**

### Time Savings
- **Original Phase 3**: 5-6 weeks (build 6 screens + navigation + features)
- **Revised Phase 3**: **2-3 weeks** (integration + governance implementation)
- **Savings**: **2-3 weeks** (~50% reduction)

**Note**: Initial estimate was 1-2 weeks (saving 3-4 weeks), but ultra-deep scan revealed governance layer must be built from scratch, requiring additional week.

### Risk Reduction
- **UI/UX de-risked 100%** (all 43 screens exist, production-ready)
- **Design system complete** (no styling work needed)
- **Component library ready** (55 components across 6 categories)
- **TypeScript types define contracts** (12 type files + 18 additional types = 30+ total)

### Backend Focus
- **Phase 1-2 API clarity**: Mobile TypeScript types (30+ total) define exact contracts
- **No ambiguity**: Backend knows exactly what shapes to return (Session, Receipt, AuthorityItem, etc.)
- **Integration testing**: Mobile app becomes API contract validation tool

### Governance Reality
- **Governance status**: ~15% complete (UI mockups only, NO enforcement logic)
- **Critical missing**: Hash-chaining, capability tokens, RLS enforcement, API service layer, backend dependencies
- **Phase 3 scope**: Integration + governance implementation from scratch (not just connection)

### Roadmap Acceleration
- **Total timeline**: **39-51 weeks** (down from 42-54 weeks)
- **Time saved**: **2-3 weeks** (~6% reduction in total roadmap)
- **MVP timeline**: Phases 1-3 complete in ~16-18 weeks (not 19-22 weeks)
- **Earlier beta launch**: Phase 5 starts 2-3 weeks earlier

---

**END OF PHASE 3 PLAN** - Last Updated: 2026-02-08
