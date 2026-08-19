---
name: plaid-balance
description: Check bank account balances and available money.
version: 0.1.0
author: Brent Lemons
license: MIT
platforms:
  - macos
  - linux
metadata:
  hermes:
    tags:
      - finance
      - banking
      - balance
    related_skills: []
---

# Bank account balances

Reads the current and available balance of the bank accounts behind one linked
Plaid connection and reports them as JSON.

## When to use this

Use it whenever the user asks how much money they have, what their balance is,
whether an amount is affordable, or how much is in a named account — including
when the question is a step inside a larger task ("can I cover this?", "budget
the trip against what's actually in checking").

Do not use it for transaction history, transfers, or payments. This skill reads
balances and nothing else; it cannot move money.

## Prerequisites

The user must have linked an account already and stored four values where the
script can find them. That is a one-time manual setup, described in
[references/setup.md](references/setup.md). If any value is missing the script
says which one, so run it first and read the error rather than interviewing the
user about their configuration.

## How to run it

One command, no arguments:

terminal(command="python3 ~/.hermes/skills/plaid-balance/scripts/plaid_balance.py")

If a lookup fails in a way that needs diagnosing, re-run with tracebacks and
Plaid's own error text restored:

terminal(command="python3 ~/.hermes/skills/plaid-balance/scripts/plaid_balance.py --debug")

## Reading the output

Success prints `"status": "ok"` and an `accounts` list. Each entry carries
exactly five things — the balance figures with their currency, the account
name, the last four digits, the account type, and the institution name:

```json
{
  "status": "ok",
  "accounts": [
    {
      "account_name": "Everyday Checking",
      "account_type": "depository/checking",
      "balances": { "available": 812.44, "currency": "USD", "current": 845.19 },
      "institution_name": "Example Bank",
      "mask": "0000"
    }
  ]
}
```

`available` is what the user can spend now; `current` includes pending activity.
Quote the figure with its currency, and when more than one account came back,
name each account so the user can tell them apart.

Failure prints `"status": "error"` with a stable `error_code` and exits
non-zero. Relay the `message` and `next_step` as written. Do not invent a
remedy: if the error does not tell you what to do, say so and point the user at
the setup runbook rather than guessing.

| `error_code` | What it means |
|---|---|
| `CONFIG_MISSING` | A required variable is unset; the message names which |
| `CONFIG_INVALID` | `PLAID_ENV` is not `sandbox` or `production` |
| `ITEM_LOGIN_REQUIRED` | The bank connection needs re-linking; see the runbook |
| `RATE_LIMIT_EXCEEDED` | Plaid's balance limit is spent; wait, do not retry |
| `NETWORK_ERROR` | Plaid was unreachable from this machine |
| `PLAID_ERROR` | Plaid rejected the request for another reason |
| `UNEXPECTED_RESPONSE` | Plaid's reply was not in the expected shape |
| `OUTPUT_BLOCKED` | The skill refused to print its own output; a defect, report it |

## Pitfalls

**Do not put this on a timer.** Plaid allows 5 balance reads per minute and 30
per hour for each linked institution. Run it when the user asks, once. A
background refresh burns the budget the user needs, and the script will not
retry a rate-limited call for the same reason.

**Do not echo credentials.** The script never prints the access token, the
client id, the secret, or a full account number, and it refuses to print
anything that is not one of the five allowlisted fields. Do not work around
that by reading the user's environment file yourself or by asking them to paste
a token into the conversation.

**Stale figures are the normal failure.** `ITEM_LOGIN_REQUIRED` after a
password change is expected, not a bug. The fix is re-linking, which is a
manual step in the runbook, not something to attempt from here.

## Verifying it works

Run the command. A `"status": "ok"` document with at least one account means
the connection, the credentials, and the environment setting all agree. A
`CONFIG_MISSING` error means setup was never finished; send the user to
[references/setup.md](references/setup.md).
