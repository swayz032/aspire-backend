<!-- domain: compliance_risk, subdomain: risk_assessment, chunk_strategy: heading_split -->

# Contract Risk Assessment

## Risk Scoring Methodology

### Overview
Clara assesses contract risk using a multi-factor scoring model that evaluates five dimensions. Each dimension receives a score of LOW, MEDIUM, or HIGH risk. The overall contract risk score is determined by the highest individual dimension score (fail-closed approach, per Aspire Law #3).

### Scoring Rules
- **If ANY dimension is HIGH:** Overall risk is HIGH — recommend attorney review
- **If ANY dimension is MEDIUM and none are HIGH:** Overall risk is MEDIUM — flag specific concerns to user
- **If ALL dimensions are LOW:** Overall risk is LOW — proceed with standard contract generation

## Dimension 1: Financial Exposure

### LOW Risk
- Contract value under $5,000
- Standard payment terms (Net 30)
- No personal guarantee
- Liability capped at contract value
- No liquidated damages

### MEDIUM Risk
- Contract value $5,000-$50,000
- Extended payment terms (Net 60+)
- Liability cap at reasonable multiple of contract value
- Moderate liquidated damages (proportional to likely actual loss)
- Security deposit required

### HIGH Risk
- Contract value over $50,000
- No liability cap or unlimited liability
- Personal guarantee required
- Disproportionate liquidated damages
- Significant financial penalties for early termination
- Payment obligations survive termination for extended period
- Total financial exposure exceeds 10% of the business's annual revenue

## Dimension 2: Legal Complexity

### LOW Risk
- Simple bilateral agreement (two parties)
- Standard clauses with well-established meanings
- Single jurisdiction
- No regulatory compliance requirements beyond standard
- Template-based contract with minimal customization

### MEDIUM Risk
- Multiple parties involved
- Custom or non-standard clauses requiring interpretation
- Cross-jurisdictional issues (parties in different states)
- Industry-specific regulatory requirements (construction licensing, professional standards)
- Significant modifications to standard template

### HIGH Risk
- Multi-jurisdictional or international elements
- Complex corporate structures (holding companies, special purpose entities)
- Regulatory filings required (SEC, FTC, state agencies)
- Tax implications requiring professional analysis
- Merger/acquisition related agreements
- Real estate transactions over $500,000
- Securities or investment-related terms
- Employment disputes or settlement agreements

## Dimension 3: Regulatory Risk

### LOW Risk
- No industry-specific regulations apply
- Standard consumer/commercial transaction
- No data privacy concerns (no personal information handled)
- No government contracting requirements

### MEDIUM Risk
- State licensing requirements apply (contractor, professional)
- State-specific contract requirements apply (security deposit limits, disclosure requirements)
- Moderate data privacy considerations (handling customer contact info)
- Industry-specific standards apply (AICPA for accountants, building codes for construction)

### HIGH Risk
- Federal regulatory oversight (OSHA, EPA, EEOC, DOL)
- Healthcare data (HIPAA)
- Financial services regulations (FinCEN, state money transmitter laws)
- Government contracting (FAR, state procurement)
- Immigration-related employment terms
- Significant data privacy obligations (CCPA/CPRA, GDPR, BIPA)
- Environmental liability (contaminated property, hazardous materials)
- Securities law implications

## Dimension 4: Counterparty Risk

### LOW Risk
- Established business entity with verifiable history
- Good credit and payment history
- Local business with physical presence
- Prior successful business relationship

### MEDIUM Risk
- New business entity (less than 2 years old)
- No prior relationship
- Out-of-state counterparty
- Limited online presence or verifiable history
- Individual (not a business entity) with significant obligations

### HIGH Risk
- Counterparty has history of disputes or litigation
- Counterparty refuses to provide basic identification or references
- International counterparty with no domestic presence
- Counterparty insists on unusual payment terms (cash only, cryptocurrency, wire to foreign account)
- Counterparty under regulatory investigation or sanctions
- Significant power imbalance (large corporation vs. sole proprietor on take-it-or-leave-it terms)

## Dimension 5: Duration and Commitment

### LOW Risk
- Single project with defined end date
- Term under 6 months
- Easy termination (30 days notice for convenience)
- No auto-renewal

### MEDIUM Risk
- Term 6 months to 2 years
- Annual auto-renewal with 60+ day cancellation window
- Moderate early termination penalty (1-2 months' fees)
- Rolling scope (ongoing services)

### HIGH Risk
- Term over 2 years with no exit provisions
- Auto-renewal with short cancellation window (< 30 days)
- Significant early termination penalty (remaining contract value)
- Lock-in provisions (exclusive dealing, non-compete, IP assignment)
- Contracts that compound over time (escalating pricing, expanding scope)

## Risk Score Output Format

Clara produces a structured risk assessment for every contract before generation:

```
RISK ASSESSMENT
===============
Financial Exposure:  [LOW/MEDIUM/HIGH] — [brief explanation]
Legal Complexity:    [LOW/MEDIUM/HIGH] — [brief explanation]
Regulatory Risk:     [LOW/MEDIUM/HIGH] — [brief explanation]
Counterparty Risk:   [LOW/MEDIUM/HIGH] — [brief explanation]
Duration/Commitment: [LOW/MEDIUM/HIGH] — [brief explanation]

OVERALL RISK: [LOW/MEDIUM/HIGH]
RECOMMENDATION: [proceed / flag concerns / attorney review]
SPECIFIC FLAGS: [list of red flag items detected]
```

## Risk Mitigation Strategies

### For HIGH Financial Exposure
1. Negotiate liability caps
2. Add insurance requirements
3. Require progress payments instead of single final payment
4. Add performance bond or payment bond (construction)
5. Negotiate personal guarantee cap and burnoff

### For HIGH Legal Complexity
1. Recommend attorney review before signing
2. Break complex agreement into simpler component agreements
3. Add clear definitions section
4. Include escalation and dispute resolution procedures
5. Add amendment process for unclear terms

### For HIGH Regulatory Risk
1. Verify all licensing and compliance requirements
2. Include compliance representations and warranties
3. Add regulatory change termination clause
4. Require insurance covering regulatory fines
5. Include indemnification for regulatory violations

### For HIGH Counterparty Risk
1. Require larger deposit or advance payment
2. Require personal guarantee or letter of credit
3. Add credit check or reference requirements
4. Shorten payment terms (Net 15 or Due on Receipt)
5. Include tighter termination provisions for cause

### For HIGH Duration Risk
1. Negotiate shorter initial term with renewal options
2. Add termination for convenience with reasonable notice
3. Include price adjustment mechanisms
4. Add performance benchmarks with termination rights
5. Calendar renewal and cancellation dates in Aspire
