"""Lyrebird launcher (this is what Lyrebird.exe runs).

First run: downloads Python, PyTorch and Chatterbox into %LOCALAPPDATA%\\Lyrebird with a progress window.
Later runs: starts the app straight away. A release with a changed requirements.txt re-runs setup.
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

CHATTERBOX = "chatterbox-tts==0.1.7"  # installed with --no-deps: it pins torch 2.6, which lacks RTX 50-series support
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # bundled files when frozen
REQS = HERE / "requirements.txt"
HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lyrebird"
ENV = HOME / "env"
CACHE = HOME / "uv-cache"
MARKER = ENV / "lyrebird-setup.txt"
LOG = HOME / "setup.log"
UV_ENV = {**os.environ, "UV_CACHE_DIR": str(CACHE), "UV_PYTHON_INSTALL_DIR": str(HOME / "python")}


def setup_id():
    # Normalise line endings: git gives CI (CRLF) and source checkouts (LF) different bytes for the same file.
    text = "\n".join(REQS.read_text(encoding="utf-8").splitlines())
    return hashlib.sha256((text + CHATTERBOX).encode()).hexdigest()


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:  # file vanished mid-walk (uv renames temp files)
                pass
    return total


def launch(root):
    """Start the app, and report it if it dies during startup."""
    root.withdraw()
    # python.exe, not pythonw.exe: a venv's pythonw.exe hands off to the console interpreter anyway.
    # CREATE_NO_WINDOW hides the console; the app's output goes to the log.
    log = open(HOME / "lyrebird.log", "w", encoding="utf-8")
    app = subprocess.Popen([str(ENV / "Scripts" / "python.exe"), str(HERE / "lyrebird.py")],
                           stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
    deadline = time.time() + 15

    def check():
        code = app.poll()
        if code:
            messagebox.showerror("Lyrebird", f"Lyrebird failed to start (exit code {code}).\n\nDetails: {HOME / 'lyrebird.log'}")
        if code is not None or time.time() > deadline:
            root.destroy()
        else:
            root.after(500, check)

    check()


class Setup:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.started = time.time()
        self.downloaded = 0

        root.title("Lyrebird setup")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both")
        ttk.Label(frame, text="Setting up Lyrebird", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frame, justify="left", text="First run only: downloading Python, PyTorch and Chatterbox "
                  "(about 3 GB with an NVIDIA GPU;\nneeds about 6 GB of disk space). Later launches start straight away.").pack(anchor="w", pady=(4, 12))
        self.step = tk.StringVar(value="Starting...")
        ttk.Label(frame, textvariable=self.step).pack(anchor="w")
        bar = ttk.Progressbar(frame, mode="indeterminate", length=460)
        bar.pack(fill="x", pady=6)
        bar.start(12)
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail, foreground="gray", width=72).pack(anchor="w")
        self.stats = tk.StringVar()
        ttk.Label(frame, textvariable=self.stats, foreground="gray").pack(anchor="w", pady=(4, 0))

        threading.Thread(target=self.measure, daemon=True).start()
        threading.Thread(target=self.work, daemon=True).start()
        self.tick()

    def ui(self, fn, *args):
        self.root.after(0, fn, *args)

    def measure(self):  # walking the cache takes a moment, so keep it off the Tk thread
        base = dir_size(CACHE)  # a re-run (update) starts with a full cache
        while True:
            self.downloaded = dir_size(CACHE) - base
            time.sleep(1)

    def tick(self):
        mins, secs = divmod(int(time.time() - self.started), 60)
        self.stats.set(f"{self.downloaded / 1e9:.2f} GB on disk so far  ·  {mins}:{secs:02d} elapsed")
        self.root.after(500, self.tick)

    def run(self, step, *args):
        self.ui(self.step.set, step)
        self.output = []
        cmd = [HERE / "uv.exe" if (HERE / "uv.exe").exists() else shutil.which("uv") or "uv", *args]
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"\n> {' '.join(map(str, cmd))}\n")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", env=UV_ENV,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
            for line in self.proc.stdout:
                log.write(line)
                self.output.append(line)
                if line.strip():
                    self.ui(self.detail.set, line.strip()[:100])
            if self.proc.wait():
                raise RuntimeError(f"{step} failed.")

    def work(self):
        py = ("--python", ENV)
        try:
            if not (ENV / "Scripts" / "python.exe").exists():  # an update reuses it; it may be in use by an open Lyrebird
                self.run("Step 1 of 3: Downloading Python", "venv", "--managed-python", "--python", "3.12", ENV)
            try:
                self.run("Step 2 of 3: Downloading PyTorch and libraries", "pip", "install", *py, "-r", REQS, "--torch-backend", "auto")
            except RuntimeError:
                # Only when no CUDA build fits (e.g. an old NVIDIA driver). A network error must not
                # quietly leave a GPU machine on the CPU build for good, so that goes to fail() instead.
                if not any("No solution found" in line for line in self.output):
                    raise
                self.run("Step 2 of 3: Downloading PyTorch (CPU version)", "pip", "install", *py, "-r", REQS, "--torch-backend", "cpu")
            self.run("Step 3 of 3: Installing Chatterbox", "pip", "install", *py, "--no-deps", CHATTERBOX)
            MARKER.write_text(setup_id())
            self.ui(launch, self.root)
        except Exception as e:
            self.ui(self.fail, e)

    def fail(self, error):
        reason = next((line.strip() for line in reversed(getattr(self, "output", []))
                       if line.strip().startswith(("error:", "Caused by:"))), "")
        if "Access is denied" in reason or "being used by another process" in reason:
            hint = "Lyrebird may already be open. Close it, then start Lyrebird again."
        else:
            hint = "Check your internet connection and start Lyrebird again; setup resumes where it stopped."
        messagebox.showerror("Lyrebird setup", f"{error}\n{reason}\n\n{hint}\n\nDetails: {LOG}")
        self.root.destroy()

    def cancel(self):
        if messagebox.askyesno("Lyrebird setup", "Stop setup? It resumes where it left off next time."):
            if self.proc:
                self.proc.kill()
            os._exit(1)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    HOME.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    for icon in (HERE / "lyrebird.ico", HERE / "brand" / "lyrebird.ico"):  # release bundle, then source tree
        if icon.exists():
            root.iconbitmap(default=str(icon))
            break
    if MARKER.exists() and MARKER.read_text() == setup_id():
        launch(root)
    else:
        Setup(root)
    root.mainloop()


if __name__ == "__main__":
    main()
