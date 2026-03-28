# UI System Status

Last updated: 2026-03-28

## Scope

This document tracks the current UI architecture status and stabilization progress for the [resources](../resources) folder.

## Current System Status

### Architecture

1. Style source: SCSS in [resources](../resources)
2. Generated output: CSS files in [resources](../resources)
3. Known risk: generated CSS may drift from SCSS source if edited manually or compiled from stale input

### Component System

1. Canonical candidate for new buttons: `.action-btn` (defined in [resources/base.scss](../resources/base.scss))
2. Legacy concurrent systems still present:
   - `.button` in [resources/widgets.scss](../resources/widgets.scss)
   - `.btn` in [resources/widgets.scss](../resources/widgets.scss) and [resources/quiz.scss](../resources/quiz.scss)

### Token Governance

1. Token file: [resources/vars.scss](../resources/vars.scss)
2. Progress: color and spacing tokenization improved in key files
3. Remaining gap: many SCSS files still contain hardcoded literals and legacy patterns

## Step 1 (Stabilize) - Completion Checklist

### Completed in this phase

1. Comprehensive audit documented: [ui-audit-report-2026-03-28.md](./ui-audit-report-2026-03-28.md)
2. Living system-status document created (this file)
3. Guardrail script added: [scripts/ui_guardrails.sh](../scripts/ui_guardrails.sh)

### Guardrail Script Usage

Run report mode:

```bash
./scripts/ui_guardrails.sh
```

Run strict mode (fails when hardcoded debt exists):

```bash
./scripts/ui_guardrails.sh --strict
```

The script reports:

1. `!important` counts + top offender files
2. hardcoded hex counts
3. competing button-system definitions
4. known SCSS/CSS drift check (`table.scss` vs `table.css`)

## Team Working Rules (Phase 1)

1. Treat SCSS as the source of truth for application styling
2. Do not manually edit generated CSS artifacts
3. Prefer token usage from [resources/vars.scss](../resources/vars.scss) for new edits
4. Avoid introducing new `!important` unless strictly required and justified

## Next Steps (Phase 2 candidates)

1. Consolidate button systems onto one canonical style API
2. Expand token usage to top offender files (`base.scss`, `problem.scss`, `content-description.scss`)
3. Add enforceable lint/CI checks for style governance
