"""The background payment checker: a hidden copy of Plan Sender (started with --background) that runs from Windows
start-up, whether or not the Plan Sender window is open.

About once a minute it looks for plans waiting for payment. Only when there are some does it read the payment-alert
inbox(es) and release plans that are paid (desktop.payments.check_inbox). It quits by itself once automatic
payments are turned off. Plan Sender itself never reads the inbox; it just shows what the checker found.
"""
import ctypes
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from desktop import config, db, payments

log = logging.getLogger(__name__)

CHECK_EVERY = 60          # seconds between looks while plans are waiting for payment
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Plan Sender payments"
MUTEX = "Local\\PlanSenderPaymentChecker"


def command():
    """How Windows starts the checker: the packaged PlanSender.exe, or pythonw + run_desktop.py from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve().parent.parent / "run_desktop.py"
    return [str(pythonw if pythonw.exists() else sys.executable), str(script), "--background"]


def enabled(settings=None):
    auto = payments.auto_settings(settings or config.load_settings())
    return auto["enabled"] and bool(payments.inboxes(auto))


# ------------------------------------------------------------------ Windows start-up and process
def _set_startup(cmd):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if cmd:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def is_running():
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, MUTEX)  # SYNCHRONIZE
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def _launch():
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(command(), creationflags=flags, close_fds=True, cwd=str(Path(command()[0]).parent))


def sync(settings=None):
    """Match Windows to the settings: automatic payments on → start with Windows and be running now; off → not
    started with Windows (a running checker notices and quits within a minute). Also refreshes the start-up entry
    in case the app folder moved."""
    on = enabled(settings)
    try:
        _set_startup(command() if on else None)
        if on and not is_running():
            _launch()
    except Exception:
        log.exception("Couldn't set up the background payment checker")


# ------------------------------------------------------------------ the checker itself
def status_path():
    return config.data_dir() / "payment_check.json"


def write_status(errors):
    try:
        status_path().write_text(json.dumps({"checked_at": db.now(), "errors": errors}), encoding="utf-8")
    except OSError:
        pass


def read_status():
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tick(connect=None):
    """One round. Returns False when the checker should quit (automatic payments turned off)."""
    settings = config.load_settings()
    if not enabled(settings):
        return False
    if not db.list_txs(["sent"]):  # nothing waiting for payment: no need to read the inbox
        write_status([])
        return True
    try:
        result = payments.check_inbox(settings, connect)
    except Exception as exc:  # never let one bad round stop the checker
        log.exception("Payment check failed")
        result = {"released": [], "review": 0, "errors": [f"Couldn't check for payments: {exc}"]}
    if result["released"] or result["review"]:
        log.info("Payment check: %s", result)
    for error in result.get("errors", []):
        log.warning("Payment check: %s", error)
    write_status(result.get("errors", []))
    return True


def run():
    """Entry point for PlanSender.exe --background."""
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: one checker is enough
        return
    db.init()
    log.info("Background payment checker started")
    try:
        while tick():
            time.sleep(CHECK_EVERY)
    finally:
        log.info("Background payment checker stopped (automatic payments are off)")
        ctypes.windll.kernel32.CloseHandle(mutex)
