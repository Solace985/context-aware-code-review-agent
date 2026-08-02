---
id: authorize-every-resource
severity: high
---
# Authenticating is not authorising

`require_user` establishes who the caller is. It does not establish that they
may touch a particular row. Every handler that reads or writes a user-owned
resource must also call `assert_owner` (or `require_role`) against that
specific resource before acting on it.

A new endpoint added beside an authorised one is the classic miss: the
neighbouring handler does the check and the new one does not.
