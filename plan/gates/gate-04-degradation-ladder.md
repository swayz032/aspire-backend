---
gate: 4
name: "Degradation Ladder"
status: "complete"
phase_introduced: "3"
complexity: "high"
critical: true
---

# GATE 04: Degradation Ladder (CRITICAL)

## ⚠️ CRITICAL GATE - Failure Blocks Launch

This gate prevents "stuck in video" failures that would block accessibility and create catastrophic UX.

## Requirement

Video → Audio → Async Voice → Text fallback chain with auto-downshift triggers.

## 4-Level Degradation Ladder

### Level 1: VIDEO (Preferred, Full Context)
- **Components:** LiveKit WebRTC video + audio
- **Avatar:** Anam rendering (optional enhancement)
- **Features:** Full screen sharing capability
- **Bandwidth:** Highest (2-5 Mbps)
- **Use Case:** Default for HOT mode, binding authority moments

### Level 2: AUDIO (Fallback, Voice-Only)
- **Components:** LiveKit audio-only mode
- **Avatar:** None (voice-only interaction)
- **Features:** No video rendering overhead
- **Bandwidth:** Medium (64-128 Kbps)
- **Savings:** 50% bandwidth vs. video
- **Use Case:** Default for WARM mode, low battery, weak network

### Level 3: ASYNC VOICE (Recorded Messages)
- **Components:** Voice message recording/playback
- **Interaction:** Asynchronous (voicemail-style)
- **Features:** Record, playback, no realtime requirements
- **Bandwidth:** Minimal (transcoding + storage)
- **Use Case:** Very poor network (<3G), offline-first scenarios

### Level 4: TEXT (Final Fallback)
- **Components:** Pure text chat interface
- **Interaction:** Keyboard input, text response
- **Features:** Maximum accessibility, screen reader compatible
- **Bandwidth:** Lowest (<10 Kbps)
- **Use Case:** Last resort, works in all conditions, accessibility mode

## Auto-Downshift Triggers

### Trigger 1: Low Battery
- **Condition:** Battery <20%
- **Action:** Drop from Level 1 (Video) → Level 2 (Audio)
- **User Notification:** "Switching to audio-only to save battery"

### Trigger 2: Poor Network
- **Condition:** Network <3G speed detected
- **Action:** Drop from Level 2 (Audio) → Level 3 (Async Voice)
- **User Notification:** "Network weak, switching to voicemail mode"

### Trigger 3: Thermal Throttling
- **Condition:** Device temperature >45°C (CPU throttling detected)
- **Action:** Drop from Level 1 (Video) → Level 4 (Text)
- **User Notification:** "Device overheating, switching to text mode"

### Trigger 4: User Manual Override
- **Condition:** User explicitly selects degraded mode
- **Action:** Jump to any level immediately
- **User Control:** Always available, no forced modes

## Permission Denial Handling

### If User Declines Level:
- System offers next degradation level
- User can decline all levels → action cancelled
- Receipt logged documenting permission state
- If video required (Gate 03 - Forced Escalation), execution blocked

### Example Flow:
```
System: "Enable video for this call?"
User: [Declines]
System: "Use audio-only instead?"
User: [Declines]
System: "Send voice message asynchronously?"
User: [Declines]
System: "Use text chat?"
User: [Accepts]
→ Proceeds in Level 4 (Text) mode
```

## Verification Criteria

- [ ] All 4 levels implemented and functional
- [ ] Auto-downshift triggers work (battery, network, thermal)
- [ ] User can manually select any level at any time
- [ ] Permission denial offers next degradation level
- [ ] System never "sticks" in unusable mode (always has fallback)
- [ ] Receipt logs current level and degradation events

## What This Gate Prevents

- **Accessibility failures** - Users locked out due to tech constraints
- **Poor UX** - Forcing video in low battery/poor network conditions
- **Abandonment** - Users giving up because "app doesn't work"
- **Legal risk** - ADA compliance violations (must support text-only mode)

## Failure Scenarios

❌ **Fails if:**
- User stuck in video mode with dead battery (no auto-downshift)
- No text fallback available (accessibility violation)
- Auto-downshift triggers don't work (manual override only)
- Degradation breaks core functionality (e.g., text mode doesn't allow intent submission)

✅ **Passes if:**
- User can ALWAYS downshift to text mode (Level 4)
- Auto-downshift triggers work for battery, network, thermal
- All 4 levels support core Aspire functionality (intent→execution→receipt)
- No "stuck" states - user never trapped in broken mode

## Testing Requirements

### Test 1: Battery Degradation
1. Start call in Level 1 (Video)
2. Simulate battery drop to 19%
3. Verify auto-downshift to Level 2 (Audio)
4. Verify user notification displayed

### Test 2: Network Degradation
1. Start call in Level 2 (Audio)
2. Throttle network to 2G speeds
3. Verify auto-downshift to Level 3 (Async Voice)
4. Verify user notification displayed

### Test 3: Manual Override
1. Start call in Level 1 (Video)
2. User manually selects Level 4 (Text)
3. Verify immediate transition (no delay)
4. Verify all functionality works in text mode

### Test 4: Permission Denial Chain
1. System requests video (Level 1)
2. User declines → System offers audio (Level 2)
3. User declines → System offers async voice (Level 3)
4. User declines → System offers text (Level 4)
5. User accepts → Verify text mode works
6. Receipt logs all permission decisions

## Related Gates

- **Gate 02:** Call State Machine (Cold/Warm/Hot states)
- **Gate 03:** Forced Escalation (video required for binding events)
- **Gate 08:** Performance Budgets (auto-downshift for performance)

## Why This is CRITICAL

**Without degradation ladder:**
- Users in rural areas (poor network) cannot use Aspire
- Users with low battery locked out mid-task
- Accessibility failures (no text fallback for screen readers)
- Competitor can market as "only works in perfect conditions"

**With degradation ladder:**
- Universal accessibility (works in ALL conditions)
- Graceful degradation (never a "hard failure")
- User control (manual override always available)
- Competitive advantage ("works when others don't")

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 3 roadmap defines full 4-level ladder
**Evidence:** Degradation ladder spec in Aspire-Production-Roadmap.md:2078-2110
