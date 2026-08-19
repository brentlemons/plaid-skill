# Setup runbook — linking a bank account and getting an access token

This is the one-time manual setup behind the `plaid-balance` skill. It assumes
you are comfortable in a terminal and know nothing about Plaid or Hermes. It
takes about twenty minutes, most of it waiting on a browser.

You do this yourself. The skill deliberately contains no code that acquires
credentials: the token this runbook produces is, in practice, as sensitive as
your bank password, and minting it is not something an agent should be able to
do on your behalf.

**Nothing here needs `pip install`.** The helper script uses only the Python
standard library. You need Python 3.9 or newer and `curl`.

---

## What you are actually building

Plaid sits between your bank and this skill. Four terms are worth knowing
before the steps make sense:

- **Client ID and secret** — your developer credentials with Plaid. One client
  ID; a separate secret per environment.
- **Item** — one bank connection. An Item can hold several accounts (checking,
  savings, a card). Rate limits are counted per Item.
- **Link** — Plaid's hosted flow where you sign in to your bank. You do this in
  a browser; your bank credentials go to your bank, never to this repository
  and never to the agent.
- **Access token** — the long-lived credential that lets this skill read that
  Item's balances. It does not expire on a clock, but it breaks when the bank
  password changes or the bank forces a re-authentication.

The sequence below mints a link token, sends you through Link in a browser,
converts the result into a public token, and exchanges that for the access
token you keep.

---

## If Hermes runs on a different machine

The steps below assume one machine does everything. If you develop here and run
Hermes somewhere else, the work splits — and the split matters, because the
access token has to cross the gap without going through the repository.

**On whichever machine you like** — Steps 1 through 6. They need `curl` and a
browser, nothing else. The output of Step 6 is a single access token string.

**On the Hermes machine** — Step 2's environment file, Step 7's install, and
every later run of the skill. Nothing else from this repository needs to be
there:

```
terminal(command="scp -r skill you@hermes-host:~/.hermes/skills/plaid-balance")
```

The skill directory is all that travels. `tests/`, `.githooks/`, and any
virtualenv are development concerns and have no role where the skill runs. The
skill needs Python 3.9 or later on that machine and nothing else — it imports
only the standard library, so there is no install step to get wrong.

### Moving the token without leaking it

The access token must end up in `~/.hermes/plaid-balance.env` **on the Hermes
machine**. Create the file there, as in Step 2, and paste the value in over an
encrypted connection:

```
terminal(command="ssh you@hermes-host 'mkdir -p ~/.hermes && touch ~/.hermes/plaid-balance.env && chmod 600 ~/.hermes/plaid-balance.env'")
```

Then edit that file on the remote host and paste the token in.

**Do not move it through this repository.** The pre-commit hook and the ignore
rules protect files inside the repository; they do nothing about a token you
paste into a file you then copy, mail to yourself, or drop in a shared
directory. The hook is a guard on one path, not a general property of the
value.

Two habits worth keeping for the same reason:

- Do not paste the token into a chat with an agent, including this one. Anything
  in a transcript is in a transcript.
- If you use a scratch file during Steps 5 and 6, put it outside the repository
  and delete it afterwards. `/tmp` is fine; the repository is not.

Everything after this section reads the same on either machine. Where a step
belongs to only one of them, it says so.

---

## Step 0 — Point git at the committed hooks

*Development machine only — this is repository work. The Hermes machine has no
clone of this repository, only the copied skill directory.*

Do this before you create any credentials, so the guard exists before there is
anything to leak. From the repository root:

terminal(command="git config core.hooksPath .githooks")

This tells git to use the hooks committed in `.githooks/` rather than the empty
`.git/hooks/` a clone gives you. The pre-commit hook runs `gitleaks` over your
staged changes and refuses the commit on a hit. Install it first:

terminal(command="brew install gitleaks")

On Linux, follow the instructions at
`https://github.com/gitleaks/gitleaks#installing`.

The hook fails closed: with gitleaks missing, commits are refused rather than
waved through. That is deliberate. This repository is public, and Plaid keys are
not covered by GitHub's secret-scanning partner programme, so nothing catches a
leaked token after the push.

---

## Step 1 — Create a Plaid account and read your keys

1. Sign up at `https://dashboard.plaid.com/signup`. The free Trial plan is
   enough for this skill and covers the products it needs.
2. Open **Developers → Keys**.
3. Copy your **client ID** and your **Sandbox secret**.

Sandbox is Plaid's test environment. Its banks and balances are synthetic, so
you can get the whole flow working without touching a real account. You will
repeat the linking steps against Production later.

---

## Step 2 — Create the environment file

*Do this on the machine where Hermes runs. That is where the skill reads it.*

Real credentials live outside this repository, at the path the skill looks in
by default:

terminal(command="mkdir -p ~/.hermes && touch ~/.hermes/plaid-balance.env && chmod 600 ~/.hermes/plaid-balance.env")

Open that file in an editor and copy in the four variables from
[`.env.example`](../../.env.example) at the repository root, filling in the two
values you have so far:

```
PLAID_CLIENT_ID=<your client id from step 1>
PLAID_SECRET=<your sandbox secret from step 1>
PLAID_ACCESS_TOKEN=
PLAID_ENV=sandbox
```

Leave `PLAID_ACCESS_TOKEN` empty for now; step 6 fills it in.

`.env.example` is the single definition of these four names. Spell them exactly
as they appear there — the skill does not guess at near-misses, and a typo
surfaces as `CONFIG_MISSING` naming the variable it could not find.

A file called `.env` inside the repository is gitignored and may hold **sandbox
values only**. Sandbox data is synthetic, so nothing real is at stake if that
file is mishandled. Production values never go there.

Load the variables into your current shell for the steps below:

terminal(command="set -a && . ~/.hermes/plaid-balance.env && set +a")

---

## Step 3 — Mint a link token

Write the request body to a scratch file so no credential ends up in your shell
history:

terminal(command="printf '{\"client_id\":\"%s\",\"secret\":\"%s\",\"client_name\":\"Plaid Balance Skill\",\"language\":\"en\",\"country_codes\":[\"US\"],\"user\":{\"client_user_id\":\"local-operator-1\"},\"products\":[\"balance\"],\"hosted_link\":{}}' \"$PLAID_CLIENT_ID\" \"$PLAID_SECRET\" > /tmp/link_token_request.json")

terminal(command="curl -sS -X POST https://sandbox.plaid.com/link/token/create -H 'Content-Type: application/json' -d @/tmp/link_token_request.json")

The response contains a `link_token` and a `hosted_link_url`.

Two things about `products`. It is fixed when the token is minted — widening it
later means linking the account again — so ask for the narrowest set that does
what you need, which for balances is `balance` alone. And if Plaid rejects the
product for your institution, open **Developers → Products** in the dashboard
to see what your plan has enabled, and use the narrowest option that still
includes balance data. Do not reach for a broader product than you need; the
grant is the data you are handing over.

`hosted_link` being present, even as an empty object, is what makes Plaid host
the Link flow for you. Without it you would need to run a web page yourself.
With it there is no server, no webhook, and no redirect URI to configure.

Clean up the scratch file:

terminal(command="rm -f /tmp/link_token_request.json")

---

## Step 4 — Link the account in a browser

Open the `hosted_link_url` from the previous response in a browser and complete
the flow.

In Sandbox, use Plaid's test institution and the test credentials shown on the
page (`user_good` / `pass_good` at the time of writing). Your real bank
password is not involved at this stage.

When the flow finishes, the browser tells you it is done. Nothing is returned to
your terminal — the result is fetched in the next step.

---

## Step 5 — Retrieve the public token

terminal(command="printf '{\"client_id\":\"%s\",\"secret\":\"%s\",\"link_token\":\"%s\"}' \"$PLAID_CLIENT_ID\" \"$PLAID_SECRET\" \"<the link_token from step 3>\" > /tmp/link_token_get.json")

terminal(command="curl -sS -X POST https://sandbox.plaid.com/link/token/get -H 'Content-Type: application/json' -d @/tmp/link_token_get.json")

terminal(command="rm -f /tmp/link_token_get.json")

The response describes the link session you just completed. Find the
`public_token` inside its results. A public token is short-lived — exchange it
in the next step rather than saving it.

If the response shows no completed session, the browser flow did not finish.
Repeat step 4 with the same `hosted_link_url`.

---

## Step 6 — Exchange it for the access token

terminal(command="printf '{\"client_id\":\"%s\",\"secret\":\"%s\",\"public_token\":\"%s\"}' \"$PLAID_CLIENT_ID\" \"$PLAID_SECRET\" \"<the public_token from step 5>\" > /tmp/exchange.json")

terminal(command="curl -sS -X POST https://sandbox.plaid.com/item/public_token/exchange -H 'Content-Type: application/json' -d @/tmp/exchange.json")

terminal(command="rm -f /tmp/exchange.json")

The response contains `access_token`. **This is the credential to protect.**
Put it in `~/.hermes/plaid-balance.env` as `PLAID_ACCESS_TOKEN`, and nowhere
else. Not in this repository, not in a commit, not pasted into a chat with an
agent.

---

## Step 7 — Install the skill and verify

*Hermes machine. If you are working across two hosts, see "If Hermes runs on a
different machine" above for how the directory and the token get there.*

Copy the skill directory to where Hermes looks for skills:

terminal(command="cp -r skill ~/.hermes/skills/plaid-balance")

Run the helper script directly:

terminal(command="python3 ~/.hermes/skills/plaid-balance/scripts/plaid_balance.py")

A document with `"status": "ok"` and at least one account means everything is
wired up. `CONFIG_MISSING` names the variable that is still empty.

Then ask Hermes a balance question in plain words and confirm it loads the skill
on its own. The README describes how to check that properly, in a session that
has never seen the skill.

---

## Step 8 — Moving to a real account

Sandbox proves the plumbing. A real balance needs Production:

1. Request Production access in the Plaid dashboard and wait for approval.
2. Copy your **Production secret** from **Developers → Keys**.
3. Update `~/.hermes/plaid-balance.env`: set `PLAID_SECRET` to the production
   secret and `PLAID_ENV` to `production`.
4. Repeat steps 3 to 6 against `https://production.plaid.com` instead of
   `https://sandbox.plaid.com`, and sign in to your own bank in the browser.
5. Replace `PLAID_ACCESS_TOKEN` with the new value.

A Sandbox access token does not work against Production. The two environments
share nothing but your client ID.

---

## Storing the token more safely

The environment file is what this version implements, and it is the weakest
part of the setup: a plain file readable by anything running as you. `chmod 600`
narrows it to your account and no further.

The hardening option, **not implemented here**, is your OS keychain:

- **macOS** — store it with
  terminal(command="security add-generic-password -a \"$USER\" -s plaid-balance -w")
  and read it back with
  terminal(command="security find-generic-password -a \"$USER\" -s plaid-balance -w")
- **Linux** — `secret-tool store` / `secret-tool lookup` from libsecret.

Using either today means exporting the value into the environment before
invoking the skill, since the script reads only the environment and the
environment file. Teaching the script to read a keychain directly is deferred
work, recorded rather than done.

---

## When something breaks

| Symptom | Cause | What to do |
|---|---|---|
| `CONFIG_MISSING` naming a variable | That name is unset and absent from the environment file | Check the spelling against `.env.example` |
| `CONFIG_INVALID` | `PLAID_ENV` is not `sandbox` or `production` | Fix the value; there is no default |
| `ITEM_LOGIN_REQUIRED` | Bank password changed, bank forced re-auth, or a European consent window lapsed | Repeat steps 3 to 6 and replace the access token |
| `RATE_LIMIT_EXCEEDED` | More than 5 balance reads in a minute or 30 in an hour, for one Item | Wait. Do not re-run in a loop; the script will not retry for you |
| `NETWORK_ERROR` | This machine could not reach Plaid | Check connectivity, then Plaid's status page |
| `unknown institution` in the output | The institution name lookup did not resolve | Harmless; the balances are still correct |

Rate limits are counted per Item, per rolling window. They are the reason this
skill is invoked on demand and never on a schedule.
