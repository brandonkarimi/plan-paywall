"""Plan Sender desktop app: a single window."""
import ctypes
import logging
import shutil
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


def clear_page_cache(storage):
    """WebView2 can keep showing an old copy of the page after an update; drop its page cache on every start.
    Only the browser cache is removed; settings and transactions live elsewhere."""
    for name in ("Cache", "Code Cache"):
        shutil.rmtree(Path(storage) / "EBWebView" / "Default" / name, ignore_errors=True)


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
    storage = config.data_dir() / "webview"
    clear_page_cache(storage)
    webview.start(private_mode=False, storage_path=str(storage))


if __name__ == "__main__":
    main()
