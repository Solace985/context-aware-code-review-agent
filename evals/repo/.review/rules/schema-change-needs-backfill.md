---
id: schema-change-needs-backfill
severity: high
---
# A schema or enum change needs a migration, a default, and null-safe reads

Adding a field or an enum value splits the data in two: rows written after the
change have it, rows written before do not. Any change that does this must
also state what happens to the old rows.

All three are required:

1. a migration or backfill for existing rows;
2. a sensible default for the new field;
3. null-safe handling on every read path, including code that predates the
   field and was not touched by this change.

Adding an enum member also means every branch over that enum
(`if`, `match`, membership sets such as `TERMINAL_STATUSES`) is now
potentially incomplete and must be re-checked.
