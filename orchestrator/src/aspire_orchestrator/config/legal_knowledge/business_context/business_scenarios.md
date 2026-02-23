<!-- domain: business_context, subdomain: scenarios, chunk_strategy: heading_split -->

# Common Business Scenarios

## Hiring an Independent Contractor

### Scenario
Small business owner needs to hire a specialist for a specific project (e.g., plumber hiring an electrician for a bathroom remodel, accountant hiring a bookkeeper for tax season overflow).

### Contract Needed
Independent Contractor Agreement (trades_independent_contractor_agreement)

### Key Considerations
- **Worker classification:** Ensure the relationship genuinely qualifies as independent contractor (not employee). Key factors: contractor controls HOW work is done, uses own tools, works for multiple clients, has own business entity.
- **Scope definition:** Clearly define deliverables and project boundaries to prevent scope creep and classification risk.
- **Payment terms:** Project-based or milestone-based payment, NOT hourly salary. Invoice-based, not payroll.
- **Insurance:** Verify contractor carries their own liability insurance and workers' compensation (if they have employees).
- **IP ownership:** If contractor creates anything (designs, plans, code), explicitly assign IP to the business.
- **Tax documentation:** Collect W-9 before first payment. File 1099-NEC for payments $600+ in a calendar year.

### Risk Tier
YELLOW — creating the agreement requires user confirmation. RED if the contractor will handle financial data or have system access.

### Template Selection Logic
- If hiring for a construction trade: `trades_subcontractor_agreement` (includes lien waiver, insurance, retainage provisions)
- If hiring for professional services: `trades_independent_contractor_agreement`
- If the contractor will access confidential systems: Add `general_mutual_nda` or `general_one_way_nda`

## Signing an Office or Retail Lease

### Scenario
Business owner signing a lease for office, retail, or warehouse space.

### Contract Needed
Commercial lease (not currently in Aspire template registry — flag for attorney review)

### Key Considerations
- **Lease type:** Understand the rent structure — gross lease (landlord pays expenses), net lease (tenant pays some expenses), triple-net/NNN (tenant pays taxes, insurance, maintenance), or modified gross.
- **Personal guarantee:** Many commercial landlords require a personal guarantee from the business owner. This puts personal assets at risk if the business defaults. Negotiate to cap the guarantee amount or duration.
- **Tenant improvements (TI):** Negotiate TI allowance for buildout. Clarify ownership of improvements at lease end.
- **Assignment and subletting:** Ensure the lease permits assignment or subletting with landlord consent (not to be unreasonably withheld) — important if the business is sold or downsizes.
- **Exclusive use clause:** For retail, negotiate an exclusive use clause preventing the landlord from leasing to a competing business in the same property.
- **CAM charges:** Common Area Maintenance charges can be unpredictable. Negotiate a CAM cap or audit rights.
- **Early termination:** Negotiate a kick-out clause (right to terminate early with penalty) in case business needs change.

### Risk Tier
RED — commercial leases involve significant financial commitment and are binding. Attorney review recommended.

### Attorney Escalation
Clara should recommend attorney review for ALL commercial leases. Too many jurisdiction-specific and financial implications for automated generation.

## Client Engagement Agreement

### Scenario
Professional service provider (accountant, consultant, marketing agency) onboarding a new client.

### Contract Needed
- For accountants: `acct_engagement_letter` (required by professional standards)
- For other professionals: `trades_msa_lite` or standalone service agreement
- Consider adding: `general_one_way_nda` (client disclosing confidential information)

### Key Considerations
- **Scope specificity:** Clearly define what services ARE and ARE NOT included. "Bookkeeping" is too vague — specify "monthly bank reconciliation, accounts payable entry, and monthly financial statement preparation."
- **Fee structure:** Fixed monthly fee, hourly rate, or project-based. Specify billing frequency and payment terms.
- **Out-of-scope work:** Define the process for requesting and pricing additional services.
- **Client responsibilities:** Document what the client must provide (data, access, decisions) and the consequences of delay.
- **Professional standards disclaimer:** For CPAs, the engagement letter must reference applicable professional standards (GAAP, SSARS, etc.).
- **Limitation of liability:** Standard practice to cap liability at fees paid in the prior 12 months.
- **Termination:** Either party should be able to terminate with reasonable notice (30-60 days typical).

### Risk Tier
YELLOW — standard service agreement requiring user confirmation.

## Vendor Agreement

### Scenario
Business owner contracting with a vendor for ongoing supplies or services (e.g., plumber purchasing materials from a supplier, accountant subscribing to a software service).

### Contract Needed
Typically provided by the vendor — business owner is reviewing, not drafting.

### Key Considerations
- **Auto-renewal:** Check for automatic renewal clauses and cancellation deadlines. Many vendor agreements auto-renew for 1-year terms with a 30-60 day cancellation window.
- **Price escalation:** Look for price increase provisions. Acceptable: CPI-based annual increases. Concerning: unlimited discretionary increases.
- **Liability caps:** Vendor agreements often heavily limit the vendor's liability. Review to ensure the cap is reasonable relative to the contract value.
- **Service levels:** For service vendors (SaaS, maintenance), look for SLA terms (uptime guarantees, response times, remedies for failure).
- **Data ownership:** For software/SaaS vendors, ensure the business retains ownership of its data and can export it upon termination.
- **Indemnification:** Vendor should indemnify for IP infringement claims and their own negligence.

### Risk Tier
YELLOW for most vendor agreements. RED if the vendor agreement involves financial services, data processing, or significant financial commitment.

## Partnership or Joint Venture Formation

### Scenario
Two or more business owners forming a partnership or joint venture for a specific project or ongoing business.

### Contract Needed
Partnership agreement or joint venture agreement (not currently in Aspire template registry — flag for attorney review)

### Key Considerations
- **Structure:** LLC operating agreement vs. general partnership agreement vs. joint venture agreement. LLC provides liability protection; general partnership does not.
- **Capital contributions:** Document each partner's initial contribution (cash, property, services) and obligations for future capital calls.
- **Profit/loss sharing:** Specify distribution ratios. Default (without agreement) varies by state — often 50/50 regardless of capital contribution.
- **Management:** Who makes day-to-day decisions? Who has authority for major decisions (spending thresholds, new contracts, hiring)?
- **Buy-sell provisions:** What happens when a partner wants to exit, dies, becomes disabled, or divorces? Right of first refusal, valuation method, and payment terms.
- **Deadlock resolution:** For 50/50 partnerships, include a tie-breaking mechanism (mediator, coin flip, shotgun clause).
- **Non-compete:** Partners typically agree not to compete with the partnership during and after their involvement.

### Risk Tier
RED — partnership formation has significant legal, tax, and liability implications. Attorney review required.

### Attorney Escalation
Clara should ALWAYS recommend attorney review for partnership/JV agreements. Too complex for automated generation.

## Protecting Confidential Information

### Scenario
Business owner needs to share sensitive information with a potential partner, vendor, or employee before a formal relationship is established.

### Contract Needed
- Sharing information both ways: `general_mutual_nda`
- Sharing information one way: `general_one_way_nda`

### Key Considerations
- **Timing:** NDA should be signed BEFORE any confidential information is shared. Once shared without an NDA, protection is limited.
- **Definition scope:** Balance breadth (protecting all sensitive information) with enforceability (overly broad definitions may be challenged).
- **Duration:** Match the NDA duration to the expected relationship timeline. 2-3 years is standard; trade secrets should have indefinite protection.
- **Permitted disclosure:** Allow sharing with employees, advisors, and attorneys who need to know, bound by similar obligations.
- **Remedies:** Include injunctive relief clause — monetary damages alone may be inadequate for confidentiality breaches.

### Risk Tier
YELLOW — NDA creation requires user confirmation but is a standard protective measure.

## Property Management — New Tenant Onboarding

### Scenario
Landlord signing a lease with a new residential tenant.

### Contract Needed
- `landlord_residential_lease_base`
- `landlord_lease_addenda_pack` (pet, smoking, parking addenda as needed)
- `landlord_move_in_checklist`

### Key Considerations
- **Jurisdiction compliance:** Residential lease laws vary dramatically by state. Security deposit limits, required disclosures, notice periods, and eviction procedures are ALL state-specific. Clara MUST apply the correct jurisdiction rules.
- **Fair housing:** Lease terms and screening criteria must comply with Fair Housing Act. Cannot discriminate on race, color, national origin, religion, sex, familial status, or disability.
- **Security deposit:** Collect per state limits. Provide receipt. Hold in proper account per state law.
- **Move-in checklist:** Document property condition at move-in to protect against security deposit disputes. Have tenant sign.
- **Disclosures:** Lead paint (pre-1978), mold, sex offender registry, flood zone — requirements vary by state and property age.
- **Addenda:** Pet policy, smoking policy, parking assignment, utility allocation — attach as separate signed addenda.

### Risk Tier
RED — residential leases are binding with significant landlord-tenant law implications. Jurisdiction-specific compliance is critical.
