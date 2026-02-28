# Patent Blueprint Add-on (Docs-Only)

Purpose:
- Use `aspire_saas_patent_pack.zip` as an architecture blueprint **without** mixing it into Trust Spine code.
- Create a single mapping from patent invariants → implementation modules → proof tests.
- Align identity naming: `suite_id` (company) + `office_number` (seat label) + `office_id` (canonical seat id).

How to apply:
1) Unzip this add-on into your main repo root (the repo created from `Aspire_Handoff_v1_Full_TrustSpine_Brain_Gateway_NO_PLAN.zip`).
2) Extract your `aspire_saas_patent_pack.zip` into:
   `docs/patent-pack/aspire_saas_patent_pack/`
3) Treat these docs as read-only references; canonical implementation contracts live in `/contracts/`.

What this add-on DOES NOT do:
- It does not change migrations or runtime code.
- It does not add provider integrations.
