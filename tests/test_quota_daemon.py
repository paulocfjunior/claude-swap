"""Tests for the reliable quota daemon (``cswap-quotad``) and its serial-fetch
support in the switcher.

Network is never touched: ``_fetch_account_usage`` is stubbed so tests assert the
orchestration (serial ordering, spacing, last-known-good preservation, cache
writes) and the daemon's config normalization / cycle logic in isolation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_swap import quota_daemon
from claude_swap import switcher as switcher_mod
from claude_swap.cache import read_cache
from claude_swap.json_output import (
    USAGE_FETCHED_AT_KEY,
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.switcher import ClaudeAccountSwitcher, usage_entry_age


def _acct(num: int, email: str = "a@example.com", is_active: bool = False):
    """Build a minimal accounts_info tuple (num, email, org, uuid, active, creds)."""
    return (num, email, "", "", is_active, "creds")


def _no_stamp(entry):
    """A usage entry without its ``fetched_at`` bookkeeping key, for comparison."""
    return {k: v for k, v in entry.items() if k != USAGE_FETCHED_AT_KEY}


# --------------------------------------------------------------------------- #
# _usage_cache_ttl (env-configurable TTL)
# --------------------------------------------------------------------------- #

def test_usage_cache_ttl_default_when_unset(monkeypatch):
    monkeypatch.delenv("CSWAP_USAGE_CACHE_TTL", raising=False)
    assert switcher_mod._usage_cache_ttl() == switcher_mod._DEFAULT_USAGE_CACHE_TTL


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("42", 42.0),
        ("0", 0.0),
        ("  90 ", 90.0),
        ("", switcher_mod._DEFAULT_USAGE_CACHE_TTL),
        ("notanumber", switcher_mod._DEFAULT_USAGE_CACHE_TTL),
        ("-5", switcher_mod._DEFAULT_USAGE_CACHE_TTL),
    ],
)
def test_usage_cache_ttl_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv("CSWAP_USAGE_CACHE_TTL", raw)
    assert switcher_mod._usage_cache_ttl() == expected


# --------------------------------------------------------------------------- #
# collect_usage_serial (serial ordering, spacing, cache write, preservation)
# --------------------------------------------------------------------------- #

def test_collect_usage_serial_is_serial_and_spaced(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    calls: list[int] = []

    def fake_fetch(info):
        calls.append(info[0])
        return {"five_hour": {"pct": float(info[0])}}

    monkeypatch.setattr(sw, "_fetch_account_usage", fake_fetch)

    slept: list[float] = []
    accounts = [_acct(1), _acct(2), _acct(3)]
    result = sw.collect_usage_serial(
        accounts, spacing_seconds=7, sleep_fn=slept.append
    )

    # Fetched every account, in roster order.
    assert calls == [1, 2, 3]
    assert [_no_stamp(r) for r in result] == [
        {"five_hour": {"pct": 1.0}},
        {"five_hour": {"pct": 2.0}},
        {"five_hour": {"pct": 3.0}},
    ]
    # Every fresh entry carries a fetch stamp, so a later preserved copy can be
    # told apart from a current one.
    assert all(usage_entry_age(r) is not None and usage_entry_age(r) < 60 for r in result)
    # Spaced BETWEEN calls only: n-1 sleeps, never before the first or after last.
    assert slept == [7, 7]

    # Wrote the native-format cache keyed by account number.
    cached = read_cache(sw.backup_dir / "cache" / "usage.json", float("inf"))
    assert {k: _no_stamp(v) for k, v in cached.items()} == {
        "1": {"five_hour": {"pct": 1.0}},
        "2": {"five_hour": {"pct": 2.0}},
        "3": {"five_hour": {"pct": 3.0}},
    }


def test_collect_usage_serial_no_spacing_no_sleep(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)
    slept: list[float] = []
    sw.collect_usage_serial([_acct(1), _acct(2)], spacing_seconds=0, sleep_fn=slept.append)
    assert slept == []


def test_preserve_last_good_carries_forward_on_failure(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    # Seed a prior cache: account 1 had a good value last cycle.
    from claude_swap.cache import write_cache

    # Inside _MAX_PRESERVE_SECONDS: old enough that its age is clearly visible,
    # recent enough that preservation still applies (the bound is covered by
    # test_preservation_stops_once_the_reading_is_too_old).
    stale_stamp = time.time() - 120
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {
            "1": {"seven_day": {"pct": 64.0}, USAGE_FETCHED_AT_KEY: stale_stamp},
            "2": {"seven_day": {"pct": 10.0}, USAGE_FETCHED_AT_KEY: stale_stamp},
        },
    )

    # This sweep: account 1 fails (None), account 2 succeeds fresh.
    def fake_fetch(info):
        return None if info[0] == 1 else {"seven_day": {"pct": 11.0}}

    monkeypatch.setattr(sw, "_fetch_account_usage", fake_fetch)
    result = sw.collect_usage_serial(
        [_acct(1), _acct(2)], preserve_last_good=True
    )

    # Account 1 keeps its last-known-good; account 2 takes the fresh value.
    assert _no_stamp(result[0]) == {"seven_day": {"pct": 64.0}}
    assert _no_stamp(result[1]) == {"seven_day": {"pct": 11.0}}
    cached = read_cache(sw.backup_dir / "cache" / "usage.json", float("inf"))
    assert _no_stamp(cached["1"]) == {"seven_day": {"pct": 64.0}}
    assert _no_stamp(cached["2"]) == {"seven_day": {"pct": 11.0}}

    # The preserved entry keeps its ORIGINAL stamp, so its true age stays visible
    # even though the cache file's own timestamp was just rewritten. Without this
    # a pinned value is indistinguishable from a current one forever.
    assert cached["1"][USAGE_FETCHED_AT_KEY] == stale_stamp
    assert usage_entry_age(cached["1"]) > 100
    assert usage_entry_age(cached["2"]) < 60


def test_preserve_last_good_off_by_default_writes_none(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    from claude_swap.cache import write_cache

    write_cache(sw.backup_dir / "cache" / "usage.json", {"1": {"seven_day": {"pct": 64.0}}})
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)
    result = sw.collect_usage_serial([_acct(1)])  # preserve_last_good defaults False
    assert result == [None]


def test_token_expired_does_not_blank_a_good_reading(temp_home, monkeypatch):
    """TOKEN_EXPIRED is transient, so it must not overwrite a real number.

    `is_oauth_token_expired` counts a token as expired for the 5 minutes BEFORE it
    lapses, so this sentinel appears around every token expiry while Claude Code is
    already refreshing. Measured 2026-08-14: it blanked the active account from
    17:48:43 to 17:53:43, blinded the watchdog for three sweeps, and paged Paulo
    about a condition that fixed itself.
    """
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"1": {"seven_day": {"pct": 94.0}, USAGE_FETCHED_AT_KEY: time.time() - 30}},
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: USAGE_TOKEN_EXPIRED)

    result = sw.collect_usage_serial([_acct(1, is_active=True)], preserve_last_good=True)

    assert _no_stamp(result[0]) == {"seven_day": {"pct": 94.0}}


def test_no_credentials_still_overwrites(temp_home, monkeypatch):
    """A definitive sentinel must still replace a stale value: it is settled news."""
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json", {"1": {"seven_day": {"pct": 94.0}}}
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: USAGE_NO_CREDENTIALS)

    result = sw.collect_usage_serial([_acct(1)], preserve_last_good=True)

    assert result == [USAGE_NO_CREDENTIALS]


def test_api_key_still_overwrites(temp_home, monkeypatch):
    from claude_swap.cache import write_cache
    from claude_swap.json_output import USAGE_API_KEY

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json", {"1": {"seven_day": {"pct": 94.0}}}
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: USAGE_API_KEY)

    assert sw.collect_usage_serial([_acct(1)], preserve_last_good=True) == [USAGE_API_KEY]


def test_token_expired_passes_through_when_there_is_nothing_to_keep(temp_home, monkeypatch):
    """Preserving is not inventing: with no prior reading the sentinel still shows."""
    sw = ClaudeAccountSwitcher()
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: USAGE_TOKEN_EXPIRED)

    assert sw.collect_usage_serial([_acct(1)], preserve_last_good=True) == [
        USAGE_TOKEN_EXPIRED
    ]


def test_preservation_stops_once_the_reading_is_too_old(temp_home, monkeypatch):
    """Carrying a value forward is a bet the next sweep succeeds; bound the bet.

    Unbounded, an account that never reads again republishes the last number it
    ever produced, forever, and the cache's own timestamp keeps it looking current.
    """
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"1": {"seven_day": {"pct": 64.0}, USAGE_FETCHED_AT_KEY: time.time() - 5000}},
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)

    result = sw.collect_usage_serial(
        [_acct(1)], preserve_last_good=True, max_preserve_seconds=900
    )

    assert result == [None]  # unknown is honest; the old number would not be


def test_a_reading_inside_the_bound_is_still_kept(temp_home, monkeypatch):
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"1": {"seven_day": {"pct": 64.0}, USAGE_FETCHED_AT_KEY: time.time() - 60}},
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)

    result = sw.collect_usage_serial(
        [_acct(1)], preserve_last_good=True, max_preserve_seconds=900
    )

    assert _no_stamp(result[0]) == {"seven_day": {"pct": 64.0}}


def test_the_bound_outlives_the_token_expiry_window(temp_home, monkeypatch):
    """The default must not blank a value during an ordinary 5-minute refresh."""
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"1": {"seven_day": {"pct": 64.0}, USAGE_FETCHED_AT_KEY: time.time() - 300}},
    )
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: USAGE_TOKEN_EXPIRED)

    result = sw.collect_usage_serial([_acct(1)], preserve_last_good=True)

    assert _no_stamp(result[0]) == {"seven_day": {"pct": 64.0}}
    assert switcher_mod._MAX_PRESERVE_SECONDS > 300  # longer than the expiry buffer


def test_an_undatable_entry_is_kept_rather_than_discarded(temp_home, monkeypatch):
    """No stamp means the parallel writer produced it, which is fresh when written."""
    from claude_swap.cache import write_cache

    sw = ClaudeAccountSwitcher()
    write_cache(sw.backup_dir / "cache" / "usage.json", {"1": {"seven_day": {"pct": 64.0}}})
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)

    result = sw.collect_usage_serial(
        [_acct(1)], preserve_last_good=True, max_preserve_seconds=1
    )

    assert _no_stamp(result[0]) == {"seven_day": {"pct": 64.0}}


def test_transient_classification():
    assert switcher_mod._is_transient_usage(None) is True
    assert switcher_mod._is_transient_usage(USAGE_TOKEN_EXPIRED) is True
    assert switcher_mod._is_transient_usage(USAGE_NO_CREDENTIALS) is False
    assert switcher_mod._is_transient_usage({"five_hour": {}}) is False


def test_preserve_does_not_resurrect_sentinel_or_missing_prior(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    from claude_swap.cache import write_cache

    # Prior for acct 1 was itself a failure (None) -> nothing good to preserve.
    write_cache(sw.backup_dir / "cache" / "usage.json", {"1": None})
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)
    result = sw.collect_usage_serial([_acct(1)], preserve_last_good=True)
    assert result == [None]


# --------------------------------------------------------------------------- #
# quota_daemon.load_config normalization
# --------------------------------------------------------------------------- #

def test_load_config_defaults_when_missing(tmp_path):
    cfg = quota_daemon.load_config(tmp_path / "nope.json")
    assert cfg == {
        "enabled": True,
        "spacing_seconds": 20,
        "cycle_seconds": 45,
        "stale_after_seconds": 300,
        "accounts": "all",
    }


def test_load_config_invalid_json_falls_back(tmp_path):
    p = tmp_path / "quota-daemon.json"
    p.write_text("{ this is not json", encoding="utf-8")
    cfg = quota_daemon.load_config(p)
    assert cfg["spacing_seconds"] == 20 and cfg["accounts"] == "all"


def test_load_config_clamps_and_coerces(tmp_path):
    p = tmp_path / "quota-daemon.json"
    p.write_text(
        json.dumps(
            {
                "enabled": 0,
                "spacing_seconds": -3,
                "cycle_seconds": 1,  # below _MIN_CYCLE_SECONDS
                "accounts": [1, "2", 3],
            }
        ),
        encoding="utf-8",
    )
    cfg = quota_daemon.load_config(p)
    assert cfg["enabled"] is False
    assert cfg["spacing_seconds"] == quota_daemon._MIN_SPACING_SECONDS
    assert cfg["cycle_seconds"] == quota_daemon._MIN_CYCLE_SECONDS
    assert cfg["accounts"] == {"1", "2", "3"}


def test_load_config_unknown_accounts_string_is_all(tmp_path):
    p = tmp_path / "quota-daemon.json"
    p.write_text(json.dumps({"accounts": "bogus"}), encoding="utf-8")
    assert quota_daemon.load_config(p)["accounts"] == "all"


def test_load_config_empty_accounts_list_is_all(tmp_path):
    p = tmp_path / "quota-daemon.json"
    p.write_text(json.dumps({"accounts": []}), encoding="utf-8")
    assert quota_daemon.load_config(p)["accounts"] == "all"


def test_ensure_config_file_creates_then_is_idempotent(tmp_path):
    p = tmp_path / "quota-daemon.json"
    quota_daemon.ensure_config_file(p)
    assert p.exists()
    assert json.loads(p.read_text()) == quota_daemon.DEFAULT_CONFIG
    # Editing then re-ensuring must NOT clobber the user's file.
    p.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    quota_daemon.ensure_config_file(p)
    assert json.loads(p.read_text()) == {"enabled": False}


# --------------------------------------------------------------------------- #
# _select_accounts + run_cycle
# --------------------------------------------------------------------------- #

def test_select_accounts_all_and_filter():
    roster = [_acct(1), _acct(2), _acct(3)]
    assert quota_daemon._select_accounts(roster, "all") == roster
    assert quota_daemon._select_accounts(roster, {"2"}) == [_acct(2)]
    assert quota_daemon._select_accounts(roster, {"9"}) == []


class _FakeSwitcher:
    def __init__(self, roster, usages=None, keychain_was_flipped=False):
        self._roster = roster
        self._usages = usages
        self._keychain_was_flipped = keychain_was_flipped
        self.calls = []
        self.resets = 0

    def reset_keychain_capability(self):
        self.resets += 1
        return self._keychain_was_flipped

    def _build_accounts_info(self):
        return self._roster

    def collect_usage_serial(self, accounts_info, spacing_seconds, preserve_last_good):
        self.calls.append((accounts_info, spacing_seconds, preserve_last_good))
        if self._usages is not None:
            return self._usages
        return [{"ok": True} for _ in accounts_info]


def test_run_cycle_selects_and_preserves():
    sw = _FakeSwitcher([_acct(1), _acct(2), _acct(3)])
    cfg = {"spacing_seconds": 20, "cycle_seconds": 45, "accounts": {"1", "3"}}
    usages = quota_daemon.run_cycle(sw, cfg)
    assert len(sw.calls) == 1
    selected, spacing, preserve = sw.calls[0]
    assert [i[0] for i in selected] == [1, 3]
    assert spacing == 20
    assert preserve is True
    assert len(usages) == 2


def test_run_cycle_empty_selection_skips_fetch():
    sw = _FakeSwitcher([_acct(1)])
    cfg = {"spacing_seconds": 20, "cycle_seconds": 45, "accounts": {"9"}}
    usages = quota_daemon.run_cycle(sw, cfg)
    assert usages == []
    assert sw.calls == []  # never called the network path


# --------------------------------------------------------------------------- #
# Keychain re-probe (a transient failure must not blind the daemon for its life)
# --------------------------------------------------------------------------- #

def _cfg(**over):
    base = {
        "spacing_seconds": 0,
        "cycle_seconds": 45,
        "stale_after_seconds": 300,
        "accounts": "all",
    }
    base.update(over)
    return base


def test_run_cycle_re_arms_keychain_probe_every_cycle():
    """The store's usability cache is sticky per process; a daemon must re-probe.

    Without this, one 5s `security` timeout leaves the ACTIVE credential read in
    file-fallback mode (where macOS has no file) for the life of the daemon.
    """
    sw = _FakeSwitcher([_acct(1)])
    quota_daemon.run_cycle(sw, _cfg())
    quota_daemon.run_cycle(sw, _cfg())
    assert sw.resets == 2


def test_run_cycle_reports_when_a_flip_was_actually_cleared(caplog):
    sw = _FakeSwitcher([_acct(1)], keychain_was_flipped=True)
    with caplog.at_level("WARNING"):
        quota_daemon.run_cycle(sw, _cfg())
    assert "Keychain was marked unusable" in caplog.text


def test_run_cycle_quiet_when_keychain_never_failed(caplog):
    sw = _FakeSwitcher([_acct(1)], keychain_was_flipped=False)
    with caplog.at_level("WARNING"):
        quota_daemon.run_cycle(sw, _cfg())
    assert "Keychain was marked unusable" not in caplog.text


# --------------------------------------------------------------------------- #
# Blindness detection + reporting for the guarded (ACTIVE) account
# --------------------------------------------------------------------------- #

def test_blind_reason_flags_missing_sentinel_and_stale():
    assert quota_daemon._blind_reason(None, 300) == "fetch returned no data"
    assert "no credentials" in quota_daemon._blind_reason(USAGE_NO_CREDENTIALS, 300)
    stale = {"five_hour": {"pct": 5.0}, USAGE_FETCHED_AT_KEY: time.time() - 900}
    assert "preserved reading" in quota_daemon._blind_reason(stale, 300)


def test_blind_reason_accepts_a_fresh_reading():
    fresh = {"five_hour": {"pct": 5.0}, USAGE_FETCHED_AT_KEY: time.time()}
    assert quota_daemon._blind_reason(fresh, 300) is None


def test_blind_reason_unstamped_entry_is_not_called_stale():
    """No stamp means the parallel writer produced it, not that it is old."""
    assert quota_daemon._blind_reason({"five_hour": {"pct": 5.0}}, 300) is None


def test_active_watch_counts_streak_then_reports_recovery(caplog):
    watch = quota_daemon.ActiveAccountWatch()
    with caplog.at_level("WARNING"):
        watch.observe(2, "fetch returned no data")
        watch.observe(2, "fetch returned no data")
    assert watch.blind_streak == 2
    assert "2 consecutive sweep(s)" in caplog.text

    caplog.clear()
    with caplog.at_level("INFO"):
        watch.observe(2, None)
    assert watch.blind_streak == 0
    assert "readable again after 2 blind sweep(s)" in caplog.text


def test_run_cycle_warns_when_the_guarded_account_is_invisible(caplog):
    """The exact production failure: idle accounts read fine, the active one does not."""
    roster = [_acct(1), _acct(2, is_active=True), _acct(3)]
    usages = [{"five_hour": {"pct": 1.0}}, USAGE_NO_CREDENTIALS, {"five_hour": {"pct": 3.0}}]
    sw = _FakeSwitcher(roster, usages=usages)
    watch = quota_daemon.ActiveAccountWatch()
    with caplog.at_level("WARNING"):
        quota_daemon.run_cycle(sw, _cfg(), watch)
    assert "BLIND: active account #2" in caplog.text
    assert watch.blind_streak == 1


def test_run_cycle_silent_when_active_reads_cleanly(caplog):
    roster = [_acct(1), _acct(2, is_active=True)]
    usages = [None, {"five_hour": {"pct": 3.0}, USAGE_FETCHED_AT_KEY: time.time()}]
    sw = _FakeSwitcher(roster, usages=usages)
    with caplog.at_level("WARNING"):
        quota_daemon.run_cycle(sw, _cfg(), quota_daemon.ActiveAccountWatch())
    assert "BLIND" not in caplog.text


def test_summarize_names_the_unreadable_accounts_and_marks_active():
    roster = [_acct(1), _acct(2, is_active=True), _acct(3)]
    usages = [{"five_hour": {}}, USAGE_NO_CREDENTIALS, None]
    line = quota_daemon._summarize(roster, usages)
    assert line.startswith("1 ok, 2 unavailable/sentinel")
    assert "#2 (ACTIVE)='no credentials'" in line
    assert "#3=no data" in line


def test_summarize_stays_terse_when_everything_read():
    roster = [_acct(1)]
    assert quota_daemon._summarize(roster, [{"five_hour": {}}]) == "1 ok, 0 unavailable/sentinel"


def test_load_config_clamps_stale_after_seconds(tmp_path):
    p = tmp_path / "quota-daemon.json"
    p.write_text(json.dumps({"stale_after_seconds": 1}), encoding="utf-8")
    assert quota_daemon.load_config(p)["stale_after_seconds"] == 30.0
