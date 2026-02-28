# Technology Stack (LOCKED)

**Extracted from:** `Aspire-Production-Roadmap.md` (lines 1492-1534)
**Status:** Locked - changes require founder approval

---

## Core Infrastructure
- **Orchestrator:** LangGraph (Single Brain)
- **Cognition:** Ava (GPT-5 via OpenAI API)
- **Tool Plane:** MCP Protocol (Model Context Protocol)
- **Memory:** pgvector (PostgreSQL vector extension)
- **Utilities:** n8n (background automation, webhooks, scheduled jobs)
- **Workers:** Agents SDK (bounded, stateless execution)

## Frontend
- **Mobile:** React Native + Expo (iOS/Android)
- **Video:** LiveKit (WebRTC) + Anam (avatar rendering)
- **Desktop Background (Optional):** Unity Engine (desktop/tablet only)

## Data Layer
- **Production Database:** Supabase (managed Postgres)
- **Production Cache:** Upstash (managed Redis)
- **Production Storage:** AWS S3 (receipts, blobs)
- **Local Development:** Native Postgres 16 + Redis 7 on WSL2

## Local Development Hardware
- **Workstation:** Skytech Shadow (Ryzen 7 7700, RTX 5060, 32GB DDR5, 1TB NVMe)
- **OS:** Windows 11 + WSL2 (Ubuntu 22.04)
- **Local AI:** Llama 3 (8B) on RTX 5060 for embeddings/summaries

## API Partners
- **E-Signature:** DocuSign API v2.1
- **Notary:** Proof Platform API v3
- **Invoicing:** Stripe, QuickBooks, Xero
- **Email (External):** Gmail API, MS Graph
- **Email (White-Label):** PolarisM Mail
- **Calendar:** Google Calendar, Outlook Calendar
- **Video:** LiveKit Cloud, Anam API
- **Telephony:** LiveKit Phone Numbers
- **Payments:** Stripe

## Cost
- Local Dev: $0/mo
- Production V1: $14/mo
- vs Legacy Docker: $133/mo (90% savings)

---

**End of Technology Stack**
