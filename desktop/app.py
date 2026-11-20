"""Plan Sender desktop app: window + system-tray icon + background Stripe watcher."""
import ctypes
import logging
import sys
import threading
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

from desktop import config, db
from desktop.api import Api
from desktop.watcher import Watcher

log = logging.getLogger("plansender")


def resource(rel):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def tray_image():
    ico = resource("desktop/ui/icon.png")
    if ico.exists():
        return Image.open(ico)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(29, 78, 216, 255))
    d.rectangle([20, 14, 44, 50], fill=(255, 255, 255, 255))
    d.rectangle([26, 22, 38, 26], fill=(29, 78, 216, 255))
    d.rectangle([26, 31, 38, 35], fill=(29, 78, 216, 255))
    return img


def already_running() -> bool:
    if sys.platform != "win32":
        return False
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\PlanSenderSingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    logging.basicConfig(filename=config.log_path(), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if already_running():
        ctypes.windll.user32.MessageBoxW(
            None, "Plan Sender is already running. Look for its icon in the taskbar tray (bottom right).",
            "Plan Sender", 0x40)
        return
    db.init()
    start_hidden = "--tray" in sys.argv
    state = {"quitting": False, "icon": None}

    def notify(title, message):
        log.info("Notify: %s | %s", title, message)
        icon = state["icon"]
        if icon is not None:
            try:
                icon.notify(message[:250], title[:60])
            except Exception:
                log.exception("Tray notification failed")

    def refresh_ui():
        try:
            window.evaluate_js("window.refreshFromPython && window.refreshFromPython()")
        except Exception:
            pass

    watcher = Watcher(notify=notify, on_change=refresh_ui)
    api = Api(watcher, notify)
    window = webview.create_window(
        "Plan Sender", url=str(resource("desktop/ui/index.html")), js_api=api,
        width=1040, height=820, min_size=(760, 600), hidden=start_hidden,
    )
    api._attach(window)

    def on_closing():
        if state["quitting"]:
            return True
        window.hide()  # keep running in the tray so paid plans still go out
        notify("Plan Sender is still running", "It keeps checking for payments. Quit from the tray icon.")
        return False

    window.events.closing += on_closing

    def show(icon=None, item=None):
        window.show()
        window.restore()

    def check(icon=None, item=None):
        watcher.trigger()

    def quit_app(icon=None, item=None):
        state["quitting"] = True
        watcher.stop()
        if state["icon"]:
            state["icon"].stop()
        window.destroy()

    icon = pystray.Icon("PlanSender", tray_image(), "Plan Sender", menu=pystray.Menu(
        pystray.MenuItem("Open Plan Sender", show, default=True),
        pystray.MenuItem("Check for payments now", check),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    ))
    state["icon"] = icon
    threading.Thread(target=icon.run, daemon=True, name="tray").start()

    def on_started():
        watcher.start()

    webview.start(on_started, private_mode=False, storage_path=str(config.data_dir() / "webview"))


if __name__ == "__main__":
    main()
