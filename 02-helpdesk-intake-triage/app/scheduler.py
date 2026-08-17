"""
In-process background scheduler for escalation checks and outbox flushes.

WHY THIS EXISTS: `check_escalations()` only ran when something called it, and
nothing did. The "unacknowledged P1s escalate after N minutes" guarantee was
therefore not actually true in a running deployment — it worked when tested by
hand and silently never fired otherwise. That's the worst kind of gap: the
feature demonstrably works, so nobody thinks to check that it runs.

MULTI-WORKER CAVEAT (see config.SCHEDULER_ENABLED): each worker runs its own
loop, so N workers produce N sweeps. The per-ticket escalation cooldown caps
that at N pages per cooldown window rather than unbounded repetition, but the
correct deployment is one scheduler: `--workers 1`, or SCHEDULER_ENABLED=false
plus external cron against POST /admin/check-escalations.
"""
import asyncio
import logging

from . import alerting, config

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stats = {"runs": 0, "escalations_enqueued": 0, "alerts_flushed": 0, "errors": 0, "last_error": None}


def stats() -> dict:
    return dict(_stats)


def _tick_sync() -> tuple[int, int]:
    """One sweep. Runs in a worker thread — it does blocking DB and HTTP work,
    which must not run on the event loop."""
    enqueued = alerting.check_escalations()
    # Also flush anything left pending by a crash or an earlier failed attempt.
    flushed = alerting.flush_outbox()
    return len(enqueued), flushed.get("sent", 0)


async def _loop() -> None:
    interval = max(5, config.SCHEDULER_INTERVAL_SECONDS)
    logger.info("Escalation scheduler started (every %ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            enqueued, flushed = await asyncio.to_thread(_tick_sync)
            _stats["runs"] += 1
            _stats["escalations_enqueued"] += enqueued
            _stats["alerts_flushed"] += flushed
            if enqueued or flushed:
                logger.info(
                    "Scheduler tick: %d escalation(s) enqueued, %d alert(s) delivered",
                    enqueued, flushed,
                )
        except asyncio.CancelledError:
            logger.info("Escalation scheduler stopping")
            raise
        except Exception as e:  # noqa: BLE001
            # A failing sweep must never kill the loop — otherwise one transient
            # DB error silently disables escalation for the rest of the process's
            # life, which is exactly the class of bug this module was added to fix.
            _stats["errors"] += 1
            _stats["last_error"] = str(e)
            logger.exception("Scheduler tick failed; continuing")


def start() -> bool:
    global _task
    if not config.SCHEDULER_ENABLED:
        logger.warning(
            "SCHEDULER_ENABLED=false — unacknowledged P1s will NOT escalate unless "
            "an external cron calls POST /admin/check-escalations"
        )
        return False
    if _task is not None and not _task.done():
        return True
    _task = asyncio.create_task(_loop())
    return True


async def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


def is_running() -> bool:
    return _task is not None and not _task.done()
