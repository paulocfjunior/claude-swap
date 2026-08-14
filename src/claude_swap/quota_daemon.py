"""Reliable quota daemon (``cswap-quotad``).

A system-persistent daemon that keeps every account's Claude usage (5h / 7d /
spend) fresh in ONE global cache file, ``<backup>/cache/usage.json``, so no other
process ever has to call the Anthropic usage API itself.

Why it exists: ``cswap --list`` fetches all accounts' usage *in parallel*, and
pollers (the CombusDesktop menubar, the TUI, ad-hoc CLI runs) each trigger that
burst independently. Concurrent bursts trip HTTP 429 and keep the endpoint rate
limited, so the shared cache stays empty. This daemon is instead the *single*
fetcher: it polls each account **serially**, spaced so it never 429s, and writes
the results in cswap's native cache format. With ``CSWAP_USAGE_CACHE_TTL`` raised
(default 300s), every ``cswap``/TUI/CombusDesktop read hits this daemon-written
cache and never fetches. If the daemon stops, the cache eventually goes stale and
cswap transparently falls back to its own fetch, so nothing breaks.

Configuration lives in ``<backup>/quota-daemon.json`` and is re-read at the top of
every cycle, so edits (spacing, cadence, which accounts, enable/disable) take
effect on the next cycle with **no restart** (hot-reload). Adding an account with
``cswap --add-account`` is picked up automatically, since the roster is re-read
from ``sequence.json`` each cycle too.

The daemon never switches accounts and never writes credentials beyond the
refresh-and-persist that cswap's own usage fetch already performs for inactive
accounts.

Two properties exist because a daemon is not a CLI run. First, the credential
store's Keychain-usability cache is *sticky per process* — correct for a one-shot
command, fatal here: one 5-second ``security`` timeout on 2026-08-13 left the
daemon reading the active credential in file-fallback mode (where macOS has no
file) for five days, reporting a false "no credentials" for the very account
``quota-autoswap.sh`` guards while the other accounts read fine. Every cycle now
re-arms that probe, so a blip heals within one cycle. Second, blindness is
*loud*: the sweep summary names the accounts it could not read, and losing sight
of the ACTIVE account logs a WARNING with a consecutive-sweep count rather than
blending into an idle-looking log. ``stale_after_seconds`` bounds how old the
active account's last real reading may get before that fires.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from claude_swap.paths import get_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher, usage_entry_age

_logger = logging.getLogger("cswap-quotad")

CONFIG_FILENAME = "quota-daemon.json"

# Conservative, rate-limit-safe defaults. ``spacing_seconds`` is the gap BETWEEN
# per-account calls; ``cycle_seconds`` is the idle gap between full sweeps. For 3
# accounts this is ~ (2 * 20) + 45 = ~85s per full refresh, far below any level
# that has ever tripped 429 (a single serial call is proven safe; the parallel
# burst is what 429s). Tune down later only with careful measurement.
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "spacing_seconds": 20,
    "cycle_seconds": 45,
    "accounts": "all",
    # How old the ACTIVE account's last real reading may get before a sweep calls
    # the daemon blind. Matches the default usage-cache TTL: past this, consumers
    # would be acting on a number nobody has confirmed.
    "stale_after_seconds": 300,
}

# Floors so a bad/aggressive config edit can never turn the daemon into a 429
# machine or a busy-loop. A spacing of 0 is still allowed (single-account setups
# never burst), but multi-account sweeps below the floor are clamped up.
_MIN_SPACING_SECONDS = 0.0
_MIN_CYCLE_SECONDS = 5.0
# Below a sweep's own duration a "stale" verdict would fire on every cycle by
# construction, so keep the floor above the cadence of a realistic sweep.
_MIN_STALE_AFTER_SECONDS = 30.0


def config_path() -> Path:
    """Location of the daemon config file (``<backup>/quota-daemon.json``)."""
    return get_backup_root() / CONFIG_FILENAME


def _coerce_number(value: Any, fallback: float, minimum: float) -> float:
    """Coerce ``value`` to a float >= ``minimum``, falling back on bad input."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num < minimum:
        return minimum
    return num


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read and normalize the daemon config, filling defaults for missing keys.

    Never raises on a malformed file: unreadable / invalid JSON falls back to the
    defaults (logged once per cycle) so a fat-fingered edit degrades to safe
    behavior instead of killing the daemon.
    """
    path = path or config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
            else:
                _logger.warning("Config %s is not a JSON object; using defaults", path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _logger.warning("Could not read config %s (%s); using defaults", path, exc)

    accounts = raw.get("accounts", DEFAULT_CONFIG["accounts"])
    if isinstance(accounts, str):
        accounts = accounts.strip().lower()
        if accounts != "all":
            _logger.warning("Unknown accounts value %r; treating as 'all'", accounts)
            accounts = "all"
    elif isinstance(accounts, list):
        # Normalize to a set of string account numbers.
        accounts = {str(a).strip() for a in accounts if str(a).strip()}
        if not accounts:
            accounts = "all"
    else:
        accounts = "all"

    return {
        "enabled": bool(raw.get("enabled", DEFAULT_CONFIG["enabled"])),
        "spacing_seconds": _coerce_number(
            raw.get("spacing_seconds"), DEFAULT_CONFIG["spacing_seconds"], _MIN_SPACING_SECONDS
        ),
        "cycle_seconds": _coerce_number(
            raw.get("cycle_seconds"), DEFAULT_CONFIG["cycle_seconds"], _MIN_CYCLE_SECONDS
        ),
        "stale_after_seconds": _coerce_number(
            raw.get("stale_after_seconds"),
            DEFAULT_CONFIG["stale_after_seconds"],
            _MIN_STALE_AFTER_SECONDS,
        ),
        "accounts": accounts,
    }


def ensure_config_file(path: Path | None = None) -> Path:
    """Create the config file with defaults if it does not exist. Idempotent."""
    path = path or config_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
            _logger.info("Wrote default config to %s", path)
        except OSError as exc:
            _logger.warning("Could not create default config %s (%s)", path, exc)
    return path


def _select_accounts(
    accounts_info: list[tuple[int, str, str, str, bool, str]],
    selector: Any,
) -> list[tuple[int, str, str, str, bool, str]]:
    """Filter the roster by the config's ``accounts`` selector ('all' or a set)."""
    if selector == "all" or not isinstance(selector, set):
        return accounts_info
    return [info for info in accounts_info if str(info[0]) in selector]


def _summarize(
    selected: list[tuple[int, str, str, str, bool, str]],
    usages: list[dict | str | None],
) -> str:
    """One-line human summary of a sweep's outcome for the log.

    Names the accounts that did *not* produce a reading. The bare counts this used
    to print ("1 ok, 2 unavailable/sentinel") looked identical whether the missing
    account was an idle spare or the one the watchdog guards.
    """
    ok = sum(1 for u in usages if isinstance(u, dict))
    other = len(usages) - ok
    summary = f"{ok} ok, {other} unavailable/sentinel"
    missing = [
        f"#{info[0]}{' (ACTIVE)' if info[4] else ''}"
        f"={'no data' if usage is None else repr(usage)}"
        for info, usage in zip(selected, usages)
        if not isinstance(usage, dict)
    ]
    return f"{summary} [{', '.join(missing)}]" if missing else summary


def _blind_reason(usage: dict | str | None, stale_after: float) -> str | None:
    """Why this sweep cannot see an account, or ``None`` when the reading is good.

    A sentinel counts as blindness for the ACTIVE slot: "no credentials" there has
    proven to mean "the credential store could not be read" as often as it means a
    real logout, and either way there is no number to act on.
    """
    if usage is None:
        return "fetch returned no data"
    if isinstance(usage, str):
        return f"reported as {usage!r}"
    age = usage_entry_age(usage)
    if age is not None and age > stale_after:
        return (
            f"serving a preserved reading {int(age)}s old "
            f"(over the {int(stale_after)}s limit)"
        )
    return None


class ActiveAccountWatch:
    """Tracks whether the daemon can see the ACTIVE account, sweep over sweep.

    The active slot is the one ``quota-autoswap.sh`` guards, so losing sight of it
    is the failure that matters: every other account can read perfectly while the
    guarded one is invisible, and because the cache carries a single top-level
    timestamp that each sweep rewrites, the file still looks fresh. Nothing in the
    old logs distinguished that state from a healthy idle daemon.
    """

    def __init__(self) -> None:
        self.blind_streak = 0

    def observe(self, number: int, reason: str | None) -> None:
        """Record this sweep's verdict for the active account and log the change."""
        if reason is None:
            if self.blind_streak:
                _logger.info(
                    "Active account #%s is readable again after %d blind sweep(s)",
                    number, self.blind_streak,
                )
            self.blind_streak = 0
            return
        self.blind_streak += 1
        _logger.warning(
            "BLIND: active account #%s %s. This is the account autoswap guards, so "
            "it cannot act while this persists (%d consecutive sweep(s))",
            number, reason, self.blind_streak,
        )


def run_cycle(
    switcher: ClaudeAccountSwitcher,
    cfg: dict[str, Any],
    watch: ActiveAccountWatch | None = None,
) -> list[dict | str | None]:
    """Run one serial sweep over the selected accounts and write the cache.

    Returns the per-account usage entries (also persisted to the global cache).
    """
    # A cycle is this daemon's equivalent of a fresh CLI run, so re-arm Keychain
    # probing first: the store's usability cache is sticky by design, and without
    # this a single transient `security` timeout would keep every later
    # active-credential read in file-fallback mode for the life of the process.
    if switcher.reset_keychain_capability():
        _logger.warning(
            "Keychain was marked unusable by an earlier failure; re-probing it this "
            "cycle (a transient failure must not blind the daemon permanently)"
        )

    accounts_info = switcher._build_accounts_info()
    selected = _select_accounts(accounts_info, cfg["accounts"])
    if not selected:
        # Nothing to fetch (roster empty or the accounts filter matched nothing).
        # Leave the existing cache untouched rather than blanking it.
        _logger.warning("No accounts selected this cycle (roster empty or filtered out)")
        return []
    _logger.info(
        "Sweeping %d account(s) serially (spacing=%.0fs)",
        len(selected),
        cfg["spacing_seconds"],
    )
    usages = switcher.collect_usage_serial(
        selected,
        spacing_seconds=cfg["spacing_seconds"],
        preserve_last_good=True,
    )
    _logger.info("Sweep complete: %s", _summarize(selected, usages))

    stale_after = cfg.get("stale_after_seconds", DEFAULT_CONFIG["stale_after_seconds"])
    active = next((pair for pair in zip(selected, usages) if pair[0][4]), None)
    if active is None:
        # No row is flagged active: either active detection failed or the accounts
        # filter excludes it. Either way the watchdog has nothing to judge, which is
        # its own kind of blindness and must not pass silently.
        _logger.warning(
            "No ACTIVE account in this sweep (active detection failed, or the "
            "accounts filter excludes it); autoswap has nothing to judge"
        )
    else:
        info, usage = active
        (watch or ActiveAccountWatch()).observe(
            info[0], _blind_reason(usage, stale_after)
        )
    return usages


class _StopSignal:
    """Interruptible sleep that returns early when SIGTERM/SIGINT arrives."""

    def __init__(self) -> None:
        self._stop = False

    def request_stop(self, *_: Any) -> None:
        self._stop = True

    @property
    def stopped(self) -> bool:
        return self._stop

    def sleep(self, seconds: float) -> None:
        """Sleep in short slices so a stop signal is honored within ~1s."""
        deadline = time.monotonic() + seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))


def run(config_file: Path | None = None, once: bool = False) -> int:
    """Daemon main loop. Returns a process exit code."""
    ensure_config_file(config_file)
    switcher = ClaudeAccountSwitcher()
    watch = ActiveAccountWatch()

    stop = _StopSignal()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)

    _logger.info("cswap-quotad starting (config=%s, once=%s)", config_file or config_path(), once)

    while not stop.stopped:
        cfg = load_config(config_file)
        if not cfg["enabled"]:
            _logger.info("Daemon disabled via config; idling for %.0fs", cfg["cycle_seconds"])
            if once:
                break
            stop.sleep(cfg["cycle_seconds"])
            continue

        try:
            run_cycle(switcher, cfg, watch)
        except Exception:  # noqa: BLE001 - never let one bad cycle kill the daemon
            _logger.exception("Cycle failed; continuing")

        if once:
            break
        stop.sleep(cfg["cycle_seconds"])

    _logger.info("cswap-quotad stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cswap-quotad",
        description="Reliable per-account Claude quota daemon (single serial fetcher).",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single sweep and exit (for testing)."
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="Override the config file path."
    )
    parser.add_argument(
        "--print-config-path", action="store_true",
        help="Print the resolved config file path and exit.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.print_config_path:
        print(args.config or config_path())
        return 0

    return run(config_file=args.config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
