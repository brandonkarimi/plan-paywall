"""Start the Plan Sender desktop app:  .venv\\Scripts\\pythonw run_desktop.py   (add --tray to start hidden)"""
import os
import sys


def unblock_bundled_files():
    """Files extracted from a downloaded zip carry a 'from the internet' mark (Zone.Identifier stream).
    .NET refuses to load marked DLLs, so pywebview fails with 'Failed to resolve Python.Runtime.Loader.Initialize'.
    Strip the mark from our own bundled files before pywebview loads .NET."""
    base = getattr(sys, "_MEIPASS", None)
    if sys.platform != "win32" or not base:
        return
    probe = os.path.join(base, "pythonnet", "runtime", "Python.Runtime.dll")
    if not os.path.exists(probe + ":Zone.Identifier"):
        return
    roots = {base, os.path.dirname(sys.executable)}
    for root in roots:
        for folder, _dirs, files in os.walk(root):
            for name in files:
                try:
                    os.remove(os.path.join(folder, name) + ":Zone.Identifier")
                except OSError:
                    pass


unblock_bundled_files()

from desktop.app import main  # noqa: E402  (must come after the unblock)

if __name__ == "__main__":
    main()
