---
name: Alsense Operations Assistant Starter
description: A clear, evidence-first workspace for synthetic operations conversations.
colors:
  signal-red: "#c8102e"
  signal-red-hover: "#a90e27"
  signal-red-wash: "#fff0f2"
  ink: "#202329"
  muted-ink: "#5e6671"
  white: "#ffffff"
  workspace-gray: "#f3f4f6"
  soft-gray: "#f2f4f6"
  control-border: "#bfc5ce"
  line: "#d9dde3"
  soft-line: "#e8ebef"
  success: "#236548"
  success-wash: "#eaf5ee"
  error: "#a10e22"
  error-wash: "#fff0f1"
  warning-ink: "#765019"
  warning-wash: "#fff5e6"
typography:
  display:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "clamp(28px, 3.1vw, 40px)"
    fontWeight: 680
    lineHeight: 1.12
    letterSpacing: "-.045em"
  page-title:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "28px"
    fontWeight: 680
    lineHeight: 1.15
    letterSpacing: "-.03em"
  welcome-mobile:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "29px"
    fontWeight: 680
    lineHeight: 1.12
  title:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "18px"
    fontWeight: 700
    lineHeight: 1.2
  section-title:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  body-compact:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  body-small:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "12px"
    fontWeight: 650
    lineHeight: 1.4
  hint:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.4
  micro:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "10px"
    fontWeight: 400
    lineHeight: 1.4
  footnote:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "9px"
    fontWeight: 400
    lineHeight: 1.4
  glyph:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "19px"
    fontWeight: 600
    lineHeight: 1
  glyph-mark:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1
  glyph-close:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "21px"
    fontWeight: 400
    lineHeight: 1
  glyph-send:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1
rounded:
  compact: "3px"
  icon: "4px"
  control: "5px"
  icon-button: "6px"
  large: "9px"
  bubble: "13px"
  pill: "99px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "20px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.signal-red}"
    textColor: "{colors.white}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.signal-red-hover}"
  input:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 10px"
---

# Design System: Alsense Operations Assistant Starter

## Overview

**Creative North Star: "The Evidence-First Operations Desk"**

This is a browser workspace for operational questions, not a dashboard of disconnected demo buttons.
The conversation owns the page: questions, readable answers, source material, and reviewable actions stay
together in a single working column. A compact utility rail carries identity and the few actions that
should remain available without interrupting the conversation.

Use a precise, practical interface suited to reading at a desk and occasionally on a phone. The palette
is restrained: white and cool gray organize the work; signal red marks primary actions; green appears only
for successful synthetic outcomes. Product identity is independent of Danfoss and LHU, and the interface
must not imply a live Alsense connection.

**Key Characteristics:**
- Conversation first, controls second.
- Evidence is presented in readable language, never as raw JSON.
- Clear separation between preparing a draft and confirming a synthetic submission.
- Accessible focus, concise labels, and responsive layout.

## Colors

White work surfaces and a cool-gray rail create the hierarchy; red is reserved for focused action and identity.

### Primary
- **Signal Red** (#c8102e): Primary session and confirmation actions, plus the small assistant identity mark.
- **Deep Signal** (#a90e27): Hover state for primary actions.
- **Signal Wash** (#fff0f2): Subtle context where an action needs emphasis.

### Neutral
- **Work Ink** (#202329): Primary text and headings.
- **Muted Ink** (#5e6671): Supporting copy, labels, and metadata; keep it legible on both white and gray.
- **White** (#ffffff): Conversation, composer, and input surfaces.
- **Workspace Gray** (#f3f4f6): Navigation rail and quiet form regions.
- **Soft Gray** (#f2f4f6): User message surface and secondary tonal grouping.
- **Rule** (#d9dde3): Structural dividers and control outlines.
- **Soft Rule** (#e8ebef): Evidence separators and low-emphasis lines.

### Named Rules
**The Evidence-First Rule.** The answer remains readable without expanding technical details; source evidence is attached to the answer it supports.

**The Synthetic-Outcome Rule.** Use success green only for completed mock actions, and label those outcomes synthetic.

## Typography

**Display Font:** Segoe UI, system-ui, sans-serif
**Body Font:** Segoe UI, system-ui, sans-serif
**Label Font:** Segoe UI, system-ui, sans-serif

**Character:** Familiar and highly legible, with compact control labels and a clear title-to-answer hierarchy. Use native system rendering rather than decorative display typography.

### Hierarchy
- **Display** (680, `clamp(28px, 3.1vw, 40px)`, 1.12): The welcome heading only.
- **Page title** (680, 28px fixed, 1.15): The demo scenarios view and the System status page.
- **Title** (700, 16–18px): Workspace and form titles.
- **Body** (400, 14–16px, 1.5–1.68): Questions, answers, and explanatory text; keep answer prose near 70ch.
- **Label** (650, 12px, 1.4): Fields, session state, and compact source metadata.
- **Micro label** (400–650, 10–11px): Secondary composer hints and status chips only.

## Layout

At desktop widths, a 276px utility rail sits beside a flexible conversation workspace. The workspace has a 72px context header, a vertically scrollable conversation, and an anchored composer. The welcome state centers its heading and four task prompts in a 740px measure; messages use the same measure. The rail stays a separate, cool-gray work surface.

At widths up to 840px, the rail narrows and the conversation measure remains fluid. At 640px and below, the rail stacks above the chat, action navigation gives way to the welcome prompts, and forms collapse to one column. The conversation and composer retain their familiar ordering; content must remain scrollable rather than clipped.

Spacing is compact inside controls and forms, with wider gaps between the identity rail, conversation, and evidence. Avoid inventing dashboard columns or decorative empty space.

## Elevation & Depth

The system is predominantly flat. Borders and restrained tonal changes establish separation; no ambient shadows are used. The composer receives a subtle focus ring, and active controls change their border or background instead of floating above the page.

## Shapes

Controls and evidence containers use small 3–5px corners. The composer is slightly softer at 9px, user speech has a 13px chat silhouette, and the active-user status is pill-shaped. Thin borders define fields and sections. Keep the overall geometry workmanlike rather than soft or playful.

## Components

### Buttons
- **Primary:** Signal-red fill, white text, 5px corners, and clear action labels.
- **Hover / Focus:** Darken the red on hover; use the shared visible keyboard focus outline.
- **Secondary:** White or transparent surface with a 1px neutral border; quick actions use a quiet rail treatment.
- **Disabled:** Reduce contrast without removing the control's label or layout.

### Cards / Containers
- **Conversation:** No enclosing card around the whole chat; distinguish individual messages and evidence in place.
- **Suggestions:** White, outlined rows with a small red utility mark and concise title plus explanation.
- **Drafts:** Inline bordered panel above the composer. Never hide a consequential review form in a modal. The report draft is a builder: a live one-line summary and the prepare action stay pinned to the panel's bottom edge while its controls scroll.
- **Action confirmation:** Show the chosen site and fields, state that nothing has been sent, then provide one explicit confirmation action. A three-step tracker (prepared, confirm, synthetic result) shows where the action stands; "Edit details" prepares a replacement draft and marks the old one replaced.

### Synthetic banner
- A permanent full-width strip above the workspace and on the System status page, in Warning Wash with Warning Ink. It says whether the demo is local or hosted, that it uses synthetic data and does not connect to Alsense, and links to System status. It is never dismissible.

### Demo scenarios
- **Gallery:** opens in place of the conversation (no modal), with a page title and two sections, "Core journeys" and "Failure cases". Each scenario is a row in one bordered list, not a grid of cards: title, what it demonstrates, "Starts as" and "Expected" pairs, a Warning Wash chip per synthetic fault, and Start scenario / Reset scenario on the right. The running scenario's row takes Signal Wash with a "Running" pill; an unavailable one is gray with its reason in error text.
- **Coach:** an inline panel above the composer with a Signal Red border on Signal Wash. The heading is the scenario title; the line below starts with "Step N of M." followed by the presenter's note. Actions: Run step (primary), Reset scenario (secondary) and End (text). When complete, the panel turns success green and shows the expected result. No eyebrow above the heading.

### Why this result?
- A collapsed `details` element under each assistant reply. It lists, as label and value pairs on a Soft Gray panel: assistant type, flow and version, site resolved, health result, outcome, lookups, evidence titles, reply checks, data source and a reference id. Display names only, never internal ids, tool names or JSON.

### Error states
- Replies that could not complete carry an Error Wash "Couldn't complete" status and name the problem and the recovery in plain words: no access to a site, a service temporarily unavailable or too slow, out-of-date status, several matching sites, no approved answer, and "prepared but not submitted" on a draft that failed to send. Offer a support request only for service failures. Never collapse these into a generic error.

### System status page
- A separate Operate page on the rail gray: back link, page title, one-line purpose, an overall pill (Demo ready in success green, Needs attention in error red) with version and commit, then bordered sections. Capability rows align in three columns: label, value with an optional muted detail, and a state badge: Synthetic (Warning Wash), Configured (Success Wash), Unavailable or Needs attention (Error Wash), Future integration or Disabled (neutral gray). A flow table lists version, status, owner and whether it is offered. Nothing secret appears on the page.

### Inputs / Fields
- **Style:** White fill, 1px neutral border, 5px corners, and 8–10px internal padding.
- **Choices:** Few, known options use native radios styled as choice cards (report type), pill chips (period presets) or a segmented control (format). Selection is a Signal Red border on a Signal Wash fill; a single permitted site shows as a fixed pill instead of a one-option select.
- **Focus:** Visible red outline for keyboard focus; composer also shifts its border and uses a restrained ring.
- **Error / Disabled:** Use a nearby text message for errors and preserve readable contrast for disabled controls. Validate date ranges inline and disable the prepare action until the range is valid.
- **Access code:** Mask by default with a user-controlled show/hide button. Never persist the value.

### Navigation
- **Utility rail:** Compact actions with a text label and small symbol; selected identity and site scope stay visible.
- **Mobile:** Stack identity above the main conversation; preserve quick prompts in the welcome state.

### Evidence and Activity
- **Sources:** Show synthetic document titles and excerpts in readable cards with approved local links. Keep tool names inside the expandable source summary.
- **Activity:** A small spinner and plain-language status sit in the assistant turn while backend tools run.
- **Actions:** Keep report and support drafts inline and distinguish prepared, submitting, submitted, and failed states.
- **Guided checks:** Each step is an assistant turn labelled with the check and step number, with Yes/No or site pills as answers; the chosen answer stays highlighted and earlier steps lock. Results show "What we checked" and, when the tree says so, a prefilled support request instead of free-form advice.

## Do's and Don'ts

### Do:
- **Do** keep questions, answers, evidence, and confirmation in one conversation.
- **Do** identify the selected synthetic persona and its permitted site scope.
- **Do** explain access-code errors and offer a direct recovery instruction without exposing the code.
- **Do** label synthetic data and mock submissions wherever they could be mistaken for live results, and keep the synthetic banner on every page.
- **Do** support keyboard submission, multiline input, visible focus, and reduced-motion preferences.

### Don't:
- **Don't** display raw tool JSON, internal source IDs, or unexplained backend errors to operators.
- **Don't** imply that a report or ticket has been sent before the user confirms it.
- **Don't** put the session token or access code in browser storage, visible conversation text, or model input.
- **Don't** copy Danfoss or Alsense production logos, customer data, or claims into this independent starter.
