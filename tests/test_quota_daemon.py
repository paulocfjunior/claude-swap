"""Tests for the reliable quota daemon (``cswap-quotad``) and its serial-fetch
support in the switcher.

Network is never touched: ``_fetch_account_usage`` is stubbed so tests assert the
orchestration (serial ordering, spacing, last-known-good preservation, cache
writes) and the daemon's config normalization / cycle logic in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import quota_daemon
from claude_swap import switcher as switcher_mod
from claude_swap.cache import read_cache
from claude_swap.switcher import ClaudeAccountSwitcher


def _acct(num: int, email: str = "a@example.com", is_active: bool = False):
    """Build a minimal accounts_info tuple (num, email, org, uuid, active, creds)."""
    return (num, email, "", "", is_active, "creds")


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
    assert result == [
        {"five_hour": {"pct": 1.0}},
        {"five_hour": {"pct": 2.0}},
        {"five_hour": {"pct": 3.0}},
    ]
    # Spaced BETWEEN calls only: n-1 sleeps, never before the first or after last.
    assert slept == [7, 7]

    # Wrote the native-format cache keyed by account number.
    cached = read_cache(sw.backup_dir / "cache" / "usage.json", float("inf"))
    assert cached == {
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

    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"1": {"seven_day": {"pct": 64.0}}, "2": {"seven_day": {"pct": 10.0}}},
    )

    # This sweep: account 1 fails (None), account 2 succeeds fresh.
    def fake_fetch(info):
        return None if info[0] == 1 else {"seven_day": {"pct": 11.0}}

    monkeypatch.setattr(sw, "_fetch_account_usage", fake_fetch)
    result = sw.collect_usage_serial(
        [_acct(1), _acct(2)], preserve_last_good=True
    )

    # Account 1 keeps its last-known-good; account 2 takes the fresh value.
    assert result[0] == {"seven_day": {"pct": 64.0}}
    assert result[1] == {"seven_day": {"pct": 11.0}}
    cached = read_cache(sw.backup_dir / "cache" / "usage.json", float("inf"))
    assert cached["1"] == {"seven_day": {"pct": 64.0}}
    assert cached["2"] == {"seven_day": {"pct": 11.0}}


def test_preserve_last_good_off_by_default_writes_none(temp_home, monkeypatch):
    sw = ClaudeAccountSwitcher()
    from claude_swap.cache import write_cache

    write_cache(sw.backup_dir / "cache" / "usage.json", {"1": {"seven_day": {"pct": 64.0}}})
    monkeypatch.setattr(sw, "_fetch_account_usage", lambda info: None)
    result = sw.collect_usage_serial([_acct(1)])  # preserve_last_good defaults False
    assert result == [None]


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
    def __init__(self, roster):
        self._roster = roster
        self.calls = []

    def _build_accounts_info(self):
        return self._roster

    def collect_usage_serial(self, accounts_info, spacing_seconds, preserve_last_good):
        self.calls.append((accounts_info, spacing_seconds, preserve_last_good))
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
