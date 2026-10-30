---
name: Voice AI Studio
description: Local console for configuring and testing bilingual voice agents.
colors:
  background: "#171611"
  surface: "#211f1a"
  surface-raised: "#2b2821"
  border: "#474238"
  foreground: "#f2ecdf"
  muted: "#aea696"
  accent: "#c98753"
  on-accent: "#21170f"
  accent-hover: "#dba16f"
  success: "#8f9f6f"
  error: "#d28373"
typography:
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.25
rounded:
  sm: "8px"
  md: "12px"
spacing:
  sm: "16px"
  md: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
    height: "40px"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
    height: "40px"
  field-control:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
    width: "100%"
  navigation-active:
    backgroundColor: "{colors.surface}"
    textColor: "#ebc19b"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
  surface:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.md}"
    padding: "16px"
---

# Voice AI Studio — Web Design Contract

## Direction

The studio is a calm, local operator console for configuring bilingual voice
agents and testing saved configurations. Its visual language uses warm ink,
limestone text, oxidized copper actions, and moss for confirmed active or
successful state. Keep the existing Next.js and Tailwind implementation; do not
introduce remote fonts, assets, runtime services, or a second design system.

## Layout and hierarchy

- Desktop navigation is a persistent left rail. At narrow widths it becomes a
  horizontal, scrollable navigation row above the workspace.
- Pages start with a clear title and a short task description. Configuration
  uses grouped sections and list rows rather than repeating a hero and a grid
  of decorative cards.
- The overview identifies the agent selected for new sessions and links to the
  agent list, shared instructions, and call test. It reports actual saved
  configuration only; do not add placeholder latency, memory, readiness, or
  benchmark values.
- The agent editor separates identity and persona, English/Hindi voice,
  language model, and speech recognition settings. Saving configuration and
  selecting the default agent are separate actions. Voice and provider choices
  are native radio groups so their selection and keyboard behavior stay
  accurate.
- Shared instructions are editable and apply across agents. An agent persona
  describes that agent's role and tone. The call test shows the selected saved
  profile, connection state, and generated/user text with labels that match
  what the runtime actually emits.
- Flow QA shares the saved-snapshot testing area: list cases before queueing, keep run status and results readable in a compact panel, and make cancellation explicit. Suggestions are clearly marked draft-only, with separate apply and dismiss actions; never publish as a side effect.

## Tokens

The frontmatter records the primary role tokens. `src/app/globals.css` is
canonical for CSS variables and shared control styles; `tailwind.config.ts` is
canonical for the full ink, limestone, copper, moss, and rust color scales.

| Token | Use |
| --- | --- |
| `--background` | Workspace canvas |
| `--surface` | Raised surface |
| `--surface-raised` | Secondary controls |
| `--border` | Dividers and borders |
| `--foreground` | Main text |
| `--muted` | Supporting text |
| `--accent` | Primary actions and focus |
| `--success` | Confirmed selected/success state |
| Tailwind `rust-400` | Errors and destructive actions |

The full ink, limestone, copper, moss, and rust scales live in the Tailwind
configuration. Use `surface`, `field-label`, `field-control`,
`button-primary`, and `button-secondary` for shared primitives. Keep corners
near 8–12px, with spacing driven by content groups instead of nested panels.

## Interaction and local operation

- Use associated labels, semantic headings and fieldsets, native controls,
  visible keyboard focus, and clear loading, empty, error, and saved states.
- Respect `prefers-reduced-motion`. Motion should communicate a real state
  change; do not use decorative pulsing or glow.
- Connection state becomes connected only after the peer connection reports
  that state. Do not imply a profile is live merely because it is selected.
- The app is an unauthenticated local tool. Its web server binds to
  `127.0.0.1`; do not expose it on the LAN or add external runtime calls.
- Render measured runtime facts only when the UI receives the corresponding
  measurements and their conditions.
