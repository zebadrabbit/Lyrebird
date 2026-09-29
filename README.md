# Lyrebird

A small Windows desktop app for voice cloning with [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) by Resemble AI.

1. **Record (10s)** of your voice, or **Import audio** of someone who has agreed to be cloned.
2. Type some text.
3. Click **Render**. You get a `.wav` file.

Everything runs locally on your PC. The network is used only to download the models on first use: the weights come from Hugging Face, and Multilingual also fetches a small Chinese word-segmentation model from GitHub. When a model loads, Lyrebird also checks Hugging Face for updates. To stop that, see `HF_HUB_OFFLINE` below.

> The name comes from the lyrebird, an Australian bird that can copy almost any sound it hears.

---

## Quick start (prebuilt executable)

1. Download `Lyrebird-win64.zip` from the Releases page and unzip it anywhere.
2. Run `Lyrebird\Lyrebird.exe`.
3. The first **Render** downloads the chosen model to your Hugging Face cache (`%USERPROFILE%\.cache\huggingface`). Each model is roughly 3 to 4 GB. After that, it also works offline.

**Requirements:** 64-bit Windows 10 or 11. An NVIDIA GPU is strongly recommended (the CUDA 12.8 build supports GTX 900 series through RTX 50 series; about 6 GB of VRAM is recommended; the models use 3 to 4 GB while rendering). Lyrebird falls back to the CPU when there is no GPU. The CPU works but is slow: expect tens of seconds or more per sentence.

## Using it

| Section | What it does |
|---|---|
| **1. Voice to clone** | **Record (10s)** records 10 seconds from your default microphone and saves it as `reference_<time>.wav` in the output folder. **Import audio...** accepts `.wav`, `.mp3`, `.flac` or `.ogg`. **Play** plays the reference and **Clear** removes it. With no reference set, the model's built-in voice is used. |
| **2. Text to speak** | Any length. Text is split at sentence ends and line breaks, and the pieces are packed into chunks of up to 300 characters (100 for Chinese, Japanese and Korean). A single sentence longer than that is cut between words. Each chunk is rendered separately, and the chunks are joined with a 0.2 s gap. The models stop after about 40 s of audio per chunk, which is why the text is split. |
| **3. Options** | See [Options](#options) below. |
| **4. Render** | Writes `lyrebird_<YYYYmmdd_HHMMSS>.wav` into the **Save to** folder (default `Documents\Lyrebird`). **Play output** plays the last render and **Open folder** opens the output folder in Explorer. |

**Tips for a good clone:** use 5 to 20 seconds of one person speaking naturally, with no music, no other voices and little room echo. If you use a reference with Turbo, it must be longer than 5 seconds. If you record, start talking right away and keep going for the full 10 seconds.

## Options

| Option | Range (default) | Models | Effect |
|---|---|---|---|
| **Model** | Standard / Turbo / Multilingual (Standard) | all | **Standard**: the original 0.5B English model, best quality and supports every option. **Turbo**: a smaller (350M) English model that is much faster and understands tags such as `[laugh]`, `[chuckle]` and `[cough]` inline in the text. **Multilingual**: the 0.5B model covering the 23 languages listed below. Only one model is kept in memory; switching models unloads the previous one. |
| **Language** | 23 languages (en) | Multilingual | The language of the *text*. The reference voice can be in any language, but a reference in the same language as the text sounds most natural. Supported: Arabic, Chinese, Danish, Dutch, English, Finnish, French, German, Greek, Hebrew, Hindi, Italian, Japanese, Korean, Malay, Norwegian, Polish, Portuguese, Russian, Spanish, Swahili, Swedish, Turkish. |
| **Exaggeration** | 0.25 – 2.0 (0.5) | Standard, Multilingual | Emotional intensity. At 0.5 the delivery is neutral. At 0.7 and above it becomes more dramatic and tends to speed up. Very high values can become unstable. |
| **CFG / pace** | 0.0 – 1.0 (0.5) | Standard, Multilingual | Classifier-free guidance weight, i.e. how closely the output follows the reference's style. Lower values give slower, more deliberate speech. Try about 0.3 when you raise Exaggeration, or if the reference speaker talks fast. With Multilingual, use 0 when the reference is in a different language from the text, to reduce accent carry-over. On Standard, 0 is treated as 0.001 because Chatterbox 0.1.7 crashes at exactly 0. |
| **Temperature** | 0.05 – 2.0 (0.8) | all | Sampling randomness. Lower values give flatter, more consistent speech. Higher values give more variety but also more mistakes. |
| **Seed** | 0 – 2³¹−1 (0) | all | 0 gives a different take on every render. Any other number makes a render repeatable: same seed, text, reference, settings and hardware give the same result. |
| **Save to** | folder (`Documents\Lyrebird`) | all | Where rendered files and recordings are written. The folder is created if it doesn't exist. |

Exaggeration and CFG are greyed out for Turbo because the Turbo model doesn't support them. Language is greyed out except for Multilingual.

Output is mono, 24 kHz, 16-bit PCM WAV.

### Environment variables

| Variable | Effect |
|---|---|
| `HF_HOME` | Moves the Hugging Face cache, where the models are stored, somewhere other than `%USERPROFILE%\.cache\huggingface`. |
| `HF_TOKEN` | Hugging Face access token. It isn't needed for the public Chatterbox models, but it avoids anonymous rate limits. |
| `HF_HUB_OFFLINE=1` | Never contacts Hugging Face. The models must already be cached. |
| `PKUSEG_HOME` | Where Multilingual stores its Chinese segmentation model (default `%USERPROFILE%\.pkuseg`). To use Multilingual offline, this folder must already be there, for example copied from a PC that has loaded the model once. |

### Logs

The `.exe` has no console window. Its output and errors go to `%LOCALAPPDATA%\Lyrebird\lyrebird.log`, which is overwritten on each launch. Please attach this file to bug reports.

## Build from source

You need Windows, Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) (`winget install astral-sh.uv`).

```powershell
git clone <this repo> Lyrebird
cd Lyrebird
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` does the following:

1. Creates `.venv` with a uv-managed Python 3.12. The build uses uv's Python because python.org installs can omit tkinter.
2. Installs `requirements.txt`, which contains PyTorch 2.7.1 with CUDA 12.8 plus Chatterbox's dependencies.
3. Installs `chatterbox-tts==0.1.7` with `--no-deps`. Chatterbox pins `torch==2.6.0`, which has no RTX 50-series (Blackwell) support, so the build installs its dependencies itself in step 2.
4. Runs the tests, then uses PyInstaller to build `dist\Lyrebird\` (a folder containing `Lyrebird.exe` and its libraries).

To ship it, zip the whole `dist\Lyrebird` folder. The CUDA build is several GB because most of it is PyTorch's CUDA libraries.

**CPU-only build**, which is much smaller: in `requirements.txt`, delete the `--extra-index-url` line and the `+cu128` suffixes, then delete `.venv` and rebuild.

**Run without building:**

```powershell
.venv\Scripts\python.exe lyrebird.py
```

**Tests** (no model or GPU needed):

```powershell
.venv\Scripts\python.exe test_lyrebird.py
```

## Project layout

```
lyrebird.py        the whole app (tkinter GUI + Chatterbox wrapper)
test_lyrebird.py   tests for the text chunker
requirements.txt   pinned runtime dependencies
build.ps1          venv setup + PyInstaller build
```

## Responsible use

Only clone voices you have permission to use. Every file Chatterbox generates carries Resemble AI's [Perth](https://github.com/resemble-ai/perth) watermark: an imperceptible mark that detection tools can find. Lyrebird doesn't remove it. Don't use this software to impersonate people, commit fraud or deceive anyone.

## Credits and license

- [Chatterbox](https://github.com/resemble-ai/chatterbox) TTS models and code: © Resemble AI, MIT License.
- Lyrebird: MIT License, see [LICENSE](LICENSE).
- Binary releases also bundle third-party packages under their own licenses. In particular, [pykakasi](https://codeberg.org/miurahr/pykakasi) (Japanese reading conversion, used by Multilingual) is GPL-3.0-or-later, so the distributed `.exe` bundle as a whole is covered by the GPL-3.0. The Lyrebird source code itself stays MIT. To build an MIT-only binary, remove the two `pykakasi` lines from `build.ps1` and add `--exclude-module pykakasi`. Japanese text then skips kanji-to-kana conversion.
