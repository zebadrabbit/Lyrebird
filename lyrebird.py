"""Lyrebird - a small Windows GUI for Chatterbox TTS voice cloning.

Record (or import) a short voice sample, type some text, hit Render, get an audio file.
"""
import ctypes
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
import tkinter as tk
import winsound
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

VERSION = "0.2.0"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lyrebird"
SETTINGS_FILE = APP_DIR / "settings.json"

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
# key: (label, low, high, default). Shared by the main sliders and the per-voice settings dialog.
SLIDERS = {
    "exaggeration": ("Exaggeration", 0.25, 2.0, 0.5),
    "cfg_weight": ("CFG / pace", 0.0, 1.0, 0.5),
    "temperature": ("Temperature", 0.05, 2.0, 0.8),
}
FORMATS = {  # label: (soundfile format, subtype, extension)
    "WAV 16-bit": ("WAV", "PCM_16", ".wav"),
    "WAV 32-bit float": ("WAV", "FLOAT", ".wav"),
    "FLAC 24-bit": ("FLAC", "PCM_24", ".flac"),
}
RATES = {"24 kHz (model native)": None, "44.1 kHz": 44100, "48 kHz": 48000}
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg")
AUDIO_TYPES = [("Audio", " ".join(f"*{e}" for e in AUDIO_EXTS))]
BUILTIN_VOICE = "(Built-in voice)"
BUILTIN_NAME = "Built-in"  # speaker name for the built-in voice in dialogue scripts
DEFAULT_MIC = "(Windows default microphone)"
LOOPBACK_PREFIX = "What you hear: "  # records whatever this PC is playing on that output device
LOOPBACK_RATE = 48000
# Paralinguistic tags the Turbo model's tokenizer knows (added_tokens.json in ResembleAI/chatterbox-turbo).
TURBO_TAGS = (
    "[laugh]", "[chuckle]", "[sigh]", "[gasp]", "[cough]", "[clear throat]", "[sniff]", "[groan]", "[shush]",
    "[whispering]", "[angry]", "[happy]", "[sarcastic]", "[surprised]", "[fear]", "[crying]", "[dramatic]",
    "[narration]", "[advertisement]",
)
TAG_RE = re.compile("|".join(map(re.escape, TURBO_TAGS)))
# Brand colours (brand/tokens.json, light theme).
INK, INK_MUTED, PLUME, PLUME_SOFT, FERN, SURFACE, LINE, TAG_OFF = (
    "#18201c", "#56615a", "#a8471f", "#f6e3d6", "#1f3d31", "#fbfbf8", "#d9ddd4", "#8a5a00")


class Stopped(Exception):
    """Raised inside a render when the user clicks Stop."""


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


def speech_bounds(audio, sr, pad=0.15):
    """(start, end) sample indices around the non-silent part of a mono recording, with a little padding."""
    import numpy as np

    hop = max(1, sr // 100)  # 10 ms frames
    frames = len(audio) // hop
    if frames == 0:
        return 0, len(audio)
    rms = np.sqrt(np.mean(audio[:frames * hop].reshape(frames, hop) ** 2, axis=1))
    loud = np.flatnonzero(rms > rms.max() * 0.05)  # within ~26 dB of the loudest frame
    if rms.max() == 0 or len(loud) == 0:
        return 0, len(audio)
    margin = int(pad * sr)
    return max(0, loud[0] * hop - margin), min(len(audio), (loud[-1] + 1) * hop + margin)


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


def voice_settings_file(name):
    return voices_dir() / f"{name}.json"  # sits next to the sample, so it moves and goes with it


def load_voice_settings(name):
    """This voice's own slider values ({} = use the main sliders). Bad or missing files count as empty."""
    try:
        data = json.loads(voice_settings_file(name).read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.items() if k in SLIDERS}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def clean_name(name):
    """A voice name that is safe as a Windows file name, or "" if nothing is left."""
    return re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip()).strip(". ")


def asset(name):
    """A bundled file: next to this script in the release, in brand/ when running from source."""
    here = Path(__file__).resolve().parent
    return next((p for p in (here / name, here / "brand" / name) if p.exists()), None)


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_audio(path):
    """Mono float32 audio and its sample rate. soundfile covers wav/flac/ogg/mp3; librosa is the fallback."""
    import numpy as np
    import soundfile as sf

    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=True)
        return audio.mean(axis=1), sr
    except Exception:
        import librosa

        audio, sr = librosa.load(path, sr=None, mono=True)
        return audio.astype(np.float32), sr


def same_input_elsewhere(index):
    """The same physical microphone under the MME and DirectSound APIs (MME cuts names to 31 characters)."""
    import sounddevice as sd

    if index is None:
        return []
    name = sd.query_devices(index)["name"]
    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    return [i for api in ("MME", "Windows DirectSound") for i, d in enumerate(sd.query_devices())
            if i != index and apis[d["hostapi"]] == api and d["max_input_channels"] > 0 and name.startswith(d["name"])]


def input_sources():
    """{label: (kind, id)} of everything Lyrebird can record from.

    Microphones come from sounddevice (WASAPI: full names, one entry per device). "What you hear" (WASAPI
    loopback of an output device) comes from soundcard: sounddevice's PortAudio build has no loopback, and
    soundcard can't open some microphones (e.g. Bluetooth hands-free ones), so each library does what it's good at.
    """
    import sounddevice as sd

    apis = sd.query_hostapis()
    api = next((a for a in apis if "WASAPI" in a["name"]), apis[sd.default.hostapi])
    sources = {sd.query_devices(i)["name"]: ("mic", i) for i in api["devices"] if sd.query_devices(i)["max_input_channels"] > 0}
    try:
        import soundcard as sc

        default = sc.default_speaker().id
        for speaker in sorted(sc.all_speakers(), key=lambda s: s.id != default):  # default output first
            sources[LOOPBACK_PREFIX + speaker.name] = ("loopback", speaker.id)
    except Exception as e:  # loopback is optional; microphones still work
        print(f"Loopback scan failed: {e}")
    return sources


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

    def render(self, kind, segments, lang, settings, seed, out_path, status,
               fmt=FORMATS["WAV 16-bit"], rate=None, cancel=None):
        """Speak segments [(voice sample path or None for the built-in voice, text, per-voice settings)] in order.

        `settings` holds the main slider values; a voice's own settings override them while it speaks.
        """
        import numpy as np
        import soundfile as sf
        import torch

        model = self.load(kind, status)
        if seed:
            torch.manual_seed(seed)
        conds = {None: self.default_conds}  # each voice is analysed once per render

        def use_voice(ref, exaggeration):
            if ref not in conds:
                status(f"Analysing voice {Path(ref).stem}...")
                model.prepare_conditionals(ref, exaggeration=exaggeration)
                conds[ref] = model.conds
            if conds[ref] is None:
                raise RuntimeError("This model has no built-in voice. Record or import a voice first.")
            model.conds = conds[ref]

        def generate_kwargs(own):
            s = {**settings, **own}
            kwargs = {"temperature": s["temperature"]}
            if kind != "turbo":  # Turbo ignores these (and logs a warning if they're set)
                # ponytail: chatterbox 0.1.7 Standard crashes at cfg_weight=0; 1e-3 is effectively no guidance
                cfg = max(s["cfg_weight"], 1e-3) if kind == "standard" else s["cfg_weight"]
                kwargs.update(exaggeration=s["exaggeration"], cfg_weight=cfg)
            if kind == "multilingual":
                kwargs["language_id"] = lang
            return kwargs

        limit = CJK_CHUNK_CHARS if kind == "multilingual" and lang in ("zh", "ja", "ko") else CHUNK_CHARS
        if kind != "turbo":  # only Turbo knows the sound tags; other models would read "[laugh]" out as a word
            segments = [(ref, strip_tags(text), own) for ref, text, own in segments]
        chunks = [(ref, chunk, own) for ref, text, own in segments for chunk in chunk_text(text, limit)]
        if not chunks:
            raise RuntimeError("There's nothing to say: the text only has sound tags, which this model doesn't use.")
        gap = np.zeros(int(model.sr * GAP_SECONDS), dtype=np.float32)
        parts = []
        for i, (ref, chunk, own) in enumerate(chunks, 1):
            if cancel is not None and cancel.is_set():
                raise Stopped
            kwargs = generate_kwargs(own)
            use_voice(ref, kwargs.get("exaggeration", settings["exaggeration"]))
            status(f"Rendering chunk {i}/{len(chunks)}" + (f" ({Path(ref).stem})" if ref else "") + "...")
            try:
                wav = model.generate(chunk, **kwargs).squeeze(0).detach().cpu().numpy().astype(np.float32)
            finally:
                if kind == "multilingual":  # ponytail: chatterbox 0.1.7 leaks an attention hook per generate
                    for layer in model.t3.tfmr.layers:
                        layer.self_attn._forward_hooks.clear()
            parts += [wav, gap]
        audio, sr = np.concatenate(parts[:-1]), model.sr
        if rate and rate != sr:
            import librosa  # installed with chatterbox; soxr-based resampling

            audio, sr = librosa.resample(audio, orig_sr=sr, target_sr=rate), rate
        sf.write(out_path, audio, sr, format=fmt[0], subtype=fmt[1])


class TrimDialog(tk.Toplevel):
    """Waveform with draggable start/end handles. After it closes, .result is (start, end) in samples, or None."""

    W, H = 640, 140

    def __init__(self, parent, audio, sr, title):
        import numpy as np

        super().__init__(parent)
        self.audio, self.sr, self.result, self.active = audio, sr, None, "end"
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.start, self.end = speech_bounds(audio, sr)
        if self.end - self.start > 30 * sr:  # a long import: start with a 15 s slice of the speech
            self.end = self.start + 15 * sr

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both")
        ttk.Label(frame, text="Drag the handles to keep only clean speech. 5-20 s works best.").pack(anchor="w")
        self.canvas = tk.Canvas(frame, width=self.W, height=self.H, background=SURFACE, highlightthickness=1,
                                highlightbackground=LINE, cursor="sb_h_double_arrow")
        self.canvas.pack(pady=8)
        self.selection = self.canvas.create_rectangle(0, 0, 0, self.H, fill=PLUME_SOFT, outline="")
        edges = np.linspace(0, len(audio), self.W + 1).astype(int)
        mid, scale = self.H / 2, 0.92 * self.H / 2 / max(float(np.abs(audio).max()), 1e-6)
        for x in range(self.W):  # min/max envelope, one line per pixel column
            seg = audio[edges[x]:max(edges[x + 1], edges[x] + 1)]
            self.canvas.create_line(x, mid - seg.max() * scale, x, mid - seg.min() * scale + 1, fill=FERN)
        self.handles = [self.canvas.create_line(0, 0, 0, self.H, fill=PLUME, width=3) for _ in range(2)]
        self.canvas.bind("<Button-1>", self.pick)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.info = ttk.Label(frame)
        self.info.pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Auto-trim", command=self.auto).pack(side="left")
        ttk.Button(buttons, text="Play selection", command=self.play).pack(side="left", padx=8)
        ttk.Button(buttons, text="Save", command=self.save, default="active").pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", lambda e: e.widget is self and winsound.PlaySound(None, 0))  # stop any preview
        self.redraw()
        self.wait_visibility()  # grabbing an unmapped window fails on Windows
        self.grab_set()
        self.focus_set()

    def x_of(self, i):
        return i * self.W / len(self.audio)

    def pick(self, event):  # move whichever handle is nearer the click
        near_start = abs(event.x - self.x_of(self.start)) <= abs(event.x - self.x_of(self.end))
        self.active = "start" if near_start else "end"
        self.drag(event)

    def drag(self, event):
        i = int(min(max(event.x, 0), self.W) * len(self.audio) / self.W)
        min_len = int(0.5 * self.sr)
        if self.active == "start":
            self.start = max(0, min(i, self.end - min_len))
        else:
            self.end = min(len(self.audio), max(i, self.start + min_len))
        self.redraw()

    def auto(self):
        self.start, self.end = speech_bounds(self.audio, self.sr)
        self.redraw()

    def redraw(self):
        x0, x1 = self.x_of(self.start), self.x_of(self.end)
        self.canvas.coords(self.selection, x0, 0, x1, self.H)
        for handle, x in zip(self.handles, (x0, x1)):
            self.canvas.coords(handle, x, 0, x, self.H)
        secs = (self.end - self.start) / self.sr
        hint = " Turbo needs more than 5 s." if secs <= 5 else " Longer than 20 s adds little." if secs > 20 else ""
        self.info.configure(text=f"Keeping {self.start / self.sr:.2f} s to {self.end / self.sr:.2f} s "
                                 f"({secs:.1f} s).{hint}", foreground=TAG_OFF if hint else INK_MUTED)

    def play(self):
        import soundfile as sf

        preview = Path(tempfile.gettempdir()) / "lyrebird-preview.wav"
        winsound.PlaySound(None, 0)  # release the previous preview file before overwriting it
        sf.write(preview, self.audio[self.start:self.end], self.sr, subtype="PCM_16")
        winsound.PlaySound(str(preview), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def save(self):
        self.result = (self.start, self.end)
        self.destroy()


class VoiceSettingsDialog(tk.Toplevel):
    """Per-voice slider values. Ticked rows override the main sliders whenever this voice speaks."""

    def __init__(self, parent, name, current):
        super().__init__(parent)
        self.name, self.rows = name, {}
        self.title(f"Settings for {name}")
        self.resizable(False, False)
        self.transient(parent)
        saved = load_voice_settings(name)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, wraplength=420, justify="left", text=f'Tick a setting to give "{name}" its own value. '
                  "It's used whenever this voice speaks, including in dialogues. Unticked settings follow "
                  "the main sliders. Turbo only uses Temperature.").grid(row=0, column=0, columnspan=3, sticky="w")
        for row, (key, (label, lo, hi, _)) in enumerate(SLIDERS.items(), 1):
            use, value = tk.BooleanVar(value=key in saved), tk.DoubleVar(value=saved.get(key, current[key]))
            scale = ttk.Scale(frame, from_=lo, to=hi, variable=value, length=240)
            shown = ttk.Label(frame, width=5)
            value.trace_add("write", lambda *_, v=value, s=shown: s.configure(text=f"{v.get():.2f}"))
            value.set(value.get())
            toggle = lambda u=use, s=scale: s.state(["!disabled"] if u.get() else ["disabled"])
            ttk.Checkbutton(frame, text=label, variable=use, command=toggle).grid(row=row, column=0, sticky="w", pady=4)
            scale.grid(row=row, column=1, sticky="ew", padx=8)
            shown.grid(row=row, column=2)
            toggle()
            self.rows[key] = (use, value)
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(SLIDERS) + 1, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        ttk.Button(buttons, text="Save", command=self.save, default="active").pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_visibility()  # grabbing an unmapped window fails on Windows
        self.grab_set()

    def save(self):
        own = {key: round(value.get(), 3) for key, (use, value) in self.rows.items() if use.get()}
        path = voice_settings_file(self.name)
        if own:
            path.write_text(json.dumps(own, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.output = None
        self.buttons = []
        self.busy = False
        self.cancel = threading.Event()

        root.title("Lyrebird")
        root.minsize(620, 600)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        pad = {"padx": 8, "pady": 4}

        # Voice library + recording source
        voice = ttk.LabelFrame(root, text="1. Voice")
        voice.grid(row=0, column=0, sticky="ew", **pad)
        voice.columnconfigure(1, weight=1)
        ttk.Label(voice, text="Voice").grid(row=0, column=0, sticky="w", **pad)
        self.voice_var = tk.StringVar(value=BUILTIN_VOICE)
        self.voice_box = ttk.Combobox(voice, textvariable=self.voice_var, state="readonly", postcommand=self.refresh_voices)
        self.voice_box.grid(row=0, column=1, sticky="ew", **pad)
        self.voice_box.bind("<<ComboboxSelected>>", self.sync_play)
        self.play_voice = ttk.Button(voice, text="Play", command=lambda: self.play(self.ref))
        self.play_voice.grid(row=0, column=2, **pad)
        manage = ttk.Menubutton(voice, text="Manage")
        self.manage_menu = tk.Menu(manage, tearoff=False, postcommand=self.sync_manage)
        self.manage_menu.add_command(label="Rename...", command=self.rename_voice)
        self.manage_menu.add_command(label="Delete", command=self.delete_voice)
        self.manage_menu.add_command(label="Voice settings...", command=self.edit_voice_settings)
        self.manage_menu.add_separator()
        self.manage_menu.add_command(label="Open voices folder", command=lambda: os.startfile(voices_dir()))
        manage["menu"] = self.manage_menu
        manage.grid(row=0, column=3, sticky="ew", **pad)
        self.buttons.append(manage)
        ttk.Label(voice, text="Source").grid(row=1, column=0, sticky="w", **pad)
        self.source_var = tk.StringVar(value=DEFAULT_MIC)
        self.source_box = ttk.Combobox(voice, textvariable=self.source_var, state="readonly", postcommand=self.refresh_sources)
        self.source_box.grid(row=1, column=1, sticky="ew", **pad)
        self._button(voice, f"Record ({RECORD_SECONDS}s)", self.record).grid(row=1, column=2, **pad)
        self._button(voice, "Import...", self.import_audio).grid(row=1, column=3, sticky="ew", **pad)
        ttk.Label(voice, foreground="gray", text="Record or import 5-20 s of clean speech to add a voice. \"What you "
                  "hear\" records audio playing on this PC.").grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        self.sources = {}
        self.refresh_voices()
        self.refresh_sources(reinit=False)

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
        self.text.tag_configure("tag_off", background="#fff0c2", foreground=TAG_OFF)
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
        self.sliders = {key: self._slider(opts, row, *spec) for row, (key, spec) in enumerate(SLIDERS.items(), 2)}
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
        ttk.Label(out, text="Format").grid(row=1, column=0, sticky="w", **pad)
        fmt_row = ttk.Frame(out)
        fmt_row.grid(row=1, column=1, columnspan=2, sticky="w")
        self.format_var = tk.StringVar(value=next(iter(FORMATS)))
        ttk.Combobox(fmt_row, textvariable=self.format_var, values=list(FORMATS), state="readonly",
                     width=18).pack(side="left", **pad)
        self.rate_var = tk.StringVar(value=next(iter(RATES)))
        ttk.Combobox(fmt_row, textvariable=self.rate_var, values=list(RATES), state="readonly",
                     width=22).pack(side="left", **pad)
        row = ttk.Frame(out)
        row.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._button(row, "Render", self.render).pack(side="left", **pad)
        self.stop_button = ttk.Button(row, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", **pad)
        self.play_output = ttk.Button(row, text="Play output", command=lambda: self.play(self.output))
        self.play_output.pack(side="left", **pad)
        ttk.Button(row, text="Open folder", command=self.open_folder).pack(side="left", **pad)
        self.progress = ttk.Progressbar(out, mode="determinate")
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(out, textvariable=self.status, wraplength=580).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        self.apply_settings(load_settings())
        self.sync_options()
        self.sync_play()

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
        for key in ("exaggeration", "cfg_weight"):
            self.sliders[key].scale.state(["disabled"] if kind == "turbo" else ["!disabled"])
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
        if hasattr(self, "play_output"):  # not yet built during __init__
            self.sync_play()

    def refresh_sources(self, reinit=True):
        """List recording sources, re-scanning so newly plugged-in devices show up."""
        if self.busy:  # re-initialising PortAudio mid-recording would kill the recording
            return
        try:
            import sounddevice as sd

            if reinit:
                sd._terminate()
                sd._initialize()
            self.sources = input_sources()
        except Exception as e:  # no audio subsystem: still usable with imported voices
            print(f"Source scan failed: {e}")
            self.sources = {}
        self.source_box.configure(values=[DEFAULT_MIC, *self.sources])
        if self.source_var.get() not in self.sources:
            self.source_var.set(DEFAULT_MIC)

    def sync_play(self, _event=None):
        """Play buttons only work when there is something to play."""
        self.play_voice.state(["!disabled"] if self.ref else ["disabled"])
        self.play_output.state(["!disabled"] if self.output else ["disabled"])

    def sync_manage(self):
        """Rename, Delete and Voice settings need a saved voice, not the built-in one."""
        state = "normal" if self.ref else "disabled"
        for label in ("Rename...", "Delete", "Voice settings..."):
            self.manage_menu.entryconfigure(label, state=state)

    def ask_voice_path(self, default):
        """Ask for a voice name; returns its .wav path in the voice library, or None if cancelled."""
        name = clean_name(simpledialog.askstring("Lyrebird", "Name this voice:", initialvalue=default, parent=self.root))
        if not name:
            return None
        if voice_files(name) and not messagebox.askyesno("Lyrebird", f'Replace the existing voice "{name}"?'):
            return None
        return voices_dir() / f"{name}.wav"

    # --- settings ------------------------------------------------------------
    def settings(self):
        try:
            seed = int(self.seed.get())
        except (tk.TclError, ValueError):
            seed = 0
        return {"model": self.model_var.get(), "language": self.lang_var.get(),
                **{key: round(var.get(), 3) for key, var in self.sliders.items()}, "seed": seed,
                "save_to": self.out_dir.get(), "format": self.format_var.get(), "sample_rate": self.rate_var.get(),
                "voice": self.voice_var.get(), "source": self.source_var.get(), "geometry": self.root.geometry()}

    def apply_settings(self, s):
        """Restore what load_settings() returned, skipping anything that no longer fits (a removed mic, etc.)."""
        choices = ((self.model_var, "model", MODELS), (self.lang_var, "language", self.lang_box.cget("values")),
                   (self.format_var, "format", FORMATS), (self.rate_var, "sample_rate", RATES),
                   (self.voice_var, "voice", self.voices), (self.source_var, "source", self.sources))
        for var, key, allowed in choices:
            if s.get(key) in allowed:
                var.set(s[key])
        for key, var in self.sliders.items():
            _, lo, hi, _ = SLIDERS[key]
            if isinstance(s.get(key), (int, float)):
                var.set(min(max(s[key], lo), hi))
        if isinstance(s.get("seed"), int):
            self.seed.set(s["seed"])
        if isinstance(s.get("save_to"), str) and s["save_to"].strip():
            self.out_dir.set(s["save_to"])

    def save_settings(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings(), indent=2), encoding="utf-8")
        except OSError as e:  # never block closing the window over this
            print(f"Could not save settings: {e}")

    def close(self):
        self.cancel.set()  # a running render stops after its current chunk
        self.save_settings()
        self.root.destroy()

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
            except Stopped:
                self.ui(self.status.set, "Stopped. Nothing was saved.")
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
        self.stop_button.state(["disabled"])
        for b in self.buttons:
            b.state(["!disabled"])

    def out_folder(self):
        folder = Path(self.out_dir.get().strip() or documents_dir() / "Lyrebird").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # --- voices --------------------------------------------------------------
    def save_voice(self, audio, sr, path, title):
        """Let the user trim the sample, then save it as the voice at `path`."""
        import soundfile as sf

        dialog = TrimDialog(self.root, audio, sr, title)
        self.root.wait_window(dialog)
        if dialog.result is None:
            self.status.set("Cancelled. The voice wasn't saved.")
            return
        start, end = dialog.result
        sf.write(path, audio[start:end], sr)
        drop_older_versions(path)
        self.refresh_voices(path.stem)
        self.status.set(f'Saved voice "{path.stem}" ({(end - start) / sr:.1f} s).')

    def record(self):
        path = self.ask_voice_path(f"My voice {datetime.now():%Y-%m-%d %H%M}")
        if not path:
            return
        label = self.source_var.get()
        kind, device = self.sources.get(label, ("mic", None))  # None = Windows default input

        def work():
            import numpy as np

            if kind == "loopback":
                import soundcard as sc

                ctypes.windll.ole32.CoInitializeEx(None, 0)  # soundcard uses COM, which each thread must initialise
                try:
                    source = sc.get_microphone(id=device, include_loopback=True)
                    sr, chunks = LOOPBACK_RATE, []
                    with source.recorder(samplerate=sr) as recorder:
                        for left in range(RECORD_SECONDS, 0, -1):
                            self.ui(self.status.set, f"Recording {label}... {left}s left - play the voice now.")
                            chunks.append(recorder.record(numframes=sr))
                finally:
                    ctypes.windll.ole32.CoUninitialize()
                mono = np.concatenate(chunks).mean(axis=1)  # program audio is often stereo: mix it down
            else:
                import sounddevice as sd

                for attempt in (device, *same_input_elsewhere(device)):
                    info = sd.query_devices(attempt, "input")
                    sr = int(info["default_samplerate"])  # the device's native format; WASAPI rejects anything else
                    try:
                        audio = sd.rec(RECORD_SECONDS * sr, samplerate=sr, channels=info["max_input_channels"],
                                       device=attempt, dtype="float32")
                        break
                    except sd.PortAudioError as e:  # WASAPI can refuse a mic MME still opens; try the next API
                        print(f"Could not open {info['name']} ({attempt}): {e}")
                else:
                    raise RuntimeError(f"Couldn't open {label}. Another app may be using it exclusively.")
                for left in range(RECORD_SECONDS, 0, -1):
                    self.ui(self.status.set, f"Recording from {info['name']}... {left}s left - speak now.")
                    time.sleep(1)
                sd.wait()
                mono = audio[:, np.abs(audio).max(axis=0).argmax()]  # keep the loudest channel
            if np.abs(mono).max() < 1e-4:
                raise RuntimeError(f"The recording from {label} is silent, so the voice wasn't saved. "
                                   "Check the source, or for 'What you hear' make sure audio is playing.")
            self.ui(self.status.set, "Trim the recording, then click Save.")
            self.ui(self.save_voice, mono.astype(np.float32), sr, path, f'Trim "{path.stem}"')

        self.run_bg(work)

    def import_audio(self):
        src = filedialog.askopenfilename(title="Choose a voice sample", filetypes=AUDIO_TYPES)
        if not src:
            return
        path = self.ask_voice_path(Path(src).stem)
        if path:
            audio, sr = load_audio(src)  # read before saving: src may be the voice file being replaced
            self.save_voice(audio, sr, path, f'Trim "{path.stem}"')

    def rename_voice(self):
        old = self.voice_var.get()
        new = clean_name(simpledialog.askstring("Lyrebird", f'New name for "{old}":', initialvalue=old, parent=self.root))
        if not new or new == old:
            return
        if new.lower() != old.lower() and (voice_files(new) or voice_settings_file(new).exists()):
            messagebox.showerror("Lyrebird", f'There is already a voice called "{new}".')
            return
        for p in voice_files(old):
            p.rename(p.with_name(new + p.suffix))
        if voice_settings_file(old).exists():
            voice_settings_file(old).rename(voice_settings_file(new))
        self.refresh_voices(new)
        self.status.set(f'Renamed "{old}" to "{new}". Update any dialogue lines that use the old name.')

    def delete_voice(self):
        name = self.voice_var.get()
        if not messagebox.askyesno("Lyrebird", f'Delete the voice "{name}"? Its sample file is deleted too.'):
            return
        for p in voice_files(name):
            p.unlink()
        voice_settings_file(name).unlink(missing_ok=True)
        self.refresh_voices()
        self.status.set(f'Deleted voice "{name}".')

    def edit_voice_settings(self):
        name = self.voice_var.get()
        dialog = VoiceSettingsDialog(self.root, name, {key: var.get() for key, var in self.sliders.items()})
        self.root.wait_window(dialog)
        own = load_voice_settings(name)
        self.status.set(f'"{name}" uses its own ' + ", ".join(SLIDERS[k][0] for k in own) + "." if own
                        else f'"{name}" follows the main sliders.')

    # --- rendering -----------------------------------------------------------
    def browse_out(self):
        folder = filedialog.askdirectory(initialdir=self.out_dir.get())
        if folder:
            self.out_dir.set(folder)

    def render(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Lyrebird", "Type some text to speak first.")
            return
        voices = {**self.voices, BUILTIN_NAME: None}
        dropdown = self.voice_var.get() if self.ref else None
        segments = []
        for who, part in split_speakers(text, voices):
            name = who or dropdown  # lines before the first "Name:" belong to the selected voice
            ref = voices.get(name) if name else None
            segments.append((ref, part, load_voice_settings(name) if ref else {}))
        if not segments:  # e.g. only "Alice:" lines so far; don't load a model for nothing
            messagebox.showwarning("Lyrebird", "Type some text to speak first.")
            return
        s = self.settings()
        fmt = FORMATS[s["format"]]
        args = dict(
            kind=MODELS[s["model"]], segments=segments, lang=s["language"].split(" ")[0],
            settings={key: s[key] for key in SLIDERS}, seed=s["seed"], fmt=fmt, rate=RATES[s["sample_rate"]],
            out_path=self.out_folder() / f"lyrebird_{stamp()}{fmt[2]}", cancel=self.cancel,
        )
        self.cancel.clear()
        self.save_settings()

        def work():
            start = time.time()
            self.engine.render(**args, status=lambda msg: self.ui(self.status.set, msg))
            self.output = str(args["out_path"])
            self.ui(self.sync_play)
            self.ui(self.status.set, f"Saved {self.output} ({time.time() - start:.0f}s).")

        self.run_bg(work)
        self.stop_button.state(["!disabled"])

    def stop(self):
        self.cancel.set()
        self.stop_button.state(["disabled"])
        self.status.set("Stopping after the current chunk...")

    def play(self, path):
        if not path:
            return
        import soundfile as sf

        if path.lower().endswith(".wav") and sf.info(path).subtype == "PCM_16":
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            os.startfile(path)  # winsound only handles plain 16-bit WAV; hand anything else to the default player

    def open_folder(self):
        os.startfile(self.out_folder())


def show_splash(root):
    """Borderless brand splash with the version and a live status line. Returns (window, set_status) or None."""
    try:
        hidpi = ctypes.windll.user32.GetDpiForSystem() >= 144
    except (AttributeError, OSError):
        hidpi = False
    image = asset("splash@2x.png" if hidpi else "splash.png") or asset("splash.png")
    if image is None:
        return None
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    photo = tk.PhotoImage(file=str(image))
    w, h, k = photo.width(), photo.height(), photo.width() / 560  # the art is laid out at 560 x 320
    canvas = tk.Canvas(win, width=w, height=h, highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.photo = photo  # keep a reference, or Tk drops the image
    canvas.create_text(242 * k, 284 * k, anchor="w", text=f"Version {VERSION}", fill=INK, font=("Segoe UI", 9, "bold"))
    status = canvas.create_text(242 * k, 303 * k, anchor="w", text="Starting...", fill=INK_MUTED, font=("Segoe UI", 9))
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 2}")
    win.update()

    def set_status(text):
        canvas.itemconfigure(status, text=text)
        win.update()

    return win, set_status


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        # Own taskbar identity, so Windows shows the Lyrebird icon rather than python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lyrebird.App")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.withdraw()  # built behind the splash, shown when ready
    icon = asset("lyrebird.ico")
    if icon:
        root.iconbitmap(default=str(icon))  # default= also covers dialogs

    def report(exc, val, tb):  # errors in Tk callbacks (bad output folder, unplayable file) go to a dialog
        import traceback
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("Lyrebird", f"{exc.__name__}: {val}")

    root.report_callback_exception = report
    shown = time.time()
    splash = show_splash(root)
    if splash:
        splash[1]("Finding voices and audio devices...")
    App(root)
    geometry = load_settings().get("geometry")
    if isinstance(geometry, str) and re.fullmatch(r"\d+x\d+[+-]-?\d+[+-]-?\d+", geometry):
        root.geometry(geometry)  # ponytail: trusts the saved position; a disconnected monitor could put it off-screen

    def reveal():
        if splash:
            splash[0].destroy()
        root.deiconify()
        root.lift()
        root.focus_force()

    root.after(max(0, int(1500 - (time.time() - shown) * 1000)), reveal)  # keep the splash up at least 1.5 s
    root.mainloop()


if __name__ == "__main__":
    main()
