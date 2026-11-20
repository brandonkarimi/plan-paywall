"""Plan Sender desktop app: a single window."""
import ctypes
import logging
import sys
from pathlib import Path

import webview

from desktop import config, db
from desktop.api import Api


def resource(rel):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def already_running() -> bool:
    if sys.platform != "win32":
        return False
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\PlanSenderSingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    logging.basicConfig(filename=config.log_path(), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if already_running():
        ctypes.windll.user32.MessageBoxW(None, "Plan Sender is already open.", "Plan Sender", 0x40)
        return
    db.init()
    api = Api()
    window = webview.create_window("Plan Sender", url=str(resource("desktop/ui/index.html")), js_api=api,
                                   width=1040, height=820, min_size=(760, 600))
    api._attach(window)
    webview.start(private_mode=False, storage_path=str(config.data_dir() / "webview"))


if __name__ == "__main__":
    main()
