# Fixtures

Every file here is a **hand-shaped placeholder**, not a recording. It is
sufficient to run the suite today and it is not evidence of anything: it was
written from Plaid's documented response shape, so a test built on it checks
the skill against an assumed contract rather than an observed one.

## They get replaced

The walking-skeleton Bolt runs twice, in order — first against Plaid Sandbox,
then against a real linked account. Each run captures the responses it
produces, and those captures replace the placeholders here. That is the one
cheap window for it: once the skeleton has shipped, getting a real response
back costs a re-link.

Each replacement is committed with a `captured` date in its `_fixture` header.

## Redaction is asymmetric

- **Sandbox captures need no redaction.** Sandbox data is synthetic; there is
  no real balance, account number, or institution behind it.
- **Real-account captures must be redacted before they are committed.** Real
  balances, account numbers, and institution names are replaced with obvious
  placeholders. A capture that has not been through that step does not get
  staged, let alone committed.

## The `_fixture` key

Each JSON file carries a top-level `_fixture` object recording its provenance.
Plaid does not send that key; it is added here. It doubles as a check on the
output boundary — an unexpected top-level field that must never reach stdout,
and does not, because the projection copies fields in by name rather than
filtering them out.
