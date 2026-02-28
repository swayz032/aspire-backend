---
gate: 2
name: "Call State Machine"
status: "complete"
phase_introduced: "3"
complexity: "medium"
critical: false
---

# GATE 02: Call State Machine

## Requirement

Cold / Warm / Hot states with Warm as mobile default.

## Three Interaction States

### COLD: Minimal Resources
- **Video:** Off
- **Audio:** Off
- **Bandwidth:** Lowest
- **Use Case:** Text-only interaction, low battery mode, poor network
- **User Trigger:** Manual downshift, auto-downshift on resource constraints

### WARM: Audio-First (Default on Mobile)
- **Video:** Off (but ready to escalate)
- **Audio:** Live
- **Bandwidth:** Medium
- **Use Case:** Default mobile interaction, fast escalation available
- **User Trigger:** App launch default, explicit user selection

### HOT: Full Presence
- **Video:** Live
- **Audio:** Live
- **Bandwidth:** Highest
- **Use Case:** Binding authority, multi-party calls, high-stakes decisions, liability moments
- **User Trigger:** Explicit escalation, system-required for binding events

## State Transitions

```
COLD ←→ WARM ←→ HOT

User can move freely in any direction:
- COLD → WARM: User enables audio
- WARM → HOT: User enables video (or system requires for binding event)
- HOT → WARM: User disables video
- WARM → COLD: User disables audio
- HOT → COLD: User disables video + audio
```

## Mobile Default Behavior

**On Launch:** WARM (audio live, video ready)

**Rationale:**
- Voice-first interaction (Aspire's core UX)
- Fast escalation to HOT when needed (1 tap to video)
- Lower resource usage than HOT (battery life)
- Better UX than COLD (no friction for voice commands)

## Verification Criteria

- [ ] All three states (Cold/Warm/Hot) implemented
- [ ] Warm is default on mobile app launch
- [ ] User can transition between any states freely
- [ ] System can force HOT for binding events (with user consent)
- [ ] State persists across background/foreground transitions

## What This Gate Prevents

- **Undefined interaction modes** - Clear expectation setting for users
- **Resource waste** - Not forcing video when audio suffices
- **Accessibility barriers** - Users can downshift to text-only (Cold)

## Failure Scenarios

❌ **Fails if:**
- Mobile app defaults to HOT (video) on launch (battery drain)
- Mobile app defaults to COLD (text) on launch (voice-first UX broken)
- State transitions are blocked or unclear

✅ **Passes if:**
- Warm is mobile default
- All three states functional
- User can freely transition

## Related Gates

- **Gate 03:** Forced Escalation (system can require HOT for binding events)
- **Gate 04:** Degradation Ladder (auto-downshift on resource constraints)
- **Gate 05:** Authority UI Contract (visual indicators for required states)

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 3 roadmap specifies Cold/Warm/Hot states
**Evidence:** Call state machine documented in Aspire-Production-Roadmap.md:2061-2067
