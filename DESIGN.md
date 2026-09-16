---
name: IBKR Options Manager
description: A read-only desktop passage plan for inspecting one verified option exit route.
colors:
  instrument-ground: "#071a2b"
  risk-ground: "#061622"
  control-panel: "#0a2233"
  control-field: "#102d40"
  status-surface: "#173447"
  ready-surface: "#0d3c46"
  plotting-sheet: "#eaf1f2"
  table-paper: "#f3f7f7"
  hydrographic-magenta: "#d34174"
  blocked-wash: "#49283a"
  sea-glass-cyan: "#7fc8c8"
  sea-glass-hover: "#a0dedd"
  sea-glass-pressed: "#63aeb0"
  quote-teal: "#177e89"
  stop-rust: "#b04a35"
  caution-gold: "#f2c14e"
  structural-line: "#315062"
  light-rule: "#b7cdd1"
  dark-muted: "#8fa8b4"
  light-muted: "#536b78"
  white: "#ffffff"
typography:
  display:
    fontFamily: "Avenir Next, sans-serif"
    fontSize: "26px"
    fontWeight: 600
  headline:
    fontFamily: "Avenir Next, sans-serif"
    fontSize: "20px"
    fontWeight: 600
  title:
    fontFamily: "SF Mono, monospace"
    fontSize: "16px"
    fontWeight: 600
  body:
    fontFamily: "Avenir Next, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "Avenir Next, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    letterSpacing: "0.8px"
  data:
    fontFamily: "SF Mono, monospace"
    fontSize: "12px"
    fontWeight: 400
  caption:
    fontFamily: "SF Mono, monospace"
    fontSize: "11px"
    fontWeight: 400
spacing:
  tight: "6px"
  control-y: "7px"
  field-row: "10px"
  section: "14px"
  header: "16px"
  panel: "22px"
  frame: "24px"
  route: "28px"
components:
  safety-banner:
    backgroundColor: "{colors.hydrographic-magenta}"
    textColor: "{colors.white}"
    typography: "{typography.label}"
    padding: "11px 24px"
  button-primary:
    backgroundColor: "{colors.sea-glass-cyan}"
    textColor: "{colors.instrument-ground}"
    typography: "{typography.label}"
    padding: "10px 14px"
    height: "42px"
  button-primary-hover:
    backgroundColor: "{colors.sea-glass-hover}"
    textColor: "{colors.instrument-ground}"
  button-primary-active:
    backgroundColor: "{colors.sea-glass-pressed}"
    textColor: "{colors.instrument-ground}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.plotting-sheet}"
    typography: "{typography.label}"
    padding: "10px 14px"
    height: "42px"
  field-dark:
    backgroundColor: "{colors.control-field}"
    textColor: "{colors.white}"
    typography: "{typography.label}"
    padding: "7px 8px"
    height: "36px"
  status-ready:
    backgroundColor: "{colors.ready-surface}"
    textColor: "{colors.white}"
    typography: "{typography.caption}"
    padding: "8px 12px"
  status-blocked:
    backgroundColor: "{colors.blocked-wash}"
    textColor: "{colors.white}"
    typography: "{typography.caption}"
    padding: "8px 12px"
  plan-table:
    backgroundColor: "{colors.table-paper}"
    textColor: "{colors.instrument-ground}"
    typography: "{typography.caption}"
---

# Design System: IBKR Options Manager

## Overview

**Creative North Star: "The Passage Plan"**

The interface reads like a navigator's working instrument: one verified position enters from the dark control side, then its observed prices and proposed exits are plotted on a cool, open sheet. The visual language is technical and calm rather than financial-dashboard theatrical. Hairline rules, square labels, and tabular numerals make every boundary and derivation inspectable.

Safety is structural, not decorative. A permanent magenta read-only strip opens the window, the current state is always written in a bordered register, and the risk qualification remains visible at the bottom. Cyan identifies observed or ready information; magenta identifies the planned route and blocking conditions; rust identifies stop marks; gold is reserved for execution risk.

**Key Characteristics:**

- A dark instrument panel beside a light plotting sheet.
- One linear workflow: verify, configure, inspect, resolve.
- Square geometry, hairline rules, and no ornamental surfaces.
- Monospaced data and tabular alignment wherever values must be compared.
- Explicit text labels for every safety state; color is reinforcement only.

## Colors

Deep marine neutrals separate operator inputs from the plotting field, while the small accent set behaves like chart ink rather than decoration.

### Primary

- **Sea-glass Cyan:** The sole action color, used for the primary refresh control, decisive field focus, ready/loading outlines, and observed-state emphasis.
- **Hydrographic Magenta:** The planned-route color and the permanent read-only warning color; it also marks blocked and stale states against dark surfaces.

### Secondary

- **Quote Teal:** Observed BID, ASK, and LAST marks on the route.
- **Stop Rust:** Stop markers only, drawn with a dashed stroke so meaning survives without color.
- **Caution Gold:** Reserved for the `EXECUTION RISK` label in the persistent footer.

### Neutral

- **Instrument Ground:** The window ground, primary dark ink, and high-contrast anchor for the entire system.
- **Risk Ground:** A slightly deeper footer field that keeps the execution warning visually persistent.
- **Control Panel / Control Field:** Nested dark surfaces for the evidence-and-request column and its editable controls.
- **Status Surface / Ready Surface:** Tonal status fields; the former is neutral or loading-adjacent, while the latter supports a verified ready state.
- **Plotting Sheet / Table Paper:** Cool, low-chroma working surfaces for the route, table, quote facts, and validations.
- **Structural Line / Light Rule:** One-pixel dividers on dark and light surfaces respectively.
- **Dark Muted / Light Muted:** Secondary text for labels, descriptions, axes, and snapshot metadata.
- **White:** High-emphasis text on saturated or dark surfaces.

### Named Rules

**The Chart-Ink Rule.** Accent colors encode observed, planned, stopped, blocked, or risky information; they are never ambient decoration.

**The Two-Field Rule.** Configuration and evidence stay on dark marine surfaces; calculated routes and validation detail stay on the cool plotting sheet.

## Typography

**Display Font:** Avenir Next (with a sans-serif fallback)  
**Body Font:** Avenir Next (with a sans-serif fallback)  
**Label/Mono Font:** SF Mono (with a monospace fallback)

**Character:** Avenir Next gives the application a restrained, legible desktop voice. SF Mono carries values, status registers, timestamps, axis labels, identifiers, and table data so quantities line up and broker evidence feels inspectable.

### Hierarchy

- **Display** (600, 26px): The application title in the dark header.
- **Headline** (600, 20px): The route-panel heading on the plotting sheet.
- **Title** (600, 16px): The verified option symbol; monospaced because it is contract evidence, not editorial copy.
- **Body** (400, 13px): The subtitle and ordinary explanatory copy.
- **Label** (700, 12px, 0.8px tracking): Section titles and decisive controls. The permanent safety banner uses a slightly larger 14px label with 1px tracking.
- **Data** (400, 12px): Facts and field values; pass/block color may modify it but never replaces its text.
- **Caption** (400, 11px): Table values, snapshot age, validation detail, and market-rule evidence. Status registers use the same size at weight 700 with 0.5px tracking.

### Named Rules

**The Evidence Is Mono Rule.** Any value the operator compares, transcribes, or audits uses SF Mono; instructions and field names remain in Avenir Next.

## Layout

The window opens at 1380 × 900px and has a hard minimum of 1120 × 820px. A full-width read-only strip and header sit above a two-pane horizontal workspace; a full-width execution-risk strip anchors the bottom. The splitter begins at 420px / 960px, expressing the four-column evidence-and-control rail beside an eight-column route field.

The left rail is vertically scrollable, fixed between 380px and 480px wide, and uses 24px horizontal insets, 22px top inset, 28px bottom inset, and 14px section rhythm. Field rows use a 10px vertical gap. The route pane expands into remaining width with 28px horizontal and 22px vertical insets. Its vertical sequence is route heading, expanding price plot, three allocation totals, plan table, then a 1:2 quote/validation lower split.

This is a minimum-size desktop interface, not a breakpoint-driven responsive layout. At the supported minimum, the left rail scrolls while the route plot, allocation register, and table remain separate. The price plot has a 180px minimum height, the plan table a 125px minimum height, and the table's final OCA column absorbs remaining width.

**The Route Owns the Width Rule.** The control rail may scroll and is width-capped; the plotted route receives all remaining horizontal space.

## Elevation & Depth

The system has no shadows. Depth comes entirely from tonal layering, one-pixel rules, the splitter seam, and the strong dark/light field change. Interactive state changes replace surface color or border weight instantly; no element lifts off the instrument plane.

**The Flat Instrument Rule.** Never use drop shadows, glows, glass, or floating cards. Establish hierarchy with fields, rules, and contrast.

## Shapes

All authored surfaces and controls are square. The QSS defines no corner radius: banners, status registers, inputs, buttons, the plot, the table, and validation rows meet on straight edges. Borders are normally one pixel; focus increases the relevant border to two pixels while reducing padding by one pixel so geometry does not jump.

Route marks use strict line geometry: basis is a heavier solid line, quote and target marks are solid, and stops are dashed. Labels sit in compact rectangular chart lanes rather than pills.

**The No-Pills Rule.** Do not round status, filter, warning, or action controls into capsules; the passage-plan language depends on chart-like rectangles and rules.

## Components

### Safety and Risk Strips

- **Permanent read-only strip:** Full-width hydrographic magenta with a bold white safety statement on the left and scope detail on the right. It is the first visual element and never disappears.
- **Execution-risk strip:** Full-width risk ground at the bottom, separated by a one-pixel rule. Gold labels the risk category; plain light copy states the limitation.

### Buttons

- **Shape:** Square, bordered, and compact, with a 42px visual height from minimum content height, padding, and border.
- **Primary:** Sea-glass cyan fill with instrument-ground text. Hover lightens, pressed darkens, and keyboard focus becomes a two-pixel white border.
- **Secondary:** Transparent on the dark panel with muted light text and a blue-gray border. Hover adds a dark cyan field and brighter border.
- **Disabled:** Returns to a low-contrast navy field with muted text. Busy state disables both preview pathways while the prior route is invalidated.

### Status Registers

- **Style:** Uppercase monospaced text inside a square one-pixel register, at least 280px wide.
- **Ready:** Deep teal surface with a sea-glass border.
- **Blocked / Stale:** Dark magenta wash with a hydrographic-magenta border.
- **Loading:** Marine blue surface with a sea-glass border.
- **Content rule:** Every state includes its literal name and message; it never communicates by hue alone.

### Inputs / Fields

- **Style:** Dark marine fill, one-pixel blue-gray border, light Avenir Next text, and compact 7px × 8px padding.
- **Focus:** A two-pixel sea-glass border replaces the normal border; padding contracts by one pixel to prevent reflow.
- **Selection:** Hydrographic magenta with white selected text.
- **Disabled:** A deeper navy fill and muted blue-gray text.
- **Labels:** Semantic labels are visibly paired and programmatically assigned as buddies to their fields.

### Price Route

- **Canvas:** Cool plotting sheet with a one-pixel light rule and six vertical calibration lines.
- **Observed marks:** Quote teal for market observations and a heavier instrument-navy basis line.
- **Planned marks:** Hydrographic-magenta targets and dashed stop-rust stops. Each target/stop pair shares a continuous muted route through basis.
- **Motion:** On state application, the route reveals from 8% to 100% over 460ms with OutCubic easing. When `IBKR_OPTIONS_MANAGER_REDUCE_MOTION` is set, the route appears immediately.
- **Alternative:** The widget exposes an accessible description listing every visible mark and price; its empty state also explains how to obtain a route.

### Allocation and Plan Table

- **Allocation register:** Three monospaced totals separated from the plot by a light top rule.
- **Table:** Cool paper background, dark marine text, monospaced 11px values, and a dark Avenir Next header. Numeric pair, quantity, target, and stop columns align right.
- **Selection:** A cyan wash with dark text; selection is single-row and the table is read-only.
- **Sizing:** Content columns size to their values while the logical OCA column stretches.

### Validation Register

- **Style:** Stacked plain rows with a one-pixel top rule and no container card.
- **Blocked:** Dark magenta text plus a written validation code and message.
- **Passed:** Deep teal text plus an explicit all-checks-passed message.
- **Fail-closed behavior:** Changed connection inputs, refresh in progress, stale data, or blocked refresh clears the route and plan table instead of leaving old evidence visible.

## Do's and Don'ts

### Do:

- **Do** keep the permanent read-only notice and execution-risk strip visible across every state.
- **Do** pair every semantic color with text, line style, weight, or an explicit state name.
- **Do** use SF Mono for broker facts, quantities, prices, identifiers, timestamps, and route labels.
- **Do** clear stale route and table output as soon as connection inputs change or refresh invalidates the snapshot.
- **Do** preserve the explicit keyboard order from account through contract, connection fields, refresh, plan fields, and preview.
- **Do** expose accessible names and descriptions for dynamic status, validation, and the custom-drawn price route.

### Don't:

- **Don't** introduce transmit, submit, modify, cancel, exercise, or global-cancel styling or controls into this read-only milestone.
- **Don't** turn the workflow into a card mosaic or generic settings form; it is one passage from verified evidence to a plotted plan.
- **Don't** add rounded pills, drop shadows, glass effects, gradients, or decorative charts.
- **Don't** use hydrographic magenta as a general brand accent; reserve it for the immutable safety banner, planned targets, selection, and blocking states.
- **Don't** depend on route animation; reduced-motion mode must remain complete and immediately legible.
- **Don't** support a window smaller than 1120 × 820px without first redesigning and retesting the information layout.
