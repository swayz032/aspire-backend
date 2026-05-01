---
name: Incoming Call Overlay chrome parity (Lane F)
description: Phone IncomingCallOverlay matches IncomingVideoCallOverlay chrome 1:1; caller-ID lookup wires through store + resolver pattern.
type: project
---

# IncomingCallOverlay chrome — locked to video reference

Lane F of plan `the-image-was-off-calm-lynx.md` rewrote the phone
`IncomingCallOverlay.tsx` to match `IncomingVideoCallOverlay.tsx` chrome
exactly per §3.10 alignment table.

**Why:** Owner signed off on chrome unification; only contextual content
(caller identity, detail rows) differs between the two overlays.

**How to apply:** When extending the phone overlay (new states, detail
rows, etc.), keep card width / hero / divider / button chrome UNCHANGED —
modify only the inner caller-detail content. If a chrome tweak is wanted,
apply to BOTH overlays in the same change so they stay paired.

## Locked tokens
- Card 440px, radius 16, bg `#1E1E1E`
- Backdrop `rgba(0,0,0,0.72)` + `backdrop-filter: blur(20px)`
- Hero 148px abstract Aspire-blue ambient (NOT a photo for voice context)
- Label 11px `#3B82F6`, letter-spacing 1.6
- Detail card `#242426`, structured rows + 56px supplementary avatar
- Decline ghost / Answer linear-gradient `#3B82F6 → #2563EB`

## Files
- `Aspire-desktop/components/calls/IncomingCallOverlay.tsx` (rewrite)
- `Aspire-desktop/lib/incomingCallOverlayStore.ts` (extended w/ resolver)
- `Aspire-desktop/components/calls/IncomingCallOverlay.demo.tsx` (3 fixtures)
- `Aspire-desktop/app/demo/incoming-call.tsx` (fixture launcher)

## Resolver pattern
Component registers a `CallerIdResolver` via `registerCallerIdResolver(fn)`
on mount. Store's `triggerIncomingCall(call)` shows overlay + fires
resolver in parallel. Stale-result guard via per-call AbortController.
Demo bypass: `setResolvedCaller(payload)` skips the resolver.

## Bundle build blocker discovered
`Aspire-desktop/components/calls/setup/PublicNumberSection.tsx` line 13 has
`*21*/*72` inside a JSDoc — the `/*` opens a block comment that doesn't
close until much later, breaking the parse and blocking the entire bundle
build (`pnpm build:static`). Lane A territory; do not fix without owner ask.
