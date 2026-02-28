# Claude Code Hooks Setup (Aspire)

## What you get
- `.claude/hooks/stop.sh` — stop hook that generates reflection proposals
- `.claude/session-notes.md` — append durable corrections/patterns during the session
- `scripts/reflect.py` — writes diffs into `proposed/reflect-*/`

## Workflow
1. During a session, append items into `.claude/session-notes.md`.
2. On stop, `stop.sh` runs and generates diff bundles in `proposed/`.
3. Review diffs and manually apply the changes you want (then commit).

## Optional (later): apply allowed proposals automatically
Run:
`python3 scripts/reflect.py --session-notes .claude/session-notes.md --skills-root skills --out proposed --apply`

High-risk files are never auto-applied.


## Reflection Receipt
Each reflect run writes `reflection-receipt.json` inside the `proposed/reflect-*` bundle.
