"""Lyrebird - a small Windows GUI for Chatterbox TTS voice cloning.

Record (or import) a short voice sample, type some text, hit Render, get a .wav.
"""
import ctypes
import os
import re
import sys
import textwrap
import threading
import time
import tkinter as tk
import winsound
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lyrebird"

# A --windowed PyInstaller build has no console, so sys.stdout/stderr are None and
# tqdm/print inside chatterbox would crash. Send them to a log file instead.
if sys.stdout is None or sys.stderr is None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(APP_DIR / "lyrebird.log", "w", encoding="utf-8", buffering=1)

RECORD_SECONDS = 10
CHUNK_CHARS = 300  # long text is rendered sentence-by-sentence in chunks of about this size
CJK_CHUNK_CHARS = 100  # CJK packs far more speech per character; each chunk is capped at ~40 s of audio
GAP_SECONDS = 0.2  # silence inserted between chunks

MODELS = {
    "Standard (English)": "standard",
    "Turbo (English, fast)": "turbo",
    "Multilingual (23 languages)": "multilingual",
}
# Mirrors chatterbox.mtl_tts.SUPPORTED_LANGUAGES; duplicated so the window opens without importing torch.
LANGUAGES = {
    "ar": "Arabic", "da": "Danish", "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "ms": "Malay", "nl": "Dutch", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese",
    "ru": "Russian", "sv": "Swedish", "sw": "Swahili", "tr": "Turkish", "zh": "Chinese",
}
AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.ogg"), ("All files", "*.*")]


def chunk_text(text, limit=CHUNK_CHARS):
    """Split text into sentence-aligned chunks of at most `limit` chars (over-long sentences are hard-wrapped)."""
    sentences = re.split(r"(?<=[.!?।؟][\"'”’)])\s+|(?<=[.!?।؟])\s+|(?<=[。！？])\s*|\s*\n\s*", text.strip())
    chunks, cur = [], ""
    for s in (piece for s in sentences for piece in textwrap.wrap(s, limit)):
        if cur and len(cur) + 1 + len(s) > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    return chunks + [cur] if cur else chunks


def documents_dir():
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL; follows OneDrive redirection
    return Path(buf.value) if buf.value else Path.home()


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class Engine:
    """Holds one loaded Chatterbox model at a time (switching models frees the previous one)."""

    def __init__(self):
        self.kind = self.model = self.default_conds = None

    def load(self, kind, status):
        if kind == self.kind:
            return self.model
        import torch

        self.kind = self.model = self.default_conds = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        status(f"Loading {kind} model on {device.upper()} (the first run downloads it, several GB)...")
        if kind == "turbo":
            from chatterbox.tts_turbo import ChatterboxTurboTTS as Model
        elif kind == "multilingual":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS as Model
        else:
            from chatterbox.tts import ChatterboxTTS as Model
        self.model = Model.from_pretrained(device=device)
        self.kind, self.default_conds = kind, self.model.conds  # built-in voice, restored when no reference is set
        return self.model

    def render(self, kind, text, ref, lang, exaggeration, cfg_weight, temperature, seed, out_path, status):
        import numpy as np
        import soundfile as sf
        import torch

        model = self.load(kind, status)
        if seed:
            torch.manual_seed(seed)
        if ref:
            status("Analysing voice reference...")
            model.prepare_conditionals(ref, exaggeration=exaggeration)
        elif self.default_conds is None:
            raise RuntimeError("This model has no built-in voice. Record or import a voice reference.")
        else:
            model.conds = self.default_conds

        kwargs = {"temperature": temperature}
        if kind != "turbo":  # Turbo ignores these (and logs a warning if they're set)
            # ponytail: chatterbox 0.1.7 Standard crashes at cfg_weight=0; 1e-3 is effectively no guidance
            kwargs.update(exaggeration=exaggeration, cfg_weight=max(cfg_weight, 1e-3) if kind == "standard" else cfg_weight)
        if kind == "multilingual":
            kwargs["language_id"] = lang

        chunks = chunk_text(text, CJK_CHUNK_CHARS if kind == "multilingual" and lang in ("zh", "ja", "ko") else CHUNK_CHARS)
        gap = np.zeros(int(model.sr * GAP_SECONDS), dtype=np.float32)
        parts = []
        for i, chunk in enumerate(chunks, 1):
            status(f"Rendering chunk {i}/{len(chunks)}...")
            try:
                wav = model.generate(chunk, **kwargs).squeeze(0).detach().cpu().numpy().astype(np.float32)
            finally:
                if kind == "multilingual":  # ponytail: chatterbox 0.1.7 leaks an attention hook per generate
                    for layer in model.t3.tfmr.layers:
                        layer.self_attn._forward_hooks.clear()
            parts += [wav, gap]
        sf.write(out_path, np.concatenate(parts[:-1]), model.sr)


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.ref = None
        self.output = None
        self.buttons = []

        root.title("Lyrebird")
        root.minsize(620, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        # Voice reference
        voice = ttk.LabelFrame(root, text="1. Voice to clone (optional, 5-20 s of clean speech)")
        voice.grid(row=0, column=0, sticky="ew", **pad)
        voice.columnconfigure(4, weight=1)
        self._button(voice, f"Record ({RECORD_SECONDS}s)", self.record).grid(row=0, column=0, **pad)
        self._button(voice, "Import audio...", self.import_audio).grid(row=0, column=1, **pad)
        ttk.Button(voice, text="Play", command=lambda: self.play(self.ref)).grid(row=0, column=2, **pad)
        ttk.Button(voice, text="Clear", command=lambda: self.set_ref(None)).grid(row=0, column=3, **pad)
        self.ref_label = ttk.Label(voice, foreground="gray")
        self.ref_label.grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        self.set_ref(None)

        # Text
        text_frame = ttk.LabelFrame(root, text="2. Text to speak")
        text_frame.grid(row=1, column=0, sticky="nsew", **pad)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, height=8, wrap="word", undo=True, font=("Segoe UI", 10))
        self.text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(text_frame, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 8))
        self.text.configure(yscrollcommand=scroll.set)

        # Options
        opts = ttk.LabelFrame(root, text="3. Options")
        opts.grid(row=2, column=0, sticky="ew", **pad)
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Model").grid(row=0, column=0, sticky="w", **pad)
        self.model_var = tk.StringVar(value=next(iter(MODELS)))
        model_box = ttk.Combobox(opts, textvariable=self.model_var, values=list(MODELS), state="readonly")
        model_box.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        model_box.bind("<<ComboboxSelected>>", lambda e: self.sync_options())
        ttk.Label(opts, text="Language").grid(row=1, column=0, sticky="w", **pad)
        self.lang_var = tk.StringVar(value="en - English")
        self.lang_box = ttk.Combobox(opts, textvariable=self.lang_var, state="readonly",
                                     values=[f"{k} - {v}" for k, v in LANGUAGES.items()])
        self.lang_box.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        self.exaggeration = self._slider(opts, 2, "Exaggeration", 0.25, 2.0, 0.5)
        self.cfg_weight = self._slider(opts, 3, "CFG / pace", 0.0, 1.0, 0.5)
        self.temperature = self._slider(opts, 4, "Temperature", 0.05, 2.0, 0.8)
        ttk.Label(opts, text="Seed (0 = random)").grid(row=5, column=0, sticky="w", **pad)
        self.seed = tk.IntVar(value=0)
        ttk.Spinbox(opts, from_=0, to=2**31 - 1, textvariable=self.seed, width=12).grid(row=5, column=1, sticky="w", **pad)

        # Output
        out = ttk.LabelFrame(root, text="4. Render")
        out.grid(row=3, column=0, sticky="ew", **pad)
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Save to").grid(row=0, column=0, sticky="w", **pad)
        self.out_dir = tk.StringVar(value=str(documents_dir() / "Lyrebird"))
        ttk.Entry(out, textvariable=self.out_dir).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(out, text="Browse...", command=self.browse_out).grid(row=0, column=2, **pad)
        row = ttk.Frame(out)
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._button(row, "Render", self.render).pack(side="left", **pad)
        ttk.Button(row, text="Play output", command=lambda: self.play(self.output)).pack(side="left", **pad)
        ttk.Button(row, text="Open folder", command=self.open_folder).pack(side="left", **pad)
        self.progress = ttk.Progressbar(out, mode="indeterminate")
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(out, textvariable=self.status, wraplength=580).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        self.sync_options()

    # --- widgets -------------------------------------------------------------
    def _button(self, parent, text, command):
        b = ttk.Button(parent, text=text, command=command)
        self.buttons.append(b)  # disabled while busy
        return b

    def _slider(self, parent, row, label, lo, hi, default):
        var = tk.DoubleVar(value=default)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=2)
        scale = ttk.Scale(parent, from_=lo, to=hi, variable=var)
        scale.grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        value = ttk.Label(parent, width=5)
        value.grid(row=row, column=2, padx=8)
        var.trace_add("write", lambda *_: value.configure(text=f"{var.get():.2f}"))
        var.set(default)
        var.scale = scale
        return var

    def sync_options(self):
        kind = MODELS[self.model_var.get()]
        for var in (self.exaggeration, self.cfg_weight):
            var.scale.state(["disabled"] if kind == "turbo" else ["!disabled"])
        self.lang_box.state(["!disabled"] if kind == "multilingual" else ["disabled"])

    def set_ref(self, path):
        self.ref = path
        self.ref_label.configure(text=f"Reference: {path}" if path else "No reference - the model's built-in voice will be used.")

    # --- background work -----------------------------------------------------
    def ui(self, fn, *args):
        """Run fn on the Tk thread."""
        self.root.after(0, fn, *args)

    def run_bg(self, work):
        for b in self.buttons:
            b.state(["disabled"])
        self.progress.start(12)

        def target():
            try:
                work()
            except Exception as e:  # show every failure to the user instead of dying silently
                import traceback
                traceback.print_exc()
                self.ui(self.status.set, "Failed.")
                self.ui(messagebox.showerror, "Lyrebird", f"{type(e).__name__}: {e}")
            finally:
                self.ui(self.done)

        threading.Thread(target=target, daemon=True).start()

    def done(self):
        self.progress.stop()
        for b in self.buttons:
            b.state(["!disabled"])

    def out_folder(self):
        folder = Path(self.out_dir.get().strip() or documents_dir() / "Lyrebird").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # --- actions -------------------------------------------------------------
    def record(self):
        path = self.out_folder() / f"reference_{stamp()}.wav"

        def work():
            import sounddevice as sd
            import soundfile as sf

            sr = int(sd.query_devices(kind="input")["default_samplerate"])
            audio = sd.rec(RECORD_SECONDS * sr, samplerate=sr, channels=1, dtype="float32")
            for left in range(RECORD_SECONDS, 0, -1):
                self.ui(self.status.set, f"Recording... {left}s left - speak now.")
                time.sleep(1)
            sd.wait()
            sf.write(path, audio, sr)
            self.ui(self.set_ref, str(path))
            self.ui(self.status.set, f"Recorded {path.name}.")

        self.run_bg(work)

    def import_audio(self):
        path = filedialog.askopenfilename(title="Choose a voice sample", filetypes=AUDIO_TYPES)
        if path:
            self.set_ref(path)

    def browse_out(self):
        folder = filedialog.askdirectory(initialdir=self.out_dir.get())
        if folder:
            self.out_dir.set(folder)

    def render(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Lyrebird", "Type some text to speak first.")
            return
        kind = MODELS[self.model_var.get()]
        try:
            seed = int(self.seed.get())
        except (tk.TclError, ValueError):
            seed = 0
        args = dict(
            kind=kind, text=text, ref=self.ref, lang=self.lang_var.get().split(" ")[0],
            exaggeration=self.exaggeration.get(), cfg_weight=self.cfg_weight.get(),
            temperature=self.temperature.get(), seed=seed,
            out_path=self.out_folder() / f"lyrebird_{stamp()}.wav",
        )

        def work():
            start = time.time()
            self.engine.render(**args, status=lambda s: self.ui(self.status.set, s))
            self.output = str(args["out_path"])
            self.ui(self.status.set, f"Saved {self.output} ({time.time() - start:.0f}s).")

        self.run_bg(work)

    def play(self, path):
        if not path:
            return
        if path.lower().endswith(".wav"):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            os.startfile(path)  # winsound only plays WAV; hand anything else to the default player

    def open_folder(self):
        os.startfile(self.out_folder())


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()

    def report(exc, val, tb):  # errors in Tk callbacks (bad output folder, unplayable file) go to a dialog
        import traceback
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("Lyrebird", f"{exc.__name__}: {val}")

    root.report_callback_exception = report
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
