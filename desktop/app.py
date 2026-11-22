"""Plan Sender desktop app: a single window."""
import ctypes
import logging
import shutil
import threading
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


def wire_drops(window):
    """Let PDFs be dragged onto the Plan PDF and Invoice PDF boxes. Only pywebview's own drop events carry the
    file's full path, so the drop is handled here and the result handed to the page."""
    import json

    from webview.dom import DOMEventHandler

    from desktop.api import pdf_details

    def handler(kind):
        def on_drop(event):
            files = (event.get("dataTransfer") or {}).get("files") or []
            path = next((f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")), None)
            result = pdf_details(path, "Invoice" if kind == "invoice" else "")
            window.evaluate_js(f"fileDropped({json.dumps(kind)}, {json.dumps(result)})")
        return on_drop

    try:
        for selector, kind in (("#pick", "plan"), ("#inv-pick", "invoice")):
            window.dom.get_element(selector).events.drop += DOMEventHandler(handler(kind), prevent_default=True,
                                                                            stop_propagation=True)
    except Exception:  # choosing files by clicking still works
        logging.getLogger(__name__).exception("Couldn't set up drag and drop")


CHECK_EVERY = 120  # seconds between looks at the inbox for payment emails


def watch_payments(window):
    """While the app is open, look for Zelle/Venmo payment emails at start-up and then every few minutes, and tell
    the page when plans were sent or a payment needs confirming."""
    import json
    import time

    from desktop import payments
    from desktop.api import payment_summary

    log = logging.getLogger("desktop.payments")
    time.sleep(5)
    while True:
        try:
            result = payments.check_inbox()
            if result["released"] or result["review"]:
                log.info("Payment check: %s", result)
                window.evaluate_js(f"paymentsFound({json.dumps(payment_summary(result))})")
        except payments.InboxError as exc:
            log.warning("Payment check: %s", exc)
        except Exception:
            log.exception("Payment check failed")
        time.sleep(CHECK_EVERY)


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
    window.events.loaded += lambda *_: wire_drops(window)
    threading.Thread(target=watch_payments, args=(window,), daemon=True).start()
    storage = config.data_dir() / "webview"
    clear_page_cache(storage)
    webview.start(private_mode=False, storage_path=str(storage))


if __name__ == "__main__":
    main()
