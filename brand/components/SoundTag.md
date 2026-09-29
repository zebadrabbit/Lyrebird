# SoundTag

A sound tag such as `[laugh]` or `[whispering]` inside a script, coloured by whether the selected model will speak it.

- **supported** — `tag` on `tag-bg`: the Turbo model will perform it.
- **ignored** — `tag-off` on `tag-off-bg`: the current model skips it; suggest switching to Turbo.
- **unknown** — `tag-bad` text with an underline: not a tag Lyrebird knows. The underline carries the meaning, not only the red.

The consumer provides the tag text including brackets. Set it in `script` (IBM Plex Mono) with `radius-sm` corners and `space-1`/`space-2` padding. Never use plume for tags; plume is the brand, tags are state.
