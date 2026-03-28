# UI Product Considerations (Owner Notes)

Last updated: 2026-03-28

This document captures current product/UI priorities for near-term GUI improvements.

## Requested Considerations

1. Navbar currently icon-only: evaluate whether this is appropriate for clarity and discoverability.
2. Problem list: allow users to choose a default list view in settings.
3. Problem list: allow sorting by time on GUI.
4. Problem creation: improve form GUI and usability.
5. Pagination: allow jump-to-page index input.
6. Site-wide forms: improve dropdown UI/UX consistency.

## Initial Product Assessment

### 1) Navbar icon-only

Recommendation:

1. Keep icon-only mode on very narrow layouts (mobile/compact).
2. Add optional label mode for desktop (or tooltip + expandable labels).
3. Add a user preference: `compact_navbar = true/false`.

Why:

1. Icon-only is space-efficient but harms discoverability for new users.
2. Text labels reduce ambiguity and support accessibility.

### 2) Problem list default view in user settings

Recommendation:

1. Add a user profile preference field for default problem-list view.
2. Respect that preference in problem list page rendering and query params.

### 3) Problem list sort by time

Recommendation:

1. Add explicit sort controls in GUI (`Newest`, `Oldest`, `Recently updated`).
2. Persist selected sort in query params and optionally user preference.

### 4) Problem creation form GUI

Recommendation:

1. Break long forms into sections with sticky section nav.
2. Improve spacing/labels/help-text consistency.
3. Promote critical fields and validation feedback clarity.

### 5) Pagination jump-to-index

Recommendation:

1. Add `Go to page` input with submit and bounds validation.
2. Preserve current filters/sort when jumping page.

### 6) Site-wide dropdown UI

Recommendation:

1. Standardize dropdown visual style and states via shared tokens/components.
2. Improve keyboard accessibility and focus styles.
3. Audit select2/native select usage and define one preferred pattern.

## Suggested Rollout Sequence

1. Pagination jump + problem list sort controls (quick UX wins)
2. Default problem-list view preference
3. Dropdown standardization (site-wide impact)
4. Navbar mode preference
5. Problem creation form redesign

## Tracking

Use this file as product intent baseline for upcoming implementation tasks and PR breakdowns.
