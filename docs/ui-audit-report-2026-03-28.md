# UI Audit Report (2026-03-28)

This report summarizes a comprehensive style-system audit of [resources](../resources) and related UI assets.

## Executive Summary

The current UI layer has significant consistency and maintainability debt. Core issues are:

1. Competing button systems (`.action-btn`, `.button`, `.btn`)
2. Source-of-truth drift between SCSS source and compiled CSS artifacts
3. Large hardcoded-style footprint (color and spacing)
4. High CSS specificity debt from `!important`
5. Uneven token governance and naming consistency

## Measured Snapshot

Metrics captured during audit:

1. `!important` usages in SCSS: `519`
2. Unique hardcoded hex values in SCSS: `249`
3. Button system definitions detected in:
   - [resources/base.scss](../resources/base.scss)
   - [resources/widgets.scss](../resources/widgets.scss)
   - [resources/quiz.scss](../resources/quiz.scss)
4. Confirmed drift example:
   - [resources/table.scss](../resources/table.scss): `background-color: $theme_color;`
   - [resources/table.css](../resources/table.css): `background-color: #DAA520;`

## Top Findings (Prioritized)

### High

1. SCSS/CSS source-of-truth drift (table styles)
2. Three active button systems with overlapping purpose
3. Excessive `!important` causing specificity wars
4. Hardcoded style literals still widespread

### Medium

1. Dark mode generated externally (DarkReader artifact flow)
2. Token taxonomy evolved without strict governance
3. Naming conventions mixed between legacy and new tokens

### Low

1. Some component domains maintain local style systems
2. Generated assets and source styles are too easy to confuse

## Recommendation

Adopt a phased remediation:

1. Stabilize (policy + guardrails + reporting)
2. Standardize (component and token consolidation)
3. Enforce (lint/CI + contribution rules)

See [ui-system-status.md](./ui-system-status.md) for current status and implementation progress.
