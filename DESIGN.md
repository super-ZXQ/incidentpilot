---
name: IncidentPilot
description: Lightweight incident investigation and human approval workspace
colors:
  paper: "#f6f6f1"
  surface: "#fff"
  ink: "#172f30"
  muted: "#526363"
  line: "#ced7d2"
  accent: "#12645c"
  error: "#a22f26"
  focus: "#a44700"
typography:
  headline:
    fontSize: "32px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-.02em"
  title:
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "15px"
    lineHeight: 1.65
  code:
    fontFamily: 'Consolas, "Courier New", monospace'
    fontSize: "12px"
rounded:
  control: "5px"
  mode: "4px"
  conclusion: "6px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "12px 22px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "12px 22px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "11px 12px"
---

# Design System: IncidentPilot

## Overview

The implemented Operate interface is a lightweight investigation workbench. Its visual language follows the user's professional, no-decoration constraint: readable evidence, ordinary controls, and explicit operational states. It does not establish a separate marketing identity.

Source of truth: `src/incidentpilot/api/static/demo.html`, `demo.css`, and `demo.js`; product constraints live in `PRODUCT.md` and the surface scope in `docs/audit/DEMO_DESIGN.md`.

## Colors

Accent teal identifies actions, links, and run status. Dark teal ink sits on off-white paper; white forms and mode labels provide restrained separation. Muted text carries supporting detail, thin pale rules divide sections, and error and focus colors communicate actionable states. Frontmatter values are normative.

## Typography

Native system fonts intentionally keep the interface lightweight: Segoe UI, Microsoft YaHei, then sans-serif. No external font is loaded. Headings use restrained weight and size; body paragraphs are limited to 75 characters in width. Labels are (13px), weight (600). IDs, hashes, and Diff use Consolas with Courier New fallback. On mobile the page headline becomes (27px).

## Layout

The centered main area has a maximum width of (1240px), desktop padding of (40px 30px 70px), and section grids separated by (50px). Investigation uses two columns in a (1:1.6) ratio; remediation uses (1.5:1). At (760px) and below, both become one column, forms stack, and main padding becomes (28px 20px 48px). Tables scroll horizontally when needed; long evidence and code wrap within scrollable blocks.

## Elevation & Depth

There are no shadows or decorative animations. White surfaces, fine borders, and a pale green root-cause panel establish grouping. Depth does not imply that a conclusion is verified.

## Shapes

Controls have modest rounded corners, mode labels are slightly tighter, and the conclusion panel is slightly softer. Most sections remain open on the page with single-pixel dividing rules rather than enclosing cards.

## Components

Primary buttons use solid accent teal; secondary buttons use an outlined transparent surface. Buttons have a minimum height of (46px), visible hover states, and muted disabled states. Keyboard focus uses an orange outline (3px) offset by (3px), including links, disclosures, inputs, and scrollable code.

White text fields use a single-pixel border, persistent labels, and inherited body typography. Mode labels distinguish Fake/real model, Mock/real GitHub, and local/Docker execution. Test copy separately identifies healthy-only regression and fault-active validation.

Evidence and test output use native disclosures; Diff is a scrollable, focusable text block. Empty and pending states are explicit. Approval controls become available only for the current artifact, the waiting-approval state, and an authorized role; the request binds the artifact ID and hash. Model and PR results must never imply execution that has not occurred.

## Do's and Don'ts

- Do preserve operational distinctions and show uncertainty beside the relevant evidence or result.
- Do render API content through `textContent`, retain same-origin CSP, and keep credentials only in page memory; refresh clears them.
- Do preserve labels, visible focus, mobile stacking, and readable long values.
- Don't add external assets, frontend frameworks, decorative animation, or invented success data.
- Don't treat healthy regression, workflow completion, or a Mock PR as proof that the incident is resolved or an external PR exists.

Review provenance: the finish reviewer accepted the product-constraint persistence and UTC timestamp corrections for shipment. Desktop (1440px) and mobile (390px) screenshots were verified at `.impeccable/review/desktop.png` and `.impeccable/review/mobile.png`. This records that limited review; it does not claim a broader accessibility or production validation pass.
