---
name: Research Card Patterns (Wave 1)
description: Hotel, Product, Business card components for Adam research modal — patterns, tokens, card interface
type: project
---

## Research Card Components (Wave 1, 2026-04-06)

**Location**: `Aspire-desktop/components/cards/{HotelCard,ProductCard,BusinessCard}.tsx`

**CardProps interface** (task-specified, richer than CardRegistry's):
- `record, artifactType, index, total, confidence, onAction, isActive`

**Why:** These cards are CONTENT components wrapped by BaseCard shell (another agent's responsibility). The CardRegistry has a simpler `CardProps` — the BaseCard adapter will bridge the two interfaces.

**How to apply:** When building Wave 2 cards (PropertyCard, OpportunityCard, etc.), follow the same pattern: own CardProps interface, 500px width, hero area + content + action buttons, exported from barrel index.ts.

### Established Patterns
- Card width: 500px fixed, hero 200px (hotel/product) or 160px (business)
- Hero: `expo-image` for photos, `expo-linear-gradient` fallback with icon + text
- Action buttons: Outlined cyan border, 40px height, Pressable with opacity + cyanLight bg press
- Stars: `renderStars()` helper using Unicode star characters
- Active state: Cyan border + web `boxShadow` glow via `Platform.select` + `as unknown as ViewStyle`
- All values from `Colors`, `Spacing`, `Typography`, `BorderRadius` tokens
- `StyleSheet.create()` for all styles (codebase convention)
