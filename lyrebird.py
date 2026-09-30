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
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

VERSION = "0.3.0"
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
TEXT_EXTS = (".txt", ".md")
SESSION_EXT = ".lyrebird"
OPEN_TYPES = [("Sessions, scripts and voice clips", " ".join(f"*{e}" for e in (SESSION_EXT, *TEXT_EXTS, *AUDIO_EXTS))),
              ("Lyrebird sessions", f"*{SESSION_EXT}"), ("Text", "*.txt *.md"), *AUDIO_TYPES]
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
# Brand palette (brand/tokens.json) as (light, dark) pairs; CustomTkinter follows the Windows theme.
GROUND = ("#f2f3ee", "#111613")
SURFACE = ("#fbfbf8", "#19201c")
LINE = ("#d9ddd4", "#2c3631")
INK = ("#18201c", "#e7ece6")
MUTED = ("#56615a", "#9aa79f")
PLUME = ("#a8471f", "#f0a36e")  # the one brand hue: Render, sliders, focus
PLUME_HOVER = ("#8e3a18", "#f5b88c")
ON_PLUME = ("#fffaf5", "#1b0f08")
PLUME_SOFT = ("#f6e3d6", "#3a2216")
# Small accents that call out what each area is for.
ACCENT = {"voice": ("#2f7a5a", "#6cc59b"), "script": ("#1a5fb4", "#8fb8f0"),
          "delivery": ("#b8862a", "#e2b04a"), "render": PLUME}
RECORD_RED = ("#c01c28", "#ff7b82")
# Script highlight colours (brand editor tokens).
EDITOR = {"speaker": ("#1a5fb4", "#8fb8f0"), "tag": ("#0b4f8a", "#b5d6ff"), "tag_bg": ("#d7ebff", "#13314f"),
          "tag_off": ("#8a5a00", "#f2cf73"), "tag_off_bg": ("#fff0c2", "#3d2e05"), "tag_bad": ("#c01c28", "#ff7b82")}
SLIDER_STYLE = dict(fg_color=LINE, progress_color=PLUME, button_color=PLUME, button_hover_color=PLUME_HOVER)
OFF = ("#b9bfb6", "#46514b")  # a control that doesn't apply to the selected model


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


def trim_bounds(audio, sr):
    """Starting selection for the trim dialog: the speech, but at most a 15 s slice of a long clip."""
    start, end = speech_bounds(audio, sr)
    return start, (start + 15 * sr if end - start > 30 * sr else end)


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


def pick(pair):
    """The light or dark half of a colour pair, for plain Tk widgets that CustomTkinter doesn't theme."""
    return pair[ctk.get_appearance_mode() == "Dark"]


def dot_image(pair, size=10):
    """A small filled circle in a colour pair, for button call-outs (e.g. the red dot on Record)."""
    from PIL import Image, ImageDraw

    def draw(color):
        img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=color)
        return img.resize((size, size), Image.LANCZOS)  # drawn big and scaled down for smooth edges

    return ctk.CTkImage(light_image=draw(pair[0]), dark_image=draw(pair[1]), size=(size, size))


def own_icon(window):
    """CustomTkinter swaps in its own icon ~200 ms after a window opens; put Lyrebird's back."""
    icon = asset("lyrebird.ico")
    if icon:
        window.after(250, lambda: window.iconbitmap(str(icon)))


def fonts():
    return {"brand": ctk.CTkFont("Segoe UI Semibold", 17), "title": ctk.CTkFont("Segoe UI Semibold", 14),
            "body": ctk.CTkFont("Segoe UI", 13), "small": ctk.CTkFont("Segoe UI", 12),
            "button": ctk.CTkFont("Segoe UI Semibold", 13), "script": ctk.CTkFont("Segoe UI", 14)}


def button(parent, text, command, kind="secondary", **kw):
    """Primary = the one russet action on screen; secondary = quiet outlined buttons."""
    style = {"primary": dict(fg_color=PLUME, hover_color=PLUME_HOVER, text_color=ON_PLUME),
             "secondary": dict(fg_color=GROUND, hover_color=LINE, text_color=INK, border_width=1, border_color=LINE)}
    options = {"corner_radius": 8, "height": 34, "text_color_disabled": MUTED, **style[kind], **kw}
    return ctk.CTkButton(parent, text=text, command=command, **options)


class Menu(ctk.CTkOptionMenu):
    """A calm dropdown that can refresh its choices right before it opens (new voices, plugged-in mics)."""

    def __init__(self, parent, variable, values, refresh=None, **kw):
        super().__init__(parent, variable=variable, values=list(values) or [""], dynamic_resizing=False,
                         corner_radius=8, height=34, fg_color=GROUND, button_color=GROUND, button_hover_color=LINE,
                         text_color=INK, text_color_disabled=MUTED, dropdown_fg_color=SURFACE,
                         dropdown_hover_color=GROUND, dropdown_text_color=INK, **kw)
        self.refresh = refresh

    def _open_dropdown_menu(self):
        if self.refresh:
            self.refresh()
        super()._open_dropdown_menu()


def popup(widget, entries):
    """A plain dropdown menu under `widget`: entries are (label, command, enabled) or None for a separator."""
    menu = tk.Menu(widget, tearoff=False)
    for entry in entries:
        if entry is None:
            menu.add_separator()
        else:
            label, command, enabled = entry
            menu.add_command(label=label, command=command, state="normal" if enabled else "disabled")
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())


def ask_text(parent, title, prompt, initial=""):
    """A small themed text prompt. Returns the text, or None if cancelled."""
    f = fonts()
    dialog = ctk.CTkToplevel(parent, fg_color=SURFACE)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    own_icon(dialog)
    result = {"text": None}
    ctk.CTkLabel(dialog, text=prompt, font=f["body"], text_color=INK).pack(anchor="w", padx=20, pady=(18, 6))
    var = tk.StringVar(value=initial)
    entry = ctk.CTkEntry(dialog, textvariable=var, width=320, height=34, corner_radius=8, font=f["body"],
                         fg_color=GROUND, border_color=LINE, text_color=INK)
    entry.pack(padx=20)
    row = ctk.CTkFrame(dialog, fg_color="transparent")
    row.pack(fill="x", padx=20, pady=(14, 18))

    def ok():
        result["text"] = var.get()
        dialog.destroy()

    button(row, "OK", ok, kind="primary", width=90).pack(side="right")
    button(row, "Cancel", dialog.destroy, width=90).pack(side="right", padx=8)
    dialog.bind("<Return>", lambda e: ok())
    dialog.bind("<Escape>", lambda e: dialog.destroy())
    dialog.wait_visibility()
    dialog.grab_set()
    entry.focus_set()
    entry.select_range(0, "end")
    parent.wait_window(dialog)
    return result["text"]


class TrimDialog(ctk.CTkToplevel):
    """Waveform with draggable start/end handles. After it closes, .result is (start, end) in samples, or None."""

    W, H = 640, 150

    def __init__(self, parent, audio, sr, title):
        import numpy as np

        super().__init__(parent, fg_color=SURFACE)
        self.audio, self.sr, self.result, self.active = audio, sr, None, "end"
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        own_icon(self)
        f = fonts()
        self.start, self.end = trim_bounds(audio, sr)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 0))
        ctk.CTkFrame(head, width=8, height=8, corner_radius=4, fg_color=ACCENT["voice"]).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(head, text="Keep only clean speech", font=f["title"], text_color=INK).pack(side="left")
        ctk.CTkLabel(self, text="Drag in the waveform to move the nearest handle. 5 to 20 seconds works best.",
                     font=f["small"], text_color=MUTED).pack(anchor="w", padx=20, pady=(2, 10))
        self.canvas = tk.Canvas(self, width=self.W, height=self.H, background=pick(GROUND), highlightthickness=1,
                                highlightbackground=pick(LINE), cursor="sb_h_double_arrow")
        self.canvas.pack(padx=20)
        self.selection = self.canvas.create_rectangle(0, 0, 0, self.H, fill=pick(PLUME_SOFT), outline="")
        edges = np.linspace(0, len(audio), self.W + 1).astype(int)
        mid, scale = self.H / 2, 0.9 * self.H / 2 / max(float(np.abs(audio).max()), 1e-6)
        wave = pick(ACCENT["voice"])
        for x in range(self.W):  # min/max envelope, one line per pixel column
            seg = audio[edges[x]:max(edges[x + 1], edges[x] + 1)]
            self.canvas.create_line(x, mid - seg.max() * scale, x, mid - seg.min() * scale + 1, fill=wave)
        self.handles = [self.canvas.create_line(0, 0, 0, self.H, fill=pick(PLUME), width=3) for _ in range(2)]
        self.canvas.bind("<Button-1>", self.pick)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.info = ctk.CTkLabel(self, font=f["small"], text_color=MUTED)
        self.info.pack(anchor="w", padx=20, pady=(8, 0))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(12, 18))
        button(row, "Auto-trim", self.auto).pack(side="left")
        button(row, "▶  Play selection", self.play).pack(side="left", padx=8)
        button(row, "Save voice", self.save, kind="primary").pack(side="right")
        button(row, "Cancel", self.destroy).pack(side="right", padx=8)
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
        self.start, self.end = trim_bounds(self.audio, self.sr)
        self.redraw()

    def redraw(self):
        x0, x1 = self.x_of(self.start), self.x_of(self.end)
        self.canvas.coords(self.selection, x0, 0, x1, self.H)
        for handle, x in zip(self.handles, (x0, x1)):
            self.canvas.coords(handle, x, 0, x, self.H)
        secs = (self.end - self.start) / self.sr
        hint = "  Turbo needs more than 5 s." if secs <= 5 else "  Longer than 20 s adds little." if secs > 20 else ""
        self.info.configure(text=f"Keeping {self.start / self.sr:.2f} s to {self.end / self.sr:.2f} s  ·  {secs:.1f} s{hint}",
                            text_color=EDITOR["tag_off"] if hint else MUTED)

    def play(self):
        import soundfile as sf

        preview = Path(tempfile.gettempdir()) / "lyrebird-preview.wav"
        winsound.PlaySound(None, 0)  # release the previous preview file before overwriting it
        sf.write(preview, self.audio[self.start:self.end], self.sr, subtype="PCM_16")
        winsound.PlaySound(str(preview), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def save(self):
        self.result = (self.start, self.end)
        self.destroy()


class VoiceSettingsDialog(ctk.CTkToplevel):
    """Per-voice slider values. Switched-on rows override the main sliders whenever this voice speaks."""

    def __init__(self, parent, name, current):
        super().__init__(parent, fg_color=SURFACE)
        self.name, self.rows = name, {}
        self.title(f"Settings for {name}")
        self.resizable(False, False)
        self.transient(parent)
        own_icon(self)
        f = fonts()
        saved = load_voice_settings(name)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 0))
        ctk.CTkFrame(head, width=8, height=8, corner_radius=4, fg_color=ACCENT["delivery"]).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(head, text=f"{name}'s own delivery", font=f["title"], text_color=INK).pack(side="left")
        ctk.CTkLabel(self, font=f["small"], text_color=MUTED, justify="left", wraplength=400, anchor="w",
                     text="Switch a setting on to give this voice its own value, used whenever it speaks, including "
                          "in dialogues. Settings left off follow the main sliders. Turbo only uses Temperature."
                     ).pack(fill="x", padx=20, pady=(2, 12))
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="x", padx=20)
        for row, (key, (label, lo, hi, _)) in enumerate(SLIDERS.items()):
            use, value = tk.BooleanVar(value=key in saved), tk.DoubleVar(value=saved.get(key, current[key]))
            shown = ctk.CTkLabel(grid, width=40, font=f["small"], text_color=MUTED, anchor="e")
            slider = ctk.CTkSlider(grid, from_=lo, to=hi, variable=value, width=200, **SLIDER_STYLE,
                                   command=lambda v, s=shown: s.configure(text=f"{v:.2f}"))
            shown.configure(text=f"{value.get():.2f}")
            toggle = lambda u=use, s=slider: s.configure(state="normal" if u.get() else "disabled")
            ctk.CTkSwitch(grid, text=label, variable=use, command=toggle, font=f["body"], text_color=INK,
                          progress_color=PLUME, button_color=SURFACE, button_hover_color=GROUND, fg_color=LINE
                          ).grid(row=row, column=0, sticky="w", pady=6)
            slider.grid(row=row, column=1, padx=12)
            shown.grid(row=row, column=2)
            toggle()
            self.rows[key] = (use, value)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(14, 18))
        button(buttons, "Save", self.save, kind="primary", width=90).pack(side="right")
        button(buttons, "Cancel", self.destroy, width=90).pack(side="right", padx=8)
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
        self.buttons = []  # disabled while recording or rendering
        self.busy = False
        self.cancel = threading.Event()
        self.voices, self.sources = {}, {}
        f = self.f = fonts()

        root.title("Lyrebird")
        root.configure(fg_color=GROUND)
        root.geometry("1180x800")
        root.minsize(1000, 720)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)

        # Header: name, version, appearance
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 8))
        logo = asset("lyrebird-256.png")
        if logo:
            from PIL import Image

            image = ctk.CTkImage(Image.open(logo), size=(26, 26))
            ctk.CTkLabel(header, text="", image=image).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(header, text="Lyrebird", font=f["brand"], text_color=INK).pack(side="left")
        ctk.CTkLabel(header, text=f"  {VERSION}  ·  Voice cloning with Chatterbox", font=f["small"],
                     text_color=MUTED).pack(side="left", pady=(3, 0))
        self.session_label = ctk.CTkLabel(header, text="", font=f["small"], text_color=MUTED)
        self.session_label.pack(side="left", padx=(12, 0), pady=(3, 0))
        self.appearance = tk.StringVar(value="System")  # apply_settings restores a saved choice
        ctk.CTkSegmentedButton(header, values=["Light", "Dark", "System"], variable=self.appearance, font=f["small"],
                               command=self.set_appearance, height=28, corner_radius=8, fg_color=LINE,
                               selected_color=SURFACE, selected_hover_color=SURFACE, unselected_color=LINE,
                               unselected_hover_color=GROUND, text_color=INK).pack(side="right")
        for text, command in (("Save as...", lambda: self.save_session(ask=True)), ("Save", self.save_session),
                              ("Open...", self.open_dialog)):
            button(header, text, command, width=84, height=28, font=f["small"]).pack(side="right", padx=(0, 8))

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=24)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # Script (left, takes the space)
        script, s = self.card(body, "script", "Script",
                              'Type what to say. Start a line with a saved voice\'s name, like "Alice: Hi!", to switch speaker.\n'
                              "Drop a text file, voice clip or saved session anywhere on the window to open it.")
        script.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        s.rowconfigure(0, weight=1)
        s.columnconfigure(0, weight=1)
        self.editor = ctk.CTkTextbox(s, font=f["script"], wrap="word", undo=True, corner_radius=10, border_width=1,
                                     fg_color=GROUND, border_color=LINE, text_color=INK, border_spacing=10)
        self.editor.grid(row=0, column=0, sticky="nsew")
        self.text = self.editor._textbox  # the plain Tk text widget: tags, marks and <<Modified>> live here
        self.text.bind("<<Modified>>", self.on_text_modified)
        tools = ctk.CTkFrame(s, fg_color="transparent")
        tools.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        insert = button(tools, "Insert tag  ▾", None, width=120)
        insert.configure(command=lambda: popup(insert, [(t, lambda t=t: self.insert_tag(t), True) for t in TURBO_TAGS]))
        insert.pack(side="left")
        for key, label in (("tag", "spoken (Turbo)"), ("tag_off", "left out by this model"), ("tag_bad", "unknown tag")):
            chip = ctk.CTkLabel(tools, text=f" [tag] ", font=f["small"], corner_radius=6, text_color=EDITOR[key],
                                fg_color=EDITOR.get(key + "_bg", "transparent"))
            chip.pack(side="left", padx=(14, 4))
            ctk.CTkLabel(tools, text=label, font=f["small"], text_color=MUTED).pack(side="left")

        # Sidebar: voice, delivery, output
        side = ctk.CTkFrame(body, fg_color="transparent", width=370)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)

        voice, v = self.card(side, "voice", "Voice")
        voice.grid(row=0, column=0, sticky="ew")
        v.columnconfigure(0, weight=1)
        self.voice_var = tk.StringVar(value=BUILTIN_VOICE)
        self.voice_menu = Menu(v, self.voice_var, [BUILTIN_VOICE], refresh=self.refresh_voices, font=f["body"],
                               command=lambda _: self.sync_play())
        self.voice_menu.grid(row=0, column=0, sticky="ew")
        self.play_voice = button(v, "▶", lambda: self.play(self.ref), width=40)
        self.play_voice.grid(row=0, column=1, padx=(8, 0))
        self.manage = button(v, "···", None, width=40)
        self.manage.configure(command=self.show_manage)
        self.manage.grid(row=0, column=2, padx=(8, 0))
        self.buttons.append(self.manage)
        self.source_var = tk.StringVar(value=DEFAULT_MIC)
        self.source_menu = Menu(v, self.source_var, [DEFAULT_MIC], refresh=self.refresh_sources, font=f["small"])
        self.source_menu.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        row = ctk.CTkFrame(v, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        row.columnconfigure((0, 1), weight=1)
        rec = button(row, f"Record {RECORD_SECONDS} s", self.record, image=dot_image(RECORD_RED), compound="left")
        rec.grid(row=0, column=0, sticky="ew")
        imp = button(row, "Import...", lambda: self.import_audio())
        imp.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.buttons += [rec, imp]

        delivery, d = self.card(side, "delivery", "Delivery")
        delivery.grid(row=1, column=0, sticky="ew", pady=12)
        d.columnconfigure(1, weight=1)
        menus = ctk.CTkFrame(d, fg_color="transparent")
        menus.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        menus.columnconfigure(0, weight=3)
        menus.columnconfigure(1, weight=2)
        self.model_var = tk.StringVar(value=next(iter(MODELS)))
        Menu(menus, self.model_var, MODELS, font=f["body"], command=lambda _: self.sync_options()
             ).grid(row=0, column=0, sticky="ew")
        self.lang_var = tk.StringVar(value="en - English")
        self.lang_menu = Menu(menus, self.lang_var, [f"{k} - {v_}" for k, v_ in LANGUAGES.items()], font=f["body"])
        self.lang_menu.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.sliders = {}
        for row, (key, spec) in enumerate(SLIDERS.items(), 1):
            self.sliders[key] = self.slider(d, row, *spec)
        seed_row = ctk.CTkFrame(d, fg_color="transparent")
        seed_row.grid(row=len(SLIDERS) + 1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(seed_row, text="Seed", font=f["body"], text_color=INK).pack(side="left")
        ctk.CTkLabel(seed_row, text="0 = a new take every time", font=f["small"], text_color=MUTED).pack(side="left", padx=8)
        self.seed = tk.StringVar(value="0")
        ctk.CTkEntry(seed_row, textvariable=self.seed, width=110, height=32, corner_radius=8, font=f["body"],
                     fg_color=GROUND, border_color=LINE, text_color=INK, justify="right").pack(side="right")

        output, o = self.card(side, "render", "Output")
        output.grid(row=2, column=0, sticky="ew")
        o.columnconfigure(0, weight=1)
        self.out_dir = tk.StringVar(value=str(documents_dir() / "Lyrebird"))
        ctk.CTkEntry(o, textvariable=self.out_dir, height=34, corner_radius=8, font=f["small"], fg_color=GROUND,
                     border_color=LINE, text_color=INK).grid(row=0, column=0, sticky="ew")
        button(o, "Browse...", self.browse_out, width=90).grid(row=0, column=1, padx=(8, 0))
        fmt_row = ctk.CTkFrame(o, fg_color="transparent")
        fmt_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        fmt_row.columnconfigure((0, 1), weight=1)
        self.format_var = tk.StringVar(value=next(iter(FORMATS)))
        Menu(fmt_row, self.format_var, FORMATS, font=f["small"]).grid(row=0, column=0, sticky="ew")
        self.rate_var = tk.StringVar(value=next(iter(RATES)))
        Menu(fmt_row, self.rate_var, RATES, font=f["small"]).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        # Action bar
        bar = ctk.CTkFrame(root, fg_color=SURFACE, border_color=LINE, border_width=1, corner_radius=12)
        bar.grid(row=2, column=0, sticky="ew", padx=24, pady=(12, 20))
        bar.columnconfigure(4, weight=1)
        render = button(bar, "Render", self.render, kind="primary", width=120, height=38, font=f["button"])
        render.grid(row=0, column=0, rowspan=2, padx=(14, 6), pady=14)
        self.buttons.append(render)
        self.stop_button = button(bar, "■  Stop", self.stop, width=90, state="disabled")
        self.stop_button.grid(row=0, column=1, rowspan=2, padx=6)
        self.play_output = button(bar, "▶  Play output", lambda: self.play(self.output), width=130)
        self.play_output.grid(row=0, column=2, rowspan=2, padx=6)
        button(bar, "Open folder", self.open_folder, width=110).grid(row=0, column=3, rowspan=2, padx=(6, 16))
        self.status = tk.StringVar(value="Ready.")
        ctk.CTkLabel(bar, textvariable=self.status, font=f["small"], text_color=MUTED, anchor="w"
                     ).grid(row=0, column=4, sticky="sew", padx=(0, 16), pady=(12, 0))
        self.progress = ctk.CTkProgressBar(bar, height=6, corner_radius=3, fg_color=LINE, progress_color=LINE)
        self.progress.grid(row=1, column=4, sticky="new", padx=(0, 16), pady=(6, 14))
        self.progress.set(0)

        self.session_path = None
        if getattr(root, "dnd", False):
            for target in (root, self.text):  # the script box is most of the window, so it takes drops too
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<Drop>>", self.on_drop)
        for widget in (root, self.text):  # on the text box too, where Ctrl+O would otherwise insert a line
            for key in "oO":  # both cases: Caps Lock turns the letter upper-case without Shift
                widget.bind(f"<Control-{key}>", lambda e: (self.open_dialog(), "break")[1])
            for key in "sS":  # Shift (state bit 0x1), not the letter's case, means Save as
                widget.bind(f"<Control-{key}>", lambda e: (self.save_session(ask=bool(e.state & 0x1)), "break")[1])
        self.editor_colors()
        ctk.AppearanceModeTracker.add(lambda mode: self.root.after(0, self.editor_colors), root)
        ctk.ScalingTracker.add_widget(lambda *_: self.root.after(0, self.editor_colors), self.editor)
        self.refresh_voices()
        self.refresh_sources(reinit=False)
        self.apply_settings(load_settings())
        self.sync_options()

    # --- widgets -------------------------------------------------------------
    def card(self, parent, accent, title, hint=None):
        """A rounded panel whose title carries a small accent dot calling out what it's for."""
        frame = ctk.CTkFrame(parent, fg_color=SURFACE, border_color=LINE, border_width=1, corner_radius=12)
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkFrame(head, width=8, height=8, corner_radius=4, fg_color=ACCENT[accent]).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(head, text=title, font=self.f["title"], text_color=INK).pack(side="left")
        if hint:
            ctk.CTkLabel(frame, text=hint, font=self.f["small"], text_color=MUTED, anchor="w", justify="left"
                         ).pack(fill="x", padx=18, pady=(2, 0))
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(10, 16))
        return frame, body

    def slider(self, parent, row, label, lo, hi, default):
        """One line: name, slider, value."""
        var = tk.DoubleVar(value=default)
        name = ctk.CTkLabel(parent, text=label, font=self.f["body"], text_color=INK, anchor="w", width=104)
        name.grid(row=row, column=0, sticky="w", pady=(8, 0))
        scale = ctk.CTkSlider(parent, from_=lo, to=hi, variable=var, height=16, width=120, **SLIDER_STYLE)
        scale.grid(row=row, column=1, sticky="ew", padx=(4, 8), pady=(8, 0))
        value = ctk.CTkLabel(parent, font=self.f["small"], text_color=MUTED, anchor="e", width=34)
        value.grid(row=row, column=2, sticky="e", pady=(8, 0))
        var.trace_add("write", lambda *_: value.configure(text=f"{var.get():.2f}"))
        var.set(default)
        var.widgets = (scale, name)
        return var

    def insert_tag(self, tag):
        self.text.insert("insert", tag)
        self.text.focus_set()

    def editor_colors(self):
        """Tk text tags don't follow CustomTkinter's light/dark switch, so recolour them by hand."""
        import tkinter.font as tkfont

        bold = tkfont.Font(font=self.text.cget("font"))
        bold.configure(weight="bold")
        self.text.tag_configure("speaker", font=bold, foreground=pick(EDITOR["speaker"]))
        self.text.tag_configure("tag", foreground=pick(EDITOR["tag"]), background=pick(EDITOR["tag_bg"]))
        self.text.tag_configure("tag_off", foreground=pick(EDITOR["tag_off"]), background=pick(EDITOR["tag_off_bg"]))
        self.text.tag_configure("bad_tag", foreground=pick(EDITOR["tag_bad"]), underline=True)

    def set_appearance(self, mode):
        ctk.set_appearance_mode(mode)
        self.root.after(50, self.editor_colors)

    def sync_options(self):
        kind = MODELS[self.model_var.get()]
        for key in ("exaggeration", "cfg_weight"):  # Turbo ignores these: grey them out, don't just lock them
            scale, name = self.sliders[key].widgets
            on = kind != "turbo"
            scale.configure(state="normal" if on else "disabled", button_color=PLUME if on else OFF,
                            progress_color=PLUME if on else OFF)
            name.configure(text_color=INK if on else MUTED)
        self.lang_menu.configure(state="normal" if kind == "multilingual" else "disabled")
        self.highlight()

    def on_text_modified(self, _event):
        if self.text.edit_modified():  # clearing the flag re-fires <<Modified>>; skip that second call
            self.text.edit_modified(False)
            self.highlight()

    def highlight(self):
        """Colour Turbo tags and dialogue speaker names in the script."""
        if not hasattr(self, "model_var"):  # called while the window is still being built
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
        self.voice_menu.configure(values=[BUILTIN_VOICE, *self.voices])
        if select or self.voice_var.get() not in self.voices:
            self.voice_var.set(select or BUILTIN_VOICE)
        self.highlight()  # speaker names may have changed
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
        self.source_menu.configure(values=[DEFAULT_MIC, *self.sources])
        if self.source_var.get() not in self.sources:
            self.source_var.set(DEFAULT_MIC)

    def sync_play(self):
        """Play buttons only work when there is something to play."""
        if hasattr(self, "play_output"):
            self.play_voice.configure(state="normal" if self.ref else "disabled")
            self.play_output.configure(state="normal" if self.output else "disabled")

    def show_manage(self):
        has_voice = bool(self.ref)  # Rename, Delete and Voice settings need a saved voice
        popup(self.manage, [("Rename...", self.rename_voice, has_voice), ("Delete", self.delete_voice, has_voice),
                            ("Voice settings...", self.edit_voice_settings, has_voice), None,
                            ("Open voices folder", lambda: os.startfile(voices_dir()), True)])

    def ask_voice_path(self, default):
        """Ask for a voice name; returns its .wav path in the voice library, or None if cancelled."""
        name = clean_name(ask_text(self.root, "Lyrebird", "Name this voice:", default))
        if not name:
            return None
        if voice_files(name) and not messagebox.askyesno("Lyrebird", f'Replace the existing voice "{name}"?'):
            return None
        return voices_dir() / f"{name}.wav"

    # --- settings ------------------------------------------------------------
    def settings(self):
        try:
            seed = int(self.seed.get())
        except ValueError:
            seed = 0
        return {"model": self.model_var.get(), "language": self.lang_var.get(),
                **{key: round(var.get(), 3) for key, var in self.sliders.items()}, "seed": seed,
                "save_to": self.out_dir.get(), "format": self.format_var.get(), "sample_rate": self.rate_var.get(),
                "voice": self.voice_var.get(), "source": self.source_var.get(), "appearance": self.appearance.get(),
                "geometry": self.root.geometry()}

    def apply_settings(self, s):
        """Restore what load_settings() returned, skipping anything that no longer fits (a removed mic, etc.)."""
        choices = ((self.model_var, "model", MODELS), (self.lang_var, "language", [f"{k} - {v}" for k, v in LANGUAGES.items()]),
                   (self.format_var, "format", FORMATS), (self.rate_var, "sample_rate", RATES),
                   (self.voice_var, "voice", [BUILTIN_VOICE, *self.voices]), (self.source_var, "source", [DEFAULT_MIC, *self.sources]))
        for var, key, allowed in choices:
            if s.get(key) in allowed:
                var.set(s[key])
        for key, var in self.sliders.items():
            _, lo, hi, _ = SLIDERS[key]
            if isinstance(s.get(key), (int, float)):
                var.set(min(max(s[key], lo), hi))
        if isinstance(s.get("seed"), int):
            self.seed.set(str(s["seed"]))
        if isinstance(s.get("save_to"), str) and s["save_to"].strip():
            self.out_dir.set(s["save_to"])
        if s.get("appearance") in ("Light", "Dark", "System"):
            self.appearance.set(s["appearance"])
            self.set_appearance(s["appearance"])
        self.sync_play()  # the voice may have changed

    # --- sessions ------------------------------------------------------------
    def open_dialog(self):
        path = filedialog.askopenfilename(title="Open a session, script or voice clip", filetypes=OPEN_TYPES,
                                          initialdir=self.session_dir())
        if path:
            self.open_path(path)

    def on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self.root.after(0, self.open_path, paths[0])  # after the drop finishes, so dialogs can open
        return event.action

    def open_path(self, path):
        """Open whatever was chosen or dropped: a session, a text script, or an audio clip to make a voice from."""
        ext = Path(path).suffix.lower()
        if ext == SESSION_EXT:
            self.open_session(path)
        elif ext in TEXT_EXTS:
            self.load_script(path)
        elif ext in AUDIO_EXTS:
            if self.busy:  # same rule as the disabled Record and Import buttons
                self.status.set("Wait for the current recording or render to finish before adding a voice.")
                return
            self.import_audio(path)
        else:
            self.status.set(f"Lyrebird can't open {Path(path).name}. Try a session, a .txt script or an audio clip.")

    def replace_script(self, text, what):
        current = self.text.get("1.0", "end-1c").strip()
        if current and current != text.strip() and not messagebox.askyesno("Lyrebird", f"Replace the current script with {what}?"):
            return False
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()  # undo shouldn't bring back the previous script
        self.highlight()
        return True

    def load_script(self, path):
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        if self.replace_script(text, Path(path).name):
            self.status.set(f"Loaded {Path(path).name}.")

    def session_dir(self):
        if self.session_path:
            return self.session_path.parent
        try:
            return self.out_folder()
        except OSError:  # e.g. the output drive is unplugged; the dialog just starts somewhere else
            return None

    def set_session(self, path):
        self.session_path = Path(path) if path else None
        self.session_label.configure(text=f"·  {self.session_path.stem}" if self.session_path else "")
        self.root.title(f"{self.session_path.stem} - Lyrebird" if self.session_path else "Lyrebird")

    def save_session(self, ask=False):
        """Save the script and every setting to a .lyrebird file (voices are referenced by name)."""
        path = self.session_path
        if ask or path is None:
            chosen = filedialog.asksaveasfilename(title="Save session", defaultextension=SESSION_EXT,
                                                  filetypes=[("Lyrebird sessions", f"*{SESSION_EXT}")],
                                                  initialdir=self.session_dir(),
                                                  initialfile=(path.name if path else f"session{SESSION_EXT}"))
            if not chosen:
                return
            path = Path(chosen)
        text = self.text.get("1.0", "end-1c")
        names = [who for who, _ in split_speakers(text, self.voices) if who]
        if self.ref:
            names.insert(0, self.voice_var.get())
        settings = {k: v for k, v in self.settings().items() if k not in ("geometry", "appearance")}
        data = {"lyrebird": "session", "format": 1, "app_version": VERSION, "script": text,
                "settings": settings, "voices": sorted(set(names))}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.set_session(path)
        self.status.set(f"Saved session {path.name}.")

    def open_session(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            assert data.get("lyrebird") == "session" and isinstance(data.get("script"), str)
        except (OSError, ValueError, AssertionError, AttributeError):
            messagebox.showerror("Lyrebird", f"{Path(path).name} isn't a Lyrebird session.")
            return
        if not self.replace_script(data["script"], f"the session {Path(path).stem}"):
            return
        self.refresh_voices()
        self.apply_settings(data.get("settings") or {})
        self.sync_options()
        self.set_session(path)
        missing = [n for n in data.get("voices", []) if isinstance(n, str) and n not in self.voices]
        self.status.set(f"Opened {Path(path).name}." + (f" Missing voices: {', '.join(missing)}. Record or import "
                                                          "them under those names." if missing else ""))

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
            b.configure(state="disabled")
        self.progress.configure(mode="indeterminate", progress_color=PLUME)
        self.progress.start()

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
        self.progress.configure(mode="determinate", progress_color=LINE)  # idle: no stray dot at 0 %
        self.progress.set(0)
        self.stop_button.configure(state="disabled")
        for b in self.buttons:
            b.configure(state="normal")

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
                            self.ui(self.status.set, f"Recording {label}... {left} s left. Play the voice now.")
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
                    self.ui(self.status.set, f"Recording from {info['name']}... {left} s left. Speak now.")
                    time.sleep(1)
                sd.wait()
                mono = audio[:, np.abs(audio).max(axis=0).argmax()]  # keep the loudest channel
            if np.abs(mono).max() < 1e-4:
                raise RuntimeError(f"The recording from {label} is silent, so the voice wasn't saved. "
                                   "Check the source, or for 'What you hear' make sure audio is playing.")
            self.ui(self.status.set, "Trim the recording, then save it.")
            self.ui(self.save_voice, mono.astype(np.float32), sr, path, f'Trim "{path.stem}"')

        self.run_bg(work)

    def import_audio(self, src=None):
        src = src or filedialog.askopenfilename(title="Choose a voice sample", filetypes=AUDIO_TYPES)
        if not src:
            return
        path = self.ask_voice_path(Path(src).stem)
        if path:
            audio, sr = load_audio(src)  # read before saving: src may be the voice file being replaced
            self.save_voice(audio, sr, path, f'Trim "{path.stem}"')

    def rename_voice(self):
        old = self.voice_var.get()
        new = clean_name(ask_text(self.root, "Lyrebird", f'New name for "{old}":', old))
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
            self.ui(self.status.set, f"Saved {self.output} ({time.time() - start:.0f} s).")

        self.run_bg(work)
        self.stop_button.configure(state="normal")

    def stop(self):
        self.cancel.set()
        self.stop_button.configure(state="disabled")
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


class Root(ctk.CTk, TkinterDnD.DnDWrapper):
    """The main window, with drag-and-drop of files onto it (tkdnd)."""

    def __init__(self):
        super().__init__()
        try:
            TkinterDnD._require(self)
            self.dnd = True
        except RuntimeError as e:  # the app still works without it; Open... does the same job
            print(f"Drag and drop unavailable: {e}")
            self.dnd = False


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
    # The splash art is always the light theme, so it uses the light ink colours.
    canvas.create_text(242 * k, 284 * k, anchor="w", text=f"Version {VERSION}", fill=INK[0], font=("Segoe UI", 9, "bold"))
    status = canvas.create_text(242 * k, 303 * k, anchor="w", text="Starting...", fill=MUTED[0], font=("Segoe UI", 9))
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 2}")
    win.update()

    def set_status(text):
        canvas.itemconfigure(status, text=text)
        win.update()

    return win, set_status


def main():
    try:
        # Own taskbar identity, so Windows shows the Lyrebird icon rather than python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lyrebird.App")
    except (AttributeError, OSError):
        pass
    appearance = load_settings().get("appearance")
    ctk.set_appearance_mode(appearance if appearance in ("Light", "Dark", "System") else "System")
    root = Root()  # CustomTkinter also turns on per-monitor DPI awareness
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
