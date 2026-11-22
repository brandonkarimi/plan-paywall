"""The background payment checker: a hidden copy of Plan Sender (started with --background) that runs from Windows
start-up, whether or not the Plan Sender window is open.

While plans are waiting for payment it keeps one quiet connection open to each payment-alert inbox and the mail
server tells it the moment new mail arrives (IMAP IDLE), so plans go out seconds after the payment email lands.
As a safety net it also checks every few minutes, and straight after the computer wakes up or the connection comes
back. With nothing waiting it closes the connections and only glances at the local database now and then.

It runs at below-normal priority and loads none of the window or PDF code, so it stays out of the engineer's way.
It quits by itself once automatic payments are turned off. Plan Sender itself never reads the inbox; it just shows
what the checker found.
"""
import ctypes
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from desktop import config, db, payments

log = logging.getLogger(__name__)

LOOP = 10                 # seconds between glances at the local database (cheap: no network)
SAFETY_EVERY = 5 * 60     # a full check this often even if the mail server never said "new mail"
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


def _kernel32():
    """kernel32 with handle types declared: without this, 64-bit handles get cut to 32 bits and calls quietly fail."""
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.SetPriorityClass.restype = wintypes.BOOL
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


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
    k = _kernel32()
    handle = k.OpenMutexW(0x00100000, False, MUTEX)  # SYNCHRONIZE
    if handle:
        k.CloseHandle(handle)
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


class Watcher(threading.Thread):
    """Keeps a connection open to one inbox and sets ``wake`` whenever the mail server reports new mail. It also sets
    ``wake`` each time it (re)connects, which covers start-up, waking from sleep and the internet coming back."""

    def __init__(self, box, wake, connect=None):
        super().__init__(daemon=True, name=f"watch {box['email']}")
        self.box, self.wake, self.connect = box, wake, connect
        self.stop = threading.Event()

    def run(self):
        backoff, last_error = 10, None
        while not self.stop.is_set():
            conn = None
            try:
                conn = payments.open_inbox(self.box, self.connect)
                self.wake.set()
                backoff, last_error = 10, None
                if "IDLE" not in getattr(conn, "capabilities", ()):  # rare: a server that can't push new mail
                    payments.close(conn)
                    conn = None
                    while not self.stop.wait(60):
                        self.wake.set()
                    return
                while not self.stop.is_set():
                    if payments.wait_for_mail(conn, self.stop):
                        self.wake.set()
            except Exception as exc:
                if str(exc) != last_error:  # log a problem once, not on every retry
                    log.warning("Waiting for new mail in %s: %s", self.box["email"], exc)
                    last_error = str(exc)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if conn is not None:
                    payments.close(conn)


def sync_watchers(watchers, boxes, wake, connect=None):
    """One watcher per inbox in ``boxes`` ({email: box}); stop the others (and any that died get replaced)."""
    for address in list(watchers):
        if address not in boxes or not watchers[address].is_alive():
            watchers.pop(address).stop.set()
    for address, box in boxes.items():
        if address not in watchers:
            watchers[address] = Watcher(box, wake, connect)
            watchers[address].start()


def lower_priority():
    try:
        k = _kernel32()
        if not k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
            log.warning("Couldn't lower the checker's priority (error %s)", ctypes.get_last_error())
    except Exception:
        log.exception("Couldn't lower the checker's priority")


def run():
    """Entry point for PlanSender.exe --background."""
    k = _kernel32()
    mutex = k.CreateMutexW(None, False, MUTEX)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: one checker is enough
        k.CloseHandle(mutex)
        return
    lower_priority()
    db.init()
    log.info("Background payment checker started")
    wake, watchers = threading.Event(), {}
    last_check, last_seen = 0.0, time.time()
    try:
        while True:
            settings = config.load_settings()
            if not enabled(settings):
                break
            waiting = bool(db.list_txs(["sent"]))
            boxes = {b["email"]: b for b in payments.inboxes(payments.auto_settings(settings))} if waiting else {}
            sync_watchers(watchers, boxes, wake)
            now = time.time()
            woke_up = now - last_seen > LOOP + 60  # the computer was asleep
            last_seen = now
            if waiting and (wake.is_set() or woke_up or now - last_check >= SAFETY_EVERY):
                wake.clear()
                tick()
                last_check = time.time()
            wake.wait(LOOP)
    finally:
        sync_watchers(watchers, {}, wake)
        log.info("Background payment checker stopped (automatic payments are off)")
        k.CloseHandle(mutex)


def main():
    """PlanSender.exe --background."""
    logging.basicConfig(filename=config.log_path(), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()
