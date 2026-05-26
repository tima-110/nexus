from __future__ import annotations

import shutil
from crontab import CronTab

CRON_COMMENT = "nexus-reconciler"


def install_schedule(interval_minutes: int = 5) -> str:
    """Create or update the nexus reconciler crontab entry.

    Returns the cron schedule expression (e.g. "*/5 * * * *").
    Raises RuntimeError if nexus binary cannot be found.
    """
    nexus_path = shutil.which("nexus")
    if nexus_path is None:
        raise RuntimeError("Cannot find 'nexus' executable in PATH")

    cron = CronTab(user=True)
    # Remove existing entry if present
    cron.remove_all(comment=CRON_COMMENT)

    command = f"{nexus_path} reconcile"
    job = cron.new(command=command, comment=CRON_COMMENT)
    job.setall(f"*/{interval_minutes} * * * *")
    cron.write()

    return str(job.slices)


def uninstall_schedule() -> bool:
    """Remove the nexus reconciler crontab entry.

    Returns True if an entry was found and removed, False otherwise.
    """
    cron = CronTab(user=True)
    jobs = list(cron.find_comment(CRON_COMMENT))
    if not jobs:
        return False
    cron.remove_all(comment=CRON_COMMENT)
    cron.write()
    return True


def get_schedule_status() -> dict:
    """Check current crontab schedule status.

    Returns dict with keys: installed (bool), schedule (str|None), command (str|None)
    """
    cron = CronTab(user=True)
    jobs = list(cron.find_comment(CRON_COMMENT))
    if not jobs:
        return {"installed": False, "schedule": None, "command": None}
    job = jobs[0]
    return {
        "installed": True,
        "schedule": str(job.slices),
        "command": str(job.command),
    }
