# Boltzmann interface

Design guidance: [Anthropic Frontend Design](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md).

## Direction

An infrastructure console for supervising durable coding tasks. The defining
interaction is a visible human decision above a live execution workspace.
Prioritize the task, its current state, and the next decision. Avoid marketing
language, decorative charts, and repeated summary cards.

## Tokens

- Canvas: graphite `#101114`; navigation: `#0c0d10`; raised surface: `#181a20`.
- Primary text: `#edf0f5`; blue accent: `#849dff`; action amber: `#f5c575`.
- System sans (SF Pro on macOS) for the console; system mono for code, IDs,
  and execution receipts. Body 13–14px, metadata 11–12px, titles 28–32px.
- Spacing follows a 4px unit; controls 36px, touch targets at least 44px.
- Small control radii, larger workspace and modal radii. Borders separate
  functional regions; shadows belong to elevated overlays.

## Layout

Left-aligned workspace with persistent navigation and compact service status.
The overview exposes pending decisions, a task composer, and recent work.
Task screens keep the decision bar pinned under the header, followed by
execution, artifacts, checks, and optional session details.

    navigation | workspace / repository                service status
               | [human decision + action, when required]
               | title + task status
               | execution / review / files / checks
               | agent lifecycle and artifact inspector

The original oversized welcome treatment is replaced by working content.
Amber is reserved for human decisions and warnings, blue for navigation and
running work, green for completion. The execution map's subtle grid is the
only patterned surface. Preserve keyboard focus, reduced motion, native
dialogs, and all approval semantics. Verify long content and narrow screens.
