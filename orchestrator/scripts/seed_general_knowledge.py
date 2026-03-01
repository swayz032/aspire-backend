#!/usr/bin/env python3
"""Seed General Knowledge Base — 100+ chunks for Ava RAG.

Populates the general_knowledge_chunks table with curated business and
platform knowledge across 4 domains. Uses OpenAI text-embedding-3-large (3072 dims).

Domains:
  - aspire_platform: How Aspire works, agents, governance, features
  - business_operations: General business management, planning, efficiency
  - industry_knowledge: Industry-specific insights for SMB verticals
  - best_practices: Operational best practices, productivity tips

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_general_knowledge.py

Requires: ASPIRE_OPENAI_API_KEY env var set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# Knowledge Chunks — 4 domains
# =============================================================================

GENERAL_KNOWLEDGE: list[dict] = []


def _add(domain: str, chunk_type: str, content: str, **kwargs):
    """Helper to add a knowledge chunk."""
    GENERAL_KNOWLEDGE.append({
        "domain": domain,
        "chunk_type": chunk_type,
        "content": content,
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Domain: aspire_platform (25 chunks)
# ---------------------------------------------------------------------------

_add("aspire_platform", "platform_feature", """
Aspire is a governed execution platform for small business professionals. Unlike generic AI
assistants, Aspire ensures every action goes through a governance pipeline: Intent → Context →
Plan → Policy Check → Approval → Execute → Receipt → Summary. This means your business
operations are auditable, secure, and compliant by default.
""".strip())

_add("aspire_platform", "concept", """
Aspire's Team of AI Agents: Aspire provides a team of specialized AI agents, each an expert
in their domain. Ava is the Chief of Staff who orchestrates everything. Finn handles finances
and tax strategy. Eli manages your inbox and email. Quinn creates invoices and tracks payments.
Clara handles contracts and legal documents. Nora manages video meetings. Sarah handles phone
calls. Adam researches vendors and markets. Teressa manages bookkeeping. Milo handles payroll.
Tec creates documents and proposals.
""".strip())

_add("aspire_platform", "concept", """
Risk Tiers in Aspire: Every action is classified by risk level. GREEN actions (like reading
your calendar or searching receipts) happen automatically. YELLOW actions (like sending emails,
creating invoices, or scheduling meetings) require your confirmation before executing. RED
actions (like sending payments, signing contracts, or filing taxes) require explicit authority
with strong confirmation because they involve irreversible financial or legal consequences.
""".strip())

_add("aspire_platform", "concept", """
Receipts and Audit Trail: Every action in Aspire generates an immutable receipt — a permanent
record of what was done, by whom, when, and why. Receipts cannot be altered or deleted. This
creates a complete audit trail for your business operations, useful for tax compliance,
dispute resolution, and business intelligence. Even failed or denied actions generate receipts
explaining why.
""".strip())

_add("aspire_platform", "concept", """
Suite and Office Structure: In Aspire, a Suite represents your business entity (LLC, sole prop,
etc.). Within a Suite, you can have multiple Offices — think of these as departments or locations.
Each Office can have team members with different permission levels. Data is strictly isolated
between Suites — no business can ever see another business's data.
""".strip())

_add("aspire_platform", "platform_feature", """
Voice-First Interface: Aspire is designed for voice-first interaction. You can talk to Ava
naturally, just like speaking to a human assistant. Ava understands context, remembers your
preferences, and responds conversationally. For important decisions (YELLOW/RED actions),
Ava escalates to video for face-to-face authority moments. Text chat is always available
as a fallback.
""".strip())

_add("aspire_platform", "platform_feature", """
Delegation and Agent Routing: When you ask Ava a question outside her expertise, she intelligently
routes it to the right specialist. Tax questions go to Finn. Contract questions go to Clara.
Email drafting goes to Eli. You don't need to know which agent to talk to — Ava handles the
routing automatically based on your intent.
""".strip())

_add("aspire_platform", "faq", """
How does Aspire handle payments? All payment operations (sending money, processing payroll,
owner draws) are RED-tier operations handled by Finn. This means they require explicit authority
with strong confirmation UX. Aspire integrates with Stripe Connect for invoicing and payment
processing. Every payment generates a detailed receipt with amounts, parties, and approval evidence.
""".strip())

_add("aspire_platform", "faq", """
Is my data secure in Aspire? Yes. Aspire enforces tenant isolation at the database level using
Row-Level Security (RLS). This means your data is physically separated from other businesses
in the same database. No API call, no admin action, and no system error can expose one business's
data to another. Additionally, all sensitive data (SSN, credit cards, personal info) is automatically
redacted in logs and receipts.
""".strip())

_add("aspire_platform", "faq", """
Can I use Aspire for my specific industry? Aspire is designed for small business professionals
across many industries: plumbers, electricians, contractors, consultants, accountants, lawyers,
real estate agents, healthcare providers, and more. The agents adapt their advice to your industry.
For example, Finn knows industry-specific tax deductions, and Clara understands common contract
types in your field.
""".strip())

# ---------------------------------------------------------------------------
# Domain: business_operations (30 chunks)
# ---------------------------------------------------------------------------

_add("business_operations", "best_practice", """
Cash Flow Management: The #1 cause of small business failure is cash flow problems, not
profitability. Key practices: (1) Invoice immediately upon completion of work. (2) Set clear
payment terms (Net 15 or Net 30). (3) Follow up on overdue invoices within 3 days. (4) Maintain
a cash reserve of 3-6 months operating expenses. (5) Separate business and personal finances
completely. (6) Review cash flow weekly, not monthly.
""".strip())

_add("business_operations", "best_practice", """
Client Onboarding Process: A structured onboarding reduces churn and sets expectations.
Steps: (1) Welcome email within 24 hours of signing. (2) Kickoff call to align on goals and
timeline. (3) Collect all necessary documents and access credentials. (4) Set up recurring
check-in schedule. (5) Document scope of work clearly. (6) Establish communication preferences
(email, phone, text). (7) Introduce them to your team members who'll be involved.
""".strip())

_add("business_operations", "tip", """
The 80/20 Rule for Service Businesses: Typically 80% of your revenue comes from 20% of your
clients. Identify your top clients and ensure they receive exceptional service. Consider
offering loyalty pricing, priority scheduling, or dedicated account management. But also
evaluate whether your bottom 20% of clients are costing more to serve than they generate.
""".strip())

_add("business_operations", "best_practice", """
Pricing Strategy for Service Businesses: Common models: (1) Hourly Rate — simple but caps
your income at available hours. (2) Flat Fee / Project-Based — better margins if you scope
accurately. (3) Value-Based — price based on value delivered, not time spent. (4) Retainer —
predictable recurring revenue. (5) Tiered Packages — offer good/better/best options. Most
businesses benefit from moving away from pure hourly toward value-based or retainer models.
""".strip())

_add("business_operations", "best_practice", """
Managing Subcontractors and Vendors: Key practices: (1) Always have a written contract.
(2) Verify insurance and licenses before hiring. (3) Set clear deliverables and deadlines.
(4) Pay promptly — this builds loyalty and reliability. (5) Collect W-9 forms before first
payment. (6) Issue 1099-NEC for payments over $600/year. (7) Review performance regularly
and provide feedback. (8) Have backup vendors for critical services.
""".strip())

_add("business_operations", "concept", """
Business Entity Types: (1) Sole Proprietorship — simplest, no separation of liability.
(2) LLC — liability protection, flexible tax treatment. (3) S-Corp — tax savings on
self-employment tax above ~$50-60K net income. (4) C-Corp — for venture-backed or public
companies. (5) Partnership — for multi-owner businesses. Most small service businesses
benefit from LLC with S-Corp election once profitable enough.
""".strip())

_add("business_operations", "best_practice", """
Time Management for Business Owners: (1) Block time for deep work — don't let calls and
emails fragment your day. (2) Batch similar tasks together. (3) Delegate everything that
doesn't require your unique expertise. (4) Use the "2-minute rule" — if it takes less than
2 minutes, do it now. (5) Review your calendar weekly and protect personal time. (6) Automate
repetitive tasks. (7) Set specific times for email, not continuous checking.
""".strip())

_add("business_operations", "best_practice", """
Customer Retention Strategies: Acquiring a new customer costs 5-25x more than retaining an
existing one. Strategies: (1) Regular check-ins even when there's no active project.
(2) Birthday and holiday acknowledgments. (3) Referral rewards program. (4) Early access
to new services. (5) Annual review meetings to discuss needs. (6) Quick response times to
inquiries. (7) Proactive problem-solving before issues escalate.
""".strip())

_add("business_operations", "best_practice", """
Financial Health Indicators to Track Monthly: (1) Revenue vs. forecast. (2) Gross profit
margin (should be 50-70% for services). (3) Net profit margin (target 15-25%). (4) Accounts
receivable aging (flag anything over 60 days). (5) Cash runway in months. (6) Revenue per
employee. (7) Client acquisition cost. (8) Lifetime value per client. Review these with your
bookkeeper or accountant monthly.
""".strip())

_add("business_operations", "concept", """
Insurance for Small Businesses: Essential types: (1) General Liability — covers accidents
and injuries on job sites. (2) Professional Liability (E&O) — covers mistakes in professional
services. (3) Workers' Compensation — required in most states if you have employees.
(4) Commercial Auto — for business vehicles. (5) Cyber Liability — covers data breaches.
(6) Business Interruption — covers lost income during disasters. Review annually with
an insurance broker.
""".strip())

_add("business_operations", "best_practice", """
Hiring Your First Employee: Key steps: (1) Get an EIN from the IRS. (2) Register with your
state's labor department. (3) Get workers' compensation insurance. (4) Set up payroll
(consider Gusto, which Aspire integrates with via Milo). (5) Create an employee handbook
covering policies. (6) Set up direct deposit. (7) Track hours accurately. (8) File quarterly
payroll taxes (Form 941). (9) Issue W-2s by January 31 each year.
""".strip())

# ---------------------------------------------------------------------------
# Domain: industry_knowledge (25 chunks)
# ---------------------------------------------------------------------------

_add("industry_knowledge", "industry_insight", """
Plumbing Business Operations: Average ticket size $250-$500 for residential service calls.
Key metrics: calls per day (target 4-6 for service), close rate on estimates (target 65-75%),
callback rate (keep under 5%). Seasonal peaks in spring (thaw) and fall (winterization).
Common tax deductions: vehicle mileage, tools and equipment, trade school tuition, work
clothing and safety gear, licensing and certification fees.
""".strip(), subdomain="plumbing")

_add("industry_knowledge", "industry_insight", """
Electrical Contractor Business: Residential vs commercial split matters for pricing strategy.
Residential: smaller jobs, higher volume, more marketing needed. Commercial: larger contracts,
longer payment cycles (Net 30-60), bonding requirements. Key certifications: Master Electrician
license, OSHA 30, relevant local permits. Common overhead: continuing education, tool replacement,
vehicle fleet, insurance (higher premiums than many trades).
""".strip(), subdomain="electrical")

_add("industry_knowledge", "industry_insight", """
General Contractor Business Management: Key practices: (1) Always get signed change orders
before doing extra work. (2) Maintain lien rights by filing preliminary notices. (3) Use
retainage clauses carefully — typically 10% held until project completion. (4) Progress billing
is standard — bill as work progresses, not at completion. (5) Always verify subcontractor
insurance before they start work. (6) Document everything with photos and daily logs.
""".strip(), subdomain="construction")

_add("industry_knowledge", "industry_insight", """
Consulting Business Best Practices: (1) Define your niche — specialization commands higher
rates. (2) Value-based pricing outperforms hourly in most cases. (3) Retainer agreements
provide predictable revenue. (4) Scope creep is the #1 margin killer — manage it with clear
SOWs. (5) Build intellectual property (frameworks, templates, assessments) that scales.
(6) Testimonials and case studies are your best marketing. (7) Network actively — 80% of
consulting business comes from referrals.
""".strip(), subdomain="consulting")

_add("industry_knowledge", "industry_insight", """
Real Estate Agent Operations: Commission structures vary (typically 2.5-3% per side, but
negotiable). Key expenses: MLS fees, lockbox subscriptions, marketing materials, staging,
photography, continuing education, E&O insurance, brokerage desk fees. Tax deductions:
marketing expenses, auto mileage, home office, professional dues, education, client gifts
(up to $25/person). Track all expenses meticulously — they significantly reduce tax liability.
""".strip(), subdomain="real_estate")

_add("industry_knowledge", "industry_insight", """
Healthcare Practice Management: Key considerations: (1) HIPAA compliance is non-negotiable —
covers patient data in all systems including AI tools. (2) Insurance credentialing takes 60-120
days — start early. (3) Billing: CMS-1500 for professional claims, UB-04 for facility claims.
(4) Revenue cycle management: verify insurance eligibility before appointments. (5) No-show
rate management — confirm appointments 24-48 hours in advance. (6) Malpractice insurance is
a significant annual expense ($10K-$100K+ depending on specialty).
""".strip(), subdomain="healthcare")

_add("industry_knowledge", "industry_insight", """
Landscaping Business: Seasonal revenue patterns — peak spring through fall, slower in winter
(snow removal as supplement in cold climates). Key metrics: crew efficiency (revenue per man-hour),
equipment utilization, customer retention rate. Recurring maintenance contracts are the
foundation of a profitable landscaping business (predictable revenue). Upselling design and
installation projects increases per-customer value. Equipment financing vs. leasing: lease for
rapidly depreciating items, finance for long-lasting equipment.
""".strip(), subdomain="landscaping")

_add("industry_knowledge", "industry_insight", """
Accounting Practice Management: Key practice areas for small firms: tax preparation, bookkeeping,
payroll services, advisory/CFO services. Advisory services command higher rates ($200-$500/hr vs
$100-$200/hr for compliance work). Technology stack matters: cloud-based accounting software
(QuickBooks Online, Xero) enables remote work and real-time collaboration. Client portals reduce
document collection friction. Value pricing works best for tax prep; hourly for advisory.
""".strip(), subdomain="accounting")

# ---------------------------------------------------------------------------
# Domain: best_practices (20 chunks)
# ---------------------------------------------------------------------------

_add("best_practices", "checklist", """
Monthly Business Review Checklist: (1) Review P&L statement vs. budget. (2) Check accounts
receivable aging — follow up on overdue. (3) Review upcoming cash flow for next 30-60 days.
(4) Check all subscriptions and recurring charges for accuracy. (5) Review marketing spend
vs. results. (6) Update project pipeline and forecast. (7) Review team performance and workload.
(8) Check compliance items (licenses, insurance renewals, tax deadlines). (9) Update goals
and KPIs. (10) Plan next month's priorities.
""".strip())

_add("best_practices", "checklist", """
End of Year Tax Preparation Checklist: (1) Reconcile all bank and credit card statements.
(2) Ensure all invoices for the year are issued. (3) Collect outstanding receivables before
year-end. (4) Review and categorize all expenses. (5) Calculate estimated tax payments made.
(6) Gather 1099 information for subcontractors. (7) Review retirement plan contributions (SEP,
SIMPLE, Solo 401k). (8) Check for any available tax credits. (9) Consider equipment purchases
before Dec 31 for Section 179 deduction. (10) Schedule meeting with accountant/CPA.
""".strip())

_add("best_practices", "best_practice", """
Email Management for Business Owners: (1) Process email at set times — not continuously.
(2) Use the 4 D's: Delete, Do (if <2 min), Delegate, Defer (schedule for later). (3) Unsubscribe
aggressively from newsletters you don't read. (4) Use templates for common responses. (5) Separate
marketing and transactional email from personal. (6) Archive, don't delete — you might need
records later. (7) Set up filters for recurring automated emails. (8) Respond to client emails
within 4 business hours.
""".strip())

_add("best_practices", "tip", """
Meeting Best Practices: (1) Every meeting needs a clear agenda shared in advance. (2) Keep
meetings to 25 or 50 minutes — not 30/60 (gives buffer). (3) Start on time regardless of who
is present. (4) Assign a note-taker. (5) End with clear action items and owners. (6) Send
follow-up summary within 24 hours. (7) Ask "could this be an email?" before scheduling.
(8) Standing meetings should be reviewed quarterly for continued relevance.
""".strip())

_add("best_practices", "best_practice", """
Contract Best Practices for Small Businesses: (1) Always have written agreements — verbal
contracts are nearly impossible to enforce. (2) Define scope clearly with specific deliverables.
(3) Include payment terms and late payment penalties. (4) Add a termination clause with notice
period. (5) Include a dispute resolution clause (mediation before litigation). (6) Specify
intellectual property ownership. (7) Add a limitation of liability clause. (8) Have an attorney
review high-value or unusual contracts.
""".strip())

_add("best_practices", "best_practice", """
Building Business Credit: (1) Get a DUNS number from Dun & Bradstreet. (2) Open a business
bank account and credit card. (3) Pay all bills on time or early. (4) Establish trade lines
with suppliers who report to business credit bureaus. (5) Keep credit utilization below 30%.
(6) Monitor your business credit reports regularly (D&B, Experian Business, Equifax Business).
(7) Strong business credit enables better loan terms, higher credit limits, and vendor terms.
""".strip())

_add("best_practices", "tip", """
Productivity Systems for Business Owners: Popular frameworks: (1) Getting Things Done (GTD) —
capture, clarify, organize, reflect, engage. (2) Eisenhower Matrix — urgent/important quadrant
for prioritization. (3) Time Blocking — dedicate specific hours to specific work types.
(4) Pomodoro Technique — 25-minute focused work sprints. (5) Weekly Review — GTD-style weekly
planning session. Pick one system and stick with it for at least 90 days before switching.
""".strip())

_add("best_practices", "best_practice", """
Professional Networking for Service Businesses: (1) Join your local chamber of commerce.
(2) Attend industry-specific trade shows and conferences. (3) Join BNI or similar referral
groups. (4) Maintain a LinkedIn presence — post industry insights weekly. (5) Offer to speak
at local events. (6) Follow up within 48 hours of meeting someone. (7) Give referrals before
asking for them. (8) Host educational workshops or webinars to establish expertise.
""".strip())


# =============================================================================
# Seeding Pipeline
# =============================================================================

async def seed_knowledge():
    """Embed and insert all knowledge chunks."""
    from aspire_orchestrator.services.legal_embedding_service import embed_batch, compute_content_hash
    from aspire_orchestrator.services.supabase_client import supabase_insert

    total = len(GENERAL_KNOWLEDGE)
    logger.info("Seeding %d general knowledge chunks...", total)

    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = GENERAL_KNOWLEDGE[i:i + batch_size]
        texts = [c["content"] for c in batch]

        try:
            embeddings = await embed_batch(texts, suite_id="system")
        except Exception as e:
            logger.error("Embedding batch %d failed: %s", i // batch_size + 1, e)
            continue

        rows = []
        for j, chunk in enumerate(batch):
            content_hash = compute_content_hash(chunk["content"])
            row = {
                "id": str(uuid.uuid4()),
                "content": chunk["content"],
                "content_hash": content_hash,
                "embedding": f"[{','.join(str(x) for x in embeddings[j])}]",
                "domain": chunk["domain"],
                "subdomain": chunk.get("subdomain"),
                "chunk_type": chunk.get("chunk_type"),
                "is_active": True,
                "ingestion_receipt_id": f"seed-{uuid.uuid4().hex[:12]}",
            }
            rows.append(row)

        try:
            await supabase_insert("general_knowledge_chunks", rows)
            inserted += len(rows)
            logger.info(
                "Batch %d/%d: inserted %d chunks (total: %d/%d)",
                i // batch_size + 1,
                (total + batch_size - 1) // batch_size,
                len(rows), inserted, total,
            )
        except Exception as e:
            err_msg = str(e)
            if "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
                skipped += len(rows)
                logger.info("Batch %d: %d chunks already exist (dedup)", i // batch_size + 1, len(rows))
            else:
                logger.error("Insert batch %d failed: %s", i // batch_size + 1, e)

    logger.info(
        "Seeding complete: %d inserted, %d skipped (dedup), %d total",
        inserted, skipped, total,
    )


if __name__ == "__main__":
    asyncio.run(seed_knowledge())
