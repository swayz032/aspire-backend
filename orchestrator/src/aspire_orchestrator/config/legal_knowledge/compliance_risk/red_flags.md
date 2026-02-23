<!-- domain: compliance_risk, subdomain: red_flags, chunk_strategy: heading_split -->

# Contract Red Flags

## Unlimited Liability

### Description
Contracts that contain no limitation of liability clause or explicitly state that liability is unlimited.

### Risk
The party without protection faces uncapped financial exposure. A single breach or incident could exceed the entire value of the contract, potentially threatening the business.

### What to Look For
- Complete absence of a limitation of liability clause
- Language like "shall be liable for all damages, losses, and expenses without limitation"
- Consequential damages not excluded (lost profits, business interruption)
- Indemnification without a cap

### Recommended Action
Clara should flag this as HIGH risk and recommend:
1. Add a liability cap (typically 1-2x fees paid under the contract)
2. Exclude consequential and punitive damages
3. Carve out only fraud, willful misconduct, and IP infringement from the cap
4. If counterparty refuses any cap, recommend attorney review

## One-Sided Indemnification

### Description
Only one party is required to indemnify the other, with no reciprocal obligation. The indemnifying party bears all risk of third-party claims.

### Risk
The indemnifying party could be forced to pay for claims arising from the OTHER party's negligence. Without reciprocity, one party absorbs disproportionate legal and financial risk.

### What to Look For
- Indemnification clause names only one party as indemnitor
- Broad indemnification language: "for any and all claims arising out of or related to this Agreement"
- No limitation on scope of indemnification (covers indemnitee's own negligence)
- No cap on indemnification exposure

### Recommended Action
Clara should flag this as MEDIUM-HIGH risk and recommend:
1. Push for mutual indemnification
2. Each party indemnifies the other for its own negligence, breach, or misconduct
3. Limit indemnification scope (not for the other party's negligence)
4. Cap indemnification at contract value

## Auto-Renewal Without Adequate Notice

### Description
Contract automatically renews for additional terms unless cancelled within a specific window, often with a short cancellation deadline buried in the terms.

### Risk
Business gets locked into unwanted contract extensions. Cancellation windows may be 30-60 days before renewal date — easy to miss. In some states (CA, NY, IL), auto-renewal laws require specific disclosures and consent.

### What to Look For
- "This Agreement shall automatically renew for successive [1-year] terms unless either party provides written notice of termination at least [60] days prior to the end of the then-current term"
- Short cancellation windows (30 days or less)
- Renewal at increased rates (especially if increase formula is unclear)
- Auto-renewal clause buried in boilerplate

### Recommended Action
Clara should flag this as MEDIUM risk and recommend:
1. Calendar the cancellation deadline (suggest Aspire calendar event)
2. Negotiate longer cancellation window (90 days)
3. Cap renewal price increases (CPI-based or fixed percentage)
4. In CA/NY/IL: Verify compliance with auto-renewal disclosure laws

## Non-Compete Overreach

### Description
Non-compete clause that is broader than necessary — excessive in duration, geography, or scope of restricted activities.

### Risk
Could prevent the person from earning a living in their field. May be unenforceable (varies dramatically by state — completely void in CA), but litigation to challenge it is expensive.

### What to Look For
- Duration exceeding 2 years
- Geographic scope broader than the company's actual business area
- Scope restricting ALL competing activity (not just specific competitive acts)
- Applied to low-wage workers or independent contractors
- No consideration beyond continued employment (for existing employees)
- Non-compete for an industry/role where they're generally disfavored (e.g., physicians, low-wage workers)

### Recommended Action
Clara should flag this as HIGH risk and recommend:
1. Check state-specific enforceability rules (void in CA; restricted in WA, MA, IL, CO, etc.)
2. Negotiate narrower scope (specific clients, not entire industry)
3. Negotiate shorter duration (12 months or less)
4. Negotiate geographic limitation to actual business area
5. Negotiate garden leave provision (payment during restricted period)
6. Recommend attorney review if the non-compete could significantly impact livelihood

## IP Assignment Without Compensation

### Description
Contract assigns all intellectual property rights from one party to the other without additional compensation beyond the base contract payment.

### Risk
The creating party loses all rights to their work, including any pre-existing IP incorporated into deliverables. This may include tools, frameworks, methodologies, or code libraries that have value beyond the specific project.

### What to Look For
- Broad assignment clause: "all work product, inventions, ideas, and intellectual property conceived during the term of this Agreement"
- Assignment of pre-existing IP or background IP
- No license-back provision allowing the creator to continue using their own tools/methods
- Assignment of IP created outside the scope of the project
- "Work made for hire" declaration for independent contractors (may be legally incorrect — only certain categories qualify as work for hire)

### Recommended Action
Clara should flag this as MEDIUM-HIGH risk and recommend:
1. Distinguish between project-specific deliverables (assignable) and pre-existing/background IP (retain)
2. Include license-back for pre-existing IP incorporated into deliverables
3. Limit assignment to IP "created specifically for and within the scope of this Agreement"
4. Include representation that assigned work does not infringe third-party IP
5. If broad assignment is required, ensure compensation reflects the IP value

## Personal Guarantee Confusion

### Description
Business owner signs a contract where a personal guarantee is included without clear disclosure, or where signing format creates unintended personal liability.

### Risk
Owner's personal assets (home, savings, vehicles) become exposed to business contract obligations. LLC or corporate liability protection is effectively bypassed.

### What to Look For
- Personal guarantee clause buried in contract boilerplate
- Signature block that shows individual name without entity title/designation
- "Jointly and severally" language when only one entity should be liable
- Guarantee with no cap or no expiration
- Guarantee that survives contract termination indefinitely

### Recommended Action
Clara should flag this as HIGH risk and recommend:
1. Ensure entity (LLC/Corp) is the contracting party, not the individual
2. If personal guarantee is required, negotiate a cap (limited to X months' rent or X% of contract value)
3. Negotiate expiration of guarantee after proven payment history
4. Negotiate burnoff — guarantee reduces over time as performance is demonstrated
5. Ensure signature block correctly identifies entity and signer's title

## Missing Dispute Resolution

### Description
Contract has no clause specifying how disputes will be resolved — no governing law, no venue, no arbitration/mediation provision.

### Risk
Either party can file suit in any court with jurisdiction, potentially in a distant, expensive, or unfavorable forum. Litigation costs can exceed the contract value.

### What to Look For
- Complete absence of governing law clause
- No venue or jurisdiction provision
- No arbitration or mediation clause
- No escalation process

### Recommended Action
Clara should flag this as MEDIUM risk and recommend:
1. Add governing law clause (the state where the business primarily operates)
2. Add venue/jurisdiction clause (courts in the business's county/state)
3. Consider mandatory mediation before litigation/arbitration
4. Consider arbitration for disputes under $50K (faster, cheaper than litigation)
5. Include prevailing party attorney fee provision

## Unconscionable Terms

### Description
Contract terms that are so one-sided as to be potentially unenforceable, typically involving a significant power imbalance between the parties.

### What to Look For
**Procedural unconscionability:**
- Take-it-or-leave-it terms with no ability to negotiate
- Fine print or hidden terms
- Complex legal language that obscures material obligations
- Pressure to sign immediately without review period

**Substantive unconscionability:**
- Extremely one-sided terms with no mutual obligations
- Penalties grossly disproportionate to harm
- Waiver of virtually all legal rights
- Exclusive remedy provisions that effectively provide no remedy

### Recommended Action
Clara should flag this as HIGH risk and recommend:
1. Identify specific unconscionable provisions
2. Request modification of the most problematic terms
3. If counterparty refuses all modification, recommend attorney review
4. Document the take-it-or-leave-it nature of negotiations (relevant if enforceability is later challenged)

## Waiver of Jury Trial

### Description
Contract includes a waiver of the right to a jury trial. Common in financial and technology contracts.

### Risk
Jury trials may be more favorable to small businesses in disputes with larger entities. Waiving this right limits litigation options. However, bench trials (judge only) can be faster and less expensive.

### What to Look For
- "EACH PARTY HEREBY IRREVOCABLY WAIVES ANY RIGHT TO A TRIAL BY JURY"
- Often in all-caps buried in boilerplate
- Combined with mandatory arbitration (may be cumulative)

### Recommended Action
Clara should flag this as LOW-MEDIUM risk and inform the user:
1. Explain the practical impact (judge decides instead of jury)
2. Note that this is common in commercial contracts
3. If combined with mandatory arbitration, the jury waiver is likely redundant
4. Only flag for attorney review if the contract involves a high-value dispute risk

## Liquidated Damages Overreach

### Description
Contract specifies predetermined damages for breach that far exceed the probable actual loss.

### Risk
Courts may enforce liquidated damages even if actual damages are lower, unless the amount constitutes a penalty (unenforceable in most jurisdictions).

### What to Look For
- Fixed dollar amount per day of delay (e.g., $1,000/day for a $10,000 project)
- Percentage penalties exceeding lost profit margins
- No relationship between liquidated damages and estimated actual loss
- Liquidated damages that apply to minor breaches

### Recommended Action
Clara should flag this as MEDIUM-HIGH risk and recommend:
1. Verify the liquidated damages amount is reasonable relative to estimated actual loss
2. Negotiate a cap on liquidated damages (percentage of contract value)
3. Negotiate mutual liquidated damages (both parties face consequences for delay/breach)
4. Ensure liquidated damages are the exclusive remedy (not cumulative with actual damages)
