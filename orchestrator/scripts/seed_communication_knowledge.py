#!/usr/bin/env python3
"""Seed Communication Knowledge Base — 80+ chunks for Eli RAG.

Populates the communication_knowledge_chunks table with curated email,
client communication, and business writing knowledge across 4 domains.
Uses OpenAI text-embedding-3-large (3072 dims).

Domains:
  - email_best_practices: Subject lines, timing, structure, formatting
  - client_communication: Follow-up cadence, escalation, relationship management
  - business_writing: Professional tone, templates, drafting patterns
  - tone_guidance: Formality calibration, industry-appropriate language

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_communication_knowledge.py

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

COMM_KNOWLEDGE: list[dict] = []


def _add(domain: str, chunk_type: str, content: str, **kwargs):
    """Helper to add a knowledge chunk."""
    COMM_KNOWLEDGE.append({
        "domain": domain,
        "chunk_type": chunk_type,
        "content": content,
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Domain: email_best_practices (25 chunks)
# ---------------------------------------------------------------------------

_add("email_best_practices", "best_practice", """
Email Subject Line Best Practices: (1) Keep under 50 characters for mobile compatibility.
(2) Front-load the key information. (3) Be specific — "Invoice #1234 Due Nov 15" beats
"Invoice Reminder". (4) Include action needed — "Action Required:", "For Review:", "FYI:".
(5) Avoid all caps and excessive punctuation. (6) Don't use "Re:" or "Fwd:" deceptively.
(7) Use numbers when applicable — "3 Items Need Your Approval". (8) A/B test subject lines
for marketing emails.
""".strip())

_add("email_best_practices", "guideline", """
Email Timing for Maximum Open Rates: Business emails perform best: Tuesday through Thursday,
9-11 AM local time of the recipient. Monday mornings are cluttered with weekend backlog.
Friday afternoons have low engagement. For urgent items, send immediately regardless of timing.
For proposals and important documents, mid-morning Tuesday or Wednesday is optimal. Follow-up
emails: wait 2-3 business days before the first follow-up, then 5-7 days for subsequent ones.
""".strip())

_add("email_best_practices", "template", """
Professional Email Structure Template:
Subject: [Action/Context] — [Specific Topic]

Hi [Name],

[Opening: 1 sentence of context or acknowledgment]

[Body: Key information, organized as:]
- Numbered list for sequential steps
- Bullet points for parallel items
- Bold for emphasis on critical points

[Clear call to action: What do you need from them? By when?]

[Professional closing],
[Your name]
[Title, Company]
""".strip())

_add("email_best_practices", "best_practice", """
Email Response Time Standards: (1) Client emails: respond within 4 business hours, even if
just to acknowledge receipt and set expectations. (2) Vendor/partner emails: within 1 business
day. (3) Internal team: within 2-4 hours during business hours. (4) Urgent/flagged: within
1 hour. If you need more time to provide a complete answer, send a brief acknowledgment:
"Got your message. I'll review and get back to you by [specific time]."
""".strip())

_add("email_best_practices", "best_practice", """
When to Use Email vs. Phone vs. Meeting: Use EMAIL for: documentation, non-urgent items, detailed
information, multi-party updates. Use PHONE for: sensitive discussions, quick clarifications,
relationship building, urgent matters. Use MEETINGS for: collaborative problem-solving, complex
negotiations, presentations, team alignment. Rule of thumb: if you've exchanged more than 3
emails on the same topic without resolution, switch to a call.
""".strip())

_add("email_best_practices", "guideline", """
Email Attachments Best Practices: (1) Always mention attachments in the email body — "Attached
you'll find...". (2) Name files descriptively — "SmithProject_Proposal_Dec2026.pdf" not
"proposal_v3_final_FINAL.pdf". (3) Compress large files or use cloud links for files over 10MB.
(4) Use PDF for final documents (preserves formatting). (5) Use editable formats (Word, Excel)
only when you want the recipient to make changes. (6) Never attach sensitive data (SSN, credit
cards) via email — use a secure portal.
""".strip())

_add("email_best_practices", "subject_line", """
Subject Line Templates by Scenario:
- Quote/Estimate: "Estimate for [Project] — $[Amount] — Valid Until [Date]"
- Invoice: "Invoice #[Number] — $[Amount] Due [Date]"
- Meeting Request: "Meeting Request: [Topic] — [Proposed Date/Time]"
- Follow-Up: "Following Up: [Original Topic] — [Date of Last Contact]"
- Introduction: "Introduction: [Your Name] — [Context/Referral Source]"
- Thank You: "Thank You — [Specific What You're Thankful For]"
- Urgent: "Time-Sensitive: [Topic] — Response Needed by [Date]"
""".strip())

# ---------------------------------------------------------------------------
# Domain: client_communication (20 chunks)
# ---------------------------------------------------------------------------

_add("client_communication", "best_practice", """
Client Follow-Up Cadence After Proposal: Day 1: Send proposal with a clear summary email.
Day 3: Follow up — "Did you have a chance to review the proposal?" Day 7: Second follow-up —
add value (case study, testimonial, or additional insight). Day 14: Third follow-up — create
gentle urgency: "I want to make sure we can fit this into our schedule. When would be a good
time to discuss?" Day 21+: Move to monthly check-ins. Never be pushy — each follow-up should
add value, not just ask for an answer.
""".strip())

_add("client_communication", "guideline", """
Handling Difficult Client Conversations: (1) Lead with empathy: "I understand your frustration."
(2) State facts objectively: "Here's what happened." (3) Take responsibility when appropriate:
"We should have caught that sooner." (4) Present solutions, not excuses: "Here's what we'll do
to fix it." (5) Document everything in writing after the conversation. (6) Set clear expectations
for resolution timeline. (7) Follow up to confirm the issue is resolved. (8) Never argue via
email — pick up the phone.
""".strip())

_add("client_communication", "follow_up_pattern", """
Post-Project Follow-Up Sequence: Week 1: "Thank you for the project" + ask for feedback.
Month 1: Check in on how things are working. Month 3: Share relevant industry news or tip.
Month 6: Annual review meeting suggestion. Ongoing: quarterly touches (holiday cards, birthday
notes, industry articles). Purpose: stay top-of-mind for referrals and repeat business without
being intrusive. Each touchpoint should offer genuine value.
""".strip())

_add("client_communication", "guideline", """
Communicating Price Increases: (1) Give 30-60 days advance notice. (2) Explain the why:
increased costs, expanded services, market rates. (3) Highlight added value they've received.
(4) Be specific about the change: "$X to $Y, effective [date]". (5) Offer options if possible:
"You can keep current scope at the new rate, or we can adjust scope." (6) Deliver via phone
first, then confirm in writing. (7) Express gratitude for the relationship. (8) Never apologize
for fair pricing — confidence conveys value.
""".strip())

_add("client_communication", "best_practice", """
Setting Client Expectations: The single most important factor in client satisfaction. Best
practices: (1) Under-promise, over-deliver on timelines. (2) Be explicit about what IS and
IS NOT included in scope. (3) Communicate delays proactively — before the deadline, not after.
(4) Set response time expectations upfront: "I typically respond within 4 hours during business
hours." (5) Document all agreements in writing. (6) Regular progress updates build trust.
(7) When in doubt, over-communicate.
""".strip())

_add("client_communication", "guideline", """
Requesting Testimonials and Reviews: Timing matters — ask when the client is happiest (project
completion, positive results, compliment received). Template: "I'm so glad you're happy with
[specific result]. Would you be willing to share a brief testimonial? It would mean a lot to
our business. I can draft something based on our conversation for your approval, or you can
write it in your own words — whichever is easier." Make it easy. Offer to draft. Follow up once
if needed. Never pressure.
""".strip())

# ---------------------------------------------------------------------------
# Domain: business_writing (20 chunks)
# ---------------------------------------------------------------------------

_add("business_writing", "template", """
Proposal Structure for Service Businesses:
1. Executive Summary (1 paragraph — the "elevator pitch")
2. Understanding of Needs (show you listened)
3. Proposed Solution (what you'll do, specifically)
4. Scope of Work (deliverables, timeline, milestones)
5. Investment (pricing — use "investment" not "cost")
6. About Us (brief credentials, relevant experience)
7. Next Steps (clear call to action)
Tip: Lead with their problem, not your capabilities. They care about outcomes.
""".strip())

_add("business_writing", "guideline", """
Professional Writing Tone Guide: (1) Write at an 8th-grade reading level — clarity over
complexity. (2) Use active voice: "We completed the project" not "The project was completed
by us." (3) Be specific: "Wednesday at 2 PM" not "sometime this week." (4) Front-load
important information. (5) One idea per paragraph. (6) Use "we" and "you" language — creates
partnership feeling. (7) Avoid jargon unless writing to industry peers. (8) Read your writing
aloud — if it sounds stiff, it reads stiff.
""".strip())

_add("business_writing", "template", """
Statement of Work (SOW) Template:
1. Project Overview: [2-3 sentences describing the project]
2. Scope: [Specific deliverables as numbered list]
3. Out of Scope: [Explicitly list what's NOT included]
4. Timeline: [Milestones with dates]
5. Assumptions: [What must be true for this timeline/price]
6. Client Responsibilities: [What you need from them]
7. Pricing: [Detailed breakdown]
8. Payment Terms: [When payments are due]
9. Change Order Process: [How scope changes are handled]
10. Signatures: [Both parties]
""".strip())

_add("business_writing", "best_practice", """
Writing Effective Invoices: (1) Include your logo and branding — look professional.
(2) Number invoices sequentially. (3) List detailed line items — not just "services rendered."
(4) Include date of service, not just invoice date. (5) State payment terms clearly: "Due upon
receipt" or "Net 30." (6) Include accepted payment methods. (7) Add late payment terms.
(8) Thank the client: "Thank you for your business." (9) Include project/PO number for easy
reference. (10) Send as PDF, not editable format.
""".strip())

_add("business_writing", "guideline", """
Writing Effective Business Proposals: Common mistakes: (1) Making it about you instead of the
client's needs. (2) Using generic language instead of personalizing. (3) Not including a clear
price. (4) Making it too long — 3-5 pages is ideal for most service proposals. (5) Not including
social proof (testimonials, case studies). (6) Missing a clear next step / call to action.
(7) Using internal jargon. (8) Not addressing objections proactively.
""".strip())

# ---------------------------------------------------------------------------
# Domain: tone_guidance (15 chunks)
# ---------------------------------------------------------------------------

_add("tone_guidance", "tone_rule", """
Formality Spectrum for Business Communication:
Level 1 (Most Formal): Legal documents, government correspondence, formal complaints.
Use "Dear Mr./Ms.", full sentences, no contractions, formal sign-off.
Level 2 (Professional): First-time client emails, proposals, formal updates.
Use "Hi [Name]", professional tone, occasional contractions OK.
Level 3 (Warm Professional): Established client relationships, internal team.
Use first names, conversational tone, contractions fine, some personality OK.
Level 4 (Casual): Close colleagues, informal updates, chat messages.
Use casual language, abbreviations OK, personality encouraged.
Default to Level 2 for new contacts, adjust based on their communication style.
""".strip())

_add("tone_guidance", "tone_rule", """
Industry-Specific Tone Adjustments:
- Legal/Financial: More formal, precise language, avoid humor, cite specifics.
- Construction/Trades: Direct, practical, no-nonsense, minimal corporate jargon.
- Creative/Design: More personality, conversational, visual references OK.
- Healthcare: Professional but warm, patient-focused language, HIPAA awareness.
- Tech/Startup: Casual professional, industry terms OK, efficiency valued.
- Real Estate: Warm and approachable, market-aware, enthusiasm appropriate.
Mirror your client's communication style when possible.
""".strip())

_add("tone_guidance", "tone_rule", """
Tone Do's and Don'ts:
DO: Be clear and direct. Use positive framing ("Here's what we can do" vs "We can't do that").
Show genuine interest. Use the client's name. Match their energy level.
DON'T: Use excessive exclamation points (!!!). Be overly casual with new contacts. Use sarcasm
in writing (it doesn't translate). Be condescending or use "actually" as a correction. Use
passive-aggressive language ("As I previously mentioned..."). Be robotic or template-obvious.
""".strip())

_add("tone_guidance", "tone_rule", """
Apology and Recovery Tone: When something goes wrong: (1) Acknowledge promptly — don't delay.
(2) Be genuine — "I apologize" is stronger than "I'm sorry if you felt." (3) Be specific
about what went wrong. (4) Don't over-explain or make excuses. (5) Focus on the fix, not the
problem. (6) Follow up to confirm resolution. Example: "I apologize for the delay on your
proposal. That fell below our standards. I've prioritized it and you'll have it by end of day
tomorrow. To make up for the wait, I've included an additional [value-add]."
""".strip())

_add("tone_guidance", "guideline", """
Saying No Professionally: Framework: Acknowledge → Explain → Alternative → Goodwill.
Example: "I appreciate you thinking of us for this project. Unfortunately, our current
schedule doesn't allow us to give it the attention it deserves within your timeline.
I'd recommend reaching out to [referral]. If your timeline extends to [date], we'd love
to revisit this." Never just say "no" without offering an alternative or next step.
""".strip())

_add("tone_guidance", "tone_rule", """
Urgency Without Panic: When communicating urgent items: (1) Use clear subject lines with
deadlines. (2) State the impact of missing the deadline. (3) Be matter-of-fact, not alarmist.
(4) Provide specific actions needed. Good: "We need your signature by 5 PM Friday to avoid
a 30-day delay in the permit process. Here's the document — it takes about 2 minutes."
Bad: "URGENT!!! SIGN NOW OR EVERYTHING IS RUINED!!!"
""".strip())


# =============================================================================
# Seeding Pipeline
# =============================================================================

async def seed_knowledge():
    """Embed and insert all communication knowledge chunks."""
    from aspire_orchestrator.services.legal_embedding_service import embed_batch, compute_content_hash
    from aspire_orchestrator.services.supabase_client import supabase_insert

    total = len(COMM_KNOWLEDGE)
    logger.info("Seeding %d communication knowledge chunks...", total)

    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = COMM_KNOWLEDGE[i:i + batch_size]
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
            await supabase_insert("communication_knowledge_chunks", rows)
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
