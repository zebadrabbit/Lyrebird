"""Lyrebird - a small Windows GUI for Chatterbox TTS voice cloning.

Record (or import) a short voice sample, type some text, hit Render, get a .wav.
"""
import ctypes
import os
import re
import shutil
import sys
import textwrap
import threading
import time
import tkinter as tk
import winsound
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lyrebird"

# Under pythonw.exe there is no console, so sys.stdout/stderr are None and tqdm/print inside
# chatterbox would crash. Send them to a log file instead. (The launcher already redirects output.)
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
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg")
AUDIO_TYPES = [("Audio", " ".join(f"*{e}" for e in AUDIO_EXTS))]
BUILTIN_VOICE = "(Built-in voice)"
BUILTIN_NAME = "Built-in"  # speaker name for the built-in voice in dialogue scripts
DEFAULT_MIC = "(Windows default microphone)"
# Paralinguistic tags the Turbo model's tokenizer knows (added_tokens.json in ResembleAI/chatterbox-turbo).
TURBO_TAGS = (
    "[laugh]", "[chuckle]", "[sigh]", "[gasp]", "[cough]", "[clear throat]", "[sniff]", "[groan]", "[shush]",
    "[whispering]", "[angry]", "[happy]", "[sarcastic]", "[surprised]", "[fear]", "[crying]", "[dramatic]",
    "[narration]", "[advertisement]",
)
TAG_RE = re.compile("|".join(map(re.escape, TURBO_TAGS)))


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


def voices_dir():
    path = documents_dir() / "Lyrebird" / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_files(name):
    """Every sample saved under this voice name, whatever its extension (Windows names ignore case)."""
    return [p for p in voices_dir().iterdir() if p.stem.lower() == name.lower() and p.suffix.lower() in AUDIO_EXTS]


def drop_older_versions(path):
    """After saving a voice, delete samples with the same name but another extension, which would shadow it."""
    for p in voice_files(path.stem):
        if p.name.lower() != path.name.lower():
            p.unlink()


def input_devices():
    """{name: device index} of microphones. Prefers WASAPI: full device names, one entry per device."""
    import sounddevice as sd

    apis = sd.query_hostapis()
    api = next((a for a in apis if "WASAPI" in a["name"]), apis[sd.default.hostapi])
    return {sd.query_devices(i)["name"]: i for i in api["devices"] if sd.query_devices(i)["max_input_channels"] > 0}


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size  # lstat: don't count HF cache symlinks twice
            except OSError:  # file vanished mid-walk
                pass
    return total


def strip_tags(text):
    """Remove Turbo sound tags without gluing the neighbouring words together."""
    return re.sub(r"[ \t]{2,}", " ", TAG_RE.sub(" ", text)).strip()


def split_speakers(text, names):
    """Split a dialogue script into [(speaker, text)].

    A line starting with "Name:" switches to that speaker, but only when Name is in `names`
    (case-insensitive), so ordinary text such as "Note: ..." is left alone. Text before the
    first speaker line gets speaker None.
    """
    lookup = {n.lower(): n for n in names}
    segments, speaker = [], None
    for line in text.splitlines():
        m = re.match(r"\s*([^:]{1,60}):(.*)", line)
        if m and m.group(1).strip().lower() in lookup:
            speaker, line = lookup[m.group(1).strip().lower()], m.group(2)
        if segments and segments[-1][0] == speaker:
            segments[-1][1].append(line)
        else:
            segments.append((speaker, [line]))
    return [(who, "\n".join(lines).strip()) for who, lines in segments if "".join(lines).strip()]


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
        from huggingface_hub.constants import HF_HUB_CACHE

        loading = threading.Event()

        def report_download():  # from_pretrained's own progress bars only reach the log file
            start = dir_size(HF_HUB_CACHE)
            while not loading.wait(1):
                got = dir_size(HF_HUB_CACHE) - start
                if got > 1e6:
                    status(f"Downloading {kind} model (one-time, 3-4 GB): {got / 1e9:.2f} GB so far...")

        threading.Thread(target=report_download, daemon=True).start()
        try:
            self.model = Model.from_pretrained(device=device)
        finally:
            loading.set()
        status(f"Loaded {kind} model on {device.upper()}.")
        self.kind, self.default_conds = kind, self.model.conds  # built-in voice, restored when no reference is set
        return self.model

    def render(self, kind, segments, lang, exaggeration, cfg_weight, temperature, seed, out_path, status):
        """segments: [(voice sample path or None for the built-in voice, text)], spoken in order."""
        import numpy as np
        import soundfile as sf
        import torch

        model = self.load(kind, status)
        if seed:
            torch.manual_seed(seed)
        conds = {None: self.default_conds}  # each voice is analysed once per render

        def use_voice(ref):
            if ref not in conds:
                status(f"Analysing voice {Path(ref).stem}...")
                model.prepare_conditionals(ref, exaggeration=exaggeration)
                conds[ref] = model.conds
            if conds[ref] is None:
                raise RuntimeError("This model has no built-in voice. Record or import a voice first.")
            model.conds = conds[ref]

        kwargs = {"temperature": temperature}
        if kind != "turbo":  # Turbo ignores these (and logs a warning if they're set)
            # ponytail: chatterbox 0.1.7 Standard crashes at cfg_weight=0; 1e-3 is effectively no guidance
            kwargs.update(exaggeration=exaggeration, cfg_weight=max(cfg_weight, 1e-3) if kind == "standard" else cfg_weight)
        if kind == "multilingual":
            kwargs["language_id"] = lang

        limit = CJK_CHUNK_CHARS if kind == "multilingual" and lang in ("zh", "ja", "ko") else CHUNK_CHARS
        if kind != "turbo":  # only Turbo knows the sound tags; other models would read "[laugh]" out as a word
            segments = [(ref, strip_tags(text)) for ref, text in segments]
        chunks = [(ref, chunk) for ref, text in segments for chunk in chunk_text(text, limit)]
        if not chunks:
            raise RuntimeError("There's nothing to say: the text only has sound tags, which this model doesn't use.")
        gap = np.zeros(int(model.sr * GAP_SECONDS), dtype=np.float32)
        parts = []
        for i, (ref, chunk) in enumerate(chunks, 1):
            use_voice(ref)
            status(f"Rendering chunk {i}/{len(chunks)}" + (f" ({Path(ref).stem})" if ref else "") + "...")
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
        self.output = None
        self.buttons = []
        self.busy = False

        root.title("Lyrebird")
        root.minsize(620, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        # Voice library + microphone
        voice = ttk.LabelFrame(root, text="1. Voice")
        voice.grid(row=0, column=0, sticky="ew", **pad)
        voice.columnconfigure(1, weight=1)
        ttk.Label(voice, text="Voice").grid(row=0, column=0, sticky="w", **pad)
        self.voice_var = tk.StringVar(value=BUILTIN_VOICE)
        self.voice_box = ttk.Combobox(voice, textvariable=self.voice_var, state="readonly", postcommand=self.refresh_voices)
        self.voice_box.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(voice, text="Play", command=lambda: self.play(self.ref)).grid(row=0, column=2, **pad)
        ttk.Button(voice, text="Folder", command=lambda: os.startfile(voices_dir())).grid(row=0, column=3, **pad)
        ttk.Label(voice, text="Microphone").grid(row=1, column=0, sticky="w", **pad)
        self.mic_var = tk.StringVar(value=DEFAULT_MIC)
        self.mic_box = ttk.Combobox(voice, textvariable=self.mic_var, state="readonly", postcommand=self.refresh_mics)
        self.mic_box.grid(row=1, column=1, sticky="ew", **pad)
        self._button(voice, f"Record ({RECORD_SECONDS}s)", self.record).grid(row=1, column=2, **pad)
        self._button(voice, "Import...", self.import_audio).grid(row=1, column=3, **pad)
        ttk.Label(voice, foreground="gray", text="Record or import 5-20 s of clean speech to add a voice. "
                  "Voices are saved by name and stay in the list.").grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        self.mics = {}
        self.refresh_voices()
        self.refresh_mics(reinit=False)

        # Text
        text_frame = ttk.LabelFrame(root, text='2. Text to speak (dialogue: start a line with a voice name, e.g. "Alice: Hi!")')
        text_frame.grid(row=1, column=0, sticky="nsew", **pad)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, height=8, wrap="word", undo=True, font=("Segoe UI", 10))
        self.text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(text_frame, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 8))
        self.text.configure(yscrollcommand=scroll.set)
        self.text.tag_configure("speaker", font=("Segoe UI", 10, "bold"), foreground="#1a5fb4")
        self.text.tag_configure("tag", background="#d7ebff", foreground="#0b4f8a")
        self.text.tag_configure("tag_off", background="#fff0c2", foreground="#8a5a00")
        self.text.tag_configure("bad_tag", underline=True, foreground="#c01c28")
        self.text.bind("<<Modified>>", self.on_text_modified)
        tags = ttk.Frame(text_frame)
        tags.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        insert = ttk.Menubutton(tags, text="Insert tag")
        insert["menu"] = menu = tk.Menu(insert, tearoff=False)
        for tag in TURBO_TAGS:
            menu.add_command(label=tag, command=lambda t=tag: (self.text.insert("insert", t), self.text.focus_set()))
        insert.pack(side="left")
        ttk.Label(tags, foreground="gray", text="Tags like [laugh] only work with the Turbo model "
                  "(amber = ignored by this model, red = unknown tag).").pack(side="left", padx=8)

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
        self.progress = ttk.Progressbar(out, mode="determinate")
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
        self.highlight()

    def on_text_modified(self, _event):
        if self.text.edit_modified():  # clearing the flag re-fires <<Modified>>; skip that second call
            self.text.edit_modified(False)
            self.highlight()

    def highlight(self):
        """Colour Turbo tags and dialogue speaker names in the text box."""
        if not hasattr(self, "text"):  # called while the window is still being built
            return
        for style in ("speaker", "tag", "tag_off", "bad_tag"):
            self.text.tag_remove(style, "1.0", "end")
        known = "tag" if MODELS[self.model_var.get()] == "turbo" else "tag_off"
        names = {n.lower() for n in [*self.voices, BUILTIN_NAME]}
        for ln, line in enumerate(self.text.get("1.0", "end-1c").split("\n"), 1):  # "line.col" indices are cheap for Tk
            for m in re.finditer(r"\[[^\[\]]{1,30}\]", line):
                self.text.tag_add(known if m.group() in TURBO_TAGS else "bad_tag", f"{ln}.{m.start()}", f"{ln}.{m.end()}")
            m = re.match(r"\s*([^:]{1,60}):", line)  # same rule as split_speakers
            if m and m.group(1).strip().lower() in names:
                self.text.tag_add("speaker", f"{ln}.{m.start(1)}", f"{ln}.{m.end()}")

    @property
    def ref(self):
        """Path of the selected voice sample, or None for the model's built-in voice."""
        return self.voices.get(self.voice_var.get())

    def refresh_voices(self, select=None):
        self.voices = {p.stem: str(p) for p in sorted(voices_dir().iterdir()) if p.suffix.lower() in AUDIO_EXTS}
        self.voice_box.configure(values=[BUILTIN_VOICE, *self.voices])
        if select or self.voice_var.get() not in self.voices:
            self.voice_var.set(select or BUILTIN_VOICE)
        self.highlight()  # speaker names may have changed

    def refresh_mics(self, reinit=True):
        """List microphones, re-scanning so newly plugged-in devices show up."""
        if self.busy:  # re-initialising PortAudio mid-recording would kill the recording
            return
        try:
            import sounddevice as sd

            if reinit:
                sd._terminate()
                sd._initialize()
            self.mics = input_devices()
        except Exception as e:  # no audio subsystem: still usable with imported voices
            print(f"Microphone scan failed: {e}")
            self.mics = {}
        self.mic_box.configure(values=[DEFAULT_MIC, *self.mics])
        if self.mic_var.get() not in self.mics:
            self.mic_var.set(DEFAULT_MIC)

    def ask_voice_path(self, default, suffix):
        """Ask for a voice name; returns its path in the voice library, or None if cancelled."""
        name = simpledialog.askstring("Lyrebird", "Name this voice:", initialvalue=default, parent=self.root)
        name = re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip()).strip(". ")
        if not name:
            return None
        if voice_files(name) and not messagebox.askyesno("Lyrebird", f'Replace the existing voice "{name}"?'):
            return None
        return voices_dir() / f"{name}{suffix}"

    # --- background work -----------------------------------------------------
    def ui(self, fn, *args):
        """Run fn on the Tk thread."""
        self.root.after(0, fn, *args)

    def run_bg(self, work):
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])
        self.progress.configure(mode="indeterminate")
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
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)  # an idle indeterminate bar leaves a stray block
        for b in self.buttons:
            b.state(["!disabled"])

    def out_folder(self):
        folder = Path(self.out_dir.get().strip() or documents_dir() / "Lyrebird").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # --- actions -------------------------------------------------------------
    def record(self):
        path = self.ask_voice_path(f"My voice {datetime.now():%Y-%m-%d %H%M}", ".wav")
        if not path:
            return
        device = self.mics.get(self.mic_var.get())  # None = Windows default input

        def work():
            import numpy as np
            import sounddevice as sd
            import soundfile as sf

            info = sd.query_devices(device, "input")
            sr = int(info["default_samplerate"])  # the device's native format; WASAPI rejects anything else
            audio = sd.rec(RECORD_SECONDS * sr, samplerate=sr, channels=info["max_input_channels"],
                           device=device, dtype="float32")
            for left in range(RECORD_SECONDS, 0, -1):
                self.ui(self.status.set, f"Recording from {info['name']}... {left}s left - speak now.")
                time.sleep(1)
            sd.wait()
            sf.write(path, audio[:, np.abs(audio).max(axis=0).argmax()], sr)  # keep the loudest channel
            drop_older_versions(path)
            self.ui(self.refresh_voices, path.stem)
            self.ui(self.status.set, f'Saved voice "{path.stem}".')

        self.run_bg(work)

    def import_audio(self):
        src = filedialog.askopenfilename(title="Choose a voice sample", filetypes=AUDIO_TYPES)
        if not src:
            return
        path = self.ask_voice_path(Path(src).stem, Path(src).suffix.lower())
        if path:
            if not (path.exists() and os.path.samefile(src, path)):  # re-importing a library file is a no-op
                shutil.copyfile(src, path)
            drop_older_versions(path)
            self.refresh_voices(path.stem)
            self.status.set(f'Added voice "{path.stem}".')

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
        voices = {**self.voices, BUILTIN_NAME: None}
        segments = [(voices[who] if who else self.ref, part) for who, part in split_speakers(text, voices)]
        if not segments:  # e.g. only "Alice:" lines so far; don't load a model for nothing
            messagebox.showwarning("Lyrebird", "Type some text to speak first.")
            return
        args = dict(
            kind=kind, segments=segments, lang=self.lang_var.get().split(" ")[0],
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
