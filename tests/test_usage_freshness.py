"""``usageFetchedAt`` / ``usageAgeSeconds`` on ``--list --json`` rows.

The fields match upstream claude-swap's ``usage_freshness_fields`` (v0.21.0):
ISO 8601 UTC with a ``Z`` suffix at seconds precision, and the age as a float
rounded to one decimal. The age must reflect when the reading was really
fetched, through the daemon's carried-forward last-good value and the list
cache, never the time the list was printed.

Network is never touched: ``oauth.fetch_usage_for_account`` is patched and the
clock is a controllable fake.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.cache import write_cache
from claude_swap.json_output import (
    USAGE_FETCHED_AT_KEY,
    account_row,
    usage_freshness_fields,
)
from claude_swap.switcher import ClaudeAccountSwitcher

# 2026-01-01T00:00:00Z
T0 = 1767225600.0
T0_ISO = "2026-01-01T00:00:00Z"

USAGE = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}
CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-test"}})


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock(T0)
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def switcher(temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, monkeypatch):
    """Two managed accounts (1 active as test@example.com, 2), both with creds."""
    monkeypatch.delenv("CSWAP_USAGE_CACHE_TTL", raising=False)
    sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(sw.sequence_file, sample_sequence_data)
    with patch.object(sw, "_read_credentials", return_value=CREDS), \
         patch.object(sw, "_read_account_credentials", return_value=CREDS):
        yield sw


def _rows(sw: ClaudeAccountSwitcher) -> dict[int, dict]:
    payload = sw.list_accounts(json_output=True)
    assert payload["schemaVersion"] == 1
    return {a["number"]: a for a in payload["accounts"]}


# --------------------------------------------------------------------------- #
# Serialization helper (same shape as upstream)
# --------------------------------------------------------------------------- #
class TestFreshnessFields:
    def test_iso_utc_z_and_age_rounded_to_one_decimal(self):
        assert usage_freshness_fields(T0, 12.34) == {
            "usageFetchedAt": T0_ISO,
            "usageAgeSeconds": 12.3,
        }

    def test_subsecond_fetch_time_is_truncated_to_seconds(self):
        assert usage_freshness_fields(T0 + 0.9, 0.0)["usageFetchedAt"] == T0_ISO

    def test_no_fetch_time_means_no_fields(self):
        assert usage_freshness_fields(None, None) == {}
        assert usage_freshness_fields(None, 5.0) == {}

    def test_fetch_time_without_age_emits_only_fetched_at(self):
        assert usage_freshness_fields(T0, None) == {"usageFetchedAt": T0_ISO}

    def test_account_row_with_usage_carries_fields(self):
        row = account_row(
            1, "a@example.com", "", "", True, USAGE,
            usage_fetched_at=T0, usage_age_s=61.26,
        )
        assert row["usageStatus"] == "ok"
        assert row["usageFetchedAt"] == T0_ISO
        assert row["usageAgeSeconds"] == 61.3

    @pytest.mark.parametrize("entry", [None, "no credentials", "token expired", "api key"])
    def test_account_row_without_usage_has_no_fields(self, entry):
        row = account_row(
            1, "a@example.com", "", "", True, entry,
            usage_fetched_at=T0, usage_age_s=1.0,
        )
        assert row["usage"] is None
        assert "usageFetchedAt" not in row
        assert "usageAgeSeconds" not in row

    def test_internal_stamp_never_leaks_into_usage_object(self):
        row = account_row(
            1, "a@example.com", "", "", True, {**USAGE, USAGE_FETCHED_AT_KEY: T0},
            usage_fetched_at=T0, usage_age_s=0.0,
        )
        assert row["usage"] == {"fiveHour": {"pct": 10.0}, "sevenDay": {"pct": 20.0}}


# --------------------------------------------------------------------------- #
# --list --json end to end, through the real fetch, cache and carry-forward
# --------------------------------------------------------------------------- #
class TestListFreshness:
    def test_fresh_reading(self, switcher, clock):
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=dict(USAGE)):
            rows = _rows(switcher)
        for num in (1, 2):
            assert rows[num]["usageStatus"] == "ok"
            assert rows[num]["usageFetchedAt"] == T0_ISO
            assert rows[num]["usageAgeSeconds"] == 0.0
            assert rows[num]["usage"] == {"fiveHour": {"pct": 10.0}, "sevenDay": {"pct": 20.0}}

    def test_cached_list_ages_from_the_fetch_not_the_print(self, switcher, clock):
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=dict(USAGE)):
            _rows(switcher)
        clock.now = T0 + 120.0
        # Within the 300 s list-cache TTL: served from cache, no fetch at all.
        with patch("claude_swap.oauth.fetch_usage_for_account",
                   side_effect=AssertionError("cache should have been served")):
            rows = _rows(switcher)
        assert rows[1]["usageFetchedAt"] == T0_ISO
        assert rows[1]["usageAgeSeconds"] == 120.0
        assert rows[2]["usageAgeSeconds"] == 120.0

    def test_carried_forward_reading_ages_from_the_original_fetch(self, switcher, clock):
        accounts_info = switcher._build_accounts_info()
        # Daemon sweep at T0: both accounts fetch fine.
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=dict(USAGE)):
            switcher.collect_usage_serial(accounts_info, preserve_last_good=True)
        # Daemon sweep at T0+600: account 2 fails, its T0 reading is carried forward.
        clock.now = T0 + 600.0
        fresh = {"five_hour": {"pct": 30.0}}

        def fetch(num, *_args, **_kwargs):
            return None if num == "2" else dict(fresh)

        with patch("claude_swap.oauth.fetch_usage_for_account", side_effect=fetch):
            switcher.collect_usage_serial(accounts_info, preserve_last_good=True)
        # List at T0+700 is served from the daemon's cache (written at T0+600).
        clock.now = T0 + 700.0
        with patch("claude_swap.oauth.fetch_usage_for_account",
                   side_effect=AssertionError("cache should have been served")):
            rows = _rows(switcher)
        assert rows[1]["usageFetchedAt"] == "2026-01-01T00:10:00Z"
        assert rows[1]["usageAgeSeconds"] == 100.0
        # The carried-forward row keeps its ORIGINAL fetch time, not the sweep's.
        assert rows[2]["usage"] == {"fiveHour": {"pct": 10.0}, "sevenDay": {"pct": 20.0}}
        assert rows[2]["usageFetchedAt"] == T0_ISO
        assert rows[2]["usageAgeSeconds"] == 700.0

    def test_parallel_reading_carried_forward_expires_to_unknown(self, switcher, clock):
        """A reading the parallel ``--list`` path fetched is now stamped, so the
        daemon's preserve limit applies to it: once older than the limit it is
        reported as unknown, with no freshness fields, instead of republished."""
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=dict(USAGE)):
            _rows(switcher)  # parallel path, cache stamped at T0
        accounts_info = switcher._build_accounts_info()
        clock.now = T0 + 1000.0  # past the 900 s preserve limit
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.collect_usage_serial(accounts_info, preserve_last_good=True)
        with patch("claude_swap.oauth.fetch_usage_for_account",
                   side_effect=AssertionError("cache should have been served")):
            rows = _rows(switcher)
        for row in rows.values():
            assert row["usageStatus"] == "unavailable"
            assert row["usage"] is None
            assert "usageFetchedAt" not in row
            assert "usageAgeSeconds" not in row

    def test_rows_without_usage_have_no_fields(self, switcher, clock):
        def fetch(num, *_args, **_kwargs):
            return None  # every fetch fails

        with patch("claude_swap.oauth.fetch_usage_for_account", side_effect=fetch), \
             patch.object(switcher, "_read_account_credentials", return_value=""):
            rows = _rows(switcher)
        assert rows[1]["usageStatus"] == "unavailable"
        assert rows[2]["usageStatus"] == "no_credentials"
        for row in rows.values():
            assert "usageFetchedAt" not in row
            assert "usageAgeSeconds" not in row

    def test_unstamped_cache_entry_reports_unknown_not_a_guess(self, switcher, clock):
        """A cache written before this change has no per-reading fetch time. The
        file's own timestamp is not used: a carried-forward entry can be far older
        than the file, so the fields are left out (unknown) instead of guessed."""
        write_cache(switcher.backup_dir / "cache" / "usage.json",
                    {"1": dict(USAGE), "2": dict(USAGE)})
        clock.now = T0 + 30.0
        with patch("claude_swap.oauth.fetch_usage_for_account",
                   side_effect=AssertionError("cache should have been served")):
            rows = _rows(switcher)
        assert rows[1]["usageStatus"] == "ok"
        assert "usageFetchedAt" not in rows[1]
        assert "usageAgeSeconds" not in rows[1]

    @pytest.mark.parametrize("bad", ["2026-01-01T00:00:00Z", True, None, [1]])
    def test_malformed_stamp_reports_unknown(self, switcher, clock, bad):
        write_cache(switcher.backup_dir / "cache" / "usage.json",
                    {"1": {**USAGE, USAGE_FETCHED_AT_KEY: bad}, "2": dict(USAGE)})
        with patch("claude_swap.oauth.fetch_usage_for_account",
                   side_effect=AssertionError("cache should have been served")):
            rows = _rows(switcher)
        assert rows[1]["usageStatus"] == "ok"
        assert "usageFetchedAt" not in rows[1]

    def test_status_path_cache_write_is_stamped(self, switcher, clock):
        """``--status`` refreshes the active entry in the same cache ``--list``
        reads; that entry must carry its fetch time too."""
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=dict(USAGE)):
            entry = switcher._active_account_usage("1", "test@example.com")
        assert entry[USAGE_FETCHED_AT_KEY] == T0
        cached = json.loads(
            (switcher.backup_dir / "cache" / "usage.json").read_text()
        )["data"]
        assert cached["1"][USAGE_FETCHED_AT_KEY] == T0
