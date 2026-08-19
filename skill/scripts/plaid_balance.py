#!/usr/bin/env python3
"""Read bank account balances from Plaid and print an allowlisted summary.

Design notes that are load-bearing rather than decorative:

* **Standard library only.** ``urllib.request`` talks to Plaid's REST API
  directly. There is no SDK and no HTTP library, so installing this skill is
  copying a directory - there is no dependency to audit and no install step to
  get wrong.

* **One way out.** :func:`emit` is the only function in this module that writes
  to a stream. Everything it prints is checked against an explicit field
  allowlist first, and then the rendered text is checked once more against the
  credential values this process loaded. A field that is not named cannot be
  printed, and a value that is a credential cannot be printed even if some
  future edit puts it in an allowlisted field by mistake.

* **No retries, no timers, no schedule.** Exactly one HTTP request per lookup,
  plus one optional lookup for the institution's display name. A rate-limited
  request is reported to the caller and never repeated: Plaid caps balance
  reads per linked institution, so an automatic retry would spend the very
  budget the caller needs.

* **Read only.** The two endpoints below are the only ones this script knows
  about, and both are reads.

* **No tracebacks in normal operation.** Failures become a JSON document with a
  stable ``error_code``. ``--debug`` puts the traceback back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

__version__ = "0.1.0"

SKILL_NAME = "plaid-balance"

# Relative path, as a reader of the skill directory would see it. Never an
# absolute path: this string is quoted back to the user inside error output.
SETUP_RUNBOOK = "references/setup.md"

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# Both endpoints are reads. Nothing in this module writes, moves, or modifies
# anything at the bank or at Plaid.
BALANCE_ENDPOINT = "/accounts/balance/get"
INSTITUTION_ENDPOINT = "/institutions/get_by_id"

# Widen this if you link an institution outside the United States; Plaid
# requires the country list on the institution lookup.
INSTITUTION_COUNTRY_CODES = ("US",)

# Defined once in .env.example and spelled the same way here, in the runbook,
# and in the README.
REQUIRED_VARS = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ACCESS_TOKEN", "PLAID_ENV")
ENV_FILE_VAR = "PLAID_BALANCE_ENV_FILE"
DEFAULT_ENV_FILE = os.path.join("~", ".hermes", "plaid-balance.env")

REQUEST_TIMEOUT_SECONDS = 30

# Plaid returns only the last four digits of an account number in `mask`, but
# this script truncates anyway. If that ever stops being true, the truncation is
# what stands between a full account number and the terminal.
MASK_LENGTH = 4

UNKNOWN_INSTITUTION = "unknown institution"

RATE_LIMIT_MESSAGE = (
    "Plaid refused the request because the balance rate limit for this linked "
    "institution is exhausted. The limits are 5 balance reads per minute and 30 "
    "per hour. This script does not retry on its own, because a retry would "
    "spend the same budget you are waiting on."
)

ITEM_LOGIN_REQUIRED_MESSAGE = (
    "The saved bank connection is no longer authorised. This normally means the "
    "bank password changed, the bank forced a re-authentication, or a European "
    "institution's 180-day consent window expired. The stored access token "
    "cannot be repaired; the account has to be linked again."
)


# ---------------------------------------------------------------------------
# The output allowlist
# ---------------------------------------------------------------------------
# NFR-1.1 and NFR-1.2: exactly five fields per account may be printed. Anything
# Plaid returns that is not in this tuple - account identifiers, official
# names, routing or account numbers, item metadata, request ids - has no path
# to a terminal, because the projection never copies it and `emit` would refuse
# it if it somehow appeared.
ACCOUNT_FIELD_ALLOWLIST = (
    "balances",  # the balance figures, plus the currency they are quoted in
    "account_name",
    "mask",  # last four digits only
    "account_type",
    "institution_name",
)

BALANCE_FIELD_ALLOWLIST = ("available", "current", "currency")

DOCUMENT_FIELD_ALLOWLIST = {
    "ok": ("status", "accounts"),
    "error": (
        "status",
        "error_code",
        "message",
        "next_step",
        "plaid_error_type",
        "plaid_error_code",
        "detail",
    ),
}

# Values loaded from the environment that must never appear in output. Short
# values are ignored: a two-character "secret" would match everywhere and turn
# the check into noise.
MIN_REGISTERED_SECRET_LENGTH = 8
_REGISTERED_SECRETS = set()


def register_secret(value):
    """Record a credential value so :func:`emit` can refuse to print it."""
    if isinstance(value, str) and len(value.strip()) >= MIN_REGISTERED_SECRET_LENGTH:
        _REGISTERED_SECRETS.add(value.strip())


def registered_secrets():
    """Return a copy of the registered credential values (for tests)."""
    return set(_REGISTERED_SECRETS)


def forget_secrets():
    """Drop every registered credential value (for tests)."""
    _REGISTERED_SECRETS.clear()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillError(Exception):
    """A failure the caller is meant to read, carrying a stable error code."""

    def __init__(self, error_code, message, next_step, extra=None, detail=None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.next_step = next_step
        self.extra = dict(extra or {})
        self.detail = detail

    def as_document(self, debug=False):
        document = {
            "status": "error",
            "error_code": self.error_code,
            "message": self.message,
            "next_step": self.next_step,
        }
        document.update(self.extra)
        # Plaid's own `error_message` is free text from a remote system. It is
        # useful when diagnosing, and it is not something to print by default.
        if debug and self.detail:
            document["detail"] = self.detail
        return document


class OutputBlocked(Exception):
    """Raised when :func:`emit` refuses to print what it was handed."""

    def __init__(self, reason, offenders):
        super().__init__(reason)
        self.reason = reason
        self.offenders = list(offenders)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Config:
    """Resolved credentials and the host they apply to."""

    def __init__(self, client_id, secret, access_token, environment):
        self.client_id = client_id
        self.secret = secret
        self.access_token = access_token
        self.environment = environment

    @property
    def host(self):
        return PLAID_HOSTS[self.environment]

    def __repr__(self):
        # A traceback under --debug prints repr() of locals in some tools. This
        # class holds three credentials, so its repr says so and nothing else.
        return (
            f"Config(environment={self.environment!r}, client_id=<redacted>, secret=<redacted>, "
            "access_token=<redacted>)"
        )


def read_env_file(path):
    """Parse a KEY=VALUE file. A missing or unreadable file yields no values."""
    if not path:
        return {}
    expanded = os.path.expanduser(os.path.expandvars(path))
    try:
        with open(expanded, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}

    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_config(environ=None, env_file_path=None):
    """Resolve the four required variables, environment first, then the file."""
    environ = os.environ if environ is None else environ
    resolved = {name: (environ.get(name) or "").strip() for name in REQUIRED_VARS}

    if not all(resolved.values()):
        path = env_file_path or environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE
        from_file = read_env_file(path)
        for name in REQUIRED_VARS:
            if not resolved[name]:
                resolved[name] = (from_file.get(name) or "").strip()

    for name in REQUIRED_VARS:
        if not resolved[name]:
            raise SkillError(
                "CONFIG_MISSING",
                f"{name} is not set, so the balance lookup cannot run.",
                "Set {} in your environment, or add it to the skill's environment "
                "file. {} lists every variable and where to get its value; the "
                "setup runbook at {} walks through it.".format(name, ".env.example", SETUP_RUNBOOK),
            )

    environment = resolved["PLAID_ENV"].lower()
    if environment not in PLAID_HOSTS:
        raise SkillError(
            "CONFIG_INVALID",
            "PLAID_ENV is set to a value this skill does not recognise.",
            "Set PLAID_ENV to one of: {}.".format(", ".join(sorted(PLAID_HOSTS))),
        )

    register_secret(resolved["PLAID_CLIENT_ID"])
    register_secret(resolved["PLAID_SECRET"])
    register_secret(resolved["PLAID_ACCESS_TOKEN"])

    return Config(
        client_id=resolved["PLAID_CLIENT_ID"],
        secret=resolved["PLAID_SECRET"],
        access_token=resolved["PLAID_ACCESS_TOKEN"],
        environment=environment,
    )


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _error_from_http(exc):
    """Turn an HTTPError from Plaid into a SkillError with a stable code."""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - a body we cannot read is simply absent
        raw = b""

    body = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            body = parsed

    error_type = str(body.get("error_type") or "")
    error_code = str(body.get("error_code") or "")
    detail = str(body.get("error_message") or "") or None

    extra = {}
    if error_type:
        extra["plaid_error_type"] = error_type
    if error_code:
        extra["plaid_error_code"] = error_code

    status = getattr(exc, "code", None)

    if status == 429 or error_type == "RATE_LIMIT_EXCEEDED":
        return SkillError(
            "RATE_LIMIT_EXCEEDED",
            RATE_LIMIT_MESSAGE,
            "Wait for the limit window to pass and ask again. Do not re-run the "
            "command in a loop; each attempt consumes the same budget.",
            extra=extra,
            detail=detail,
        )

    if error_code == "ITEM_LOGIN_REQUIRED":
        return SkillError(
            "ITEM_LOGIN_REQUIRED",
            ITEM_LOGIN_REQUIRED_MESSAGE,
            f"Re-link the account by following the setup runbook at {SETUP_RUNBOOK}, then "
            "replace PLAID_ACCESS_TOKEN with the new value.",
            extra=extra,
            detail=detail,
        )

    return SkillError(
        "PLAID_ERROR",
        "Plaid rejected the balance request.",
        "Check the Plaid error code above against Plaid's error reference, then "
        f"re-check the credentials described in {SETUP_RUNBOOK}.",
        extra=extra,
        detail=detail,
    )


def post_json(host, endpoint, payload):
    """Make exactly one POST to Plaid and return the decoded JSON body.

    One request. There is no retry, no backoff, and no loop anywhere in this
    function: a failure is reported to the caller, never repeated.
    """
    request = urllib.request.Request(  # noqa: S310 - host comes from PLAID_HOSTS
        host + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"{SKILL_NAME}/{__version__}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(  # noqa: S310 - see above
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _error_from_http(exc) from None
    except urllib.error.URLError as exc:
        raise SkillError(
            "NETWORK_ERROR",
            "Could not reach Plaid.",
            f"Check that this machine is online and can reach {host}, then ask again.",
            detail=str(getattr(exc, "reason", "")) or None,
        ) from None

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SkillError(
            "UNEXPECTED_RESPONSE",
            "Plaid's reply was not valid JSON.",
            "Try again in a moment. If it keeps happening, check Plaid's status "
            "page before changing anything here.",
        ) from None

    if not isinstance(body, dict):
        raise SkillError(
            "UNEXPECTED_RESPONSE",
            "Plaid's reply was not in the expected shape.",
            "Try again in a moment. If it keeps happening, check Plaid's status "
            "page before changing anything here.",
        )

    return body


def fetch_balances(config):
    """Read balances for every account on the linked Item."""
    return post_json(
        config.host,
        BALANCE_ENDPOINT,
        {
            "client_id": config.client_id,
            "secret": config.secret,
            "access_token": config.access_token,
        },
    )


def resolve_institution_name(config, payload):
    """Look up the institution's display name; degrade rather than fail.

    The balance response carries an institution *id*, not a name. The name is a
    separate read. If that read fails the balances are still correct and still
    worth printing, so the failure is absorbed into a placeholder.
    """
    item = payload.get("item")
    institution_id = ""
    if isinstance(item, dict):
        institution_id = str(item.get("institution_id") or "").strip()
    if not institution_id:
        return UNKNOWN_INSTITUTION

    try:
        body = post_json(
            config.host,
            INSTITUTION_ENDPOINT,
            {
                "client_id": config.client_id,
                "secret": config.secret,
                "institution_id": institution_id,
                "country_codes": list(INSTITUTION_COUNTRY_CODES),
            },
        )
    except SkillError:
        return UNKNOWN_INSTITUTION

    institution = body.get("institution")
    if isinstance(institution, dict):
        name = str(institution.get("name") or "").strip()
        if name:
            return name
    return UNKNOWN_INSTITUTION


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _as_amount(value):
    """Keep numeric balances, drop anything else. Booleans are not amounts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def project_accounts(payload, institution_name):
    """Reduce a raw Plaid response to the five allowlisted fields per account.

    This is a copy-in, not a filter-out: fields are read by name into a fresh
    dictionary, so a field Plaid adds tomorrow cannot arrive in the output by
    default.
    """
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise SkillError(
            "UNEXPECTED_RESPONSE",
            "Plaid's reply did not contain an account list.",
            f"Confirm the access token belongs to a linked account by following {SETUP_RUNBOOK}.",
        )

    projected = []
    for account in accounts:
        if not isinstance(account, dict):
            continue

        balances = account.get("balances")
        if not isinstance(balances, dict):
            balances = {}

        currency = balances.get("iso_currency_code") or balances.get("unofficial_currency_code")

        kind = str(account.get("type") or "").strip()
        subtype = str(account.get("subtype") or "").strip()
        account_type = "/".join(part for part in (kind, subtype) if part) or "unknown"

        projected.append(
            {
                "account_name": str(account.get("name") or "unnamed account"),
                "account_type": account_type,
                "institution_name": institution_name,
                "mask": str(account.get("mask") or "")[-MASK_LENGTH:],
                "balances": {
                    "available": _as_amount(balances.get("available")),
                    "current": _as_amount(balances.get("current")),
                    "currency": str(currency or "unknown"),
                },
            }
        )

    if not projected:
        raise SkillError(
            "UNEXPECTED_RESPONSE",
            "Plaid returned no accounts for this connection.",
            f"Confirm the account is still linked by following {SETUP_RUNBOOK}.",
        )

    return projected


# ---------------------------------------------------------------------------
# Output - the single way out
# ---------------------------------------------------------------------------


def _check_allowlist(document):
    """Raise OutputBlocked unless every field in `document` is named."""
    if not isinstance(document, dict):
        raise OutputBlocked("output document was not a mapping", ["<document>"])

    allowed = DOCUMENT_FIELD_ALLOWLIST.get(document.get("status"))
    if allowed is None:
        raise OutputBlocked("output document had no recognised status", ["status"])

    unexpected = sorted(set(document) - set(allowed))
    if unexpected:
        raise OutputBlocked("document field is not on the allowlist", unexpected)

    if document.get("status") != "ok":
        return

    accounts = document.get("accounts")
    if not isinstance(accounts, list):
        raise OutputBlocked("account list was not a list", ["accounts"])

    for account in accounts:
        if not isinstance(account, dict):
            raise OutputBlocked("account entry was not a mapping", ["accounts[]"])
        stray = sorted(set(account) - set(ACCOUNT_FIELD_ALLOWLIST))
        if stray:
            raise OutputBlocked("account field is not on the allowlist", stray)
        balances = account.get("balances")
        if not isinstance(balances, dict):
            raise OutputBlocked("balance entry was not a mapping", ["balances"])
        stray_balances = sorted(set(balances) - set(BALANCE_FIELD_ALLOWLIST))
        if stray_balances:
            raise OutputBlocked("balance field is not on the allowlist", stray_balances)


def emit(document, stream=None):
    """Write `document` as JSON. The only function here that writes anything.

    Two checks run before a single byte is written: the field allowlist, and a
    scan of the rendered text for any credential this process loaded. The second
    check is the backstop for the first - it catches a credential that reached
    an allowlisted field through some future mistake.
    """
    _check_allowlist(document)
    text = json.dumps(document, indent=2, sort_keys=True)

    leaked = sorted(secret for secret in _REGISTERED_SECRETS if secret in text)
    if leaked:
        # Report how many, never which, and never the value.
        raise OutputBlocked(
            "output text contained a credential value",
            [f"<credential x{len(leaked)}>"],
        )

    target = sys.stdout if stream is None else stream
    target.write(text + "\n")


def _blocked_document(blocked):
    return {
        "status": "error",
        "error_code": "OUTPUT_BLOCKED",
        "message": (
            f"The skill refused to print its own output: {blocked.reason}. Nothing was printed."
        ),
        "next_step": (
            "This is a defect in the skill, not a problem with your account. "
            "Report it with the offending field names: {}.".format(
                ", ".join(blocked.offenders) or "none recorded"
            )
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose error path cannot echo its input.

    argparse's default error() writes the offending argument verbatim to
    stderr and exits. In an agent context stderr is transcript, so that path
    bypasses emit()'s allowlist entirely -- and it runs before load_config(),
    so emit()'s credential rescan would have nothing registered to scan for
    even if the message were routed through it. The message is therefore
    dropped rather than sanitised: the reason is named, the input never is.
    """

    def error(self, message):  # noqa: A003 - argparse's own hook name
        emit(
            {
                "status": "error",
                "error_code": "BAD_ARGUMENTS",
                "message": "The command line could not be parsed.",
                "next_step": (
                    "Run the script with --help to see the accepted flags. The "
                    "offending text is deliberately not echoed, because anything "
                    "printed here would land in the transcript unscanned."
                ),
            }
        )
        raise SystemExit(2)


def build_parser():
    parser = _Parser(
        prog="plaid_balance.py",
        description=(
            "Print the balances of the bank accounts behind one linked Plaid connection, as JSON."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Restore Python tracebacks and include Plaid's own error text. Never "
            "prints credentials: the output check still applies."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def build_document(debug=False):
    """Produce the document to print, and the exit code. Writes nothing."""
    try:
        config = load_config()
        payload = fetch_balances(config)
        institution_name = resolve_institution_name(config, payload)
        accounts = project_accounts(payload, institution_name)
        return {"status": "ok", "accounts": accounts}, 0
    except SkillError as exc:
        return exc.as_document(debug=debug), 1
    except Exception:
        if debug:
            raise
        return {
            "status": "error",
            "error_code": "UNEXPECTED_ERROR",
            "message": "The balance lookup failed for an unexpected reason.",
            "next_step": (
                "Run the same command again with --debug to see the full Python traceback."
            ),
        }, 1


def run(argv=None):
    """Run one lookup. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    document, exit_code = build_document(debug=args.debug)
    try:
        emit(document)
    except OutputBlocked as blocked:
        emit(_blocked_document(blocked))
        return 1
    return exit_code


def main():
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
