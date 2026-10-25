# Interaction polish research

Date: 25 October 2026. Scope: small details in everyday position management; preserve the current flow and copy.

## Direction

Use the existing “Cancel changes” reveal as the app's motion vocabulary: short travel, a quick settle, and feedback attached to a deliberate action. Apple recommends purposeful, brief feedback, consistent spatial relationships, and allowing interaction before motion completes. Its guidance also cautions against making frequent interactions demand repeated attention. The implementation choices below are recommendations derived from that guidance, not prescribed platform timing values. [Apple Human Interface Guidelines: Motion](https://developer.apple.com/design/human-interface-guidelines/motion)

## Existing implementation

`src/ibkr_options_manager/app/web/static/layers.css` already provides:

- A 160 ms cancel reveal with 7 px upward travel and the easing `cubic-bezier(0.16, 1, 0.3, 1)`; staged server-rendered reviews use a matching entry keyframe.
- A 180 ms quantity-loading indicator reveal.
- A 160 ms sold-layer overlay dissolve, including a keyboard-focus equivalent.
- Reduced-motion variants for all three.

These details form a coherent base. The staged cancel keyframe runs whenever a matching element is inserted; any future entrance effect must distinguish a deliberate new local state from routine server replacement to avoid repeated entrances.

## Narrow first pass

| Detail | Recommendation | Boundary |
| --- | --- | --- |
| Editable bracket row | A very light background/border change on hover and focus within; 120–160 ms color transition | Keep field values and labels steady; preserve the visible focus ring |
| Enabled local buttons | Brief press feedback, approximately 1 px down, returning immediately on release | Exclude disabled controls and avoid translating whole rows or financial values |
| Newly added draft layer | 4–7 px settle and fade, approximately 160 ms, only after explicit Add | No animation when opening a position, refreshing, reconnecting, or replacing the same row |
| Small disclosures | Match the existing cancel reveal's easing and short travel | Make reverse motion consistent; never wait for animation to complete before changing state |
| Loading indicator | Keep the current restrained reveal | Do not animate price/quantity numbers through intermediate values |

The exact timings and distances are a proposed local style. They deliberately reuse the existing cancel treatment rather than introduce another motion system.

## Implementation constraints supported by primary sources

- Prefer `transform` and `opacity` for motion; geometry changes such as width/height can trigger layout. Existing cancel/spinner expansion is small and local, but new whole-list height animations should be avoided. Do not add permanent `will-change` without a measured issue. [Chrome/web.dev animation guide](https://web.dev/articles/animations-guide)
- Nonessential interaction motion should be removable. Under `prefers-reduced-motion: reduce`, omit translation/scale; an immediate state change or short opacity/color transition can preserve feedback. WCAG 2.3.3 is AAA guidance, not a claim of blanket conformance. [W3C Animation from Interactions](https://www.w3.org/WAI/WCAG21/Understanding/animation-from-interactions), [Chrome/web.dev accessibility](https://web.dev/learn/design/accessibility)
- Keyboard focus must remain visible; a temporary hover tint is not a replacement. [W3C Focus Visible](https://www.w3.org/WAI/WCAG21/Understanding/focus-visible.html)
- Hidden controls must also be noninteractive. `pointer-events: none` alone still permits Tab focus; native disabled controls are removed from normal focus and interaction. The current cancel script already disables its button while hidden; preserve that behavior. [MDN pointer-events](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/pointer-events), [MDN disabled](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Attributes/disabled)

## Review checklist

Inspect normal and reduced motion, mouse and keyboard, rapid repeated editing, position switching, and routine refresh. Check that retained rows do not replay entrances, disabled actions remain disabled immediately, focus rings are not clipped, and broker states and numeric values update immediately. Visual feedback must never imply a broker acknowledgement or delay a safety gate. No transport or order-planning changes are proposed.

## First pass implemented

- Newly revealed active bracket/trailing review rows settle upwards by 4 px over 160 ms. Initial hydration and subsequent keystrokes do not replay the entrance. Hiding a row cancels its animation; reduced motion omits it.
- Editable draft/working rows gain a faint tint while focus is inside, preserving the existing field focus rings.
- Enabled Management/review buttons move down 1 px during a press, excluding navigation tabs and layer labels. Reduced motion removes the movement. Background, border and focus-shadow transitions use explicit properties; disabled opacity and state changes remain immediate.
- Layer number fields use tabular numerals. Existing flow, copy, calculations, confirmation gates and transport are unchanged.
- Deferred new-draft-row entrances: current submissions replace the document, so a generic insertion animation would also replay on navigation or refresh. That needs a separate explicit-action signal.

Verification: `.venv/bin/python -m pytest -q tests/test_app_browser.py tests/test_app_workbench.py tests/test_journal_browser.py::test_journal_views_render_at_desktop_and_minimum_size` passed (119 tests); Ruff and mypy passed. A separate temporary Qt/WebEngine probe verified normal/reduced-motion media behavior, no entrance on initial hydration, no animation restart during repeated edits, immediate calculated prices, and immediate disabled/hidden state on cancel.

## Contextual feedback follow-up

The workbench no longer mounts the floating toaster. Settings briefly shows
“Saved”; Refresh briefly swaps its icon for a checkmark. A new draft layer gets
a short background highlight tied to the explicit creation action, with no
replay on reload. Verified price changes show a disabled green “Successfully
edited” action button for 2.5 seconds, then restore the disabled grey “Review
price changes” button. A new edit interrupts success immediately and the old
timer cannot overwrite it. Market-sell acknowledgements show “Sell order sent”
in the action button without claiming a fill. Success never inserts a separate
message into the workspace. Failures appear beside review
controls, and connection failures appear below the header. Warnings and errors
have no expiry timer and survive page reloads; a subsequent action can replace
them. Existing recovery dialogs and contract locks remain authoritative for
unresolved broker state. An acknowledged write whose refresh fails uses the
persistent warning treatment. Movement is omitted under reduced motion.

Bracket creation uses “Bracket orders sent” in the same disabled action-button
slot after complete acknowledgements. The submission dialog's status refresh
is distinct from the Settings form, so it cannot label Settings as “Saved”.
The main Management scroll viewport retains a per-session/account/contract
offset through document updates, clamped naturally when content shrinks.
This also keeps each position's place when navigating away and back. Browser
storage failure is tolerated and never affects broker operations.
