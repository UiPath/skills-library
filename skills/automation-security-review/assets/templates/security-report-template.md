# Security Review — <TARGET_NAME>

| | |
|---|---|
| Target | `<TARGET_DIR>` |
| Projects | <PROJECT_LIST_WITH_TYPES> |
| Data egress | <ROUTE> — <MODE> (<gateway-confirmed / user-confirmed / defaulted>) |
| Scanner | <N> text files, <M> Office files scanned; <K> skipped (too large) |
| UiPath tools | <COMMANDS_RUN_OR_NOT_AVAILABLE> |
| Result | Critical: <C> · Warning: <W> · Info: <I> · Needs validation: <V> |

> Secret and personal-data values are never shown. Fingerprints (`#xxxxxxxx`) identify repeated values across files.

## Rotate Now

<!-- Every confirmed secret that is git-tracked, shipped in build output, or shared. One line each. Omit section if none. -->

| Secret type | Location | Fingerprint | Rotate in |
|---|---|---|---|
| <TYPE> | `<FILE>:<LINE>` | `#<FP>` | <SOURCE_SYSTEM> |

## Critical

### C-1 · <RULE_ID> · <SHORT_TITLE>

- **Where:** `<FILE>:<LINE>`
- **What:** <REDACTED_HINT>
- **Verified:** confirmed — <ONE_LINE_REASON>
- **Fix:** <IMPERATIVE_FIX>
- **Owner skill:** <SKILL_NAME>

## Warning

<!-- Same block format, W-1, W-2, ... -->

## Info

<!-- Compact table: ID, rule, location, note. Include downgraded synthetic/test findings here with the downgrade reason. -->

## Needs Validation

<!-- Findings that could not be confirmed or disproved, each with the exact question to answer. -->

## Not Covered

<!-- Mode limits (local-only → no judgment review), missing tools, skipped large/binary files, PDFs/images in data folders, git history not scanned. -->

## Next Steps

1. Rotate every secret in **Rotate Now**.
2. <FIXES_ROUTED_BY_OWNER_SKILL>
3. Purge history for committed secrets / personal data if the repository was shared.
4. Re-run this review after fixes.
