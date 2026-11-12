"""SIS-ping: poll UVA SIS for an open seat in a specific class and email when it opens."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import random
import signal
import sys
from pathlib import Path

from dotenv import dotenv_values

from notifier import SMTPConfig, send_seat_open_email, send_startup_email
from sis_client import ClassAvailability, SISClient

LOG = logging.getLogger("sis_ping")


def setup_logging(log_file: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


def load_config() -> dict:
    """Merge values from .env (if present) with process env; process env wins."""
    env_file = Path(__file__).with_name(".env")
    values: dict = {}
    if env_file.exists():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    # Legacy: TERM in .env. Never read TERM from the process env -- that's the
    # shell's terminal type (e.g. "xterm-256color"), not a SIS term code.
    if "TERM" in values and "SIS_TERM" not in values:
        values["SIS_TERM"] = values["TERM"]
    for key in (
        "SIS_TERM",
        "CLASS_NBR",
        "MNEMONIC",
        "POLL_INTERVAL",
        "EMAIL_TO",
        "EMAIL_FROM",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_APP_PASSWORD",
        "LOG_FILE",
        "SEND_STARTUP_EMAIL",
        "MAX_RUNTIME",
    ):
        if key in os.environ and os.environ[key] != "":
            values[key] = os.environ[key]
    return values


def require(cfg: dict, key: str) -> str:
    val = cfg.get(key)
    if val is None or val == "":
        raise SystemExit(f"Missing required config: {key}")
    return val


async def run_once(term: str, class_nbr: int) -> ClassAvailability:
    """Helper for --check-only: fetch & print availability then exit."""
    client = await SISClient.create()
    try:
        return await client.fetch_class(term=term, class_nbr=class_nbr)
    finally:
        await client.close()


async def watch(cfg: dict, stop: asyncio.Event) -> int:
    term = require(cfg, "SIS_TERM")
    class_nbr = int(require(cfg, "CLASS_NBR"))
    mnemonic = cfg.get("MNEMONIC")
    poll_interval = int(cfg.get("POLL_INTERVAL", "60"))
    # Optional: exit cleanly after this many seconds (for CI runners with job time limits).
    max_runtime = int(cfg.get("MAX_RUNTIME") or 0)

    if poll_interval < 30:
        LOG.warning(
            "POLL_INTERVAL=%ds is below 30s; be respectful of the SIS API.",
            poll_interval,
        )

    smtp_cfg = SMTPConfig.from_env(cfg)

    client = await SISClient.create()
    try:
        # Initial fetch: confirm the class exists and show what we're watching.
        first = await client.fetch_class(term=term, class_nbr=class_nbr)
        LOG.info(
            "Watching %s  title=%r  status=%s  available=%d/%d  waitlist=%d/%d",
            first.label,
            first.title,
            first.status,
            first.enrollment_available,
            first.class_capacity,
            first.wait_list_total,
            first.wait_list_capacity,
        )

        if cfg.get("SEND_STARTUP_EMAIL", "true").lower() in ("1", "true", "yes"):
            try:
                await send_startup_email(smtp_cfg, first)
            except Exception as e:
                LOG.error("Startup email failed: %s", e)

        alerted = first.is_open  # if already open, don't immediately email again on next tick
        if first.is_open:
            LOG.warning(
                "Seat is ALREADY open (%d available). Sending alert now.",
                first.enrollment_available,
            )
            try:
                await send_seat_open_email(smtp_cfg, first, mnemonic_override=mnemonic)
            except Exception as e:
                LOG.error("Initial alert email failed: %s", e)
                alerted = False  # retry on next tick

        backoff = poll_interval
        max_backoff = 600

        if max_runtime > 0:
            asyncio.get_running_loop().call_later(max_runtime, stop.set)

        while not stop.is_set():
            # Sleep with jitter, but wake up early if we're asked to stop.
            sleep_for = max(5, poll_interval + random.uniform(-5, 5))
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
                break  # stop was set
            except asyncio.TimeoutError:
                pass

            try:
                info = await client.fetch_class(term=term, class_nbr=class_nbr)
                backoff = poll_interval  # reset backoff on success
            except Exception as e:
                LOG.warning("Fetch failed: %s (backing off %ds)", e, backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(max_backoff, backoff * 2)
                continue

            LOG.info(
                "Check: status=%s available=%d total=%d/%d waitlist=%d/%d",
                info.status,
                info.enrollment_available,
                info.enrollment_total,
                info.class_capacity,
                info.wait_list_total,
                info.wait_list_capacity,
            )

            if info.is_open and not alerted:
                LOG.info("Seat open -> sending alert")
                try:
                    await send_seat_open_email(smtp_cfg, info, mnemonic_override=mnemonic)
                    alerted = True
                except Exception as e:
                    LOG.error("Alert email failed: %s (will retry next tick)", e)
            elif not info.is_open and alerted:
                LOG.info("Seat filled again; re-arming alert")
                alerted = False

        LOG.info("Stop requested; exiting cleanly")
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="UVA SIS seat-open watcher")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Fetch once, print availability, exit (does not send email).",
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg.get("LOG_FILE"))

    if args.check_only:
        term = require(cfg, "SIS_TERM")
        class_nbr = int(require(cfg, "CLASS_NBR"))
        info = asyncio.run(run_once(term=term, class_nbr=class_nbr))
        print(
            f"{info.label}  {info.title!r}\n"
            f"  status               = {info.status}\n"
            f"  enrollment_available = {info.enrollment_available}\n"
            f"  enrollment_total     = {info.enrollment_total} / {info.class_capacity}\n"
            f"  wait_list            = {info.wait_list_total} / {info.wait_list_capacity}"
        )
        return 0

    # Validate SMTP config early so we fail fast instead of discovering at first alert.
    for key in ("EMAIL_TO", "SMTP_USER", "SMTP_APP_PASSWORD"):
        require(cfg, key)

    stop = asyncio.Event()

    async def _run() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        return await watch(cfg, stop)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
