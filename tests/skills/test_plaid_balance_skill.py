"""Tests for the plaid-balance skill.

The eight named cases in the team's testing posture are the definition of done
for this skill, and they appear below under `TestCase1` .. `TestCase8` so each
one is individually pass/fail. Everything after them mechanises an invariant
that would otherwise only be checked by eye.

Two rules shape how this file is written:

* **No live network call, ever.** An autouse fixture replaces `urlopen` with a
  guard that fails the test if anything reaches for the network, and clears the
  Plaid variables so a configured machine cannot make a test pass that would
  fail on a fresh clone.

* **No credential-shaped literal.** The repository's own pre-commit hook scans
  this file. Sentinels and the deliberately scanner-triggering key used in
  case 8 are assembled from fragments at runtime, so the scanner does not
  choke on the suite that tests it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skill" / "scripts" / "plaid_balance.py"
SKILL_DOC = REPO_ROOT / "skill" / "SKILL.md"
SETUP_RUNBOOK = REPO_ROOT / "skill" / "references" / "setup.md"
README = REPO_ROOT / "README.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
HOOKS_DIR = REPO_ROOT / ".githooks"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load_module():
    spec = importlib.util.spec_from_file_location("plaid_balance", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plaid_balance"] = module
    spec.loader.exec_module(module)
    return module


plaid_balance = _load_module()


# ---------------------------------------------------------------------------
# Sentinels, assembled rather than written
# ---------------------------------------------------------------------------


def _assemble(*parts):
    """Join fragments into a sentinel.

    Written as fragments on purpose: a contiguous credential-shaped literal in
    this file would be caught by the pre-commit hook that case 8 exercises,
    making the suite uncommittable.
    """
    return "".join(parts)


SENTINEL_TOKEN = _assemble("sentinel-", "access-token-", "must-never-be-printed")
SENTINEL_ACCOUNT_NUMBER = _assemble("sentinel-", "full-account-number-", "must-never-be-printed")
# A Plaid-shaped access token, assembled from fragments like the sentinels above.
#
# This deliberately is NOT the canonical AWS example key (AKIA + IOSFODNN7 +
# EXAMPLE): gitleaks allowlists that value precisely because it appears in AWS
# documentation everywhere, so planting it proved nothing. Verified against
# gitleaks 8.30.1 -- this value fires the `plaid-api-token` rule, which is the
# rule that actually protects this repository.
SCANNER_TRIGGERING_FAKE_KEY = _assemble(
    "access-sandbox-", "de3ce8ef-33f8-", "452c-a685-", "8671031fc0f6"
)

GOOD_ENV = {
    "PLAID_CLIENT_ID": "test-client-id-value",
    "PLAID_SECRET": "test-secret-value",
    "PLAID_ACCESS_TOKEN": "test-access-token-value",
    "PLAID_ENV": "sandbox",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _http_error(status, body):
    return urllib.error.HTTPError(
        url="https://sandbox.plaid.com/accounts/balance/get",
        code=status,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def _route(routes):
    """Build the side effect that answers a request by endpoint suffix."""

    def _side_effect(request, timeout=None):
        url = request.full_url
        for suffix, outcome in routes.items():
            if url.endswith(suffix):
                if isinstance(outcome, Exception):
                    raise outcome
                return _FakeResponse(json.dumps(outcome).encode("utf-8"))
        raise AssertionError("test made a request to an unrouted URL: " + url)

    return _side_effect


def requested_endpoints(opener):
    """The endpoint path of every call made through a patched opener."""
    return [call.args[0].full_url for call in opener.call_args_list]


def load_fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def emitted(capsys):
    """Return (parsed stdout document, raw stdout, raw stderr)."""
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.out, captured.err


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


NO_NETWORK = "a test attempted a live network call"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Offline, unconfigured, and with no credential left over from a neighbour."""
    for name in plaid_balance.REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)
    # Point the fallback env file at somewhere that certainly does not exist, so
    # the operator's own ~/.hermes/plaid-balance.env can never make a test pass.
    monkeypatch.setenv(plaid_balance.ENV_FILE_VAR, str(tmp_path / "absent" / "plaid-balance.env"))
    plaid_balance.forget_secrets()

    guard = mock.patch.object(
        plaid_balance.urllib.request,
        "urlopen",
        side_effect=AssertionError(NO_NETWORK),
    )
    guard.start()
    try:
        yield
    finally:
        guard.stop()
        plaid_balance.forget_secrets()


@pytest.fixture
def configured(monkeypatch):
    def _configure(**overrides):
        values = dict(GOOD_ENV)
        values.update(overrides)
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return values

    return _configure


@pytest.fixture
def network():
    """Patch the one network seam, and hand back the mock that replaced it.

    The mock is the point: `call_count` is what makes "did not retry" a real
    assertion rather than an inspection of the source.
    """
    started = []

    def _install(routes):
        patcher = mock.patch.object(
            plaid_balance.urllib.request, "urlopen", side_effect=_route(routes)
        )
        opener = patcher.start()
        started.append(patcher)
        return opener

    try:
        yield _install
    finally:
        for patcher in reversed(started):
            patcher.stop()


BALANCE_ROUTE = plaid_balance.BALANCE_ENDPOINT
INSTITUTION_ROUTE = plaid_balance.INSTITUTION_ENDPOINT


# ===========================================================================
# Case 1 - single-account balance render
# ===========================================================================


class TestCase1SingleAccountRender:
    """The figure and the currency appear in the output. (B-3, SM-4, FR-1.1)"""

    def test_balance_figure_and_currency_are_rendered(self, configured, network, capsys):
        configured()
        network(
            {
                BALANCE_ROUTE: load_fixture("balance_get_single_account.json"),
                INSTITUTION_ROUTE: load_fixture("institutions_get_by_id.json"),
            }
        )

        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 0
        assert document["status"] == "ok"
        assert len(document["accounts"]) == 1

        account = document["accounts"][0]
        assert account["balances"]["available"] == 812.44
        assert account["balances"]["current"] == 845.19
        assert account["balances"]["currency"] == "USD"

        assert "812.44" in raw_out
        assert "845.19" in raw_out
        assert "USD" in raw_out
        assert raw_err == ""


# ===========================================================================
# Case 2 - multi-account render
# ===========================================================================


class TestCase2MultiAccountRender:
    """Accounts distinguished by name, mask, type, and institution name.

    (B-4, FR-1.2, FR-1.3; scope extended to institution name by correction UC-1)
    """

    def test_every_account_is_distinguishable(self, configured, network, capsys):
        configured()
        network(
            {
                BALANCE_ROUTE: load_fixture("balance_get_multi_account.json"),
                INSTITUTION_ROUTE: load_fixture("institutions_get_by_id.json"),
            }
        )

        exit_code = plaid_balance.run([])
        document, raw_out, _ = emitted(capsys)

        assert exit_code == 0
        accounts = document["accounts"]
        assert len(accounts) == 3

        assert [a["account_name"] for a in accounts] == [
            "Placeholder Checking",
            "Placeholder Savings",
            "Placeholder Rewards Card",
        ]
        assert [a["mask"] for a in accounts] == ["0000", "1111", "2222"]
        assert [a["account_type"] for a in accounts] == [
            "depository/checking",
            "depository/savings",
            "credit/credit card",
        ]
        # UC-1: institution name is required on every account, not just present
        # somewhere in the document.
        assert all(a["institution_name"] == "Placeholder Bank" for a in accounts)
        assert "Placeholder Bank" in raw_out

        # The four distinguishing fields are genuinely distinct per account.
        assert len({a["account_name"] for a in accounts}) == 3
        assert len({a["mask"] for a in accounts}) == 3

    def test_institution_name_survives_a_failed_lookup(self, configured, network, capsys):
        """A failed name lookup degrades the field; it does not fail the read."""
        configured()
        network(
            {
                BALANCE_ROUTE: load_fixture("balance_get_multi_account.json"),
                INSTITUTION_ROUTE: _http_error(500, {"error_type": "API_ERROR"}),
            }
        )

        exit_code = plaid_balance.run([])
        document, _, _ = emitted(capsys)

        assert exit_code == 0
        assert all(
            a["institution_name"] == plaid_balance.UNKNOWN_INSTITUTION for a in document["accounts"]
        )


# ===========================================================================
# Case 3 - ITEM_LOGIN_REQUIRED
# ===========================================================================


class TestCase3ItemLoginRequired:
    """Structured JSON naming the condition and pointing at the runbook, no traceback.

    (B-6, R-5, TC-6, FR-4.1, FR-4.2, FR-4.6)
    """

    def test_broken_credential_is_reported_as_structured_json(self, configured, network, capsys):
        configured()
        fixture = load_fixture("error_item_login_required.json")
        network({BALANCE_ROUTE: _http_error(400, fixture)})

        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 1
        assert document["status"] == "error"
        assert document["error_code"] == "ITEM_LOGIN_REQUIRED"
        assert document["plaid_error_code"] == "ITEM_LOGIN_REQUIRED"
        assert document["plaid_error_type"] == "ITEM_ERROR"

        # It points at the runbook rather than leaving the reader to guess.
        assert plaid_balance.SETUP_RUNBOOK in document["next_step"]

        # FR-4.6: no traceback anywhere.
        assert "Traceback" not in raw_out
        assert "Traceback" not in raw_err
        assert raw_err == ""

    def test_plaid_error_text_is_withheld_unless_debug_is_asked_for(
        self, configured, network, capsys
    ):
        configured()
        fixture = load_fixture("error_item_login_required.json")
        network({BALANCE_ROUTE: _http_error(400, fixture)})

        plaid_balance.run([])
        document, _, _ = emitted(capsys)
        assert "detail" not in document

    def test_debug_restores_plaid_error_text(self, configured, network, capsys):
        configured()
        fixture = load_fixture("error_item_login_required.json")
        network({BALANCE_ROUTE: _http_error(400, fixture)})

        plaid_balance.run(["--debug"])
        document, _, _ = emitted(capsys)
        assert fixture["error_message"] in document["detail"]


# ===========================================================================
# Case 4 - rate limiting
# ===========================================================================


class TestCase4RateLimit:
    """429 names the limit, prints no traceback, and is never retried.

    (TC-2, R-4, FR-4.3, FR-4.4)
    """

    def test_rate_limit_is_reported_and_not_retried(self, configured, network, capsys):
        configured()
        fixture = load_fixture("error_rate_limit_exceeded.json")
        opener = network({BALANCE_ROUTE: _http_error(429, fixture)})

        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 1
        assert document["error_code"] == "RATE_LIMIT_EXCEEDED"

        # FR-4.3: the message names the limit, not just the fact of a limit.
        assert "5" in document["message"]
        assert "30" in document["message"]
        assert "minute" in document["message"]
        assert "hour" in document["message"]

        # FR-4.4: exactly one request. A retry would spend the same budget the
        # caller is waiting on, so a second call here is a failure.
        assert opener.call_count == 1
        assert requested_endpoints(opener) == [plaid_balance.PLAID_HOSTS["sandbox"] + BALANCE_ROUTE]

        assert "Traceback" not in raw_out
        assert "Traceback" not in raw_err

    def test_rate_limit_recognised_from_error_type_without_a_429(self, configured, network, capsys):
        """Plaid signals the limit in the body as well as the status line."""
        configured()
        fixture = load_fixture("error_rate_limit_exceeded.json")
        opener = network({BALANCE_ROUTE: _http_error(400, fixture)})

        plaid_balance.run([])
        document, _, _ = emitted(capsys)

        assert document["error_code"] == "RATE_LIMIT_EXCEEDED"
        assert opener.call_count == 1


# ===========================================================================
# Case 5 - missing or malformed configuration
# ===========================================================================


class TestCase5Configuration:
    """Structured JSON naming the missing variable, no traceback. (SM-5, FR-4.1, FR-4.5)"""

    @pytest.mark.parametrize("missing", list(plaid_balance.REQUIRED_VARS))
    def test_each_missing_variable_is_named(self, configured, missing, capsys):
        configured(**{missing: None})

        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 1
        assert document["status"] == "error"
        assert document["error_code"] == "CONFIG_MISSING"
        # FR-4.5: the *specific* variable, not a generic complaint.
        assert missing in document["message"]
        for other in plaid_balance.REQUIRED_VARS:
            if other != missing:
                assert other not in document["message"]

        assert "Traceback" not in raw_out
        assert "Traceback" not in raw_err

    def test_malformed_environment_is_rejected_by_name(self, configured, capsys):
        configured(PLAID_ENV="prod")

        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 1
        assert document["error_code"] == "CONFIG_INVALID"
        assert "PLAID_ENV" in document["message"]
        assert "sandbox" in document["next_step"]
        assert "production" in document["next_step"]
        assert "Traceback" not in raw_out + raw_err

    def test_configuration_failure_makes_no_network_call(self, configured, capsys):
        """The isolation fixture's guard raises if anything reaches the network."""
        configured(PLAID_ACCESS_TOKEN=None)
        assert plaid_balance.run([]) == 1
        document, _, _ = emitted(capsys)
        assert document["error_code"] == "CONFIG_MISSING"

    def test_env_file_supplies_what_the_environment_lacks(self, configured, monkeypatch, tmp_path):
        configured(PLAID_ACCESS_TOKEN=None)
        env_file = tmp_path / "plaid-balance.env"
        env_file.write_text(
            "# a comment\n"
            "\n"
            'export PLAID_ACCESS_TOKEN="from-the-file"\n'
            "IGNORED line without an equals sign\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(plaid_balance.ENV_FILE_VAR, str(env_file))

        config = plaid_balance.load_config()
        assert config.access_token == "from-the-file"
        assert config.host == plaid_balance.PLAID_HOSTS["sandbox"]


# ===========================================================================
# Case 6 - the sentinel test
# ===========================================================================


class TestCase6NothingLeaks:
    """A sentinel token and a sentinel full account number reach no stream.

    (SC-1, SM-3, NFR-1.1, NFR-1.2, NFR-1.3)

    This is the case that turns a Hard NEVER rule into something mechanical.
    The sentinels are injected where a real leak would come from: the token into
    the configuration the script loads, and a full account number into the
    Plaid payload the script parses - in three separate fields, since a
    projection that reads fields by name should be indifferent to which one.
    """

    @staticmethod
    def _payload_with_account_number():
        payload = load_fixture("balance_get_single_account.json")
        account = payload["accounts"][0]
        account["account_id"] = SENTINEL_ACCOUNT_NUMBER
        account["official_name"] = "Account " + SENTINEL_ACCOUNT_NUMBER
        account["numbers"] = {
            "account": SENTINEL_ACCOUNT_NUMBER,
            "routing": SENTINEL_ACCOUNT_NUMBER,
        }
        payload["item"]["item_id"] = SENTINEL_ACCOUNT_NUMBER
        return payload

    def test_neither_sentinel_reaches_stdout_stderr_or_the_log(
        self, configured, network, capsys, caplog
    ):
        caplog.set_level(logging.DEBUG)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger().addHandler(handler)
        try:
            configured(PLAID_ACCESS_TOKEN=SENTINEL_TOKEN)
            network(
                {
                    BALANCE_ROUTE: self._payload_with_account_number(),
                    INSTITUTION_ROUTE: load_fixture("institutions_get_by_id.json"),
                }
            )

            exit_code = plaid_balance.run([])
            document, raw_out, raw_err = emitted(capsys)
        finally:
            logging.getLogger().removeHandler(handler)

        # The lookup succeeded: the sentinels were dropped by the projection,
        # not merely caught on the way out by the emit-time backstop.
        assert exit_code == 0
        assert document["status"] == "ok"

        for stream_name, text in (
            ("stdout", raw_out),
            ("stderr", raw_err),
            ("caplog", caplog.text),
            ("root logger", log_stream.getvalue()),
        ):
            assert SENTINEL_TOKEN not in text, "access token reached " + stream_name
            assert SENTINEL_ACCOUNT_NUMBER not in text, "full account number reached " + stream_name

    def test_sentinels_do_not_leak_on_the_error_path_either(self, configured, network, capsys):
        configured(PLAID_ACCESS_TOKEN=SENTINEL_TOKEN)
        fixture = load_fixture("error_item_login_required.json")
        fixture["error_message"] = "token " + SENTINEL_TOKEN + " is no longer valid"
        network({BALANCE_ROUTE: _http_error(400, fixture)})

        plaid_balance.run([])
        _, raw_out, raw_err = emitted(capsys)

        assert SENTINEL_TOKEN not in raw_out
        assert SENTINEL_TOKEN not in raw_err

    def test_the_client_id_and_secret_do_not_leak_either(self, configured, network, capsys):
        values = configured()
        network(
            {
                BALANCE_ROUTE: load_fixture("balance_get_single_account.json"),
                INSTITUTION_ROUTE: load_fixture("institutions_get_by_id.json"),
            }
        )

        plaid_balance.run([])
        _, raw_out, raw_err = emitted(capsys)

        for name in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ACCESS_TOKEN"):
            assert values[name] not in raw_out + raw_err

    def test_a_full_account_number_arriving_in_mask_is_truncated(self):
        payload = load_fixture("balance_get_single_account.json")
        payload["accounts"][0]["mask"] = SENTINEL_ACCOUNT_NUMBER
        projected = plaid_balance.project_accounts(payload, "Placeholder Bank")
        assert projected[0]["mask"] == SENTINEL_ACCOUNT_NUMBER[-4:]
        assert len(projected[0]["mask"]) == plaid_balance.MASK_LENGTH


# ===========================================================================
# Case 7 - fresh clone, offline, no environment file
# ===========================================================================


class TestCase7FreshCloneOffline:
    """The suite passes with no .env present and the machine offline.

    (upstream no-live-network convention, SM-5, NFR-2.2)
    """

    def test_the_network_is_unreachable_by_default_in_this_suite(self):
        """Every test starts with the network guarded, not merely unused."""
        guarded = plaid_balance.urllib.request.urlopen
        assert isinstance(guarded, mock.MagicMock), "the seam is not patched"
        with pytest.raises(AssertionError, match=NO_NETWORK):
            guarded("https://example.invalid")

    def test_no_plaid_variable_is_set_and_no_env_file_is_found(self):
        for name in plaid_balance.REQUIRED_VARS:
            assert not os.environ.get(name)
        assert plaid_balance.read_env_file(os.environ[plaid_balance.ENV_FILE_VAR]) == {}

    def test_the_skill_reports_cleanly_rather_than_crashing(self, capsys):
        exit_code = plaid_balance.run([])
        document, raw_out, raw_err = emitted(capsys)

        assert exit_code == 1
        assert document["error_code"] == "CONFIG_MISSING"
        assert "Traceback" not in raw_out + raw_err

    def test_the_suite_declares_no_third_party_import_beyond_pytest(self):
        import ast

        source = Path(__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert imported - {"pytest"} <= set(sys.stdlib_module_names)

    def test_the_suite_follows_the_affirmed_test_convention(self):
        """NFR-2.4: this path, pytest, and unittest.mock from the stdlib."""
        here = Path(__file__).resolve()
        assert here.parent.name == "skills"
        assert here.parent.parent.name == "tests"
        assert re.fullmatch(r"test_[a-z0-9_]+_skill\.py", here.name)
        assert mock.__name__ == "unittest.mock"
        assert "from unittest import mock" in here.read_text(encoding="utf-8")


# ===========================================================================
# Case 8 - the pre-commit hook refuses a planted secret
# ===========================================================================


def _run_git(args, cwd, env):
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)


def _git_env(extra_path=None):
    env = dict(os.environ)
    # Keep the operator's global and system git config out of the temp repo.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_path is not None:
        env["PATH"] = extra_path
    return env


def _write_stub(directory, exit_code):
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "gitleaks"
    stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.fixture
def scanned_repo(tmp_path):
    """A throwaway git repo wired to this repository's committed hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    assert _run_git(["init", "-q"], repo, env).returncode == 0
    for key, value in (
        ("user.email", "test@example.invalid"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(HOOKS_DIR)),
    ):
        assert _run_git(["config", key, value], repo, env).returncode == 0
    return repo


class TestCase8PreCommitHookRefusesSecrets:
    """Committing a planted, obviously-fake scanner-triggering string is refused.

    (B-7, SC-3, R-2, NFR-1.5)

    Three checks, deliberately separated. The first two run everywhere, because
    what actually breaks in practice is the wiring - a hook that is not
    executable, a `core.hooksPath` nobody set, an exit code the script
    swallows. The third needs real gitleaks and is skipped without it, since
    detection is gitleaks' job rather than this repository's.
    """

    def test_the_hook_is_committed_executable_and_redacts(self):
        hook = HOOKS_DIR / "pre-commit"
        assert hook.is_file(), "the hook must live in the repository, not .git/hooks"
        assert os.access(str(hook), os.X_OK), "a non-executable hook is never run"

        body = hook.read_text(encoding="utf-8")
        assert "gitleaks protect --staged" in body
        # Without --redact, gitleaks prints the caught secret into the terminal
        # it is protecting, turning a blocked commit into a disclosure.
        assert "--redact" in body

    def test_a_scanner_hit_blocks_the_commit_and_a_clean_scan_does_not(
        self, scanned_repo, tmp_path
    ):
        """Wiring test: the hook is found, runs, and propagates the exit code."""
        (scanned_repo / "note.txt").write_text("anything at all\n", encoding="utf-8")

        failing_bin = tmp_path / "bin-fail"
        _write_stub(failing_bin, 1)
        env = _git_env(str(failing_bin) + os.pathsep + os.environ.get("PATH", ""))
        assert _run_git(["add", "note.txt"], scanned_repo, env).returncode == 0
        blocked = _run_git(["commit", "-m", "should be refused"], scanned_repo, env)
        assert blocked.returncode != 0, "the hook let a flagged commit through"

        passing_bin = tmp_path / "bin-pass"
        _write_stub(passing_bin, 0)
        env = _git_env(str(passing_bin) + os.pathsep + os.environ.get("PATH", ""))
        allowed = _run_git(["commit", "-m", "should be allowed"], scanned_repo, env)
        assert allowed.returncode == 0, (
            "the hook blocks every commit, which would pass the check above for "
            "the wrong reason: " + allowed.stderr
        )

    def test_the_hook_fails_closed_when_the_scanner_is_absent(self, scanned_repo, tmp_path):
        empty_bin = tmp_path / "bin-empty"
        empty_bin.mkdir()
        git_binary = shutil.which("git")
        assert git_binary is not None
        restricted = str(empty_bin) + os.pathsep + os.path.dirname(git_binary)
        if shutil.which("gitleaks", path=restricted) is not None:
            pytest.skip("gitleaks shares a directory with git on this machine")

        (scanned_repo / "note.txt").write_text("anything at all\n", encoding="utf-8")
        env = _git_env(restricted)
        assert _run_git(["add", "note.txt"], scanned_repo, env).returncode == 0
        result = _run_git(["commit", "-m", "no scanner available"], scanned_repo, env)
        assert result.returncode != 0, "commits proceeded with no scanner installed"

    def test_real_gitleaks_refuses_a_planted_credential_shaped_string(self, scanned_repo):
        if shutil.which("gitleaks") is None:
            pytest.skip("gitleaks is not installed on this machine")

        planted = scanned_repo / "leak.txt"
        planted.write_text(
            "PLAID_ACCESS_TOKEN=" + SCANNER_TRIGGERING_FAKE_KEY + "\n", encoding="utf-8"
        )
        env = _git_env()
        assert _run_git(["add", "leak.txt"], scanned_repo, env).returncode == 0
        result = _run_git(["commit", "-m", "planted secret"], scanned_repo, env)

        assert result.returncode != 0, "gitleaks did not stop a planted secret"
        combined = result.stdout + result.stderr
        assert SCANNER_TRIGGERING_FAKE_KEY not in combined, (
            "the hook printed the secret it caught; --redact is not in effect"
        )


# ===========================================================================
# The truncation invariant and the frontmatter schema
# ===========================================================================
#
# FR-2.1, FR-2.2, FR-2.5. The skill index truncates `description` at 57
# characters, so this is the mechanism by which the skill gets selected at all.


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _scalar(raw):
    text = raw.strip()
    if text in ("", "~", "null"):
        return None
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _parse_block(lines, index, indent):
    """Parse the subset of YAML this frontmatter uses. Returns (value, index)."""
    if lines[index].lstrip().startswith("- "):
        items = []
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if _indent_of(line) < indent or not line.lstrip().startswith("- "):
                break
            items.append(_scalar(line.lstrip()[2:]))
            index += 1
        return items, index

    mapping = {}
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if _indent_of(line) < indent:
            break
        key, _, rest = line.strip().partition(":")
        index += 1
        if rest.strip():
            mapping[key.strip()] = _scalar(rest)
            continue
        lookahead = index
        while lookahead < len(lines) and not lines[lookahead].strip():
            lookahead += 1
        if lookahead >= len(lines) or _indent_of(lines[lookahead]) <= indent:
            mapping[key.strip()] = None
            index = lookahead
        else:
            child, index = _parse_block(lines, lookahead, _indent_of(lines[lookahead]))
            mapping[key.strip()] = child
    return mapping, index


def parse_frontmatter(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", "the document must open with ---"
    closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    block = lines[1:closing]
    value, _ = _parse_block(block, 0, _indent_of(block[0]))
    return value


@pytest.fixture(scope="module")
def frontmatter():
    return parse_frontmatter(SKILL_DOC)


# The words a person would actually say when asking this question. The budget is
# 57 characters, and vendor vocabulary competes for it.
TRIGGER_KEYWORDS = ("balance", "bank", "account", "money")
TRUNCATION_POINT = 57
DESCRIPTION_LIMIT = 60
MARKETING_WORDS = (
    "powerful",
    "seamless",
    "effortless",
    "blazing",
    "amazing",
    "simply",
    "easily",
    "best-in-class",
    "revolutionary",
)


class TestTruncationInvariant:
    def test_description_is_within_the_length_limit(self, frontmatter):
        description = frontmatter["description"]
        assert len(description) <= DESCRIPTION_LIMIT, (
            f"description is {len(description)} characters"
        )

    def test_description_is_one_sentence_ending_in_a_period(self, frontmatter):
        description = frontmatter["description"]
        assert description.endswith(".")
        assert description.count(".") == 1, "more than one sentence"
        assert "!" not in description
        assert "?" not in description
        assert ";" not in description

    @pytest.mark.parametrize("keyword", TRIGGER_KEYWORDS)
    def test_trigger_keyword_survives_truncation(self, frontmatter, keyword):
        visible = frontmatter["description"][:TRUNCATION_POINT].lower()
        assert keyword in visible, (
            f"'{keyword}' falls outside the first {TRUNCATION_POINT} characters, where the index "
            "truncates"
        )

    def test_description_carries_no_marketing_language(self, frontmatter):
        lowered = frontmatter["description"].lower()
        for word in MARKETING_WORDS:
            assert word not in lowered


class TestFrontmatterSchema:
    @pytest.mark.parametrize(
        "key", ["name", "description", "version", "author", "license", "platforms"]
    )
    def test_required_top_level_key_is_present(self, frontmatter, key):
        assert frontmatter.get(key), "frontmatter is missing " + key

    def test_hermes_metadata_is_present(self, frontmatter):
        hermes = frontmatter["metadata"]["hermes"]
        assert "tags" in hermes
        assert "related_skills" in hermes
        assert isinstance(hermes["tags"], list) and hermes["tags"]
        assert isinstance(hermes["related_skills"], list)

    def test_platforms_is_a_list(self, frontmatter):
        assert isinstance(frontmatter["platforms"], list)
        assert frontmatter["platforms"]

    def test_document_version_matches_the_script(self, frontmatter):
        assert frontmatter["version"] == plaid_balance.__version__


# ===========================================================================
# The output boundary, asserted directly
# ===========================================================================


class TestOutputAllowlist:
    """NFR-1.1 and NFR-1.2: one way out, five fields, and nothing else."""

    def test_the_allowlist_is_exactly_the_five_agreed_fields(self):
        assert set(plaid_balance.ACCOUNT_FIELD_ALLOWLIST) == {
            "balances",
            "account_name",
            "mask",
            "account_type",
            "institution_name",
        }

    def test_projection_copies_in_by_name_and_drops_everything_else(self):
        payload = load_fixture("balance_get_single_account.json")
        payload["accounts"][0]["surprise_field"] = "should not survive"
        projected = plaid_balance.project_accounts(payload, "Placeholder Bank")
        assert set(projected[0]) == set(plaid_balance.ACCOUNT_FIELD_ALLOWLIST)

    def test_emit_refuses_an_unlisted_field(self):
        document = {
            "status": "ok",
            "accounts": [
                {
                    "account_name": "x",
                    "account_type": "y",
                    "institution_name": "z",
                    "mask": "0000",
                    "balances": {"available": 1, "current": 1, "currency": "USD"},
                    "account_id": "leaked",
                }
            ],
        }
        stream = io.StringIO()
        with pytest.raises(plaid_balance.OutputBlocked) as caught:
            plaid_balance.emit(document, stream=stream)
        assert caught.value.offenders == ["account_id"]
        assert stream.getvalue() == "", "emit wrote before it checked"

    def test_emit_refuses_a_registered_credential_in_a_listed_field(self):
        plaid_balance.register_secret(SENTINEL_TOKEN)
        document = {
            "status": "ok",
            "accounts": [
                {
                    "account_name": SENTINEL_TOKEN,
                    "account_type": "y",
                    "institution_name": "z",
                    "mask": "0000",
                    "balances": {"available": 1, "current": 1, "currency": "USD"},
                }
            ],
        }
        stream = io.StringIO()
        with pytest.raises(plaid_balance.OutputBlocked):
            plaid_balance.emit(document, stream=stream)
        assert stream.getvalue() == ""

    def test_a_blocked_document_never_repeats_the_value(self):
        plaid_balance.register_secret(SENTINEL_TOKEN)
        blocked = plaid_balance.OutputBlocked(
            "output text contained a credential value", ["<credential x1>"]
        )
        rendered = json.dumps(plaid_balance._blocked_document(blocked))
        assert SENTINEL_TOKEN not in rendered

    def test_an_unrecognised_status_is_refused(self):
        with pytest.raises(plaid_balance.OutputBlocked):
            plaid_balance.emit({"status": "surprise"}, stream=io.StringIO())

    def test_loading_configuration_arms_the_backstop(self, configured):
        """All three credentials are registered, so all three are unprintable."""
        values = configured()
        plaid_balance.load_config()
        registered = plaid_balance.registered_secrets()
        for name in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ACCESS_TOKEN"):
            assert values[name] in registered, name + " was never registered"
        # PLAID_ENV is not a credential and must not be treated as one: it would
        # match the word "sandbox" wherever it appeared.
        assert values["PLAID_ENV"] not in registered


# ===========================================================================
# Static properties of the helper script
# ===========================================================================


@pytest.fixture(scope="module")
def tree():
    import ast

    return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))


class TestScriptShape:
    """FR-1.4, FR-2.4, NFR-3.1 - verifiable by reading the source, so read it."""

    def test_only_standard_library_modules_are_imported(self, tree):
        import ast

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), (
            "third-party import in the helper script: "
            + ", ".join(sorted(imported - set(sys.stdlib_module_names)))
        )
        assert "requests" not in imported
        assert "plaid" not in imported

    @pytest.mark.parametrize(
        "module", ["time", "sched", "threading", "asyncio", "logging", "subprocess"]
    )
    def test_no_scheduling_or_logging_module_is_imported(self, tree, module):
        import ast

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != module for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != module

    def test_there_is_no_loop_that_could_become_a_retry(self, tree):
        import ast

        assert not [node for node in ast.walk(tree) if isinstance(node, ast.While)], (
            "a while loop in the request path is how an accidental retry gets in"
        )

    def test_only_read_endpoints_are_referenced(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        endpoints = set(re.findall(r'"(/[a-z_]+/[a-z_/]+)"', source))
        assert endpoints == {"/accounts/balance/get", "/institutions/get_by_id"}
        for write_ish in ("/transfer", "/payment", "/item/remove", "/sandbox/"):
            assert write_ish not in source

    def test_only_one_function_writes_to_a_stream(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "print(" not in source, "output must go through emit(), not print()"
        assert source.count(".write(") == 1, (
            "more than one write path; the allowlist stops being the only way out"
        )


# ===========================================================================
# Repository hygiene
# ===========================================================================

# Assembled rather than written, so this file does not fail its own check.
MACHINE_LOCAL_PATTERNS = (
    "/Us" + "ers/",
    "/ho" + "me/",
    "C:" + chr(92) + "Users",
)

OWNED_PATHS = (
    "README.md",
    ".env.example",
    "pyproject.toml",
    ".gitignore",
    ".githooks",
    "skill",
    "tests",
)


def _owned_files():
    for entry in OWNED_PATHS:
        target = REPO_ROOT / entry
        if target.is_file():
            yield target
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    yield path


class TestRepositoryHygiene:
    def test_no_committed_file_carries_a_machine_local_path(self):
        offenders = []
        for path in _owned_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in MACHINE_LOCAL_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.name}: {pattern}")
        assert not offenders, "machine-local paths found: " + ", ".join(offenders)

    def test_the_workflow_record_is_ignored(self):
        """NFR-1.6: aidlc/ is 828 KB of working material, not part of the skill."""
        if shutil.which("git") is None:
            pytest.skip("git is not available")
        result = _run_git(["check-ignore", "-q", "aidlc"], REPO_ROOT, _git_env())
        assert result.returncode == 0, "aidlc/ is not gitignored"

    def test_environment_variable_names_agree_everywhere(self):
        """FR-3.4: one definition, one spelling, no drift."""
        pattern = re.compile(r"PLAID_[A-Z0-9_]+")
        script_source = SCRIPT_PATH.read_text(encoding="utf-8")

        defined = set(pattern.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
        assert defined, ".env.example defines no variables"

        # Some PLAID_-prefixed tokens in the documents are error codes rather
        # than variables. Derive them from the script instead of listing them
        # here, so a new code does not need this test edited and a genuine typo
        # in a variable name still fails.
        error_codes = set(re.findall(r'SkillError\(\s*"([A-Z_]+)"', script_source))
        assert "PLAID_ERROR" in error_codes, "error-code derivation stopped working"

        from_script = set(plaid_balance.REQUIRED_VARS) | {plaid_balance.ENV_FILE_VAR}
        assert from_script <= defined, "script uses a name .env.example never defines"

        for document in (SKILL_DOC, SETUP_RUNBOOK, README):
            found = set(pattern.findall(document.read_text(encoding="utf-8")))
            stray = found - defined - error_codes
            assert not stray, f"{document.name} uses an undefined name: {sorted(stray)}"

    def test_the_runbook_exists_once_and_is_linked_from_both_documents(self):
        """FR-6.2: one file under references/, linked rather than duplicated."""
        references = REPO_ROOT / "skill" / "references"
        assert sorted(p.name for p in references.glob("*.md")) == ["setup.md"]
        assert "references/setup.md" in SKILL_DOC.read_text(encoding="utf-8")
        assert "setup.md" in README.read_text(encoding="utf-8")

    def test_the_runbook_documents_keychain_hardening(self):
        """FR-3.3: documented as an option, and named as not implemented."""
        text = SETUP_RUNBOOK.read_text(encoding="utf-8").lower()
        assert "keychain" in text
        assert "not implemented" in text

    @pytest.mark.parametrize("document", ["SKILL.md", "references/setup.md"])
    def test_commands_are_framed_for_the_tool_not_written_as_shell_prose(self, document):
        """FR-2.6: terminal(command="…"), never a bare shell fence."""
        text = (REPO_ROOT / "skill" / document).read_text(encoding="utf-8")
        for fence in ("```bash", "```sh", "```shell", "```console", "```zsh"):
            assert fence not in text, document + " contains a bare " + fence + " block"
        assert 'terminal(command="' in text

    def test_no_realistic_credential_placeholder_is_used(self):
        """SC-2: placeholders must be obviously fake, not merely fake."""
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in plaid_balance.REQUIRED_VARS:
            match = re.search("^" + name + r"=(.*)$", text, re.MULTILINE)
            assert match, name + " is not defined in .env.example"
            value = match.group(1).strip()
            assert value, name + " has no placeholder value"
            if name == "PLAID_ENV":
                assert value in plaid_balance.PLAID_HOSTS
                continue
            assert value.startswith("your-") and value.endswith("-here"), (
                name + " placeholder does not read as obviously fake: " + value
            )

    def test_the_pre_push_hook_runs_the_suite(self):
        """NFR-2.3: the suite has a mechanical home, since there is no CI."""
        hook = HOOKS_DIR / "pre-push"
        assert hook.is_file()
        assert os.access(str(hook), os.X_OK)
        assert "pytest" in hook.read_text(encoding="utf-8")


# --- Regression: argparse must not echo its input (reviewer Major finding) ---


def test_unrecognised_argument_is_not_echoed_to_any_stream(capsys):
    """argparse's default error() wrote the offending argument verbatim to
    stderr, bypassing emit()'s allowlist. That path runs before load_config(),
    so the credential rescan has nothing registered to scan for. The fix drops
    the message entirely rather than sanitising it."""
    sentinel = "SENTINEL" + "LEAK" + "9137"
    with pytest.raises(SystemExit) as exc:
        plaid_balance.run(["--no-such-flag=" + sentinel])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert captured.err == ""
    document = json.loads(captured.out)
    assert document["error_code"] == "BAD_ARGUMENTS"
