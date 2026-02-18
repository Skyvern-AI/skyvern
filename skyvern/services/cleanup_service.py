"""
Cleanup service for periodic cleanup of temporary data.

This service is responsible for:
1. Cleaning up temporary files in the temp directory
2. Killing stale playwright/node/browser processes
"""

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import structlog

from skyvern.config import settings
from skyvern.forge import app

LOG = structlog.get_logger()

# Process names to look for when cleaning up stale processes
STALE_PROCESS_NAMES = frozenset(
    {
        "playwright",
        "node",
        "chromium",
        "chrome",
        "firefox",
        "webkit",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "msedge",
        "microsoft-edge",
        "microsoft-edge-stable",
    }
)


async def check_running_tasks_or_workflows() -> tuple[bool, int, int]:
    """
    Check if there are any actively running tasks or workflow runs.

    Tasks/workflows that haven't been updated for longer than the configured
    stale threshold are considered stuck (not actively running), and a warning
    will be logged for them.

    Returns:
        Tuple of (has_active_tasks_or_workflows, stale_task_count, stale_workflow_count).
    """
    try:
        stale_threshold = settings.CLEANUP_STALE_TASK_THRESHOLD_HOURS

        # Check tasks
        active_tasks, stale_tasks = await app.DATABASE.get_running_tasks_info_globally(
            stale_threshold_hours=stale_threshold
        )

        # Check workflow runs
        active_workflows, stale_workflows = await app.DATABASE.get_running_workflow_runs_info_globally(
            stale_threshold_hours=stale_threshold
        )

        # Log warnings for stale tasks/workflows
        if stale_tasks > 0:
            LOG.warning(
                "Found stale tasks that haven't been updated",
                stale_task_count=stale_tasks,
                threshold_hours=stale_threshold,
            )

        if stale_workflows > 0:
            LOG.warning(
                "Found stale workflow runs that haven't been updated",
                stale_workflow_count=stale_workflows,
                threshold_hours=stale_threshold,
            )

        has_active = active_tasks > 0 or active_workflows > 0
        return (has_active, stale_tasks, stale_workflows)
    except Exception:
        LOG.exception("Error checking for running tasks/workflows")
        # If we can't check, assume there are running tasks to be safe
        return (True, 0, 0)


def cleanup_temp_directory() -> int:
    """
    Clean up temporary files in the temp directory.

    Returns:
        Number of files/directories removed.
    """
    temp_path = Path(settings.TEMP_PATH)
    if not temp_path.exists():
        LOG.debug("Temp directory does not exist", temp_path=str(temp_path))
        return 0

    removed_count = 0
    try:
        for item in temp_path.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    removed_count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    removed_count += 1
            except Exception:
                LOG.warning("Failed to remove temp item", item=str(item), exc_info=True)
    except Exception:
        LOG.exception("Error cleaning temp directory", temp_path=str(temp_path))

    return removed_count


def get_stale_browser_processes(max_age_minutes: int = 60) -> list[psutil.Process]:
    """
    Get browser-related processes that have been running for too long.

    Args:
        max_age_minutes: Maximum age in minutes before a process is considered stale.

    Returns:
        List of stale processes.
    """
    stale_processes = []
    cutoff_time = datetime.now() - timedelta(minutes=max_age_minutes)

    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            proc_name = proc.info.get("name", "").lower()  # type: ignore[union-attr]
            create_time = proc.info.get("create_time")  # type: ignore[union-attr]

            if create_time is None:
                continue

            # Check if process name matches any of the stale process names
            is_browser_related = any(name in proc_name for name in STALE_PROCESS_NAMES)

            if is_browser_related:
                proc_start_time = datetime.fromtimestamp(create_time)
                if proc_start_time < cutoff_time:
                    stale_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return stale_processes


def kill_stale_processes() -> int:
    """
    Kill stale playwright/node/browser processes.

    Returns:
        Number of processes killed.
    """
    stale_processes = get_stale_browser_processes()
    killed_count = 0

    for proc in stale_processes:
        try:
            proc_name = proc.name()
            proc_pid = proc.pid

            # Try graceful termination first
            proc.terminate()

            # Wait a short time for graceful shutdown
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                # Force kill if graceful termination didn't work
                proc.kill()

            LOG.info("Killed stale process", name=proc_name, pid=proc_pid)
            killed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            LOG.warning("Failed to kill stale process", pid=proc.pid, exc_info=True)

    return killed_count


async def run_cleanup() -> None:
    """
    Run the cleanup process.

    This function checks if there are actively running tasks/workflows before cleaning up.
    If there are active (recently updated) tasks/workflows, it skips the cleanup.
    Tasks/workflows that haven't been updated for longer than the stale threshold
    are considered stuck and will not block cleanup (but a warning will be logged).
    """
    LOG.debug("Starting cleanup process")

    # Check if there are running tasks or workflows
    has_active, stale_tasks, stale_workflows = await check_running_tasks_or_workflows()

    if has_active:
        LOG.info("Skipping cleanup: tasks or workflows are currently running")
        return

    # Log summary if there are stale tasks/workflows (they won't block cleanup)
    if stale_tasks > 0 or stale_workflows > 0:
        LOG.info(
            "Proceeding with cleanup despite stale tasks/workflows",
            stale_tasks=stale_tasks,
            stale_workflows=stale_workflows,
        )

    # Clean up temp directory
    temp_files_removed = cleanup_temp_directory()
    if temp_files_removed > 0:
        LOG.info("Cleaned up temp directory", files_removed=temp_files_removed)

    # Kill stale processes
    processes_killed = kill_stale_processes()
    if processes_killed > 0:
        LOG.info("Killed stale browser processes", processes_killed=processes_killed)

    LOG.debug("Cleanup process completed")


async def cleanup_scheduler() -> None:
    """
    Scheduler that runs the cleanup process periodically.

    This runs in an infinite loop, sleeping for the configured interval between runs.
    """
    interval_seconds = settings.CLEANUP_CRON_INTERVAL_MINUTES * 60

    LOG.info(
        "Cleanup scheduler started",
        interval_minutes=settings.CLEANUP_CRON_INTERVAL_MINUTES,
    )

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await run_cleanup()
        except asyncio.CancelledError:
            LOG.info("Cleanup scheduler cancelled")
            break
        except Exception:
            LOG.exception("Error in cleanup scheduler")
            # Continue running despite errors


_cleanup_task: asyncio.Task | None = None


def start_cleanup_scheduler() -> asyncio.Task | None:
    """
    Start the cleanup scheduler as a background task.

    Returns:
        The asyncio Task running the scheduler, or None if cleanup is disabled.
    """
    global _cleanup_task

    if not settings.ENABLE_CLEANUP_CRON:
        LOG.debug("Cleanup cron is disabled")
        return None

    if _cleanup_task is not None and not _cleanup_task.done():
        LOG.warning("Cleanup scheduler is already running")
        return _cleanup_task

    _cleanup_task = asyncio.create_task(cleanup_scheduler())
    return _cleanup_task


async def stop_cleanup_scheduler() -> None:
    """
    Stop the cleanup scheduler if it's running.
    """
    global _cleanup_task

    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        LOG.info("Cleanup scheduler stopped")

    _cleanup_task = None
