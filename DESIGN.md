---
name: Portfolio Pi
description: Self-hosted wealth tracker with a warm-paper, teal-ink private ledger aesthetic
colors:
  paper: "#f4efe8"
  paper-shade: "#efe4d6"
  ink: "#171b1a"
  muted-ink: "#66706b"
  ledger-teal: "#0f6a63"
  ledger-teal-deep: "#0b5751"
  copper-accent: "#c9792b"
  growth-green: "#2d7b53"
  alert-red: "#b54848"
  surface-glass: "rgba(255,255,255,.84)"
  surface-glass-faint: "rgba(255,255,255,.58)"
  surface-strong: "#fffdf9"
  hairline: "rgba(28,35,34,.10)"
typography:
  display:
    fontFamily: "Satoshi, Inter, system-ui, sans-serif"
    fontSize: "clamp(1.9rem, 3vw, 2.9rem)"
    fontWeight: 700
    lineHeight: 0.96
    letterSpacing: "-0.06em"
  title:
    fontFamily: "Satoshi, Inter, system-ui, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 700
    letterSpacing: "-0.03em"
  body:
    fontFamily: "Satoshi, Inter, system-ui, sans-serif"
    fontSize: "0.92rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Satoshi, Inter, system-ui, sans-serif"
    fontSize: "0.72rem"
    fontWeight: 700
    letterSpacing: "0.14em"
rounded:
  sm: "0.8rem"
  md: "1.15rem"
  lg: "1.35rem"
  pill: "999px"
spacing:
  xs: "0.45rem"
  sm: "0.75rem"
  md: "1rem"
  lg: "1.35rem"
components:
  button-primary:
    backgroundColor: "{colors.ledger-teal}"
    textColor: "#ffffff"
    rounded: "{rounded.pill}"
    padding: "0.62rem 0.88rem"
  button-ghost:
    backgroundColor: "{colors.surface-glass-faint}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0.72rem 1rem"
  input:
    backgroundColor: "{colors.surface-glass-faint}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.7rem 0.9rem"
    height: "46px"
  card:
    backgroundColor: "{colors.surface-glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "1.1rem"
---

# Design System: Portfolio Pi

## Overview

**Creative North Star: "The Private Ledger"**

Portfolio Pi looks like a beautifully kept private bank ledger brought to a screen: warm paper instead of sterile white, deep teal ink instead of default blue, and one measured brush of copper where attention should land. Every page sits on the same atmosphere — a layered background of soft radial glows over warm paper (with an optional faint 28px grid overlay), translucent glass surfaces floating on ambient shadows. Nothing shouts; numbers are the protagonists and chrome recedes behind blur and hairlines.

The voice is calm, warm precision. Density is comfortable rather than cramped: generous card padding (≈1.1–2rem), a small rem-based spacing scale, and tight negative-tracked headlines that give each page a confident masthead. Color is disciplined — teal carries identity and actions, copper appears only in gradients, kickers, and hairline accents, and green/red are reserved strictly for money direction (gains vs. losses).

Both themes are first-class citizens: every token has a dark counterpart applied automatically via `prefers-color-scheme`, with `color-scheme` set per theme. New UI must define its colors through the CSS custom properties so the dark theme stays automatic.

**Key Characteristics:**

- Warm paper background with layered radial glows; never flat white or pure black
- Translucent glass cards (`backdrop-filter: blur(14px)`) on hairline borders
- Full-pill interactive controls; soft-cornered (0.8–1.35rem) containers
- Ambient two-layer soft shadows at rest; lift-by-1px hover feedback
- Satoshi typeface throughout, tight-tracked bold headlines, uppercase tracked labels
- Teal = brand/action, copper = accent warmth, green/red = money semantics only

## Colors

A warm neutral paper world with one deep teal voice and a copper whisper; semantic greens and reds stay in the ledger, not the furniture.

### Primary
- **Ledger Teal** (#0f6a63): the app's single action color — primary buttons, links of emphasis, focus rings, active states, chart identity. Dark theme shifts it to #6eb7ac (hover #8cc8bf).
- **Ledger Teal Deep** (#0b5751): hover/pressed end of the primary gradient. Dark-theme equivalent is #8cc8bf.

### Secondary
- **Copper Accent** (#c9792b): warmth reserved for hero/header gradients, kicker pills, top hairlines on cards, and the secondary stop of primary-button gradients. Never used for large fills. Dark theme: #e2a15f.

### Tertiary
- **Growth Green** (#2d7b53): positive money movement, success badges, gains. Appears only in data contexts. (Calendar/expenses pages use a near-identical variant #2f7d42 — treat #2d7b53 as canonical when touching new code.) Dark theme: #73bf8b.
- **Alert Red** (#b54848): errors, destructive buttons, losses. Dark theme: #ea7b7b.

### Neutral
- **Paper** (#f4efe8): page background base; layered under radial glows of Paper Shade (#efe4d6) and teal.
- **Ink** (#171b1a): all primary text. Dark theme: #eef1ed.
- **Muted Ink** (#66706b): secondary copy, subtitles, inactive tabs. Dark theme: #9aa7a0.
- **Surface Glass** (`rgba(255,255,255,.84)`): standard card/header fill, always paired with `backdrop-filter: blur(14px)`. Dark: `rgba(24,30,29,.84)`.
- **Surface Glass Faint** (`rgba(255,255,255,.58)`): inputs, ghost buttons, tab wells, tint layers. Dark: `rgba(34,42,40,.72)`.
- **Surface Strong** (#fffdf9): opaque panels where glass would hurt legibility. Dark: #1a2120.
- **Hairline** (`rgba(28,35,34,.10)`): the only border color; always 1px. Dark: `rgba(255,255,255,.08)`.

### Named Rules
**The Ink-on-Paper Rule.** Text is always Ink or Muted Ink on paper/glass surfaces — never pure black (#000) or pure white (#fff) except white-on-teal inside primary buttons.

**The Copper Whisper Rule.** Copper appears only as small doses (kickers, gradient stops, hairlines). If a copper area could dominate a viewport, it's wrong.

## Typography

**Display Font:** Satoshi (700/500/400) via Fontshare, falling back to Inter, then system-ui
**Body Font:** Satoshi 400 (same stack)
**Label/Mono Font:** none distinct — labels are uppercase Satoshi 700

**Character:** A geometric-humanist sans that reads modern without feeling techy; tight tracking on big sizes gives headlines an engraved-ledger confidence while body copy stays airy at 1.6 line-height.

### Hierarchy
- **Display** (700, clamp(1.9rem, 3vw, 2.9rem), line-height ≈0.96, tracking −0.06em): page mastheads inside header cards; login hero goes larger (clamp(2.2rem, 5vw, 4rem), −0.08em).
- **Title** (700, 1.05rem, tracking −0.03em): card headings; sub-headings at 0.88rem.
- **Body** (400, 0.92–1rem, line-height 1.6): all running copy, max-width ≈44–64ch in narrative blocks.
- **Label** (700, 0.72rem, letter-spacing +0.14em, UPPERCASE): form field labels and kicker pills — the system's signature micro-typography.

### Named Rules
**The Tight Masthead Rule.** Display-size headings always carry negative tracking (−0.04em or tighter) and weight 700; never render a loose or light masthead.

## Layout

Single-column shell centered on the page: `.shell { max-width: 1320px }` (mortgage page widens to 1400px) with `1.25rem` inline padding and ~2rem bottom breathing room. Content stacks as full-width glass cards separated by ~1rem gaps; complex pages use CSS grid splits (e.g. `minmax(280px,360px) minmax(0,1fr)` for simulator layouts). Spacing follows the four-step rem scale (xs .45 / sm .75 / md 1 / lg 1.35rem). Responsive behavior is simple collapse: grids drop to one column around 880px, headers wrap via flex-wrap, and touch targets keep ≥40px min-height. Density stays comfortable — padding inside cards is never tightened below the sm step.

## Elevation & Depth

Depth is ambient, not structural. Surfaces rest permanently on soft two-layer shadows and separate from the background through translucency plus backdrop blur rather than stacking levels. There is no z-depth hierarchy of shadows — one elevated value for headers/heroes, one soft value for everything else.

### Shadow Vocabulary
- **Ambient High** (`box-shadow: 0 24px 60px rgba(42,42,32,.08)`): page headers, hero panels, login card. Dark theme: `0 24px 60px rgba(0,0,0,.28)`.
- **Ambient Low** (`box-shadow: 0 8px 24px rgba(42,42,32,.06)`): resting state of all buttons and standard cards. Dark theme: `0 8px 24px rgba(0,0,0,.18)`.

### Named Rules
**The Soft Rest Rule.** Buttons and cards float at rest on Ambient Low; nothing ever receives a hard, tight, or colored drop shadow, and shadows never appear as hover-only surprises.

## Shapes

Two corner languages coexist by role. Containers are softly rounded: 1.15rem standard (`--radius`), 1.35rem for page headers (calc(+.2rem)), 0.8rem for inner elements like inputs and tabs. Interactive controls are always full pills (999px) — buttons, icon buttons, chips, kickers, scrollbars alike. Borders are exclusively 1px Hairline; emphasis comes from color-mix tints toward teal/copper, never thicker strokes. Cards often carry a signature detail: a fading copper hairline across their top edge (gradient to transparent at 65%).

## Components

### Buttons
- **Shape:** full pill (999px); min-height 40–46px
- **Primary:** Ledger Teal fill (in practice a 135° gradient from teal into a 25% copper mix), white text, 600–700 weight; hover deepens the gradient and lifts `translateY(-1px)`
- **Ghost:** Surface Glass Faint pill with hairline border and Ambient Low shadow; hover tints background/border ~28% toward teal while lifting 1px; press pushes `translateY(1px)` and drops the shadow
- **Danger/Edit variants:** tiny pills tinted 6% toward red/teal over transparent, bold colored text
- **Icon buttons:** 42px circles whose label expands out of the pill on hover/focus (max-width transition, 0.18s ease)

### Chips
- **Style:** uppercase kicker pills — 0.69–0.72rem, 700 weight, +0.12–0.14em tracking, Surface Strong mixed 28% with copper
- **State:** static labels, not interactive filters

### Cards / Containers
- **Corner Style:** 1.15rem (headers 1.35rem)
- **Background:** Surface Glass with `backdrop-filter: blur(14px)`
- **Shadow Strategy:** Ambient Low at rest (see Elevation)
- **Border:** 1px Hairline
- **Internal Padding:** 1.1rem standard; login/auth cards go 2rem

### Inputs / Fields
- **Style:** 1px hairline border, Surface Glass Faint fill, 0.8rem radius, min-height 46px, 0.92rem font
- **Focus:** 2px solid Ledger Teal outline offset 2px with border going transparent — no glow
- **Error / Disabled:** error banners are red mixed 10% into surface with a 25% red border; disabled buttons fade to 45% opacity

### Navigation
- **Style:** segmented tab control — a 0.85rem-radius well of Surface Glass Faint holding equal tabs; active tab floats on Surface Glass with a soft shadow; inactive tabs are Muted Ink
- **States:** 0.12s background/color transitions; page-level nav lives in header action rows as ghost pills
- **Mobile:** header actions wrap; grids collapse at ~880px

### Signature Component
**The Ledger Card.** Glass card + copper top hairline + Ambient Low shadow — this trio defines the app's silhouette and must accompany any new panel-style surface.

## Do's and Don'ts

### Do:
- **Do** reference the CSS custom properties (`--primary`, `--surface`, `--border`, …) for every color; both themes update automatically.
- **Do** build tints with `color-mix(in oklab, <token> N%, <base>)` instead of inventing new hex values.
- **Do** keep interactive controls pill-shaped (999px) with ≥40px min-height and 1px-lift hover feedback.
- **Do** reserve Growth Green and Alert Red strictly for money semantics and status.
- **Do** pair every glass surface with a 1px Hairline border and `backdrop-filter: blur(14px)`.

### Don't:
- **Don't** introduce a new hue beyond teal, copper, green, red — extensions of the palette come from color-mix, not fresh pigments.
- **Don't** use borders thicker than 1px or radii sharper than 0.8rem on containers.
- **Don't** apply hard, tight, or colored box-shadows; only the two ambient values exist.
- **Don't** set text in pure black/white or place copper in large fills.
- **Don't** forget the dark-theme counterpart when adding a token — every light value needs its dark pairing in the same template's `prefers-color-scheme` block.
