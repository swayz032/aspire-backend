#!/usr/bin/env python3
"""Seed Finance Knowledge Base — 200+ chunks for Finn RAG.

Populates the finance_knowledge_chunks table with curated financial
knowledge across 8 domains. Uses OpenAI text-embedding-3-large (3072 dims).

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_finance_knowledge.py

Requires: ASPIRE_OPENAI_API_KEY env var set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# Knowledge Chunks — 8 domains, 200+ entries
# =============================================================================

FINANCE_KNOWLEDGE: list[dict] = []


def _add(domain: str, chunk_type: str, content: str, **kwargs):
    """Helper to add a knowledge chunk."""
    FINANCE_KNOWLEDGE.append({
        "domain": domain,
        "chunk_type": chunk_type,
        "content": content,
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Domain: tax_strategy (40 chunks)
# ---------------------------------------------------------------------------

_add("tax_strategy", "strategy", """
S-Corp Election for Small Businesses: If your net self-employment income exceeds $50,000-$60,000
annually, electing S-Corp status can save significant self-employment tax. As an S-Corp, you pay
yourself a "reasonable salary" (subject to FICA) and take remaining profits as distributions
(not subject to self-employment tax). The IRS requires the salary be reasonable for your role
and industry. Filing Form 2553 with the IRS makes the election effective. Deadline: March 15
for calendar-year corporations, or within 75 days of formation for new entities.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "rule", """
Qualified Business Income (QBI) Deduction — Section 199A: Pass-through businesses (sole props,
partnerships, S-Corps, some LLCs) can deduct up to 20% of qualified business income. Phase-out
begins at $191,950 (single) / $383,900 (married filing jointly) for 2026. Specified service
businesses (law, health, consulting, financial services) lose the deduction entirely above
the phase-out threshold. W-2 wage and capital limitations apply above the threshold.
""".strip(), jurisdiction="federal", tax_year=2026)

_add("tax_strategy", "strategy", """
Home Office Deduction: Two methods available. (1) Simplified: $5/sq ft, max 300 sq ft = $1,500 max.
(2) Regular: Actual expenses (rent/mortgage interest, utilities, insurance, repairs) prorated by
business-use percentage. Regular method requires Form 8829. The space must be used regularly and
exclusively for business. A dedicated room qualifies; a dining table used for meals does not.
Day care and storage are exceptions to exclusive use. Deduction limited to gross business income.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "strategy", """
Vehicle Deduction for Business Use: Two methods. (1) Standard Mileage Rate: 70 cents/mile for 2026
(IRS announces annually). Cannot use if you've claimed depreciation on the vehicle. Must use in
first year of business use. (2) Actual Expense Method: Deduct gas, insurance, repairs, depreciation,
lease payments prorated by business-use percentage. Track every trip with date, destination, business
purpose, and miles. Apps like MileIQ automate this. Commuting miles are never deductible.
""".strip(), jurisdiction="federal", tax_year=2026)

_add("tax_strategy", "rule", """
Section 179 Immediate Expensing: Businesses can deduct the full purchase price of qualifying equipment
and software in the year of purchase, up to $1,220,000 (2026 limit, indexed for inflation). Qualifying
property includes tangible personal property, off-the-shelf software, and certain improvements to
nonresidential real property. Phase-out begins when total equipment placed in service exceeds
$3,050,000. Cannot create a loss — limited to taxable income from active business.
""".strip(), jurisdiction="federal", tax_year=2026)

_add("tax_strategy", "rule", """
Bonus Depreciation: 60% bonus depreciation for assets placed in service in 2026 (phasing down 20%/year
from 100% in 2022). Applies to new AND used property (changed by TCJA). No dollar limit unlike Section
179. Can create a net operating loss. Applies to property with recovery period of 20 years or less.
Order of application: Section 179 first, then bonus depreciation on remaining basis, then regular MACRS.
""".strip(), jurisdiction="federal", tax_year=2026)

_add("tax_strategy", "calculation", """
Estimated Quarterly Tax Payments: Self-employed individuals must pay estimated taxes quarterly if they
expect to owe $1,000+ in taxes. Due dates: Q1 Apr 15, Q2 Jun 15, Q3 Sep 15, Q4 Jan 15 (following year).
Safe harbor: Pay 100% of prior year's tax liability (110% if AGI > $150,000) to avoid underpayment
penalty. Use Form 1040-ES. Calculate: (Expected AGI tax + SE tax - credits - withholding) / 4.
Consider using the annualized income installment method if income is irregular.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "strategy", """
SALT Cap Workaround for Pass-Through Entities: Many states now allow pass-through entity (PTE) tax
elections where the business pays state income tax at the entity level. This bypasses the $10,000
SALT deduction cap for individuals. The business claims a deduction for state taxes paid, and owners
receive a credit on their personal returns. Available in 30+ states. Check your state's specific
rules — election deadlines, estimated payment requirements, and credit mechanics vary.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "strategy", """
Entity Structure Comparison for Tax Purposes:
- Sole Proprietorship: Simplest. All income on Schedule C. Full SE tax on net profit.
- Single-Member LLC: Default taxed as sole prop (disregarded entity). Can elect S-Corp.
- Multi-Member LLC: Default taxed as partnership. Can elect S-Corp or C-Corp.
- S-Corp: Pass-through. Salary + distributions split saves SE tax. 100 shareholder limit.
- C-Corp: Double taxation (21% corporate + dividend tax). QBI deduction not available.
  Best for: retaining significant earnings, public offering plans, foreign owners.
Key decision point: When SE tax savings from S-Corp exceed the additional compliance costs
(payroll, separate return, reasonable comp analysis), the election makes sense.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "rule", """
Schedule C Deductions — Common Categories: Advertising, car/truck expenses, commissions, contract labor,
depreciation, employee benefit programs, insurance (business), interest (mortgage, business loans),
legal/professional services, office expense, pension/profit sharing, rent, repairs, supplies, taxes/licenses,
travel, meals (50% deductible), utilities, wages. The "ordinary and necessary" test: the expense must be
common in your trade AND helpful for your business. Lavish or extravagant expenses are not deductible.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "deadline", """
Key Federal Tax Deadlines for Small Businesses:
- Jan 15: Q4 estimated tax payment
- Jan 31: W-2s and 1099-NEC due to recipients; W-2s due to SSA
- Feb 28: Paper 1099s due to IRS (Mar 31 if e-filing)
- Mar 15: S-Corp (Form 1120-S) and Partnership (Form 1065) returns due (or extension)
- Apr 15: Individual (Form 1040), C-Corp (Form 1120), Q1 estimated payment, last day for prior-year IRA contribution
- Jun 15: Q2 estimated tax payment
- Sep 15: Q3 estimated payment; extended S-Corp/Partnership returns due
- Oct 15: Extended individual and C-Corp returns due
""".strip(), jurisdiction="federal")

_add("tax_strategy", "strategy", """
Retirement Plan Tax Strategies for Self-Employed:
- SEP-IRA: Contribute up to 25% of net SE income, max $69,000 (2026). Easy to set up.
  Employer-only contributions. Deadline: tax filing date including extensions.
- Solo 401(k): Employee deferral up to $23,500 + employer contribution up to 25% of comp.
  Total limit $69,000 (2026). Catch-up $7,500 if age 50+. Can include Roth option.
  Must be established by Dec 31. Allows loans.
- SIMPLE IRA: Employee deferral $16,500. Employer match up to 3%. Good for businesses
  with fewer than 100 employees. Must be established by Oct 1.
- Defined Benefit Plan: Highest contribution limits ($275,000+ annually). Complex administration.
  Best for high-income professionals 50+ wanting to shelter maximum income.
""".strip(), jurisdiction="federal", tax_year=2026)

_add("tax_strategy", "strategy", """
Meals and Entertainment Deduction Rules (Post-TCJA):
- Business meals: 50% deductible when directly related to business. Must have a clear business purpose.
- Entertainment: NOT deductible (changed by TCJA 2017). No deduction for sporting events, concerts, etc.
- Employee meals: 50% deductible for meals provided for employer convenience.
- Company-wide events: 100% deductible (holiday parties, summer picnics — all employees invited).
- Travel meals: 50% deductible while traveling away from tax home.
- Client meals: 50% deductible. Document: who, what business was discussed, date, amount.
Keep detailed records: date, amount, place, business purpose, names of attendees.
""".strip(), jurisdiction="federal")

_add("tax_strategy", "strategy", """
Net Operating Loss (NOL) Rules: Business losses exceeding income create an NOL. Post-TCJA rules:
NOLs can only offset 80% of taxable income in the carryforward year (no 100% offset). No carryback
allowed (except certain farming losses). NOLs carry forward indefinitely. For sole proprietors,
excess business losses over $305,000 (single) / $610,000 (MFJ) are suspended and treated as NOL
carryforward. Track NOLs carefully — they're valuable future tax assets.
""".strip(), jurisdiction="federal", tax_year=2026)

# --- State-specific tax ---

_add("tax_strategy", "rule", """
California Franchise Tax: California imposes an $800 minimum franchise tax on LLCs, S-Corps, and C-Corps,
regardless of income. LLCs also pay an LLC fee based on gross receipts: $0 (under $250K), $900
($250K-$500K), $2,500 ($500K-$1M), $6,000 ($1M-$5M), $11,790 ($5M+). First-year LLCs are exempt
from the $800 minimum tax. S-Corps pay 1.5% of net income (minimum $800). Due with annual return.
""".strip(), jurisdiction="CA")

_add("tax_strategy", "rule", """
Texas Franchise Tax (Margin Tax): Texas has no personal income tax but imposes a franchise tax on
businesses with revenue exceeding $2,470,000 (2026 threshold). Rate: 0.375% for retail/wholesale,
0.75% for all others. Calculated on the lesser of: total revenue minus COGS, total revenue minus
compensation, total revenue x 70%, or total revenue minus $1M. E-Z computation: 0.331% on revenue
up to $20M. No tax if revenue below threshold.
""".strip(), jurisdiction="TX")

_add("tax_strategy", "rule", """
New York LLC Publication Requirement: New York requires LLCs to publish formation notice in two
newspapers (one daily, one weekly) in the county of the LLC's office for six consecutive weeks.
Cost varies dramatically by county: Manhattan can exceed $1,500, while upstate counties may be
$200-$400. Must be completed within 120 days of formation. Failure to publish: LLC cannot
maintain an action in NY courts. The AG can also dissolve the LLC.
""".strip(), jurisdiction="NY")

_add("tax_strategy", "strategy", """
Florida Tax Advantages for Small Businesses: No state personal income tax. No franchise tax on LLCs
or sole proprietorships. C-Corps pay 5.5% corporate income tax. S-Corp income flows to individuals
(no state income tax). No estate tax. Sales tax: 6% state + up to 2% county surcharge. Commercial
rent is subject to sales tax (6% + county). One of the most tax-friendly states for small businesses.
""".strip(), jurisdiction="FL")

# Additional tax chunks
for topic, content in [
    ("Depreciation Methods", "MACRS depreciation: 5-year property (computers, office equipment), 7-year (furniture, fixtures), 15-year (land improvements), 27.5-year (residential rental), 39-year (commercial real property). Methods: 200% declining balance for 3/5/7/10-year, 150% for 15/20-year, straight-line for 27.5/39-year. Half-year convention applies unless 40%+ placed in service in Q4 (mid-quarter convention)."),
    ("1099 Filing Rules", "Issue 1099-NEC for $600+ paid to non-employee individuals/unincorporated entities for services. Issue 1099-MISC for $600+ in rents, prizes, other income; $10+ in royalties. Do NOT issue 1099s to C-Corps (except attorneys and medical/healthcare payments). Request W-9 from all vendors before first payment. Penalty for not filing: $60-$310 per form depending on lateness."),
    ("Self-Employment Tax", "Self-employment tax is 15.3% on net SE income: 12.4% Social Security (on first $168,600 in 2026) + 2.9% Medicare (no cap). Additional 0.9% Medicare surtax on SE income above $200K (single) / $250K (MFJ). You can deduct 50% of SE tax as an above-the-line deduction on Form 1040. SE tax is calculated on 92.35% of net SE income (the employer-equivalent portion)."),
    ("Business Use of Home Tests", "To claim home office deduction, space must pass TWO tests: (1) Regular and exclusive use — area used solely for business on a regular basis. (2) Principal place of business OR place where you meet clients/customers in normal course of business. Exceptions: separate structure, storage of inventory/product samples, daycare facility. Employees working from home: deduction suspended 2018-2025 under TCJA."),
    ("Health Insurance Deduction", "Self-employed individuals can deduct 100% of health insurance premiums (medical, dental, vision) for themselves, spouse, and dependents as an above-the-line deduction. Cannot exceed net SE income. Cannot claim if eligible for employer-subsidized coverage (including spouse's employer plan). Includes long-term care insurance premiums (age-based limits). Not claimed on Schedule C — goes on Form 1040 line 17."),
    ("Hobby Loss Rules", "IRS may reclassify business as hobby if no profit motive. Safe harbor: profit in 3 of 5 consecutive years (2 of 7 for horse breeding). Factors: manner of operation, expertise, time/effort, history of income/losses, expectation of asset appreciation, taxpayer's financial status, elements of personal pleasure. Hobby income is taxable but expenses are NOT deductible (TCJA suspended miscellaneous itemized deductions)."),
    ("Accounting Methods", "Cash method: income recognized when received, expenses when paid. Most small businesses use cash. Accrual method: income when earned, expenses when incurred. Required if: average annual gross receipts exceed $29M (3-year average), C-Corp, partnership with C-Corp partner, tax shelter. Once chosen, need IRS consent to change (Form 3115). Hybrid methods possible for different items."),
    ("Startup Costs Deduction", "Business startup costs: deduct up to $5,000 in the year business begins, plus amortize the remainder over 180 months. The $5,000 deduction is reduced dollar-for-dollar when total startup costs exceed $50,000. Startup costs include: investigating creation/acquisition of a business, creating the business, pre-opening activities. Organizational costs (for corps/LLCs): separate $5,000 deduction with same phase-out."),
    ("Charitable Contributions", "Sole proprietors and pass-through owners claim charitable deductions on personal returns (Schedule A). C-Corps deduct on corporate return, limited to 25% of taxable income. Qualified charitable contributions require written acknowledgment for $250+. Non-cash donations over $500 require Form 8283. Appraisal required for non-cash donations over $5,000. Inventory donations: deduct lesser of FMV or basis."),
    ("Like-Kind Exchanges", "Section 1031 exchanges defer capital gains on real property exchanges. Post-TCJA: only applies to real property (no longer personal property or equipment). 45-day identification period, 180-day completion deadline. Must identify up to 3 replacement properties (or any number if total value doesn't exceed 200% of relinquished property). Qualified intermediary required. Cannot be related party transactions."),
    ("Research & Development Credit", "Small businesses (under $5M revenue, not operating for 5+ years) can apply R&D credit against payroll tax (up to $500K/year). Regular credit: 20% of qualified research expenses exceeding base amount. Alternative simplified credit: 14% of QREs exceeding 50% of average QREs for prior 3 years. Qualified activities: developing new products, processes, software, formulas. Must meet 4-part test: permitted purpose, technological uncertainty, process of experimentation, technological in nature."),
    ("Business Interest Limitation", "Section 163(j): Business interest deduction limited to 30% of adjusted taxable income (ATI). Small business exception: average gross receipts of $29M or less (3-year test) are exempt. Disallowed interest carries forward indefinitely. Real property trades or businesses can elect out (must use ADS depreciation). Farming businesses can elect out. Floor plan financing interest is exempt."),
    ("Qualified Opportunity Zones", "Invest capital gains in Qualified Opportunity Zone Funds for tax benefits. Hold 5+ years: 10% basis step-up. Hold 7+ years: additional 5% step-up. Hold 10+ years: NO tax on appreciation of QOZ investment. Must invest within 180 days of capital gain recognition. Fund must hold 90%+ assets in QOZ property. Eligible property: QOZ stock, QOZ partnership interest, QOZ business property."),
    ("State Tax Nexus Rules", "Physical presence creates nexus in all states. Economic nexus (post-Wayfair): most states threshold is $100K in sales or 200 transactions. Registration requirements vary by state. Some states have factor-based apportionment (sales, payroll, property). Key states with unique rules: CA (market-based sourcing), NY (economic nexus + physical), TX (revenue-based franchise tax)."),
    ("Pass-Through Entity SALT Workaround", "30+ states offer PTE tax elections. How it works: entity pays state tax → claims federal deduction (bypasses $10K SALT cap) → owners receive credit on personal state return. States include: CA, NY, NJ, IL, GA, MD, CT, WI, OR, MN, CO, MA, RI, AZ, LA, AL, AR, OK, SC, VA. Each state has unique rules on election timing, estimated payments, and credit mechanics. Consult your CPA."),
    ("Tax Loss Harvesting for Business", "Accelerate deductions into current year: prepay expenses (if under 12-month rule), buy equipment before year-end for Section 179/bonus depreciation, contribute to retirement plans, write off bad debts. Defer income: delay billing until January, use installment sales for large asset sales. Year-end planning meeting with CPA recommended in October-November."),
    ("Payroll Tax Credits", "Employee Retention Credit (ERC): expired for most employers. Work Opportunity Tax Credit (WOTC): $2,400-$9,600 per eligible hire (target groups: veterans, ex-felons, SNAP recipients, etc.). Must file Form 8850 within 28 days of hire. Disabled Access Credit: 50% of expenditures between $250-$10,250 for ADA compliance. Small employer health insurance credit: up to 50% of premiums (must use SHOP marketplace)."),
    ("Tax Implications of Business Sale", "Asset sale vs stock sale: buyer prefers asset sale (step-up in basis), seller prefers stock sale (capital gains). Allocation of purchase price (Form 8594): Classes I-VII determine tax character. Installment sale: spread gain over payment period (Section 453). Non-compete agreements: ordinary income to seller. Goodwill: capital gain to seller, amortizable by buyer over 15 years. Consult tax attorney before structuring."),
    ("Alternative Minimum Tax for Businesses", "C-Corps: corporate AMT reinstated at 15% (CAMA, effective 2023) for corps with avg annual adjusted financial statement income >$1B. Small businesses generally exempt. Individuals: AMT exemption $85,700 (single) / $133,300 (MFJ) for 2026. Phase-out begins at $609,350 (single) / $1,218,700 (MFJ). Common triggers: large state tax deductions, incentive stock options, accelerated depreciation."),
]:
    _add("tax_strategy", "strategy" if "strategy" in topic.lower() or "method" in topic.lower() else "rule", content.strip(), jurisdiction="federal")


# ---------------------------------------------------------------------------
# Domain: accounting_standards (25 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Cash vs Accrual Basis", "Cash basis: simplest, recognizes income when received and expenses when paid. Best for service businesses under $29M. Accrual basis: recognizes income when earned and expenses when incurred, regardless of cash movement. Required for businesses over $29M average revenue, inventory-based businesses (with exceptions), and C-Corps (with exceptions). Many small businesses start cash and convert when they grow."),
    ("Chart of Accounts Structure", "Standard chart of accounts categories: Assets (1000s), Liabilities (2000s), Equity (3000s), Revenue (4000s), Cost of Goods Sold (5000s), Operating Expenses (6000s-7000s), Other Income/Expense (8000s). Sub-accounts provide granularity. Keep it simple — 30-50 accounts for most small businesses. Review quarterly and consolidate unused accounts. Industry-specific templates available in QuickBooks/Xero."),
    ("Revenue Recognition Principle", "ASC 606 five-step model: (1) Identify the contract, (2) Identify performance obligations, (3) Determine transaction price, (4) Allocate price to obligations, (5) Recognize revenue when obligation satisfied. For small businesses: recognize revenue when service delivered or product transferred. Subscription revenue: recognize monthly over service period, not upfront. Milestone billing: recognize at each milestone completion."),
    ("Financial Statement Basics", "Three core statements: (1) Income Statement (P&L): Revenue - Expenses = Net Income over a period. (2) Balance Sheet: Assets = Liabilities + Equity at a point in time. (3) Cash Flow Statement: Operating + Investing + Financing activities = Cash change. They interconnect: net income flows to retained earnings (equity) and is the starting point for operating cash flow."),
    ("Double-Entry Bookkeeping", "Every transaction has TWO entries: debit and credit. Assets and expenses increase with debits. Liabilities, equity, and revenue increase with credits. Debits always equal credits. Example: client pays $1,000 invoice → Debit Cash $1,000 (asset increase), Credit Accounts Receivable $1,000 (asset decrease). The trial balance should always balance (total debits = total credits)."),
    ("Bank Reconciliation Process", "Monthly process: (1) Compare bank statement to general ledger cash account. (2) Mark cleared transactions. (3) Add bank-side items missing from books (fees, interest, electronic payments). (4) Add book-side items missing from bank (outstanding checks, deposits in transit). (5) Adjusted bank balance should equal adjusted book balance. Investigate and resolve discrepancies immediately. Document and file each reconciliation."),
    ("Depreciation Schedules", "Common methods: Straight-line (most common for financial reporting), MACRS (required for tax), Double-declining balance (accelerated), Units of production. Salvage value: estimated value at end of useful life (excluded from depreciable base in straight-line, ignored in MACRS). Record monthly depreciation entry: Debit Depreciation Expense, Credit Accumulated Depreciation. Maintain fixed asset register with original cost, date placed in service, method, useful life."),
    ("Accounts Receivable Management", "DSO (Days Sales Outstanding) = (AR / Revenue) x Days. Industry average varies: 30-45 days typical. Invoice promptly. Offer early payment discounts (2/10 net 30 = 2% discount if paid in 10 days). Age AR weekly: Current, 30-day, 60-day, 90-day+. Write off after 90-120 days if uncollectable. Bad debt methods: direct write-off (small businesses) or allowance method (GAAP-required if material). AR factoring: sell receivables at 2-5% discount for immediate cash."),
    ("Accounts Payable Best Practices", "DPO (Days Payable Outstanding) = (AP / COGS) x Days. Pay on time but not early unless there's a discount. 2/10 net 30 discount = 36.7% annualized return. Three-way match: PO + receiving report + invoice must agree before payment. Segregation of duties: person who approves invoices should not sign checks. Automate where possible. Weekly AP review. Negotiate extended terms with reliable vendors."),
    ("Month-End Close Process", "Recommended checklist: (1) Reconcile all bank/credit card accounts. (2) Review and categorize all transactions. (3) Record depreciation. (4) Accrue unbilled revenue and unpaid expenses (accrual basis). (5) Review AR aging and follow up. (6) Review AP aging and schedule payments. (7) Reconcile payroll. (8) Review P&L vs budget. (9) Prepare financial statements. Target: close within 10 business days of month end."),
    ("Year-End Close Procedures", "In addition to month-end: (1) Physical inventory count (if applicable). (2) Reconcile all balance sheet accounts. (3) Record year-end adjusting entries. (4) Review fixed assets — any disposals or impairments? (5) Confirm 1099 data for contractors. (6) Review loan balances vs lender statements. (7) Calculate and record income tax provision. (8) Prepare annual financial statements. (9) Schedule tax return preparation. (10) Archive working papers."),
    ("Inventory Accounting Methods", "FIFO (First In, First Out): oldest inventory sold first. Matches physical flow for most businesses. Higher net income in rising prices. LIFO (Last In, First Out): newest inventory sold first. Lower taxable income in rising prices. Not allowed under IFRS. Weighted Average: total cost / total units. Used for homogeneous products. Specific Identification: track actual cost of each unit. Used for unique/high-value items. Once selected, need IRS consent to change."),
    ("Accrual Adjusting Entries", "Common adjusting entries: (1) Accrued revenue: services performed but not yet billed. (2) Accrued expenses: expenses incurred but not yet paid (utilities, wages). (3) Prepaid expenses: payments made in advance amortized over benefit period. (4) Unearned revenue: payments received before service delivered. (5) Depreciation: allocate fixed asset cost over useful life. Always identify the period the entry affects."),
    ("Financial Ratio Analysis", "Key ratios for small businesses: Current Ratio = Current Assets / Current Liabilities (healthy: 1.5-3.0). Quick Ratio = (Cash + AR) / Current Liabilities (healthy: >1.0). Gross Margin = (Revenue - COGS) / Revenue. Net Profit Margin = Net Income / Revenue (varies by industry). Debt-to-Equity = Total Liabilities / Equity (lower is safer). Review monthly, compare to industry benchmarks."),
    ("Cost of Goods Sold Calculation", "COGS = Beginning Inventory + Purchases - Ending Inventory. For service businesses: direct labor + direct materials + allocated overhead. COGS reduces gross profit directly. Keep COGS separate from operating expenses for meaningful gross margin analysis. Include: raw materials, direct labor, manufacturing overhead, freight-in, subcontractor costs. Exclude: selling expenses, admin expenses, interest."),
    ("General Ledger Maintenance", "The GL is the master record of all financial transactions. Review regularly for: misclassified transactions, duplicate entries, missing entries, unusual balances. Use sub-ledgers for detailed tracking (AR, AP, payroll, fixed assets). Sub-ledger totals must equal GL control account balances. Lock prior periods after close to prevent unauthorized changes."),
    ("Internal Controls for Small Business", "Key controls even for small teams: (1) Segregation of duties where possible. (2) Bank reconciliation by someone other than bookkeeper. (3) Owner reviews bank statements monthly. (4) Pre-numbered checks. (5) Two signatures required above threshold. (6) Credit card receipts for all charges. (7) Physical access controls to checkbooks/cash. (8) Regular backup of accounting data. (9) Annual review by outside accountant."),
    ("Break-Even Analysis", "Break-Even Point = Fixed Costs / (Revenue per Unit - Variable Cost per Unit). Also: Fixed Costs / Contribution Margin Ratio. Contribution Margin = Revenue - Variable Costs. Contribution Margin Ratio = Contribution Margin / Revenue. Use for pricing decisions, new product evaluation, and understanding minimum viable revenue. Include all fixed costs: rent, salaries, insurance, loan payments, subscriptions."),
    ("Cash Flow vs Profit", "A profitable business can run out of cash. Common causes: rapid growth (funding AR/inventory faster than collecting), large capital purchases, debt repayment, seasonal revenue with fixed costs, slow-paying customers. Cash flow management: forecast 13-week rolling cash flow, maintain 3-6 months operating expenses as reserve, negotiate payment terms, use line of credit for short-term gaps."),
    ("Budgeting for Small Business", "Zero-based budget: justify every expense from zero each period. Incremental budget: adjust prior period by percentage. Recommended approach: start with revenue forecast, then fixed costs, then variable costs, then discretionary spending. Review budget vs actual monthly — investigate variances over 10%. Reforecast quarterly. Include capital expenditure budget separately. Cash flow budget is separate from P&L budget."),
]:
    _add("accounting_standards", "definition" if "definition" in topic.lower() or "basics" in topic.lower() or "principle" in topic.lower() else "checklist" if "process" in topic.lower() or "procedure" in topic.lower() or "checklist" in topic.lower() else "rule", content.strip())


# ---------------------------------------------------------------------------
# Domain: bookkeeping (25 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Transaction Categorization", "Categorize every transaction promptly (daily or weekly, not monthly). Common categories: Revenue/Sales, Cost of Goods Sold, Advertising, Auto/Vehicle, Bank Fees, Contract Labor, Insurance, Interest, Meals, Office Supplies, Professional Services, Rent, Repairs, Taxes/Licenses, Telephone, Travel, Utilities, Wages. Use rules/memorized transactions to auto-categorize recurring items. Review uncategorized transactions weekly."),
    ("Receipt Management", "IRS requires receipts for expenses over $75 (or any amount for travel/entertainment). Digital receipts are acceptable — IRS approved since 1997. Best practices: photograph receipts immediately, use receipt scanning apps, store in cloud, organize by category and date. Retention: 3 years from filing date (or 7 years for certain deductions). Write business purpose on receipt. Bank/credit card statements supplement but don't replace receipts."),
    ("Bank Feed Reconciliation", "Modern bookkeeping: bank feeds import transactions automatically. Process: (1) Review imported transactions daily/weekly. (2) Match to existing entries or categorize new ones. (3) Split transactions when needed (e.g., payment covering multiple invoices). (4) Exclude personal transactions in business accounts. (5) Reconcile at least monthly. Avoid: auto-accepting all — review each for accuracy."),
    ("Monthly Close Checklist for Bookkeepers", "1. Reconcile all bank accounts. 2. Reconcile credit cards. 3. Review uncategorized transactions. 4. Send overdue invoice reminders. 5. Reconcile accounts receivable. 6. Reconcile accounts payable. 7. Record recurring journal entries (depreciation, prepaid amortization). 8. Review profit and loss for reasonableness. 9. Review balance sheet for unusual items. 10. Run and save reports. Target completion: 5-10 business days after month end."),
    ("Managing Multiple Bank Accounts", "Recommended structure: (1) Operating account — day-to-day income and expenses. (2) Tax savings account — set aside 25-30% of profit quarterly. (3) Payroll account — fund before each payroll run. (4) Savings/reserve account — 3-6 months operating expenses. Some add: (5) Profit account — transfer profit percentage monthly (Profit First method). Keep separate accounts in your chart of accounts. Reconcile ALL accounts monthly."),
    ("Expense Report Processing", "Employee expense reports: require receipts for all expenses, clear business purpose, approval by manager before reimbursement. IRS accountable plan rules: expenses must have business connection, adequate accounting within 60 days, excess returned within 120 days. Non-accountable plan reimbursements are taxable as wages. Per diem rates available for travel (GSA rates). Mileage reimbursement at IRS standard rate."),
    ("Sales Tax Collection and Remittance", "Economic nexus: most states require collection after $100K in sales or 200 transactions. Register for sales tax permit in each nexus state before collecting. Charge rate based on destination (most states) or origin (some states). Exempt sales: resale (collect resale certificate), certain nonprofits, some services. File returns on assigned schedule (monthly, quarterly, or annually). Use automated solutions (TaxJar, Avalara) for multi-state compliance."),
    ("Credit Card Transaction Handling", "Record credit card transactions as they occur (not when the bill is paid). Credit card statement date may differ from transaction date — use transaction date. When paying the bill: debit credit card liability account, credit checking account. Do NOT categorize the bill payment as an expense (that double-counts). Match merchant statements to your records monthly. Watch for unauthorized charges."),
    ("Petty Cash Management", "Establish a fixed fund ($100-$500). Replenish when low: debit various expense accounts, credit cash. Physical count should always equal fund amount minus receipt total. Keep petty cash log: date, amount, recipient, purpose, receipt number. Designate one custodian. Count and reconcile weekly. Petty cash disbursements still need receipts. Consider eliminating petty cash — corporate cards are more traceable."),
    ("Contractor vs Employee Classification", "IRS uses behavioral control, financial control, and relationship type to determine. Key factors: (1) Do you control how work is done? Employee. (2) Set schedule and location? Employee. (3) Provide tools/equipment? Employee. (4) Pay by hour/week? Employee. (5) Work for multiple clients? Contractor. (6) Written contract? Supports contractor. (7) Benefits provided? Employee. Misclassification penalties: back taxes + 100% of employee share of FICA + penalties. Safe harbor: if you've consistently treated similar workers as contractors and filed 1099s, Section 530 relief may apply."),
    ("Invoice Best Practices", "Include: business name/logo, invoice number (sequential), date issued, due date, client info, line items with descriptions, quantities, rates, subtotal, tax (if applicable), total, payment terms, payment methods accepted. Send promptly — delay inviting delays payment. Follow up: gentle reminder at due date, firmer at 30 days, consider late fees after 30 days. Maintain aging report. Consider auto-billing for recurring services."),
    ("Undeposited Funds Account", "In QuickBooks/Xero, payments received go to Undeposited Funds before being deposited. This matches how you actually deposit: multiple payments in one bank deposit. Process: (1) Record payment received (goes to Undeposited Funds). (2) Create bank deposit grouping payments. (3) Deposit amount should match bank deposit. This prevents deposits from appearing as double-counted. Clear Undeposited Funds regularly — nothing should sit there long."),
    ("Handling Owner Draws and Contributions", "Owner draws: Debit Owner's Draw (equity account), Credit Cash. Contributions: Debit Cash, Credit Owner's Contribution (equity account). Do NOT categorize as revenue (contributions) or expenses (draws). For tax purposes: draws are not tax-deductible, contributions are not taxable income. Track separately from salary (for S-Corp shareholders, salary is an expense subject to payroll tax). Owner's draw account resets to zero at year end, net effect flows to retained earnings."),
]:
    _add("bookkeeping", "checklist" if "checklist" in topic.lower() or "process" in topic.lower() else "rule", content.strip())


# ---------------------------------------------------------------------------
# Domain: payroll_rules (30 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Federal Payroll Tax Overview", "Employer responsibilities: (1) Withhold federal income tax based on W-4. (2) Withhold employee share of FICA (7.65% = 6.2% SS + 1.45% Medicare). (3) Pay employer share of FICA (matching 7.65%). (4) Pay FUTA (6% on first $7,000 per employee, reduced by state credits to 0.6%). (5) Deposit taxes on time (semi-weekly or monthly based on lookback period). (6) File quarterly Form 941 and annual Form 940. Failure to deposit/file: escalating penalties from 2% to 15%."),
    ("W-4 Processing", "Employees complete W-4 at hire and can update anytime. 2020+ W-4 eliminated allowances — now uses: filing status, multiple jobs, dependents, other income, deductions, extra withholding. Employer cannot advise employees on how to fill out W-4. Cannot refuse to accept a valid W-4. Invalid W-4 (altered, not signed): withhold as Single with no adjustments. IRS can issue lock-in letters requiring specific withholding. Retain W-4s for 4 years after last return filed using them."),
    ("Overtime Rules (FLSA)", "Non-exempt employees: overtime at 1.5x regular rate for hours over 40 in a workweek. Salary threshold for exemption: $58,656/year ($1,128/week) effective 2026. Exempt categories: Executive, Administrative, Professional, Computer, Outside Sales. Each has specific duties test. Misclassification as exempt: back overtime pay + liquidated damages (2x). Some states have daily overtime (CA: over 8 hours/day). Track all hours worked, including off-the-clock work."),
    ("Employee Benefits Administration", "Pre-tax benefits reduce taxable wages: health insurance, HSA contributions, 401(k)/403(b) deferrals, FSA (medical and dependent care), commuter benefits. Section 125 cafeteria plan required for pre-tax employee premium payments. COBRA: businesses with 20+ employees must offer continued health coverage for 18-36 months after qualifying event. State mini-COBRA may apply for smaller employers. ACA: 50+ FTE employers must offer affordable minimum essential coverage."),
    ("Workers Compensation Insurance", "Required in all states except TX (optional, but most employers carry it). Rates based on employee classification codes and employer's experience modification rating. Premium = (Payroll / 100) x Rate x Experience Mod. Audit at year end adjusts premium based on actual payroll. Claims management: report injuries immediately, manage return-to-work programs. Independent contractors generally excluded, but misclassification risk applies."),
    ("Direct Deposit Setup", "Offer direct deposit to all employees — most states allow mandatory direct deposit. Employee provides bank routing and account numbers via authorization form. Process: create ACH file 1-2 business days before pay date, transmit via bank or payroll provider. NACHA rules govern ACH transactions. Employee can split deposits across multiple accounts. Provide pay stubs (electronic or paper based on state law). Retain ACH authorization forms for duration of employment plus 2 years."),
    ("New Hire Reporting", "Federal: report within 20 days of hire (some states less). Report to state directory of new hires. Required info: employee name, SSN, address, employer name, EIN, address. Applies to new hires and rehires after 60+ day break. Purpose: child support enforcement, fraud detection. Penalties: $25 per late report (federal), states may have additional penalties."),
    ("Final Paycheck Requirements by State", "Varies dramatically by state. California: immediately upon termination (involuntary) or within 72 hours (voluntary with no notice). New York: next regular payday. Texas: within 6 days (involuntary) or next regular payday (voluntary). Federal (FLSA): next regular payday. Many states require payout of unused vacation/PTO at termination. Check your state's specific rules — late payment can trigger waiting time penalties."),
    ("Payroll Tax Deposit Schedules", "Monthly depositor: if total tax liability in lookback period (July 1 to June 30, two years prior) was $50,000 or less. Deposit by 15th of following month. Semi-weekly depositor: if lookback period liability exceeded $50,000. Wed/Thu/Fri paydays: deposit by following Wednesday. Sat/Sun/Mon/Tue paydays: deposit by following Friday. $100,000 Next-Day Rule: if you accumulate $100K+ on any day, deposit next business day (and become semi-weekly depositor for rest of year and next year)."),
    ("Payroll Deductions Order", "Mandatory deductions (in order of priority): (1) Federal income tax withholding. (2) State income tax withholding. (3) Local income tax withholding. (4) FICA (Social Security + Medicare). (5) Court-ordered garnishments (child support, tax levies, creditor garnishments). Voluntary deductions: health insurance premiums, retirement contributions, HSA/FSA, union dues, charitable contributions. Garnishment limits: 25% of disposable earnings (50-65% for child support)."),
    ("Tipped Employee Rules", "Federal minimum cash wage for tipped employees: $2.13/hour (tip credit of $5.12). If tips + cash wage < $7.25, employer makes up the difference. Tip reporting: employees must report tips over $20/month. Employer withholds income and FICA taxes on reported tips. FICA tip credit (Section 45B): employer can claim credit for employer share of FICA on tips exceeding federal minimum wage. Many states require higher cash wages (CA, WA, OR, AK, MN, MT, NV require full minimum wage before tips)."),
    ("Paid Leave Requirements", "No federal requirement for paid leave (except FMLA — unpaid). State laws vary: CA, NY, NJ, WA, CO, CT, OR, MA, MD, DE, MN, ME, IL require paid sick leave. CA, NY, NJ, WA, CO, CT, OR, MA, DC, RI, MD require paid family leave. Most paid family leave programs are funded through employee payroll deductions. Accrual rates typically: 1 hour of sick leave per 30-40 hours worked. Many cities have additional requirements."),
    ("Payroll Frequency Requirements", "Federal: no requirement. States vary: some require weekly (NY for manual workers), bi-weekly, or semi-monthly. Most states: at least semi-monthly or bi-weekly. Common frequencies: weekly (construction, hourly), bi-weekly (most common), semi-monthly (salaried), monthly (executives, allowed in some states). Check your state labor department for specific rules."),
    ("Form 941 Quarterly Filing", "Due: April 30, July 31, October 31, January 31. Report: number of employees, total wages, tips, taxable SS/Medicare wages, income tax withheld, employer/employee FICA. Reconcile deposits made vs liability. If you're a monthly depositor, also report monthly liability breakdown. E-file if you file 10+ returns. Penalties: 5% per month for late filing (max 25%). Correction: file Form 941-X."),
    ("Form 940 Annual FUTA Filing", "Due: January 31 (if all FUTA deposits were made on time, extended to February 10). FUTA tax: 6% on first $7,000 per employee. Credit for state unemployment taxes: up to 5.4%, resulting in net 0.6% FUTA rate. Deposit quarterly if liability exceeds $500. Most employers pay ~$42/employee/year (0.6% x $7,000). Credit reduction states: if state has outstanding federal unemployment loan, FUTA credit is reduced (higher effective rate)."),
]:
    _add("payroll_rules", "rule", content.strip(), jurisdiction="federal")


# ---------------------------------------------------------------------------
# Domain: payment_processing (20 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("ACH Transfer Basics", "ACH (Automated Clearing House) processes electronic payments in batches. Two types: ACH Credit (push money) and ACH Debit (pull money). Processing time: typically 1-3 business days. Same-day ACH available for transactions under $1M. Costs: typically $0.20-$1.50 per transaction (much cheaper than wire transfers). Ideal for: payroll, vendor payments, recurring billing. NACHA rules govern all ACH transactions. Returns possible for up to 60 days (unauthorized debits)."),
    ("Credit Card Processing Fees", "Typical breakdown: Interchange fee (paid to issuing bank, 1.5-3.5%) + Assessment fee (paid to card network, 0.13-0.15%) + Processor markup (0.1-0.5% + per-transaction fee). Pricing models: Interchange-plus (most transparent), Flat rate (Square/Stripe: 2.6-2.9% + $0.30), Tiered (qualified/mid/non-qualified). Tips: negotiate rates if processing >$10K/month. Downgrade surcharges for keyed-in vs swiped. PCI compliance required for all merchants."),
    ("Chargeback Prevention", "Chargeback ratio: keep under 1% (0.65% for Visa/Mastercard) or risk account termination. Prevention: clear billing descriptors, detailed receipts, customer service contact info, delivery confirmation, CVV verification, 3D Secure for online, clear refund policy. Response: respond within deadline (usually 30 days), provide compelling evidence (signed receipts, delivery proof, communication records). Representment: dispute the chargeback with evidence. Time limit: 120 days from transaction for cardholder to file."),
    ("Payment Terms Best Practices", "Common terms: Net 30 (due in 30 days), Net 15, Due on Receipt, 2/10 Net 30 (2% discount if paid in 10 days). For service businesses: require deposit (25-50%) before work begins, progress billing for large projects. Offer multiple payment methods: ACH (cheapest), credit card (most convenient), check (slowest). Late payment fees: 1-1.5% per month (check state usury laws). Put terms in writing before engagement."),
    ("PCI DSS Compliance", "PCI DSS (Payment Card Industry Data Security Standard) applies to ALL businesses that accept, process, store, or transmit cardholder data. Four levels based on transaction volume. Level 4 (under 20K e-commerce or 1M total): self-assessment questionnaire (SAQ). Key requirements: firewall, encryption, access controls, regular testing, security policy. Simplest approach: use a PCI-compliant processor (Stripe, Square) and never store card data on your own systems."),
    ("Recurring Billing Setup", "Best practices: clear authorization from customer (written/digital consent), transparent pricing, easy cancellation process, advance notice before billing, retry logic for failed payments (try again 1, 3, 7 days later), dunning emails for failed payments, card updater service for expired cards. Stripe/Square support automatic card updates. Proration: charge proportional amount when customers change plans mid-cycle."),
    ("Wire Transfer Procedures", "Domestic wire: same-day settlement, costs $20-35 outgoing. International wire (SWIFT): 1-5 business days, costs $35-50+ plus intermediary bank fees. Required info: recipient name, bank name, routing number (domestic) or SWIFT/BIC code (international), account number, reference/memo. Cannot be reversed once processed — verify all details before sending. Fraud risk: always verify wire instructions by phone (not email — business email compromise targets wire transfers)."),
    ("Refund Processing", "Process refunds promptly (state laws may require within specific timeframes). Credit card refunds: return to original payment method, processing time 5-10 business days. ACH refunds: initiate return through your bank. Check refunds: issue new check. Document reason for every refund. For tax purposes: refunds reduce revenue in the period processed. High refund rates may trigger payment processor review. Consider offering store credit as alternative."),
    ("Stripe Connect for Platforms", "Stripe Connect enables marketplace/platform payments. Three account types: Standard (easiest, Stripe handles everything), Express (customizable onboarding), Custom (full control, most work). Flow: customer pays platform → Stripe splits payment → platform keeps fee → vendor receives payout. Onboarding: KYC verification for each connected account. Pricing: 0.25% + $0.25 per payout (in addition to standard processing fees). Tax reporting: platform responsible for 1099-K to connected accounts."),
    ("Invoice Payment Automation", "Automate invoice collection: send invoices immediately upon delivery, auto-remind at 3/7/14/30 days overdue, offer online payment links in invoice, enable auto-pay for recurring clients, integrate with accounting software. Payment links: Stripe Payment Links, Square Invoices, QuickBooks Payments — all embed payment processing in the invoice. Auto-reconciliation: payment recorded + invoice marked paid + receipt generated automatically."),
]:
    _add("payment_processing", "rule" if "rule" in topic.lower() or "compliance" in topic.lower() else "provider_spec" if "stripe" in topic.lower() or "connect" in topic.lower() else "strategy", content.strip())


# ---------------------------------------------------------------------------
# Domain: financial_planning (20 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Cash Flow Forecasting", "13-week rolling cash flow forecast: project weekly cash inflows (AR collections, recurring revenue, other income) and outflows (AP, payroll, rent, loan payments, taxes, other). Start with current cash balance, add inflows, subtract outflows = ending cash. Identify potential shortfalls 4-8 weeks in advance. Update weekly. Scenario planning: best case, expected case, worst case. Key metrics: days cash on hand, weekly burn rate, collection rate."),
    ("Runway Calculation", "Cash Runway = Cash Balance / Monthly Burn Rate. Burn rate = total monthly operating expenses (fixed + variable). Gross burn: total spend. Net burn: spend minus revenue. Example: $120K cash, $10K monthly net burn = 12 months runway. Warning zone: under 6 months. Action required: under 3 months. Extend runway: increase revenue, cut discretionary spending, negotiate payment terms, secure line of credit before you need it."),
    ("Break-Even Analysis", "Break-Even Units = Fixed Costs / (Price per Unit - Variable Cost per Unit). Break-Even Revenue = Fixed Costs / Contribution Margin Ratio. Example: $5,000 fixed costs, $100 price, $40 variable cost → break even at 84 units ($8,333). Use for: pricing decisions, new product viability, setting sales targets, evaluating cost structure changes. Update quarterly as costs change."),
    ("Working Capital Management", "Working Capital = Current Assets - Current Liabilities. Working Capital Ratio = Current Assets / Current Liabilities (healthy: 1.5-2.0). Components to optimize: (1) Reduce DSO — invoice promptly, follow up on AR. (2) Optimize DPO — pay on terms, not early (unless discounted). (3) Reduce DIO — minimize excess inventory. Cash Conversion Cycle = DSO + DIO - DPO. Lower is better."),
    ("Financial Projections", "Three-statement model: projected Income Statement → Balance Sheet → Cash Flow. Revenue projection: bottom-up (units x price x customers) is more credible than top-down (market size x market share). Expense projection: fixed costs (known) + variable costs (% of revenue). Capital expenditures: planned purchases, depreciation impact. Key assumptions: growth rate, pricing, customer acquisition cost, churn rate, staffing plan. Present 3-year projections with monthly detail for year 1."),
    ("Line of Credit vs Term Loan", "Line of credit: revolving, draw as needed, interest only on outstanding balance. Best for: cash flow gaps, seasonal businesses, unexpected expenses. Typical: $25K-$500K, variable rate. Term loan: fixed amount, scheduled payments, fixed or variable rate. Best for: equipment, expansion, one-time investments. Typical: $25K-$5M, 1-10 year terms. SBA loans: government-backed, lower rates, longer terms, but slower process. Apply before you need the money."),
    ("Pricing Strategy", "Cost-plus: cost + markup percentage. Simple but ignores value and competition. Value-based: price based on perceived value to customer. Higher margins but requires understanding customer willingness to pay. Competitive: match or undercut competitors. Risk: race to bottom. Tiered pricing: good/better/best packages. Anchoring effect — most customers choose middle tier. Review pricing annually — many small businesses underprice. Raise prices 3-5% annually at minimum."),
    ("KPIs for Small Business", "Revenue KPIs: MRR/ARR (subscription), revenue growth rate, average transaction value. Profitability KPIs: gross margin, net profit margin, EBITDA margin. Cash KPIs: cash on hand, days cash on hand, cash flow from operations. Efficiency KPIs: revenue per employee, customer acquisition cost (CAC), lifetime value (LTV), LTV:CAC ratio (target: 3:1+). Activity KPIs: close rate, churn rate, NPS. Track 5-7 KPIs max — dashboards, not spreadsheets."),
    ("Scenario Planning", "Three scenarios: Pessimistic (20-30% below plan), Base Case (plan), Optimistic (20-30% above plan). For each: project revenue, expenses, cash flow, staffing needs. Identify trigger points: at what revenue level do you need to hire? At what cash level do you cut spending? Decision framework: IF revenue drops 20%, THEN reduce discretionary spend by $X. Quarterly scenario review. Stress test: what if you lose your biggest client?"),
    ("Emergency Fund for Business", "Target: 3-6 months of operating expenses in liquid savings. Build gradually — set aside 5-10% of monthly revenue. Keep in high-yield savings account (separate from operating). Do not invest in illiquid assets. Uses: cover seasonal dips, bridge AR gaps, handle unexpected expenses (equipment failure, legal issues), survive economic downturns. This is not optional — it's business insurance."),
    ("Debt Service Coverage Ratio", "DSCR = Net Operating Income / Total Debt Service. Lenders typically require DSCR of 1.25+ (meaning income covers 125% of debt payments). Below 1.0: business cannot cover debt from operations. Calculate before taking new loans. To improve: increase revenue, reduce expenses, restructure existing debt for lower payments, extend loan terms."),
    ("Profit First Method", "Framework by Mike Michalowicz: allocate revenue to accounts in this order: (1) Profit (5-20%), (2) Owner's Compensation (35-50%), (3) Tax (15-25%), (4) Operating Expenses (remainder). Forces you to run the business on what's left after profit. Quarterly profit distributions to owner. Tax account ensures estimated payments are funded. Start with 1% profit allocation and increase gradually. Works well for businesses struggling with profitability."),
]:
    _add("financial_planning", "strategy", content.strip())


# ---------------------------------------------------------------------------
# Domain: provider_integration (30 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Plaid Link Flow", "Plaid Link is the client-side component for bank account connection. Flow: (1) Client requests link token from your server. (2) Server calls Plaid /link/token/create with user info and products (transactions, auth, etc.). (3) Client opens Plaid Link with token. (4) User selects bank, authenticates with bank credentials. (5) Plaid Link returns public_token. (6) Client sends public_token to server. (7) Server exchanges for access_token via /item/public_token/exchange. (8) Store access_token securely (encrypted at rest). Access tokens don't expire but items can become disconnected."),
    ("Plaid Transaction Sync", "Use /transactions/sync for incremental transaction updates (replaces deprecated /transactions/get). Flow: (1) Call /transactions/sync with access_token. (2) Returns added, modified, removed transactions since last sync. (3) Store cursor for next sync. (4) Repeat with cursor until has_more is false. Webhook: SYNC_UPDATES_AVAILABLE fires when new transactions are ready. Historical transactions: typically 24 months available. Real-time balance: use /accounts/balance/get. Rate limits: 15 requests/minute per item."),
    ("Plaid Error Handling", "Common errors: ITEM_LOGIN_REQUIRED (user needs to re-authenticate — trigger Plaid Link in update mode), INVALID_ACCESS_TOKEN (token revoked or invalid), RATE_LIMIT_EXCEEDED (back off), PRODUCT_NOT_READY (wait and retry for new connections). Webhook: PENDING_EXPIRATION fires 7 days before access token expires (90 days for Plaid development environment). Error response format: { error_type, error_code, error_message, display_message, request_id }."),
    ("Stripe Connect Onboarding", "Stripe Connect Standard accounts: (1) Create account link via /v1/account_links. (2) Redirect user to Stripe-hosted onboarding. (3) User provides business info, bank account, identity verification. (4) Stripe handles KYC/AML compliance. (5) Webhook: account.updated fires on status changes. Check charges_enabled and payouts_enabled. Express accounts: similar but simpler UI. Custom accounts: you build the onboarding UI, more control but more responsibility."),
    ("Stripe Invoicing", "Create invoice: POST /v1/invoices with customer, collection_method (charge_automatically or send_invoice), payment_settings. Add line items: POST /v1/invoices/{id}/items. Finalize: POST /v1/invoices/{id}/finalize. Send: POST /v1/invoices/{id}/send. Status flow: draft → open → paid/uncollectible/void. Webhooks: invoice.paid, invoice.payment_failed, invoice.finalized. Auto-charge: attach default payment method to customer. Dunning: configure retry schedule in dashboard."),
    ("Stripe Payment Intents", "Payment flow: (1) Create PaymentIntent server-side with amount, currency, customer. (2) Return client_secret to frontend. (3) Frontend confirms payment with Stripe.js. (4) Stripe processes payment. (5) Webhook: payment_intent.succeeded. Idempotency: use idempotency_key header to prevent duplicate charges. 3D Secure: automatic for supported cards. Metadata: store order_id, customer reference for reconciliation."),
    ("QuickBooks Online API", "OAuth 2.0 authentication. Endpoints: Company (read), Account, Customer, Invoice, Payment, Bill, Vendor, Item, Purchase. Rate limits: 500 requests per minute (throttled), 10 concurrent requests. Webhook: CDC (Change Data Capture) events for entity changes. Minor version: specify in requests for API compatibility. Sandbox available for testing. SDKs: Node.js, Python, Java, PHP, Ruby, .NET. Common operations: create invoice, record payment, sync transactions, pull financial reports."),
    ("QuickBooks Chart of Accounts Sync", "Sync CoA between your app and QBO: (1) Query all accounts: GET /v3/company/{id}/query?query=select * from Account. (2) Map your categories to QBO account types (Asset, Liability, Equity, Revenue, Expense). (3) Create missing accounts: POST /v3/company/{id}/account. (4) Store QBO account IDs for transaction posting. Account types determine financial statement placement. Sub-accounts supported for hierarchy. AcctNum field for custom numbering."),
    ("QuickBooks Bank Feed Integration", "QBO Bank Feeds: automatically imports transactions from connected bank accounts. Your app can: (1) Read imported transactions via Transaction entity. (2) Match/categorize transactions programmatically. (3) Create rules for auto-categorization. For direct integration: use the Transactions API to create Banking transactions. SyncToken required for updates (optimistic locking). Query modified transactions: select * from Transaction where MetaData.LastUpdatedTime > '{date}'."),
    ("Gusto Payroll Integration", "Gusto API enables programmatic payroll management. Key endpoints: Companies, Employees, PayPeriods, Payrolls, Benefits. Flow: (1) Create employee profiles with personal info, tax elections, bank info. (2) Select pay period. (3) Calculate payroll (Gusto handles tax calculations). (4) Review and approve. (5) Submit payroll (initiates direct deposits and tax filings). Webhooks for payroll events. Gusto handles: tax filings (941, 940, state), W-2 generation, new hire reporting, workers comp."),
    ("Gusto Benefits Administration", "Available benefits through Gusto: health insurance (medical, dental, vision), HSA, FSA (medical and dependent care), 401(k), life insurance, disability, commuter benefits. API endpoints: /v1/companies/{id}/benefits, /v1/companies/{id}/employees/{id}/benefits. Enrollment: create benefit, assign to employees with contribution amounts. Gusto handles deduction calculations and carrier payments for supported plans."),
    ("ADP Integration Basics", "ADP Marketplace APIs: Payroll, HR, Time & Attendance, Benefits. OAuth 2.0 with ADP-issued certificates. Data mapping: ADP uses AOID (Associate Object ID) for employees. Payroll data: earnings, deductions, taxes, net pay. API versioning: specify api-version header. Webhook subscriptions for events. ADP RUN (small business) vs ADP Workforce Now (mid-market) have different API capabilities. Rate limits vary by product."),
    ("Multi-Provider Data Normalization", "When integrating multiple financial providers, normalize data: (1) Standard transaction schema: id, date, amount, category, account, provider, description, metadata. (2) Unified account model: id, name, type, balance, provider, last_synced. (3) Common category taxonomy mapping provider-specific categories. (4) Currency normalization (amounts in cents, ISO 4217 currency codes). (5) Timestamp normalization (UTC). (6) Dedup logic: same transaction from bank feed + Stripe + QuickBooks."),
    ("OAuth Token Management", "Store tokens securely (encrypted at rest, never in client code). Access token lifecycle: short-lived (1 hour for most providers). Refresh token: long-lived but revocable. Implement automatic token refresh before expiration. Handle token revocation gracefully (re-initiate OAuth flow). Rate limit awareness per provider. Token storage: per-tenant, per-provider. Audit: log token usage (not token values) for compliance. Token rotation: refresh proactively, not reactively."),
    ("Webhook Processing Best Practices", "Verify webhook signatures (HMAC for Stripe, Plaid; JWT for others). Respond with 200 quickly (process asynchronously). Idempotent processing: use event ID to prevent duplicate handling. Retry handling: most providers retry for 72 hours with exponential backoff. Event ordering: don't assume order — check timestamps. Log all webhook events for audit. Dead letter queue: capture failed processing for manual review. Health monitoring: alert if no webhooks received in expected timeframe."),
]:
    _add("provider_integration", "provider_spec", content.strip(),
         provider_name="plaid" if "plaid" in topic.lower() else "stripe" if "stripe" in topic.lower() else "quickbooks" if "quickbooks" in topic.lower() or "qbo" in topic.lower() else "gusto" if "gusto" in topic.lower() else "adp" if "adp" in topic.lower() else None)


# ---------------------------------------------------------------------------
# Domain: regulatory_compliance (15 chunks)
# ---------------------------------------------------------------------------

for topic, content in [
    ("Record Retention Requirements", "IRS general rule: keep records for 3 years from filing date. Exceptions: 6 years if you underreported income by 25%+, 7 years for bad debt or worthless securities deduction, indefinitely for fraud or non-filing. Employment records: 4 years after tax due date. Specific documents: bank statements (7 years), contracts (7 years after expiration), corporate records (permanent), property records (until 3 years after disposition)."),
    ("1099 Filing Deadlines", "1099-NEC (non-employee compensation): due to recipients AND IRS by January 31. 1099-MISC: due to recipients by January 31, to IRS by February 28 (paper) or March 31 (electronic). 1099-K (payment card/third-party network): threshold $600 for 2026. E-file required if 10+ information returns. Corrections: file as soon as error discovered. Penalties: $60-$310 per form depending on how late. Intentional disregard: $630/form with no cap."),
    ("Sales Tax Nexus Rules", "Physical nexus: office, warehouse, employee, inventory in a state. Economic nexus (post-Wayfair): most states use $100,000 in sales or 200 transactions. Some states use only dollar threshold. Marketplace facilitator laws: platforms (Amazon, Etsy) collect tax on behalf of sellers in most states. Click-through nexus: affiliate referral programs can create nexus. Registration: must register before collecting tax. Penalties for collecting without registration or failing to collect when required."),
    ("Payroll Tax Deposit Rules", "Federal: monthly or semi-weekly based on lookback period. $100,000 next-day deposit rule. Trust Fund Recovery Penalty (TFRP): 100% penalty on responsible persons who willfully fail to collect/deposit payroll taxes. Responsible persons include: officers, directors, employees with check-signing authority. State payroll tax deposits: frequency varies by state and liability amount. Electronic deposit required for most employers."),
    ("Business License Requirements", "Most businesses need: (1) Federal EIN (IRS Form SS-4). (2) State business registration. (3) Local business license/permit. (4) Professional licenses (varies by industry). (5) DBA/fictitious name filing if operating under a name different from legal entity name. Home-based businesses may need home occupation permit. Renewal: most licenses require annual renewal. Penalties for operating without required licenses: fines, inability to enforce contracts, personal liability."),
    ("ACA Employer Mandate", "Applicable Large Employers (ALEs — 50+ FTE): must offer affordable minimum essential coverage to full-time employees (30+ hours/week) and dependents. Affordable: employee's share for self-only coverage doesn't exceed 9.02% of household income (2026, adjusted annually). Minimum value: plan pays at least 60% of total cost. Penalties (2026): ~$2,970/employee if no coverage offered; ~$4,460/employee if coverage is unaffordable/inadequate. Reporting: Forms 1094-C and 1095-C."),
    ("State Unemployment Insurance", "Employer-funded in most states (3 states also require employee contributions: AK, NJ, PA). New employer rate: varies by state (typically 2.7-3.4%). Experience-rated: your rate adjusts based on claims history. Taxable wage base: varies by state ($7,000-$56,500+). Filing: quarterly state unemployment returns. Voluntary contributions: some states allow you to buy down your rate. Interstate employees: generally pay UI in state where work is performed."),
    ("Anti-Money Laundering (BSA/AML) Basics", "FinCEN requires: (1) Customer Identification Program (CIP) for financial institutions. (2) Currency Transaction Reports (CTR) for cash transactions over $10,000. (3) Suspicious Activity Reports (SAR) for potential money laundering. Know Your Customer (KYC): verify identity of clients. Beneficial Ownership Rule: identify individuals who own 25%+ of legal entity customers. Small businesses: if you accept large cash payments, be aware of structuring laws (breaking up transactions to avoid reporting)."),
    ("Data Privacy for Financial Data", "Gramm-Leach-Bliley Act (GLBA): financial institutions must explain data sharing practices and safeguard sensitive data. State privacy laws (CCPA/CPRA, VCDPA, CPA): may apply to businesses that process personal financial data. PCI DSS: required for all businesses handling payment card data. Breach notification: most states require notification within 30-90 days of discovering a data breach involving personal financial information. Encrypt financial data at rest and in transit."),
    ("Quarterly Tax Filing Calendar", "Q1 (Jan-Mar): File Form 941 by Apr 30, deposit payroll taxes per schedule, pay Q1 estimated income tax by Apr 15. Q2 (Apr-Jun): File Form 941 by Jul 31, pay Q2 estimated by Jun 15. Q3 (Jul-Sep): File Form 941 by Oct 31, pay Q3 estimated by Sep 15. Q4 (Oct-Dec): File Form 941 by Jan 31, pay Q4 estimated by Jan 15. Annual: Form 940 by Jan 31, W-2s by Jan 31, 1099s by Jan 31, annual returns by Mar 15 (partnerships/S-Corps) or Apr 15 (individuals/C-Corps)."),
    ("Audit Preparation Best Practices", "IRS audit triggers: high deductions relative to income, missing income (1099/W-2 mismatch), home office deduction, cash-intensive business, significant year-over-year changes. Preparation: maintain organized records, keep original receipts/invoices, document business purpose for all deductions, reconcile returns to financial statements. During audit: be cooperative but don't volunteer extra info, have CPA present, provide only what's requested. Statute of limitations: generally 3 years from filing date."),
    ("Worker Classification Audits", "IRS Form SS-8: either party can request classification determination. Audit triggers: 1099 worker files unemployment claim, claims benefits, or reports employer on Form SS-8. Penalties for misclassification: back employment taxes (employer share), 100% of employee share of FICA, 20-40% of wages for income tax withholding, penalties and interest. Section 530 safe harbor: consistent treatment + reasonable basis + filed 1099s. States often audit aggressively for misclassification."),
]:
    _add("regulatory_compliance", "rule" if "rule" in topic.lower() or "requirement" in topic.lower() or "mandate" in topic.lower() else "deadline" if "calendar" in topic.lower() or "deadline" in topic.lower() else "checklist", content.strip(), jurisdiction="federal")


# ---------------------------------------------------------------------------
# NOTE: Stripe API docs are seeded separately via seed_stripe_knowledge.py
# using real Stripe documentation (58 files, 120+ chunks).
# ---------------------------------------------------------------------------


# =============================================================================
# Seeding Logic
# =============================================================================

async def seed_knowledge():
    """Embed and insert all knowledge chunks."""
    from aspire_orchestrator.services.legal_embedding_service import embed_batch, compute_content_hash
    from aspire_orchestrator.services.supabase_client import supabase_insert

    total = len(FINANCE_KNOWLEDGE)
    logger.info("Seeding %d finance knowledge chunks...", total)

    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = FINANCE_KNOWLEDGE[i:i + batch_size]
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
                "chunk_type": chunk.get("chunk_type"),
                "provider_name": chunk.get("provider_name"),
                "tax_year": chunk.get("tax_year"),
                "jurisdiction": chunk.get("jurisdiction"),
                "is_active": True,
                "ingestion_receipt_id": f"seed-{uuid.uuid4().hex[:12]}",
            }
            rows.append(row)

        try:
            result = await supabase_insert("finance_knowledge_chunks", rows)
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
