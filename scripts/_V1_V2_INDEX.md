# backend/scripts/ — V2 Anam setup + ops

| File | Status | Purpose |
|------|--------|---------|
| `setup-anam-personas.ts` | `[v2]` | Canonical V2 Anam persona setup — creates / updates Ava-Anam (`6ac64cc3-...` setup ID, but live runtime uses `58f82b89-...` from desktop env) and Finn-Anam (`b6852adf-...`). Also defines avatar IDs (Cara, Thomas) and voice IDs (Hope, Jack John). |
| `retest_full_v2.py` | `[shared]` | Test harness. |
| (other scripts) | `[shared]` | Operational scripts. |

## Note on Ava-Anam dual ID

`setup-anam-personas.ts` line 102 sets `KNOWN_PERSONA_IDS.ava = "6ac64cc3-68c4-4791-962b-1ec7974e0682"`. This is a stale setup-time reference; the live runtime persona is `58f82b89-8ae7-43cc-930d-be8def14dff3` (set in `Aspire-desktop/server/routes.ts:22-23` and `Aspire-desktop/.env:8`). When this script is next edited, update line 102 to the live ID.
