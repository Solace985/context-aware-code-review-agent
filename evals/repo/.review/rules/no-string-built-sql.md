---
id: no-string-built-sql
severity: high
---
# Never build SQL by string concatenation or interpolation

Query text and query parameters must stay separate. Every statement in this
codebase goes through `app/db.py` with `?` placeholders and a params tuple.

Wrong:

    query(f"SELECT * FROM users WHERE email = '{email}'")

Right:

    query("SELECT * FROM users WHERE email = ?", (email,))

If a query really must be assembled dynamically, only an identifier chosen
from a hard-coded allowlist may be interpolated — never a user-supplied value.
