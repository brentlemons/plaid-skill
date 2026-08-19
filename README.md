# plaid-balance

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill that lets
the agent answer "what's my balance?" without you leaving the conversation to
go and look.

Ask in plain words — *how much is in checking?*, *can I cover a £400 repair?* —
and the agent reads the current and available balance of the accounts you have
linked through [Plaid](https://plaid.com), then answers with the figures in
front of it.

It reads. It cannot move money, and it has no code that could.

---

## What you get

```
plaid-skill/
├── README.md                  this file
├── .env.example               the four variables, defined once
├── pyproject.toml             ruff and pytest configuration
├── .githooks/
│   ├── pre-commit             gitleaks secret scan, blocking
│   └── pre-push               test suite, blocking; lint, advisory
├── skill/                     ← the part that gets installed
│   ├── SKILL.md               what the agent reads
│   ├── references/setup.md    the setup runbook
│   └── scripts/plaid_balance.py
└── tests/
    ├── fixtures/              placeholder Plaid responses
    └── skills/test_plaid_balance_skill.py
```

Only `skill/` is installed. The tests, hooks, and this file stay in the
repository, out of the directory the agent scans.

---

## Before anything else

Point git at the hooks that ship with the repository. A clone gives you an
empty `.git/hooks/`, so until you run this, nothing is scanning your commits:

terminal(command="git config core.hooksPath .githooks")

Then install the scanner the pre-commit hook calls:

terminal(command="brew install gitleaks")

On Linux, see `https://github.com/gitleaks/gitleaks#installing`.

The hook fails closed. With gitleaks missing it refuses commits rather than
waving them through, because this repository is public and Plaid keys are not
covered by GitHub's secret-scanning partner programme — there is no
server-side net under a leaked token.

---

## Installing

There is no `pip install` step and no dependency to audit. The helper script
uses only the Python standard library, so installing is copying a directory.
You need Python 3.9 or newer.

terminal(command="cp -r skill ~/.hermes/skills/plaid-balance")

Then follow **[the setup runbook](skill/references/setup.md)** to link an
account and get your credentials. It explains Plaid from scratch and assumes
only that you are comfortable in a terminal. Budget about twenty minutes, most
of it waiting on a browser.

Four variables come out of that process, defined once in
[`.env.example`](.env.example) and spelled the same way everywhere. Real values
go in `~/.hermes/plaid-balance.env`, outside this repository. Sandbox values
may live in a gitignored `.env` here; production values never do.

**If Hermes runs on a different machine from where you cloned this**, only the
`skill/` directory needs to travel — the tests, hooks, and any virtualenv stay
behind. The environment file is created on the Hermes machine, and the access
token must get there without passing through this repository: the pre-commit
hook guards one path, not the value itself. The runbook's *"If Hermes runs on a
different machine"* section covers the split step by step.

Check it works:

terminal(command="python3 ~/.hermes/skills/plaid-balance/scripts/plaid_balance.py")

---

## Using it

Ask the agent. That is the whole interface — you should not have to name the
skill, and if you do, something is wrong with it rather than with you.

Under the hood it prints JSON. Success looks like this:

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

Failure prints `"status": "error"` with a stable `error_code` and exits
non-zero. `SKILL.md` lists every code and what it means. The two you will
actually meet are `ITEM_LOGIN_REQUIRED`, when your bank password changed and
the connection needs re-linking, and `RATE_LIMIT_EXCEEDED`, when you have used
up Plaid's allowance of 5 balance reads a minute or 30 an hour for one bank.

Nothing retries on your behalf. A retry would spend the same allowance you are
waiting on, so a rate-limited read is reported and left alone.

---

## What it will not print

Five fields per account, and no sixth: the balance figures with their
currency, the account name, the last four digits, the account type, and the
institution name.

That is not a convention, it is a mechanism. One function writes output, it
checks every field against that list before writing a byte, and it then scans
the rendered text for any credential the process loaded and refuses to print
if it finds one. Account identifiers, item identifiers, official account
names, and full account numbers have no path to your terminal. A test injects a
sentinel token and a sentinel full account number and asserts they appear in no
stdout, no stderr, and no log.

The access token itself never appears in output, and neither should it appear
in your conversation. If an agent asks you to paste one in, refuse.

---

## Checking that the agent finds it on its own

A skill that never gets selected is a skill that does not exist. The agent
picks from a truncated index, so `description` in `SKILL.md` has to be under 60
characters with the words a person would actually say inside the first 57.

Two halves, checked two ways.

**The mechanical half** is a test. `description` length, the trailing period,
and the trigger keywords surviving truncation are asserted over `SKILL.md`
itself, so they cannot drift unnoticed.

**The behavioural half cannot be automated**, because it is state-destroying: a
session that has seen the skill once can never again tell you whether it would
have found it unprompted. So it is a written protocol, run by hand:

1. Start a Hermes session that has never mentioned this skill.
2. Ask **one** phrasing from the list below, in plain words. Do not name the
   skill, Plaid, or the script.
3. Record two things: did the skill load without being told to, and was the
   answer correct?
4. Quit. Start a **fresh** session for the next phrasing — reusing the session
   invalidates the result.

The phrasings:

| # | Ask |
|---|---|
| 1 | What's my balance? |
| 2 | How much money do I have right now? |
| 3 | How much is in my checking account? |
| 4 | Can I afford a $400 repair this week? |
| 5 | What's in my savings? |

**Re-run this whole protocol whenever `description` changes.** That field is
the entire selection mechanism; editing it invalidates every previous result.

---

## Running the tests

The skill needs nothing installed. The test suite needs pytest, which is a
development tool rather than a runtime dependency and is deliberately not
declared in `pyproject.toml`:

terminal(command="python3 -m venv .venv && .venv/bin/pip install pytest")

terminal(command=".venv/bin/python3 -m pytest")

The suite makes no live network call, needs no credentials, and passes on a
fresh clone with the machine offline. It also runs automatically at `git push`
via the committed pre-push hook, which is the only place it runs mechanically —
this project has no CI.

The eight cases the suite exists to cover are: single-account render,
multi-account render, broken credential, rate limit, missing configuration, the
sentinel leak test, the offline fresh-clone run, and the pre-commit hook
refusing a planted secret.

---

## Status

**This is version 0.1.0, and the walking skeleton has not finished running.**

The skeleton runs twice, in this order, and neither run has happened yet:

1. **Against Plaid Sandbox**, to prove the plumbing end to end — link one
   account, read one balance, confirm it renders correctly inside a Hermes
   response.
2. **Against a real linked account**, which is the actual goal and the point at
   which the open question of whether your bank is reachable through Plaid gets
   answered.

Version 0.1.0 is not done until both have run. Each run captures the Plaid
responses it produces, and those captures replace the placeholder fixtures in
`tests/fixtures/`. The sandbox capture needs no redaction, because sandbox data
is synthetic. The real-account capture is redacted before it is committed —
real balances, account numbers, and institution names replaced with obvious
placeholders.

Known gaps, recorded rather than hidden:

- **Token storage is a file.** `~/.hermes/plaid-balance.env` with `chmod 600`
  is what this version implements. The OS keychain is documented in the runbook
  as hardening and is not wired up.
- **Transactions are not read.** Balances only, deliberately.
- **The institution-name lookup is best-effort.** If it fails you get
  `unknown institution` and correct balances, rather than an error.

---

## Licence

MIT.
