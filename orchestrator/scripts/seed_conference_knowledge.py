#!/usr/bin/env python3
"""Seed Conference Knowledge Base — 75 chunks for Nora's meeting intelligence.

Populates the conference_knowledge_chunks table with curated meeting facilitation
and intelligence knowledge across 6 domains. Uses OpenAI text-embedding-3-large (3072 dims).

Domains:
  - meeting_facilitation: Facilitation techniques, virtual etiquette, problem scenarios
  - risk_routing: Decision authority, escalation triggers, compliance detection
  - action_items: Extraction patterns, prioritization, tracking workflows
  - calendar_optimization: Scheduling intelligence, conflict resolution, time protection
  - post_meeting_workflows: Follow-up automation, summary generation, stakeholder routing
  - meeting_intelligence: Analytics, patterns, strategic insights

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_conference_knowledge.py

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
# Knowledge Chunks — 6 domains, 75 total
# =============================================================================

CONFERENCE_KNOWLEDGE: list[dict] = []


def _add(domain: str, chunk_type: str, content: str, **kwargs):
    """Helper to add a knowledge chunk."""
    CONFERENCE_KNOWLEDGE.append({
        "domain": domain,
        "chunk_type": chunk_type,
        "content": content,
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Domain 1: meeting_facilitation (20 chunks)
# ---------------------------------------------------------------------------

_add("meeting_facilitation", "best_practice", """
Virtual Meeting Facilitation Best Practices: Effective virtual meetings require intentional structure.
Start with a clear agenda shared 24 hours in advance. Begin meetings by stating the purpose and expected
outcomes. Use video whenever possible to build connection and read body language. Establish ground rules
early: mute when not speaking, raise hand before interrupting, use chat for side questions. Assign explicit
roles: facilitator (drives conversation), timekeeper (watches clock), note-taker (captures decisions).
End meetings with clear next steps and assigned owners. Record meetings only with participant consent
and clear purpose.
""".strip())

_add("meeting_facilitation", "best_practice", """
Managing Meeting Energy and Engagement: Long virtual meetings drain energy faster than in-person.
Structure meetings in 25-minute focused segments with 5-minute breaks. Use interactive techniques:
polls, breakout rooms, collaborative whiteboards, direct questions to individuals. Watch for
disengagement signals: cameras turning off, silence when questions are asked, multitasking behavior.
Re-engage participants by calling on them by name, changing the format, or taking a spontaneous break.
For meetings over 90 minutes, include a 10-15 minute intermission. Vary who speaks — long monologues
lose attention quickly.
""".strip())

_add("meeting_facilitation", "best_practice", """
Handling Difficult Meeting Dynamics: When conflicts arise, acknowledge them directly but neutrally:
"I'm hearing different perspectives on this issue." Separate the person from the problem. Use the
"parking lot" technique for off-topic important items: capture them visibly, commit to addressing
later, move on. If someone dominates conversation, intervene politely: "Thanks for that perspective,
let's hear from others who haven't spoken yet." For passive participants, use direct invitations:
"Sarah, you have deep experience here — what's your take?" When meetings derail, call a process
check: "Let's pause and check if we're still on track toward our goal."
""".strip())

_add("meeting_facilitation", "best_practice", """
Decision-Making Frameworks for Meetings: Different decisions need different processes. For consensus
decisions (everyone must agree), use structured rounds where each person speaks without interruption.
For consultative decisions (leader decides after input), clearly frame it as such: "I'll gather input
now and make a final call by end of day." For majority votes, state the threshold (simple majority
vs. supermajority) before voting. For complex decisions, use the Gradients of Agreement scale:
wholehearted agreement, agreement with reservations, can live with it, abstain, disagree but willing
to go forward, strong disagreement. Document both the decision and the decision-making process.
""".strip())

_add("meeting_facilitation", "tip", """
Virtual Meeting Etiquette Standards: Professional virtual meetings follow established norms.
Join 2-3 minutes early to test audio/video. Use a professional background or blur. Position camera
at eye level, not looking down. Dress professionally (at least business casual from waist up).
Good lighting is essential — face a window or use a ring light. Minimize background noise — use
headphones with mic. Announce yourself when joining if video is off: "This is Alex joining."
Don't eat during meetings (drinking is acceptable). Close distracting applications. If you must
step away, announce it: "I need to step away for 2 minutes." End meetings on time — running over
is disrespectful to participants' schedules.
""".strip())

_add("meeting_facilitation", "tip", """
Meeting Chat Best Practices: Chat serves multiple purposes in virtual meetings. Use it for
questions that don't need immediate answers — facilitator can address them in batches. Share
links, resources, or references without disrupting flow. Acknowledge good points with brief
affirmations. For technical issues ("can't hear you"), chat is faster than unmuting. Avoid
side conversations unrelated to the meeting — that's disrespectful to speakers. Don't overuse
chat as a substitute for speaking up. If the same question appears multiple times in chat, it
signals confusion that should be addressed verbally. Review chat log after meeting for captured
questions or insights.
""".strip())

_add("meeting_facilitation", "example", """
Scenario: One Participant Won't Stop Talking: This common situation requires tactful intervention.
First attempt: politely interrupt with a time check: "Great points, we have 5 minutes left so let's
make sure we hear from everyone." Second attempt: redirect with appreciation: "Thanks for that
thorough input, I'd like to hear other perspectives now." If pattern continues, use a more direct
approach: "I'm going to pause you there so we can get diverse input." After the meeting, consider a
private conversation if it's a recurring issue. Establish explicit speaking time limits for future
meetings: "We'll take 2-minute inputs from each person." Document ground rules clearly.
""".strip())

_add("meeting_facilitation", "example", """
Scenario: Technical Difficulties Disrupting Meeting: Technical problems are inevitable in virtual
meetings. Have a backup plan: if video conferencing fails, shift to conference call with screen
sharing. If one participant has persistent issues, continue without them and send detailed notes
after. For audio issues, use chat to communicate: "We can't hear you — try reconnecting." If your
own tech fails, communicate quickly in chat and rejoin ASAP. For screen sharing issues, have an
alternative ready: pre-shared slides or documents. Keep meetings running despite individual technical
problems — don't let one person's issues derail the whole group. Follow up individually with anyone
who missed key content.
""".strip())

_add("meeting_facilitation", "example", """
Scenario: Meeting Purpose is Unclear or Hijacked: Sometimes meetings lose direction or get taken over
by tangential topics. Intervene with a process check: "I want to pause and revisit our stated purpose
for this meeting: [restate purpose]. Are we still aligned with that, or do we need to adjust?"
If the new direction is genuinely important, acknowledge it: "This is clearly important. I suggest we
schedule a separate meeting to address it properly rather than shortchange both topics." If the hijack
is a recurring pattern, strengthen your agenda discipline: share written agendas in advance, get
explicit buy-in at meeting start, assign a timekeeper to enforce structure.
""".strip())

_add("meeting_facilitation", "best_practice", """
Effective Breakout Room Facilitation: Breakout rooms enable small group discussion in large virtual
meetings. Set clear expectations before breaking out: specific question to answer, time limit, expected
deliverable (decision, list, recommendation). Keep rooms small (3-5 people) for maximum participation.
Assign a facilitator for each room if possible. Use the broadcast message feature to give 2-minute and
1-minute warnings. When reconvening, have each room share their key insight or decision (not full recap).
Capture outputs visibly on shared document or whiteboard. Don't overuse breakouts — they add 5-10 minutes
minimum to meeting time.
""".strip())

_add("meeting_facilitation", "best_practice", """
Pre-Meeting Preparation Checklist: Successful meetings start before anyone joins. Define clear objective
and confirm meeting is necessary (could it be an email?). Create and share agenda 24 hours ahead with
time allocations. Send any pre-reading materials with specific guidance on what to focus on. Test
technology: audio, video, screen sharing, recording. Prepare any presentations or materials. Identify
who needs to be there vs. who can receive notes. Send calendar invites with dial-in details and joining
instructions. For important meetings, send a reminder 1 hour before with one-sentence purpose statement.
Prepare questions to stimulate discussion if conversation lags.
""".strip())

_add("meeting_facilitation", "best_practice", """
Managing Time in Meetings: Time discipline is essential for effective meetings. Start exactly on time
even if people are missing — this trains punctuality. Assign a timekeeper to track agenda items and warn
when time is running short. Use visible timers for discussions: "We have 10 minutes for this topic."
Timebox open discussions: "Let's take 5 minutes for initial reactions." When discussions run long, offer
choices: "We're at time. We can extend 5 minutes, table this for later, or schedule a follow-up." Protect
time for the most important items by putting them early in the agenda. End on time or early — never late
without group consent. Build in buffer time between agenda items for overruns.
""".strip())

_add("meeting_facilitation", "best_practice", """
Inclusive Meeting Practices: Create space for all voices to be heard. Actively invite input from quiet
participants: "I'd like to hear from folks who haven't spoken yet." Watch for power dynamics — junior
people may defer to senior leaders. Directly solicit dissenting views: "What concerns or objections should
we consider?" Use round-robin for important questions (everyone speaks once before anyone speaks twice).
Acknowledge and build on others' contributions: "Building on what Sarah said..." Allow think time before
requesting answers: "Take 30 seconds to consider this question." Provide multiple input channels: speaking,
chat, shared docs. Address interruptions: "Let's let Alex finish their thought." Monitor who speaks and
for how long — imbalance often indicates exclusion.
""".strip())

_add("meeting_facilitation", "best_practice", """
Effective Status Update Meetings: Standing status meetings need structure to avoid becoming time-wasters.
Use a standard format: each person shares (1) what they completed, (2) what they're working on, (3) what's
blocking them. Set strict time limits: 2-3 minutes per person maximum. Use asynchronous updates when possible:
shared document reviewed before meeting, meeting time used only for blockers and questions. Focus meeting time
on exception-based discussion: what's off-track, what needs group input, what affects multiple people. Skip
the meeting if there's nothing exceptional to discuss. Rotate who goes first to keep energy up. Document
blockers and assign owners to resolve them. Consider if the meeting could shift to weekly written updates
with biweekly discussion.
""".strip())

_add("meeting_facilitation", "best_practice", """
Running Effective Brainstorming Sessions: Good brainstorming follows specific practices. Start with a
clearly framed challenge statement. Separate idea generation from evaluation: first generate many ideas
without critique, then evaluate them. Use "yes, and" instead of "yes, but" to build on ideas. Set a quantity
goal: "Let's generate 25 ideas in 10 minutes." Use techniques like round-robin, brainwriting (written before
shared), or random word association. Welcome wild ideas — they often spark practical ones. Capture all ideas
visibly without judgment. Group similar ideas into themes. Use dot voting to prioritize: everyone gets 3 votes
to allocate. Take top-voted ideas into evaluation phase with clear criteria.
""".strip())

_add("meeting_facilitation", "best_practice", """
Remote Team Building in Virtual Meetings: Building connection in virtual environments requires intentional
effort. Start meetings with personal check-ins: "Share one thing you're grateful for this week" or "What's
your coffee/tea of choice today?" Use icebreakers sparingly: they feel forced if overdone. Celebrate wins
publicly: shout-outs for accomplishments. Create informal time: virtual coffee chats, team lunch meetings
where you don't talk about work. Use games or trivia occasionally (once monthly, not weekly). Leverage breakout
rooms for small group connection. Encourage video-on for relationship building (but respect privacy needs).
Host virtual social events: happy hours, game sessions, watch parties. The goal is authentic connection, not
forced fun.
""".strip())

_add("meeting_facilitation", "best_practice", """
Meeting Roles and Responsibilities: Effective meetings distribute responsibilities across multiple roles.
Facilitator: designs and runs the meeting, manages discussion flow, ensures objectives are met, handles
conflicts. Timekeeper: monitors agenda timing, gives warnings when time is running short, helps group make
decisions about extending discussions. Note-taker: captures key points, decisions, and action items in real-time,
shares notes afterward. Participant: comes prepared, contributes actively, stays engaged, follows agreed norms.
Sponsor/Decision-Maker (if present): provides context, makes final calls on decisions within their authority,
removes obstacles. Technical Host: manages platform features, admits participants, manages breakouts and
recording, handles tech troubleshooting.
""".strip())

_add("meeting_facilitation", "best_practice", """
Handling Emotions in Meetings: Emotions emerge in meetings, especially on difficult topics. Acknowledge
emotions without judgment: "I can see this is bringing up strong feelings." Distinguish between facts and
interpretations: "The fact is the deadline moved. Your interpretation is that it reflects poor planning."
Allow space for emotional expression within reason, then redirect to productive problem-solving. If someone
becomes very upset, offer a break: "Let's take 5 minutes to reset." For ongoing tension between participants,
suggest offline resolution: "This seems like something to work through separately." Model emotional regulation:
stay calm, speak slowly, use neutral language. Don't dismiss emotions ("let's not get emotional"), but do set
boundaries for respectful expression.
""".strip())

_add("meeting_facilitation", "best_practice", """
Decision Documentation in Meetings: Capturing decisions clearly prevents future confusion. Document: (1) the
decision made, (2) who made it or voting outcome, (3) rationale/key factors, (4) implementation owner, (5) timing,
(6) success measures. Use clear language: "DECIDED: We will launch Product X on March 1" not "We talked about
maybe launching." Note dissent if significant: "Decision made by majority vote; 2 members recorded strong concerns
about timeline." Make decisions visible during the meeting: shared screen, whiteboard, or chat. Confirm decision
with group before moving on: "So we've decided [X] with [Y] as owner. Is that correct?" Circulate decision log
within 24 hours. For major decisions, include a "decision review date" to assess outcomes.
""".strip())

_add("meeting_facilitation", "best_practice", """
Virtual Meeting Burnout Prevention: Excessive virtual meetings cause Zoom fatigue and burnout. Organizations
should establish norms: (1) No-meeting blocks (e.g., no meetings before 10am or after 3pm on Fridays). (2) Meeting
budget limits: max 20 hours/week in meetings. (3) Default to 25/50 minute meetings instead of 30/60 to build in
breaks. (4) Email-first for information sharing, meetings only for discussion and decisions. (5) Meeting-free days
weekly. (6) Asynchronous alternatives: recorded video messages, collaborative documents, Slack discussions. (7)
Quarterly meeting audits: which recurring meetings can be eliminated? Individuals can protect their calendars by
blocking focus time, declining meetings without clear purpose, and proposing asynchronous alternatives.
""".strip())


# ---------------------------------------------------------------------------
# Domain 2: risk_routing (15 chunks)
# ---------------------------------------------------------------------------

_add("risk_routing", "routing_rule", """
Meeting Decision Authority Principles: Not all decisions can or should be made in meetings. Establish clear
decision rights before meetings. Level 1 decisions (routine, reversible, low-impact) can be made by individual
contributors. Level 2 decisions (moderate impact, some risk) require team lead approval. Level 3 decisions
(strategic, high-cost, irreversible) need executive authority. In meetings, clarify who has authority to decide:
"This is a consultative decision — I'll gather input and make a final call by EOD." If a decision surfaces that
exceeds participants' authority, document it as an escalation item with recommendation rather than attempting
to decide. Use RACI framework: Responsible (does the work), Accountable (makes the decision), Consulted (provides
input), Informed (needs to know outcome).
""".strip())

_add("risk_routing", "routing_rule", """
When to Escalate from a Meeting: Certain situations require escalation beyond the meeting. Escalate when:
(1) Decision exceeds participants' authority level (budget approval, strategic shifts, policy changes). (2) Legal
or compliance concerns emerge (contract terms, regulatory requirements, liability issues). (3) Ethical concerns
arise (potential conflicts of interest, fairness questions). (4) Interpersonal conflicts can't be resolved in the
meeting (HR involvement needed). (5) Resource requirements exceed team budget or allocation. (6) Timeline changes
affect dependencies with other teams or commitments to customers. (7) Information emerges that changes fundamental
assumptions. When escalating, provide: situation summary, why it needs escalation, your recommendation, urgency level.
""".strip())

_add("risk_routing", "risk_pattern", """
Detecting Compliance Issues in Meetings: Facilitators should recognize compliance red flags and route appropriately.
Financial compliance signals: discussion of revenue recognition, expense categorization, budget overrides, financial
reporting. HR compliance signals: hiring decisions without documented process, termination discussions, compensation
changes, discrimination or harassment concerns. Legal compliance signals: contract negotiations without legal review,
IP ownership questions, liability exposure, regulatory requirements. Data privacy signals: customer data usage,
GDPR/CCPA requirements, data retention policies. When detected, pause and route: "This touches [compliance area].
We need to involve [legal/finance/HR] before proceeding." Don't make commitments in areas requiring compliance review.
""".strip())

_add("risk_routing", "routing_rule", """
Financial Decision Thresholds in Meetings: Establish clear financial decision authorities. Example framework:
Under $500: individual team member discretion. $500-$5000: team lead approval required. $5000-$25000: department
head approval. Over $25000: executive approval with business case. For meetings, if discussion involves spend
exceeding participants' authority, frame it as a recommendation to appropriate level: "We propose [action] requiring
$15K investment. Sarah [team lead] can approve, but we'll prepare a brief business case first." Never commit
organizationally to spend beyond meeting participants' authority. Document financial implications clearly in meeting
notes and route to appropriate approvers.
""".strip())

_add("risk_routing", "routing_rule", """
Customer Commitment Authority: Not all meeting participants can commit to customers or external parties. Define
who can commit to: (1) Delivery timelines and scope. (2) Pricing and discounts. (3) Service level agreements.
(4) Product roadmap items. (5) Custom development work. (6) Support response times. If customer commitments emerge
in internal meetings, mark them as "requires customer-facing approval" and route to account owner or sales. If
customer commitments come up in external meetings without proper authority, use deferral language: "I need to
confirm that timeline with our team and will get back to you by [date]." Don't let enthusiasm override authority
boundaries — failed commitments damage customer relationships.
""".strip())

_add("risk_routing", "routing_rule", """
HR and Personnel Issues Requiring Escalation: Certain topics must be immediately escalated to HR and not resolved
in regular meetings. Automatic escalations: (1) Allegations of harassment, discrimination, or hostile work environment.
(2) Termination discussions (except with HR present). (3) Complaints about manager behavior or ethics. (4) Requests
for accommodations related to disability, religion, or family status. (5) Wage and hour disputes. (6) Whistleblower
reports. (7) Significant interpersonal conflicts affecting work. When these arise, acknowledge without detail:
"This is an HR matter. I'll connect you with HR today and pause this discussion." Don't attempt to investigate or
resolve — that's HR's role. Document that escalation occurred but not the details (privacy).
""".strip())

_add("risk_routing", "risk_pattern", """
Security and Privacy Escalation Triggers: Certain discussions require immediate security or privacy team involvement.
Security escalations: (1) Actual or suspected data breaches. (2) Security vulnerability discoveries. (3) Unauthorized
access incidents. (4) Proposed architecture changes affecting security. (5) Third-party vendor access requests. Privacy
escalations: (1) New collection of personal information. (2) Changes to data retention or deletion policies. (3) Data
sharing with third parties. (4) International data transfers. (5) Customer data access requests. When detected, stop
discussion and route: "This has security/privacy implications. We need [security/privacy team] review before proceeding."
Don't make decisions that could create security or privacy risks.
""".strip())

_add("risk_routing", "routing_rule", """
Strategic vs Tactical Decisions: Distinguish between strategic (long-term, high-impact) and tactical (short-term,
reversible) decisions. Strategic decisions require executive involvement: market selection, product direction, major
partnerships, organizational structure, annual budgets, brand positioning. Tactical decisions can be team-level:
tool selection, process improvements, task prioritization, individual project timelines. In meetings, if strategic
questions emerge beyond participants' scope, frame as input to leadership: "This is a strategic question. Let's
capture our perspective to provide as input to [executives], but we can't decide it here." Use Jeff Bezos's
framework: Type 1 decisions (irreversible, strategic) deserve deep analysis and high-level authority. Type 2
decisions (reversible, tactical) should be made quickly at lower levels.
""".strip())

_add("risk_routing", "routing_rule", """
Crisis Situations Requiring Immediate Escalation: Some situations require immediate leadership notification, even
interrupting the meeting. Immediate escalations: (1) Outages affecting customers or revenue. (2) Security breaches
or attacks in progress. (3) Legal demands or regulatory actions. (4) Safety incidents or threats. (5) Major customer
escalations (churn risk, public complaints). (6) Media inquiries on sensitive topics. (7) Executive or board-level
questions needing immediate response. When crisis emerges, declare it: "This is a crisis situation. I'm pausing this
meeting to escalate to [leadership] immediately." Assign someone to manage communication while issue is being handled.
Document timeline of events and decisions.
""".strip())

_add("risk_routing", "risk_pattern", """
Contractual and Legal Risk Recognition: Meeting facilitators should recognize when legal review is needed. Legal
review triggers: (1) Contract negotiations or modifications. (2) Vendor agreements over certain dollar threshold
(e.g., $10K). (3) IP licensing discussions. (4) Partnership or reseller agreements. (5) Employment agreement changes.
(6) Terms of service or privacy policy modifications. (7) Customer disputes with potential litigation risk. (8) Regulatory
compliance questions. When legal issues surface, pause and route: "This needs legal review. Let's document our business
objectives and route to legal for guidance on how to proceed." Don't have legal discussions without legal counsel —
well-meaning interpretations can create liability.
""".strip())

_add("risk_routing", "routing_rule", """
Product and Roadmap Decision Authority: Product decisions require appropriate stakeholder involvement. Feature
prioritization: product manager authority with engineering feasibility input. Architecture decisions: engineering
lead authority with product alignment. Deprecation or removal: product + engineering + customer success alignment
needed. New product lines: executive decision with cross-functional input. Breaking changes: product decision with
customer impact assessment. In meetings, if product direction questions emerge beyond participants' scope, document
as input: "We recommend [X] for product roadmap consideration." Don't commit to features or timelines without product
owner approval. Distinguish requests (we'd like this) from commitments (we will deliver this).
""".strip())

_add("risk_routing", "routing_rule", """
Cross-Functional Dependency Escalations: When meeting decisions create dependencies on other teams, escalate
appropriately. Escalation needed when decision requires: (1) Engineering resources from another team. (2) Budget
reallocation across departments. (3) Timeline changes affecting downstream teams. (4) Shared infrastructure modifications.
(5) Cross-team process changes. (6) Coordination of launches or releases. Document the dependency explicitly: "This
decision creates a dependency on [team] for [deliverable] by [date]." Route to appropriate coordination level
(team leads, program managers, or executives depending on scope). Don't assume other teams will accommodate —
confirm before committing.
""".strip())

_add("risk_routing", "risk_pattern", """
Audit and Compliance Documentation Requirements: Some discussions trigger documentation requirements for audits or
compliance. Documentation-triggering topics: (1) Financial decisions (expense approvals, budget changes, revenue
recognition). (2) Security decisions (access grants, architecture changes). (3) Privacy decisions (data usage, retention
policies). (4) Quality decisions (testing waivers, release criteria exceptions). (5) Vendor selections (procurement
process, evaluation criteria). When these topics arise, ensure meeting notes include: decision made, rationale, approvers,
date, alternatives considered, risks acknowledged. Use formal decision log for critical items. This documentation may
be required for SOX compliance, SOC 2 audits, ISO certifications, or regulatory examinations.
""".strip())

_add("risk_routing", "routing_rule", """
Vendor and Procurement Decision Authority: Vendor decisions follow procurement processes and authority levels.
Vendor selection: typically requires formal evaluation process for spend over threshold (e.g., $25K+). Contract
negotiations: legal and procurement involvement required. Emergency vendor engagements: may have expedited approval
but still need documentation. Vendor termination: requires cross-functional input (legal, finance, operations). In
meetings, if vendor discussions emerge: "For vendor selection we need to follow procurement process. Let's document
requirements and route to procurement." Don't informally commit to vendors — this can create legal obligations or
expectation issues.
""".strip())

_add("risk_routing", "routing_rule", """
Reputational Risk Escalations: Some situations carry reputational risk requiring communications/PR involvement.
Escalate when: (1) Customer issues may become public or social media concerns. (2) Employee issues may leak or
affect employer brand. (3) Product problems could affect brand perception. (4) Partnership issues involve well-known
brands. (5) Decisions touch controversial or politically sensitive topics. (6) Media or analyst inquiries. (7) Executive
or company social media responses needed. When reputational risk appears, involve communications team: "This could
become public. Let's route to communications for guidance on messaging and stakeholder management." Don't make external
statements without communications approval.
""".strip())


# ---------------------------------------------------------------------------
# Domain 3: action_items (10 chunks)
# ---------------------------------------------------------------------------

_add("action_items", "action_extraction", """
Effective Action Item Capture During Meetings: Action items are commitments to specific work after the meeting.
Every action item needs four components: (1) Clear verb-driven task: "Schedule Q2 planning meeting" not "Q2 planning."
(2) Single owner (one name, not a group). (3) Due date (specific date, not "soon"). (4) Success criteria or deliverable:
"Draft proposal" vs "Send final proposal." Capture action items visibly during meeting — shared doc or project board.
Use consistent format: "WHO will do WHAT by WHEN." Confirm action items verbally before ending meeting: read list,
verify owners, confirm dates. Distinguish action items (tasks to complete) from decisions (conclusions reached) from
discussion points (topics covered).
""".strip())

_add("action_items", "action_extraction", """
Patterns for Identifying Action Items in Conversation: Recognize language patterns that signal action items. Explicit
commitments: "I'll handle that," "I can do that by Friday," "Let me follow up." Implicit assignments: "Someone needs
to contact legal" (facilitator should assign: "Sarah, can you contact legal?"). Questions requiring follow-up: "What's
our current budget?" may require "Alex will pull current budget numbers by Tuesday." Parking lot items that need
resolution: "We tabled the timeline discussion" becomes "Team leads will propose timeline options by Thursday meeting."
Dependencies: "We can't proceed until X" becomes "Owner will resolve X by date." Watch for voluntary commitments vs
assignments — confirm: "Sarah, I heard you volunteer to own this — is that correct?"
""".strip())

_add("action_items", "action_extraction", """
Action Item Prioritization Framework: Not all action items are equally urgent. Use a prioritization matrix:
P0 (Critical): Blocking others' work, customer-facing deadline, compliance requirement. Due immediately or within
days. P1 (High): Important but not blocking, strategic initiative, leadership request. Due within 1-2 weeks. P2
(Medium): Valuable but not time-sensitive, process improvements, nice-to-haves. Due within month. P3 (Low): Backlog
items, future considerations, optional improvements. No specific deadline. In meeting notes, mark priority levels
explicitly. For P0 items, confirm owners have capacity — if not, escalate resource constraint immediately. Review
lower priority items quarterly and remove ones no longer relevant.
""".strip())

_add("action_items", "workflow_automation", """
Action Item Tracking Systems: Effective tracking prevents dropped commitments. Options: (1) Project management tools
(Asana, Monday, Jira) — best for teams with existing PM processes. (2) Shared spreadsheets — simple, accessible, low
overhead. (3) Email follow-ups — works for small teams but scales poorly. (4) Meeting notes in docs — requires
discipline to review. (5) Dedicated action item trackers. Key tracking elements: status (not started, in progress,
blocked, complete), owner, due date, origin meeting, priority, dependencies. Weekly review process: owners update
status, blockers are escalated, completed items archived. Past-due items need explicit action: extend deadline,
reprioritize, or cancel. Accountability mechanism: standing agenda item in recurring meetings to review action items.
""".strip())

_add("action_items", "action_extraction", """
Action Items from Asynchronous Meetings (Email, Chat, Documents): Action items don't only come from synchronous
meetings. Email thread patterns: someone asks a question requiring work, someone volunteers to research, decisions
are made that require implementation. Chat discussion patterns: problems identified that need solutions, requests
for information that require gathering, commitments to tasks. Document collaboration patterns: comments requiring
response, suggested changes needing implementation, open questions needing resolution. For async action items:
(1) Extract them explicitly — don't assume they'll happen. (2) Document in shared location, not just in email/chat.
(3) Confirm owner and deadline: "Sarah, I captured an action for you to finalize the budget by Friday — does that
work?" (4) Close the loop when completed.
""".strip())

_add("action_items", "workflow_automation", """
Managing Blocked Action Items: Blocked items require active management, not passive waiting. When item becomes
blocked, document: (1) What is blocking it (dependency, missing information, external party, resource constraint).
(2) Who/what needs to resolve the blocker. (3) Expected resolution timeline. (4) Escalation path if blocker persists.
Create a separate action item to resolve the blocker: "Jane will follow up with Legal by Wednesday to unblock contract
review." If blocker will persist beyond due date, proactively adjust: extend deadline and communicate to stakeholders,
or find alternative approach. Review blocked items in weekly status meetings. Escalate blockers that persist over 2
weeks — persistent blockers often indicate scope, prioritization, or resourcing issues.
""".strip())

_add("action_items", "action_extraction", """
When to Say No to Action Items: Not every suggested action should become a commitment. Decline or pushback when:
(1) Action doesn't align with team goals or priorities. (2) Owner doesn't have capacity given existing commitments.
(3) Action duplicates existing work. (4) Outcome isn't clearly valuable. (5) Action should belong to different team
or person. (6) Prerequisites aren't in place. How to decline: "I don't have capacity for this within the requested
timeline. Can we reprioritize, extend the deadline, or find another owner?" Alternative to declining: negotiate scope:
"I can do a lightweight version by Friday, or comprehensive version in two weeks — which adds more value?"
""".strip())

_add("action_items", "action_extraction", """
Follow-Up Action Items After Meetings: Post-meeting action items often emerge from meeting content. Common patterns:
(1) Distribute meeting notes/recording — standard after every meeting with decisions. (2) Follow up with stakeholders
not present — when decisions affect absent parties. (3) Create detailed documentation — when meeting made high-level
decisions needing specification. (4) Schedule follow-up meetings — when discussion needs continuation. (5) Update
related documentation or systems — when decisions change existing processes. (6) Communicate decisions downstream —
when decisions affect other teams. Assign these explicitly before ending meeting: "Who will distribute notes? By when?"
Don't let implicit post-meeting work fall through cracks.
""".strip())

_add("action_items", "workflow_automation", """
Action Item Accountability Practices: Accountability prevents action items from being forgotten. Practices:
(1) Single owner per item (groups don't have accountability). (2) Public commitment in meeting (social contract).
(3) Written documentation (can't claim "I didn't know"). (4) Regular status reviews (standing meeting agenda item).
(5) Completion confirmation (owner marks complete AND stakeholders verify). (6) Transparent status (everyone can see
status of all items). When items go past due: (1) Owner proactively communicates delay with new date. (2) Facilitator
follows up: "I see this item is past due — what's the status?" (3) Escalate persistent delays to leadership if they
affect critical work. Create culture where missing deadlines requires explanation, but proactive communication of
delays is normalized and appreciated.
""".strip())

_add("action_items", "action_extraction", """
Distinguishing Action Items from Information Sharing: Not everything that happens in meetings requires follow-up
action. Information sharing: "Revenue last month was $50K" — no action needed unless someone commits to analyzing it.
Decisions: "We decided to use vendor A" — may or may not need action items depending on implementation. Discussions:
"We discussed pricing strategy" — often needs action items to be valuable (research, proposals, decisions). Questions
answered in meeting: "What's our timeline?" answered with "March 1" — no action needed. Questions requiring follow-up:
"What's our timeline?" answered with "I'll need to check and get back to you" — becomes action item. A good meeting
has clear separation: "Here's what we discussed [information], here's what we decided [decisions], here's what we
committed to do [action items]."
""".strip())


# ---------------------------------------------------------------------------
# Domain 4: calendar_optimization (12 chunks)
# ---------------------------------------------------------------------------

_add("calendar_optimization", "scheduling_rule", """
Intelligent Meeting Scheduling Principles: Respect participants' time and energy. Best practices: (1) Avoid early
morning (before 9am) and late afternoon (after 4pm) for most participants. (2) Respect lunch hours (12-1pm). (3) Don't
schedule back-to-back meetings — leave 5-10 minute buffer. (4) Cluster meetings on certain days, protect focus time on
others. (5) Consider time zones: 9am Pacific is noon Eastern. Use tools that show multiple time zones. (6) Avoid Mondays
for strategic discussions (people catching up from weekend). (7) Avoid Fridays after 3pm (people mentally checked out).
(8) Recurring meetings should be same day/time for predictability. (9) Send invites at least 24 hours in advance for
non-urgent meetings.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Resolving Meeting Conflicts: When calendar conflicts arise, use triage criteria. Priority factors: (1) Meeting with
external parties trumps internal meetings (customers, vendors, partners). (2) Decision-making meetings trump information
sharing meetings. (3) Meetings with limited availability participants (executives, cross-timezone) trump flexible
participants. (4) Scheduled meetings trump tentative holds. (5) Smaller meetings easier to reschedule than large meetings.
For recurring conflicts, find permanent solution: move one recurring meeting to different day/time. When declining due
to conflict, propose alternatives: "I have a conflict. Can we do Tuesday at 2pm instead?" For critical conflicts, suggest
delegate: "I can't attend but Sarah can represent our team."
""".strip())

_add("calendar_optimization", "scheduling_rule", """
Optimal Meeting Lengths by Purpose: Match meeting length to purpose. 15 minutes: Quick sync, single decision, status
update. 30 minutes (actually 25): Standard meeting length, 1-2 topic discussion, routine check-ins. 60 minutes (actually
50): Multiple topics, decision-making, brainstorming, training. 90 minutes: Strategic planning, workshops, complex
problem-solving (include break at 45min). 2+ hours: Workshops, offsites, immersive sessions (break every 60-90 minutes).
Don't default to 30 or 60 minutes — choose based on actual need. If meeting regularly ends early, shorten it permanently.
If meeting regularly runs over, either lengthen it or split into multiple meetings. Time is money — a 1-hour meeting with
10 people costs 10 person-hours.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Protecting Focus Time on Calendar: Focus time is essential for deep work. Strategies: (1) Block focus time on calendar
as "busy" so others can't schedule over it. (2) Make focus blocks recurring (e.g., every morning 8-10am, or all day
Wednesdays). (3) Protect at least 2-3 hour blocks — focus work needs sustained attention. (4) Schedule focus time for
your most productive hours (morning for most people). (5) Turn off notifications during focus time. (6) Communicate
focus time norms to team: "I block mornings for deep work and respond to messages after 1pm." (7) Book focus time in
advance — if you wait for "free time" it never appears. (8) Treat focus time as important as meetings — don't break
it without good reason.
""".strip())

_add("calendar_optimization", "scheduling_rule", """
Time Zone Management for Distributed Teams: Global teams require careful scheduling. Practices: (1) Use world clock
tools to visualize overlap. (2) Rotate meeting times to share inconvenience — if one person always takes late night
calls, alternate with early morning for others. (3) Record meetings for those who can't attend live. (4) Use async
alternatives when possible. (5) Create regional meetings for most topics, all-hands only for critical items. (6) Publish
team timezone map with working hours: "Alex: US Pacific 9am-5pm, Jordan: UK GMT 9am-5pm." (7) Use tools that show time
zones in invites: "2pm Pacific / 5pm Eastern / 10pm GMT." (8) Respect work-life boundaries — don't schedule someone's
7am or 9pm without checking first. (9) Consider "golden hours" when most people overlap.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Double-Booking and Overbooking Management: Sometimes double-booking is necessary, but requires active management.
When double-booked: (1) Decide which meeting is priority based on criteria (criticality, attendance requirements,
rescheduling difficulty). (2) Decline or propose alternative for lower priority meeting. (3) If both critical, see if
either can shift time. (4) As last resort, split time between meetings or send delegate. Don't ghost on meetings —
communicate your choice. For intentional overbooking (tentative on multiple options), finalize quickly to respect
organizers' time. If habitually overbooked, root cause is scheduling problems: too many commitments, insufficient
delegation, poor meeting hygiene, need to decline more invitations.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Meeting-Free Time Policies: Organizations benefit from coordination on meeting-free time. Common policies: (1) No-meeting
Fridays: entire company avoids meetings on Fridays for focus work. (2) Core collaboration hours: 10am-3pm meetings allowed,
morning/late afternoon protected. (3) Meeting-free weeks: quarterly or during key periods (planning, year-end). (4) No
recurring meetings in first/last week of month. (5) Lunch hour protection: 12-1pm always free. (6) No meetings before
10am or after 4pm. (7) 25/50 minute default instead of 30/60 to build in breaks. These policies only work with leadership
support and cultural enforcement. Benefits: reduced meeting fatigue, increased productivity, better work-life balance,
protected deep work time.
""".strip())

_add("calendar_optimization", "scheduling_rule", """
Recurring Meeting Optimization: Recurring meetings accumulate and often outlive their usefulness. Quarterly audit
questions: (1) Is this meeting still necessary or has its purpose been accomplished? (2) Are the right people invited
or has the team changed? (3) Is the frequency right or should it be weekly/biweekly/monthly instead? (4) Is the time
slot still optimal? (5) Could this be async instead (email update, shared doc)? (6) Are we using the time well or do
meetings often end early/run over? Cancel recurring meetings that: no longer serve a purpose, have low attendance,
regularly get rescheduled, could be async, duplicate other meetings. Update recurring meetings when team or needs
change. Establish expectation: recurring meetings aren't permanent, they're re-evaluated regularly.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Managing Executive Calendar Access: Executives' time is high-value and requires gatekeeping. Practices if you manage
executive calendar: (1) Clarify priorities with executive regularly. (2) Default decline to protect time, require
requestor to justify importance. (3) Batch similar meetings (1:1s on Tuesdays, external meetings on Thursdays). (4)
Protect strategic thinking time — block it as unavailable. (5) Build in buffer time around meetings for prep and
transition. (6) Limit meeting length — executives can often accomplish in 15 minutes what takes others 30. (7) Decline
or delegate meetings where executive presence isn't essential. (8) Schedule breaks and think time. (9) Allow emergency
overrides but require escalation. Managing up: if requesting executive time, be specific about why they're needed,
what decision/input is required, and what pre-work you've done.
""".strip())

_add("calendar_optimization", "scheduling_rule", """
Calendar Sharing and Visibility Practices: Calendar transparency helps scheduling but requires boundaries. Options:
(1) Full transparency: everyone sees all meeting details (good for small teams, tight collaboration). (2) Free/busy
only: shows time blocks but not meeting details (balances privacy and schedulability). (3) Limited sharing: share with
team but not entire org. (4) Private events: block time without revealing purpose (medical appointments, personal time).
Best practices: (1) Share working hours calendar so people know your availability. (2) Mark true out-of-office (vacation,
sick) clearly. (3) Block personal appointments as busy but private. (4) Update calendar promptly when plans change.
(5) For interview/sensitive meetings, use generic labels ("meeting") rather than details.
""".strip())

_add("calendar_optimization", "calendar_strategy", """
Personal Time and Work-Life Boundaries on Calendar: Protect personal time proactively. Techniques: (1) Block personal
commitments (kids' activities, workouts, appointments) on work calendar so they're not scheduled over. (2) Set hard start
and end times for work day and enforce them. (3) Block lunch and actually take it. (4) Schedule "travel time" if you
have in-person meetings in different locations. (5) Build in buffer time between intense meetings. (6) Block end-of-day
wrap-up time for email and planning. (7) Schedule vacation on calendar far in advance to prevent it being scheduled over.
(8) Use "Out of office" status when truly unavailable. Don't martyr yourself by leaving calendar completely open —
boundaries are healthy and prevent burnout.
""".strip())

_add("calendar_optimization", "scheduling_rule", """
Meeting Invitation Best Practices: Good meeting invites set expectations and improve attendance. Include: (1) Clear
subject line: "Q1 Planning — DECISION MEETING" not just "Meeting." (2) Purpose statement: 1-2 sentences on why this
meeting and what success looks like. (3) Agenda: even high-level topics list. (4) Pre-work if any: "Please review budget
doc before meeting." (5) Meeting link/location: don't make people search for Zoom link. (6) Time zone clarity for
distributed teams. (7) Correct attendee designations: required vs optional. (8) Calendar holds should be tentative not
busy until confirmed. (9) For recurring meetings, include agenda as recurring calendar description. (10) Update invite
if agenda or attendees change. Send invites with enough notice: 24 hours minimum, 1 week for important meetings, 2-4
weeks for large group meetings.
""".strip())


# ---------------------------------------------------------------------------
# Domain 5: post_meeting_workflows (10 chunks)
# ---------------------------------------------------------------------------

_add("post_meeting_workflows", "transcript_analysis", """
Effective Meeting Summary Structure: A good meeting summary enables action and provides record. Essential sections:
(1) Meeting metadata: date, attendees, purpose. (2) Key decisions: what was decided, by whom, rationale. (3) Action
items: who, what, by when (detailed list). (4) Discussion highlights: major topics covered, options considered.
(5) Parking lot items: topics tabled for future discussion. (6) Next steps: upcoming meetings or milestones. (7) Links
to related documents or resources. Optional sections: (8) Risks or concerns raised. (9) Blockers needing escalation.
(10) Metrics or data shared. Keep summaries concise — aim for 1 page or less for most meetings. Use clear formatting:
bullets, headers, bold for emphasis. Distribute within 24 hours while meeting is fresh. Store in accessible location:
shared drive, wiki, project management tool.
""".strip())

_add("post_meeting_workflows", "follow_up_template", """
Meeting Follow-Up Communication: Follow-up ensures meeting leads to action. Standard follow-ups: (1) Distribute
meeting notes to all attendees (within 24 hours). (2) Send action items to owners individually with due dates. (3)
Communicate decisions to stakeholders not in meeting but affected by outcomes. (4) Update relevant documentation or
systems to reflect decisions. (5) Schedule follow-up meetings if needed. (6) File meeting notes in shared repository.
(7) Create tasks in project management system. For important decisions, consider: (8) Announce to wider team via email
or Slack. (9) Update roadmaps or strategy docs. (10) Add to FAQ if decision addresses common questions. For meetings
with external parties, send thank you and summary to external attendees within same day.
""".strip())

_add("post_meeting_workflows", "routing_rule", """
Routing Information to Right Stakeholders: Not everyone needs full meeting notes — route information appropriately.
For executives: high-level decisions, escalations, risks, strategic implications (1 paragraph or less). For project
teams: detailed action items, decisions affecting their work, resource implications. For cross-functional partners:
decisions that create dependencies or affect their roadmap. For broader team: major decisions affecting processes,
organizational announcements, strategic shifts. For customers/partners: decisions that affect them directly, next
steps in engagement. Use different communication channels by stakeholder: executives might get Slack summary, project
teams get full notes, broader org gets announcement in all-hands. Don't create information overload — send only what's
relevant to each audience.
""".strip())

_add("post_meeting_workflows", "transcript_analysis", """
Meeting Recording and Transcript Management: Recordings supplement but don't replace written notes. Recording best
practices: (1) Announce at meeting start that it's being recorded. (2) Store in accessible location with clear naming:
"2024-03-15_Q1Planning_ProductTeam.mp4". (3) Set retention policy (e.g., delete after 90 days). (4) Restrict access
appropriately (not all meetings should be public). (5) Provide timestamp markers for key moments. (6) Enable auto-transcription
for searchability. (7) Extract and document decisions/actions in written form — don't rely on people watching recording.
(8) For sensitive discussions, consider not recording or deleting after notes are finalized. Recordings are useful for:
people who couldn't attend, clarifying what was said, reviewing complex technical discussions. But written summaries
are still primary artifact.
""".strip())

_add("post_meeting_workflows", "follow_up_template", """
Converting Decisions to Artifacts: Meeting decisions should update canonical documentation. Decision type → Artifact
update: Product decisions → Update roadmap, feature specs, or backlog. Process decisions → Update team wiki, handbook,
or runbooks. Technical decisions → Update architecture docs, tech specs, or decision logs. Resource decisions → Update
staffing plans, budget spreadsheets. Strategic decisions → Update strategy docs, OKRs, or planning documents. Policy
decisions → Update employee handbook, compliance docs. Without artifact updates, decisions live only in meeting notes
and get lost. Assign responsibility: "After this decision, Alex will update the roadmap by EOW." Review quarterly:
are our docs reflecting our actual decisions and practices?
""".strip())

_add("post_meeting_workflows", "routing_rule", """
Escalation Routing After Meetings: When meetings surface issues needing escalation, route them promptly and clearly.
Escalation components: (1) Situation summary: what's the issue? (2) Impact: why does it matter? (3) Options: what are
possible solutions? (4) Recommendation: what do you suggest? (5) Urgency: when does this need decision? (6) Context:
any relevant background or history. Route via appropriate channel: Urgent escalations (blocking, customer-facing):
direct message to decision-maker, followed by email. Important but not urgent: email with clear subject line and request
for decision by [date]. FYI escalations: include in weekly summary email or next standing meeting. For cross-functional
escalations, cc relevant stakeholders. Follow up if you don't receive response within expected timeframe.
""".strip())

_add("post_meeting_workflows", "transcript_analysis", """
Categorizing Meeting Notes for Searchability: Structured categorization makes notes findable later. Categorization
dimensions: (1) Meeting type: planning, decision, brainstorm, status, retrospective, all-hands. (2) Team/project: tag
with relevant team or project name. (3) Date: YYYY-MM-DD format for sorting. (4) Topics/tags: keyword tags for major
themes discussed. (5) Attendees: particularly key stakeholders. (6) Related documents: links to specs, proposals,
artifacts. Use consistent naming conventions: "[Date]_[MeetingType]_[Team]_[TopicKeyword].doc". Store in logical folder
structure: by team, by project, or by date depending on access patterns. For wikis or shared drives, use tagging systems.
Goal: someone searching for past decisions should be able to find them in under 2 minutes.
""".strip())

_add("post_meeting_workflows", "follow_up_template", """
Post-Meeting Survey and Feedback: For recurring or important meetings, gather feedback to improve. Simple post-meeting
questions: (1) Did we accomplish the meeting objective? (Yes/No) (2) Was your time well-spent? (1-5 scale) (3) What
should we start/stop/continue doing? (Open text) (4) How could this meeting be improved? (Open text). Keep surveys short
(2-3 questions) or people won't complete them. Review feedback regularly and act on it: "Based on feedback, we're
shortening this meeting to 25 minutes and sending agenda 24 hours in advance." Close the loop: tell participants what
changed based on their feedback. For major meetings (offsites, all-hands), more detailed retrospectives are valuable.
Feedback culture improves meeting quality over time.
""".strip())

_add("post_meeting_workflows", "routing_rule", """
Cross-Team Communication After Meetings: When meetings affect multiple teams, communication requires coordination.
Communication plan: (1) Identify all affected teams/stakeholders. (2) Determine what each needs to know. (3) Choose
appropriate channel: email for detailed decisions, Slack for announcements, dedicated meetings for complex changes.
(4) Sequence communication: inform leadership first, then individual teams, then broader org. (5) Prepare for questions:
FAQs, office hours, or Q&A sessions. (6) Assign communication owners for each stakeholder group. For significant changes,
consider: cascade briefings (brief managers who brief their teams), town halls, or video announcements. Track that
communication happened — check that each stakeholder group was reached. Follow up with teams who have questions or pushback.
""".strip())

_add("post_meeting_workflows", "follow_up_template", """
Closing the Loop on Action Items: Action items only create value if they're completed and verified. Completion workflow:
(1) Owner completes work and marks action item as done. (2) Owner notifies stakeholders: "I completed [action]. Here's
the outcome: [deliverable/link]." (3) Stakeholders verify completion meets expectations. (4) Action item is archived
or removed from active tracking. For critical action items, schedule explicit review: "We'll review this at next week's
meeting to confirm it's done." If action item can't be completed by deadline, owner proactively communicates: "This will
be delayed until [new date] because [reason]. Let me know if that's a problem." Don't let action items silently fail —
accountability requires closure. Review completed action items periodically to confirm they actually happened and had
intended effect.
""".strip())


# ---------------------------------------------------------------------------
# Domain 6: meeting_intelligence (8 chunks)
# ---------------------------------------------------------------------------

_add("meeting_intelligence", "meeting_insight", """
Meeting Metrics and Analytics: Track meeting effectiveness with data. Key metrics: (1) Meeting load: hours per week
per person in meetings. Industry benchmark: 20-30% for individual contributors, 50-70% for managers. (2) Meeting size:
average attendees per meeting. Smaller is usually better — over 7 people reduces participation. (3) On-time start rate:
percentage of meetings starting on time. Target: 90%+. (4) Meeting length vs scheduled: are meetings using full time
or ending early? If consistently ending early, shorten them. (5) Acceptance rate: percentage of invitations accepted
vs declined. Low acceptance may indicate poor relevance or scheduling. (6) Action item completion rate: percentage
completed by due date. Target: 80%+. (7) Meeting cancellation rate: high rate may indicate poor planning or shifting
priorities. Review these metrics quarterly to identify improvement opportunities.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Identifying Recurring Meeting Problems: Patterns signal systemic issues. Common patterns: (1) Meetings regularly run
over time: insufficient time allocated, poor facilitation, or unclear objectives. Solution: shorten agenda, better
time management, or split into multiple meetings. (2) Same topics repeatedly discussed without resolution: decision
authority unclear or underlying disagreement. Solution: clarify who decides, surface the underlying conflict. (3) Low
participation: wrong people invited, topics not relevant, psychological safety issues. Solution: audit attendee list,
improve engagement techniques, address team dynamics. (4) Meetings frequently rescheduled: attendees overcommitted or
poor initial scheduling. Solution: better scheduling practices, reduce attendee list. (5) Action items not completed:
unrealistic commitments, accountability gaps, or capacity issues. Solution: better prioritization, capacity assessment,
stronger accountability.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Strategic Insights from Meeting Patterns: Aggregate meeting data reveals organizational dynamics. Analysis questions:
(1) Where is time being spent? If product team has 30 hours/week of meetings, that limits execution time. (2) Who are
the bottlenecks? If one person is required for 20+ meetings/week, they can't go deep on anything. (3) What topics
consume the most meeting time? Frequent budget discussions might signal resource constraints. Frequent technical
troubleshooting might signal technical debt. (4) Are cross-functional meetings increasing? May indicate growing
interdependence or lack of clear ownership. (5) How much time is spent on forward-looking (planning, strategy) vs
reactive (troubleshooting, status updates)? (6) Are meeting patterns aligned with stated priorities? If strategy says
"customer focus" but no customer meetings, there's misalignment.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Meeting ROI Analysis: Calculate whether meetings are worth the investment. Simple ROI formula: (Meeting duration hours
× Number of attendees × Weighted average hourly cost) = Meeting cost. Then assess: What value was created? For decision
meetings: value of decision made, cost of delay if decision postponed. For planning meetings: value of aligned execution,
cost of misalignment. For information-sharing meetings: value rarely justifies cost — consider async alternatives.
For brainstorming meetings: value of ideas generated and implemented. High-cost meetings (many attendees, senior people,
long duration) need to create proportional value. If 10 people at $100/hour average spend 2 hours in meeting, that's
$2000 investment. Did it create $2000+ of value? If not, redesign or eliminate the meeting.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Meeting Culture Indicators: Meeting behaviors reveal organizational culture. Culture signals: (1) Do meetings start
on time? Indicates respect for time and punctuality norms. (2) Do people multitask (email during meetings)? Indicates
engagement issues or meeting overload. (3) Do junior people speak up freely? Indicates psychological safety and inclusive
culture. (4) Are meetings canceled when objectives are met early? Indicates results focus vs time-filling. (5) Are
decisions documented and followed? Indicates accountability culture. (6) Do leaders regularly decline meetings? Indicates
healthy boundaries and delegation. (7) Are recurring meetings regularly re-evaluated? Indicates continuous improvement
mindset. (8) Is it acceptable to decline meeting invitations? Indicates trust and autonomy. Use meeting culture as a
leverage point for broader cultural change.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Meeting Investment by Function: Analyze meeting time distribution across different work types. Categories: (1) Strategic
meetings: planning, roadmapping, vision-setting. (2) Tactical meetings: project status, sprint planning, coordination.
(3) Operational meetings: 1:1s, team syncs, standup. (4) Problem-solving meetings: troubleshooting, retrospectives,
incident reviews. (5) Relationship meetings: customer calls, partner discussions, team building. Healthy distribution
varies by role and company stage. Early-stage companies need more strategic and problem-solving meetings. Growth-stage
companies need more tactical and operational meetings. Mature companies need balance with renewed strategic focus. If
all meetings are tactical/operational, strategy suffers. If all meetings are strategic, execution suffers. Track distribution
quarterly and adjust intentionally.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Participant Engagement Analytics: Measure participation to identify inclusion issues. Metrics: (1) Speaking time distribution:
in 60-minute meeting with 6 people, balanced would be ~10 minutes each. Reality often: 2 people speak 30 minutes each,
4 people speak 5 minutes total. (2) Number of contributions: who asks questions, makes suggestions, voices concerns?
(3) Patterns over time: does same person always dominate or stay silent? (4) Response rates: when direct questions asked,
who responds and who doesn't? (5) Video usage: who keeps camera on vs off? (Can indicate engagement but also consider
privacy/bandwidth). Use this data to: (1) Ensure diverse voices are heard. (2) Coach dominant participants to create
space. (3) Actively invite quiet participants. (4) Identify possible team dynamic issues. (5) Recognize when meeting
size is too large for participation.
""".strip())

_add("meeting_intelligence", "meeting_insight", """
Meeting Load and Productivity Correlation: More meetings don't equal more productivity — often the opposite. Research
shows: (1) Optimal meeting load: 20-30% of time for individual contributors (1-2 days/week). Beyond this, deep work
suffers. (2) Manager meeting load: 50-70% is typical but should include meaningful 1:1s and strategic discussions, not
just status updates. (3) Meeting-free time: individuals with at least one meeting-free day per week report higher
productivity and lower burnout. (4) Context switching cost: back-to-back meetings prevent processing time and deep
work. (5) Large meeting effect: meetings with 8+ people significantly reduce individual participation and engagement.
Monitor meeting load by individual and team. When load exceeds healthy thresholds, interventions: decline non-essential
meetings, move to async, delegate attendance, cancel recurring meetings no longer needed.
""".strip())


# =============================================================================
# Seeding Pipeline
# =============================================================================

async def seed_knowledge():
    """Embed and insert all conference knowledge chunks."""
    from aspire_orchestrator.services.legal_embedding_service import embed_batch, compute_content_hash
    from aspire_orchestrator.services.supabase_client import supabase_insert

    total = len(CONFERENCE_KNOWLEDGE)
    logger.info("Seeding %d conference knowledge chunks...", total)

    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = CONFERENCE_KNOWLEDGE[i:i + batch_size]
        texts = [c["content"] for c in batch]

        try:
            # Use system suite UUID for global knowledge (receipts table requires UUID, not NULL)
            embeddings = await embed_batch(texts, suite_id="00000000-0000-0000-0000-000000000000")
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
            await supabase_insert("conference_knowledge_chunks", rows)
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
