"""Self-updater: pull the latest code from GitHub and restart.

Two install shapes are supported:
- a git clone (developer machine): `git pull --ff-only`
- a ZIP install (no git, e.g. the Windows quick setup): the repo zipball is
  downloaded from the GitHub API and extracted over the install directory,
  never touching `.env` or `.venv`; the applied commit is recorded in a
  VERSION file so future checks know the local state.

After applying, requirements are re-installed and the server restarts
itself — on Windows by exiting with code 42, which start_bot.bat treats as
"restart me"; elsewhere by exec-ing a fresh copy of the process.

Private repos need a GitHub token (Settings → Updates, or GITHUB_TOKEN in
.env) with read-only contents access.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 42
_SKIP = {".env", ".venv", ".git", "VERSION", "__pycache__"}


def _extract_over(zip_path: Path, root: Path) -> int:
    """Extract a GitHub zipball (single top-level folder) over `root`,
    skipping local-only files. Returns the number of files written."""
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if len(parts) < 2 or info.is_dir():
                continue  # top-level folder itself
            rel = Path(*parts[1:])
            if rel.parts[0] in _SKIP:
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written


class Updater:
    def __init__(self, settings: Settings, root: Path | None = None):
        self.s = settings
        self.root = root or Path(__file__).resolve().parents[1]
        self.updating = False
        self.message: str | None = None

    # ------------------------------------------------------------ queries --
    def _api(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "stock-trader-updater",
            **({"Authorization": f"Bearer {self.s.github_token}"}
               if self.s.github_token else {}),
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def is_git_checkout(self) -> bool:
        return (self.root / ".git").exists()

    def local_sha(self) -> str | None:
        if self.is_git_checkout():
            try:
                out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                                     capture_output=True, text=True, timeout=10)
                return out.stdout.strip() or None
            except Exception:
                return None
        vf = self.root / "VERSION"
        return vf.read_text().strip() if vf.exists() else None

    def remote_sha(self) -> str:
        if self.is_git_checkout():
            # use git's own credentials — works on private repos without a token
            subprocess.run(["git", "fetch", "--quiet", "origin", "main"],
                           cwd=self.root, capture_output=True, timeout=60, check=True)
            out = subprocess.run(["git", "rev-parse", "origin/main"], cwd=self.root,
                                 capture_output=True, text=True, timeout=10, check=True)
            return out.stdout.strip()
        data = self._api(f"https://api.github.com/repos/{self.s.update_repo}/commits/main")
        return data["sha"]

    def check(self) -> dict:
        out = {"repo": self.s.update_repo, "local": self.local_sha(),
               "updating": self.updating, "message": self.message,
               "update_available": False, "error": None}
        try:
            out["remote"] = self.remote_sha()
            out["update_available"] = (out["local"] or "")[:12] != out["remote"][:12]
        except urllib.error.HTTPError as e:
            out["remote"] = None
            out["error"] = (
                f"GitHub says {e.code} for {self.s.update_repo} — for a private "
                "repo, add a read-only GitHub token under ⚙ Settings → Updates."
                if e.code in (401, 403, 404) else f"update check failed: {e}"
            )
        except Exception as e:
            out["remote"] = None
            out["error"] = f"update check failed: {e}"
        return out

    # ------------------------------------------------------------- update --
    def update(self) -> dict:
        if self.updating:
            return {**self.check(), "message": "update already in progress"}
        self.updating = True
        self.message = "starting update…"
        threading.Thread(target=self._worker, name="updater", daemon=True).start()
        return {**self.check(), "message": self.message}

    def _worker(self) -> None:
        try:
            if self.is_git_checkout():
                self.message = "running git pull…"
                out = subprocess.run(["git", "pull", "--ff-only"], cwd=self.root,
                                     capture_output=True, text=True, timeout=120)
                if out.returncode != 0:
                    raise RuntimeError(f"git pull failed: {out.stderr.strip()[:300]}")
                log.info("updater: %s", out.stdout.strip())
            else:
                self.message = "downloading latest code…"
                sha = self.remote_sha()
                url = f"https://api.github.com/repos/{self.s.update_repo}/zipball/main"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "stock-trader-updater",
                    **({"Authorization": f"Bearer {self.s.github_token}"}
                       if self.s.github_token else {}),
                })
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        shutil.copyfileobj(resp, tmp)
                    zip_path = Path(tmp.name)
                self.message = "applying update…"
                n = _extract_over(zip_path, self.root)
                zip_path.unlink(missing_ok=True)
                (self.root / "VERSION").write_text(sha + "\n")
                log.info("updater: applied %d files at commit %s", n, sha[:12])

            self.message = "installing requirements…"
            subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            str(self.root / "requirements.txt"), "--quiet"],
                           check=False, timeout=900)
            self.message = "restarting server…"
            log.info("updater: update applied — restarting")
            threading.Timer(1.0, self._restart).start()
        except Exception as e:
            log.exception("update failed")
            self.message = f"update failed: {e}"
            self.updating = False

    def _restart(self) -> None:  # pragma: no cover — kills the process
        if sys.platform == "win32":
            # start_bot.bat restarts us when we exit with RESTART_EXIT_CODE
            os._exit(RESTART_EXIT_CODE)
        os.execv(sys.executable, [sys.executable, *sys.argv])
