# Lyrebird brand kit

Source of truth for Lyrebird's look and voice. Read this before touching icons, colours, fonts or user-facing copy.

## Files

| Path | What |
|---|---|
| `tokens.json` | All tokens (colour per theme, type, spacing, radius, shadow) with usage notes |
| `tokens.css` | The same as CSS custom properties + `@font-face` + type-style classes (`.display`, `.body`, `.script`…) |
| `fonts/` | Bricolage Grotesque, Atkinson Hyperlegible Next, IBM Plex Mono (woff2), each under the SIL Open Font License 1.1; see the `OFL-*.txt` files |
| `logos/` | Mark (plume / on-dark / single-ink) and wordmarks, SVG |
| `app-icon/` | Icon tile SVGs: `app-icon` (64px+), `app-icon-medium` (32–48), `app-icon-small` (16–24), `app-icon-256.png` |
| `lyrebird.ico` | Windows icon, 16–256 px, built from the three icon drawings |
| `components/` | Reference HTML + notes for SoundTag, DialogueScript and the cover (they expect `tokens.css`) |

## How the app uses it

- Window, dialog and taskbar icon: `lyrebird.py` and `launcher.py` call `root.iconbitmap(default=...)` with `lyrebird.ico`. They look next to the script first (the release bundle) and then in `brand/` (running from source). `lyrebird.py` also sets its own AppUserModelID, so the taskbar shows this icon rather than python.exe's.
- Exe icon: `build.ps1` passes `--icon brand\lyrebird.ico` to PyInstaller and bundles the `.ico` with `--add-data`.
- Tk editor colours in `lyrebird.py` map to tokens: `#1a5fb4`=`speaker`, `#d7ebff`/`#0b4f8a`=`tag-bg`/`tag`, `#fff0c2`/`#8a5a00`=`tag-off-bg`/`tag-off`, `#c01c28`=`tag-bad`. Use `tokens.json` light values in Tk.

Lyrebird is a small Windows app that turns typed dialogue into speech in voices you record or import, running entirely on your own PC. The name is the Australian bird that can copy almost any sound it hears. The brand borrows two things from it: the feather, and the idea of a perfect copy made with care.

Tagline: **Hear it once. Say it in any voice.**

## The mark

The mark is a single feather. Its outer vane is solid with two small notches, like the bands in a lyrebird's tail. Its inner vane is a row of barbs whose lengths form an audio waveform. It is the feather and the voice in one shape.

- Use `logos/lyrebird-mark.svg` (plume) on `ground` and `surface`; `lyrebird-mark-on-dark.svg` (plume + lyre barbs) on `fern` or the dark theme; `lyrebird-mark-ink.svg` when only one ink is possible (print, stamps, single-colour overlays).
- The wordmark is the mark plus "Lyrebird" set in Bricolage Grotesque 700 and outlined. Write the name as **Lyrebird**: one word, capital L, never "LyreBird" or "lyrebird" in running text.
- Keep clear space of half the mark's height on every side. Never rotate, recolour outside the three variants, add a stroke, or put the mark on `plume`.
- The app icon is the mark on a `fern` tile with `radius-lg` corners. It has three drawings, not one scaled image: full waveform at 64 px and up (`app-icon/app-icon.svg`), six thick barbs at 32–48 px (`app-icon/app-icon-medium.svg`), and a plain two-vane feather at 16–24 px (`app-icon/app-icon-small.svg`) because barbs turn to mush at taskbar size. The Windows `.ico` bundles all three.

## Voice

Lyrebird writes like a patient friend who knows the hardware. The README and HOWTO set the tone; copy them.

- Plain, short, second person. "Click **Render**. You get a `.wav` file."
- Name UI controls exactly as they appear, in bold: **Record (10s)**, **Import**, **Save to**.
- Give real numbers with honest hedges: "about 33 MB", "roughly 3 to 4 GB", "expect tens of seconds or more per sentence".
- Say what will happen before it happens, especially downloads and waits.
- Consent is part of the product, not a disclaimer. Always frame imported voices as "a clip of someone who has agreed to be cloned". Never write copy that suggests imitating a person without permission.
- No emoji, no exclamation marks, no "magic" or "AI-powered". The feature is the claim.

## Colour

- `ground` is the default background; panels sit on `surface` and separate from it with a `line` hairline rather than shadow.
- `plume` is the one brand hue. Spend it on the mark, the primary action (**Render**), links and the focus ring. Text on a plume fill is `on-plume`.
- `fern` and `lyre` are brand fills: the icon tile, dark hero panels, the mark's barbs. Never set text in `lyre` on a light ground.
- The editor colours (`speaker`, `tag`/`tag-bg`, `tag-off`/`tag-off-bg`, `tag-bad`) are the values the app already uses in `lyrebird.py`, with dark partners added. They mean state, not brand: blue = the tag will be spoken, amber = the current model ignores it, red + underline = unknown tag.
- Every text pair clears 4.5:1 in both themes. The focus ring is a solid 2 px `plume` outline with a 2 px offset.

## Type

- Headlines in **Bricolage Grotesque** (`display-xl`, `display`, `heading`, `subhead`). It has a slightly hand-cut, vocal feel; use weights 600–800 and keep it to headlines.
- Running text in **Atkinson Hyperlegible Next** (`body`, `body-sm`, `label`): built for legibility, which suits an app people use to make accessible audio.
- Dialogue scripts, sound tags, seeds and file names in **IBM Plex Mono** (`script`, `meta`).
- The desktop app itself stays on Segoe UI (`app-ui`) so it feels native on Windows. Brand faces are for the website, README banners, release notes and store art.

## Shape and layout

- Spacing on a 4 px base: `space-1` to `space-12`. Panels pad `space-4`; groups separate by `space-6`.
- Corners: `radius-sm` chips and inputs, `radius-md` buttons and panels, `radius-pill` model badges, `radius-lg` only for the icon tile and big brand panels.
- Barbs are the recurring graphic motif: short rounded bars at a steady pitch, lengths rising and falling like a waveform. Use them as dividers, loading indicators and background texture, in `plume` on light or `lyre` on `fern`.

## Components

- **SoundTag** — how `[laugh]`-style tags look in the script editor in each state.
- **DialogueScript** — a multi-speaker script as shown in docs and marketing.
