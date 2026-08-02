---
id: no-swallowed-errors
severity: medium
---
# Do not swallow errors

A caught exception must be handled, re-raised, or logged with enough context
to diagnose it. A bare `except:` that continues silently turns a loud failure
into a silent wrong answer, and hides authorisation failures in particular.

If a failure genuinely is expected and safe to ignore, catch the specific
exception type and leave a comment saying why.
