<!-- domain: business_context, subdomain: party_patterns, chunk_strategy: heading_split -->

# Party Identification Patterns

## Sender vs. Client Role Assignment

### Determining the Sender
In PandaDoc templates, the "Sender" is the party initiating the document — typically the Aspire user (the business owner).

**Sender identification rules:**
1. The logged-in Aspire user is ALWAYS the Sender unless explicitly stated otherwise
2. Sender data is pulled from the user's Aspire profile: business name, address, email, phone
3. If the user has an LLC or corporation, the Sender entity is the business (not the individual)
4. Sole proprietors: Sender is the individual doing business as (DBA) or their registered business name

### Determining the Client
The "Client" is the counterparty — the person or entity the Sender is contracting with.

**Client identification rules:**
1. Clara extracts client information from the user's intent and conversation context
2. Minimum required: name and email (for signing)
3. Preferred: full name, company name, email, address
4. If client is a business entity, the signer may be different from the entity (e.g., "Doe Plumbing LLC" is the party, "John Doe" is the authorized signer)

### Multi-Party Contracts
Some contracts involve more than two parties:
- Subcontractor agreements: GC (Sender), Subcontractor (Client), Owner (referenced but not signing)
- Joint ventures: Partner A (Sender), Partner B (Client) — may need custom roles
- Lease with guarantor: Landlord (Sender), Tenant (Client), Guarantor (additional signer)

## Business Entity Types

### Sole Proprietorship
- **No formal entity** — individual operates under their own name or a DBA
- Contract is between the individual and the counterparty
- **Personal liability:** Owner is personally liable for all business obligations
- **Signing authority:** Owner signs in their individual capacity
- **Tax ID:** Social Security Number or EIN (if they have employees or choose to obtain one)
- **Contract naming:** "John Doe, doing business as Doe Plumbing" or "John Doe d/b/a Doe Plumbing"

### Limited Liability Company (LLC)
- **Separate legal entity** — provides liability protection
- Contract is between the LLC and the counterparty
- **Signing authority:** Managing member or authorized manager signs on behalf of the LLC
- **Signature block format:** "[Name], Managing Member of [LLC Name]"
- **Operating agreement:** Governs who has authority to bind the LLC
- **Single-member vs. multi-member:** Single-member LLCs may face piercing the corporate veil if not properly maintained (commingling funds, no operating agreement)

### Corporation (Inc. / Corp.)
- **Separate legal entity** with shareholders, directors, and officers
- Contract is between the corporation and the counterparty
- **Signing authority:** Officers (President, VP, Secretary, Treasurer) typically have authority to bind the corporation; board resolution may be required for major contracts
- **Signature block format:** "[Name], [Title] of [Corporation Name]"
- **Authority verification:** Counterparty may request board resolution or certificate of authority for significant contracts

### Partnership (General / Limited)
- **General partnership:** All partners are personally liable; any partner can bind the partnership
- **Limited partnership (LP):** General partners manage and have personal liability; limited partners are passive investors with limited liability
- **Signing authority:** General partners can bind the partnership; limited partners cannot
- **Signature block format:** "[Name], General Partner of [Partnership Name]"

### Professional Corporation / PLLC
- Used by licensed professionals (attorneys, CPAs, physicians, engineers)
- Similar structure to LLC/Corp but with professional licensing requirements
- Professionals maintain personal liability for their own professional negligence (entity does not shield from malpractice)
- Non-licensed individuals generally cannot be owners

## Signing Authority Verification

### When Authority Matters
For small business contracts (our ICP), authority is usually straightforward — the owner signs. But Clara should flag these situations:

**Multi-member LLC:** If the Aspire user is one of multiple LLC members, verify they have authority to bind the LLC for the contract value/type.

**Corporate officers:** Not all officers have authority for all contracts. A VP of Marketing may not have authority to sign a $100K vendor agreement.

**Spousal consent:** In community property states (AZ, CA, ID, LA, NV, NM, TX, WA, WI), contracts affecting community property may require both spouses' consent.

**Franchise operations:** Franchisees may need franchisor approval for certain contracts (especially leases, major vendor agreements, and territory-related agreements).

### Representation of Authority
Standard contract clause: "Each party represents that the person signing this Agreement has the authority to bind such party and that this Agreement constitutes a valid and binding obligation."

## Individual vs. Entity Signing

### Personal Guarantee Trap
When a business owner signs a contract, it matters whether they sign as an individual or as an entity representative:

**Correct (entity signing):**
```
Doe Plumbing LLC
By: ________________________
Name: John Doe
Title: Managing Member
```

**Problematic (individual signing for entity):**
```
________________________
John Doe
```
This second format may create personal liability even if an LLC exists. The signer should always identify the entity they represent.

### Personal Guarantee (Intentional)
Some contracts require a personal guarantee in addition to the entity obligation:
- Commercial leases (common)
- Equipment financing
- SBA loans
- Major vendor credit lines

A personal guarantee is a separate undertaking — the individual agrees to be personally liable if the entity cannot pay. Clara should flag personal guarantee provisions and explain the implications.

## Contact Information Requirements

### Minimum for Contract Generation
- **Full legal name** (individual or entity)
- **Email address** (for PandaDoc signing invitation)

### Preferred for Complete Contract
- Full legal name (individual)
- Business/entity name (if applicable)
- Title/role within entity
- Email address
- Phone number
- Mailing address (for notices clause)
- State of incorporation/registration (for entity verification)

### Notice Address
Contracts typically require a formal notice address for each party. This should be a physical address (not just email) for service of legal notices. Clara should collect this during contract generation.
