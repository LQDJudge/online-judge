#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

strict=false
if [[ "${1:-}" == "--strict" ]]; then
  strict=true
fi

echo "UI Guardrails Report"
echo "Repository: $ROOT_DIR"
echo

# 1) Specificity debt
echo "[1/4] !important usage in SCSS"
important_count=$(grep -Roh '\!important' resources/*.scss resources/**/*.scss 2>/dev/null | wc -l | tr -d ' ')
echo "Total !important: $important_count"
echo "Top files:"
grep -RIn '\!important' resources/*.scss resources/**/*.scss 2>/dev/null | cut -d: -f1 | sort | uniq -c | sort -nr | head -n 10 || true
echo

# 2) Hardcoded color debt
echo "[2/4] Hardcoded hex literals in SCSS"
hex_count=$(grep -RohE '#[0-9A-Fa-f]{3,6}' resources/*.scss resources/**/*.scss 2>/dev/null | wc -l | tr -d ' ')
unique_hex_count=$(grep -RohE '#[0-9A-Fa-f]{3,6}' resources/*.scss resources/**/*.scss 2>/dev/null | tr 'A-Z' 'a-z' | sort -u | wc -l | tr -d ' ')
echo "Total hex literals: $hex_count"
echo "Unique hex literals: $unique_hex_count"
echo

# 3) Competing button systems
echo "[3/4] Button system definitions"
grep -REn '^\.action-btn\b|^\.button\b|^\.btn\b' resources/*.scss resources/**/*.scss 2>/dev/null || true
echo

# 4) Known SCSS/CSS drift hotspots
echo "[4/4] Known source-of-truth drift checks"
if [[ -f resources/table.scss && -f resources/table.css ]]; then
  scss_has_theme=false
  css_has_old_color=false
  grep -q 'background-color: \$theme_color;' resources/table.scss && scss_has_theme=true || true
  grep -q 'background-color: #DAA520;' resources/table.css && css_has_old_color=true || true

  if [[ "$scss_has_theme" == true && "$css_has_old_color" == true ]]; then
    echo "WARNING: table.scss/table.css drift detected (theme token vs #DAA520)."
  else
    echo "No known table.scss/table.css drift pattern detected."
  fi
else
  echo "table.scss or table.css not found; skipped."
fi
echo

if [[ "$strict" == true ]]; then
  fail=false
  if [[ "$important_count" -gt 0 ]]; then
    echo "STRICT MODE FAIL: !important count is non-zero ($important_count)."
    fail=true
  fi
  if [[ "$unique_hex_count" -gt 0 ]]; then
    echo "STRICT MODE FAIL: hardcoded hex colors are present ($unique_hex_count unique)."
    fail=true
  fi

  if [[ "$fail" == true ]]; then
    exit 1
  fi
fi

echo "Done."
