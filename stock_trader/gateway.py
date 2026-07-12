"""IB Gateway helper: detect, auto-launch, and install the gateway.

What can and cannot be automated: the gateway binary can be installed and
started automatically, but Interactive Brokers requires a human to log in
inside the gateway window (username/password, plus two-factor on live
accounts). So the flow is: we make sure the gateway is installed and running,
the user logs in once in its window, and the bot connects to the API port.

Installers are downloaded from IBKR's official server only
(download2.interactivebrokers.com, the "stable standalone" channel).
"""

import glob
import logging
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

_BASE = "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone"
INSTALLER_URLS = {
    "win32": f"{_BASE}/ibgateway-stable-standalone-windows-x64.exe",
    "darwin": f"{_BASE}/ibgateway-stable-standalone-macosx-x64.dmg",
    "linux": f"{_BASE}/ibgateway-stable-standalone-linux-x64.sh",
}

LOGIN_HELP = (
    "Log in inside the IB Gateway window: pick 'IB API' (not FIX CTCI), use "
    "your paper username (starts with DU) for paper trading, then in "
    "Configure → Settings → API → Settings enable 'ActiveX and Socket "
    "Clients', untick 'Read-Only API', and check the socket port matches "
    "(paper 4002, live 4001)."
)


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_gateway() -> Path | None:
    """Locate an installed IB Gateway, newest version first."""
    if sys.platform == "win32":
        patterns = [r"C:\Jts\ibgateway\*\ibgateway.exe",
                    str(Path.home() / r"Jts\ibgateway\*\ibgateway.exe")]
    elif sys.platform == "darwin":
        patterns = ["/Applications/IB Gateway*/IB Gateway*.app",
                    str(Path.home() / "Applications/IB Gateway*/IB Gateway*.app"),
                    "/Applications/IB Gateway*.app",
                    str(Path.home() / "Applications/IB Gateway*.app")]
    else:
        patterns = [str(Path.home() / "Jts/ibgateway/*/ibgateway")]
    hits: list[str] = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    return Path(sorted(hits)[-1]) if hits else None


def launch(path: Path) -> None:
    """Start the gateway detached; its login window appears on the desktop."""
    log.info("launching IB Gateway: %s", path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        import os
        os.startfile(str(path))  # noqa: S606 — the file we just located
    else:
        subprocess.Popen([str(path)], start_new_session=True,
                         cwd=str(path.parent))


class GatewayManager:
    """Tracks install/run state and exposes install/start actions."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.installing = False
        self.install_message: str | None = None
        self._launched_once = False

    # ------------------------------------------------------------- status --
    def status(self) -> dict:
        gw = find_gateway()
        return {
            "running": port_open(self.s.ibkr_host, self.s.ibkr_port),
            "installed": gw is not None,
            "path": str(gw) if gw else None,
            "installing": self.installing,
            "install_message": self.install_message,
            "port": self.s.ibkr_port,
            "login_help": LOGIN_HELP,
        }

    # ------------------------------------------------------------- actions --
    def start(self) -> dict:
        st = self.status()
        if st["running"]:
            return {**st, "message": f"IB Gateway is already running on port {st['port']}."}
        if not st["installed"]:
            return {**st, "message": "IB Gateway is not installed — use Install first."}
        launch(Path(st["path"]))
        return {**st, "message": "IB Gateway launched — a login window will "
                                 "appear in a few seconds. " + LOGIN_HELP}

    def ensure(self) -> None:
        """Auto-launch on server/bot start: never raises, launches at most
        once per process so a closed login window isn't reopened forever."""
        try:
            if self.s.provider != "ibkr" or self._launched_once:
                return
            if port_open(self.s.ibkr_host, self.s.ibkr_port):
                return
            gw = find_gateway()
            if gw is None:
                log.warning("IBKR selected but IB Gateway is not installed — "
                            "install it from ⚙ Settings or interactivebrokers.com")
                return
            self._launched_once = True
            launch(gw)
            log.info("IB Gateway auto-started (port %d was closed). %s",
                     self.s.ibkr_port, LOGIN_HELP)
        except Exception:
            log.exception("could not auto-start IB Gateway")

    def install(self) -> dict:
        if self.installing:
            return {**self.status(), "message": "installer already running"}
        if find_gateway() is not None:
            return {**self.status(), "message": "IB Gateway is already installed."}
        url = INSTALLER_URLS.get(sys.platform)
        if url is None:
            return {**self.status(), "message": f"no installer known for {sys.platform}"}
        self.installing = True
        self.install_message = "downloading installer from interactivebrokers.com…"
        threading.Thread(target=self._install_worker, args=(url,),
                         name="ibgw-install", daemon=True).start()
        return {**self.status(), "message": self.install_message}

    # ------------------------------------------------------------ internal --
    def _install_worker(self, url: str) -> None:
        try:
            dest = Path(tempfile.gettempdir()) / url.rsplit("/", 1)[-1]
            log.info("downloading IB Gateway installer: %s", url)
            with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done, last_pct = 0, -10
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        if pct >= last_pct + 10:
                            last_pct = pct
                            self.install_message = f"downloading installer… {pct}%"
            if sys.platform == "win32":
                self.install_message = "installing (silent) — this takes a few minutes…"
                subprocess.run([str(dest), "-q"], check=True, timeout=1200)
                ok = find_gateway() is not None
                self.install_message = (
                    "installed — press 'Start IB Gateway' next." if ok
                    else "installer finished but the gateway was not found — "
                         "run the installer manually from your Downloads."
                )
            elif sys.platform == "darwin":
                subprocess.run(["open", str(dest)], check=True)
                self.install_message = ("installer opened — finish it in the window "
                                        "that appeared, then press 'Start IB Gateway'.")
            else:
                subprocess.run(["sh", str(dest), "-q"], check=True, timeout=1200)
                self.install_message = "installed — press 'Start IB Gateway' next."
            log.info("IB Gateway install step done: %s", self.install_message)
        except Exception as e:
            log.exception("IB Gateway install failed")
            self.install_message = f"install failed: {e} — download it manually " \
                                   "from interactivebrokers.com"
        finally:
            self.installing = False
