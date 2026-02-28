# Patent Invariant Proof Tests (Placeholders)

Add automated proof tests here. The goal is not “coverage,” it’s **proof of invariants**.

Recommended test groups:

1) RLS Evil Tests
- suite A cannot read suite B on approvals/receipts/outbox/a2a/provider logs

2) Presence Enforcement Tests
- approve high-risk without presence session → VIDEO_REQUIRED
- approve with expired session → PRESENCE_EXPIRED

3) Capability Binding Tests
- high-risk job requires capability consumption before provider call

4) No Shadow Execution
- CI grep/lint that blocks provider SDK imports outside `gateway/adapters` + `executor`

5) Replay Smoke
- given a trace_id from a known fixture, replay builds the same chain of evidence
