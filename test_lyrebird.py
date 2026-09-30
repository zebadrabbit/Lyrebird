"""Run with `python test_lyrebird.py` (or pytest). Needs no model or GPU."""
import tempfile
from pathlib import Path

import lyrebird
from lyrebird import (chunk_text, clean_name, drop_older_versions, load_voice_settings, speech_bounds, trim_bounds,
                      split_speakers, strip_tags, voice_files)


def test_chunk_text():
    assert chunk_text("") == []
    assert chunk_text("  Hello there.  ") == ["Hello there."]
    assert chunk_text("One. Two! Three?", limit=10) == ["One. Two!", "Three?"]
    assert chunk_text("Para one\n\nPara two", limit=10) == ["Para one", "Para two"]
    assert chunk_text("你好。世界！", limit=3) == ["你好。", "世界！"]
    assert chunk_text("line one\nline two", limit=10) == ["line one", "line two"]
    assert chunk_text("नमस्ते। दुनिया।", limit=8) == ["नमस्ते।", "दुनिया।"]
    assert chunk_text('He said "Stop." Then left.', limit=16) == ['He said "Stop."', "Then left."]
    long = "x" * 50 + "."
    assert chunk_text(f"{long} Hi.", limit=20) == ["x" * 20, "x" * 20, "x" * 10 + ". Hi."]  # hard-wrapped
    assert chunk_text("字" * 250, limit=100) == ["字" * 100, "字" * 100, "字" * 50]
    text = " ".join(f"Sentence number {i}." for i in range(100))
    chunks = chunk_text(text)
    assert all(len(c) <= 300 for c in chunks) and " ".join(chunks) == text


def test_split_speakers():
    names = ["Alice", "Bob"]
    assert split_speakers("Just narration.", names) == [(None, "Just narration.")]
    assert split_speakers("Intro.\nalice: Hi!\nStill Alice.\n\nBob:  Hey.", names) == [
        (None, "Intro."), ("Alice", "Hi!\nStill Alice."), ("Bob", "Hey.")]
    assert split_speakers("Note: not a speaker.\n[laugh] ha", names) == [(None, "Note: not a speaker.\n[laugh] ha")]
    assert split_speakers("Alice:\nBob: Only Bob speaks.", names) == [("Bob", "Only Bob speaks.")]


def test_strip_tags():
    assert strip_tags("Hi [laugh] there. [clear throat]Ok [laughs]") == "Hi there. Ok [laughs]"
    assert strip_tags("[sigh]\nFine.") == "Fine."
    assert strip_tags("Line one [gasp]\nLine two") == "Line one \nLine two"


def test_voice_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        lyrebird.voices_dir = lambda: Path(tmp)
        for name in ("Alice.wav", "alice.MP3", "Alice notes.txt", "Bob.wav"):
            (Path(tmp) / name).write_bytes(b"x")
        assert sorted(p.name for p in voice_files("ALICE")) == ["Alice.wav", "alice.MP3"]
        (Path(tmp) / "Alice.flac").write_bytes(b"new")
        drop_older_versions(Path(tmp) / "Alice.flac")
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["Alice notes.txt", "Alice.flac", "Bob.wav"]


def test_speech_bounds():
    import numpy as np

    sr = 1000
    audio = np.zeros(10 * sr, dtype=np.float32)
    audio[3 * sr:7 * sr] = 0.5 * np.sin(np.arange(4 * sr))  # speech from 3 s to 7 s, silence around it
    start, end = speech_bounds(audio, sr, pad=0.1)
    assert abs(start - 2.9 * sr) <= 10 and abs(end - 7.1 * sr) <= 10, (start, end)
    assert speech_bounds(np.zeros(sr, dtype=np.float32), sr) == (0, sr)  # all silent: keep everything
    assert speech_bounds(np.zeros(3, dtype=np.float32), sr) == (0, 3)  # shorter than one frame


def test_trim_bounds():
    import numpy as np

    sr = 1000
    speech = 0.5 * np.sin(np.arange(60 * sr)).astype(np.float32)  # a 60 s clip that is all speech
    start, end = trim_bounds(speech, sr)
    assert start == 0 and end == 15 * sr  # long clips start on a 15 s slice, from Auto-trim too
    short = np.concatenate([np.zeros(2 * sr, np.float32), speech[: 8 * sr], np.zeros(2 * sr, np.float32)])
    assert trim_bounds(short, sr) == speech_bounds(short, sr)  # normal recordings: just the speech


def test_clean_name():
    assert clean_name('  My: "voice"?. ') == "My_ _voice__"
    assert clean_name(None) == "" and clean_name(" ... ") == ""


def test_voice_settings():
    with tempfile.TemporaryDirectory() as tmp:
        lyrebird.voices_dir = lambda: Path(tmp)
        assert load_voice_settings("Alice") == {}  # no file
        (Path(tmp) / "Alice.json").write_text('{"temperature": 0.4, "bogus": 1}')
        assert load_voice_settings("Alice") == {"temperature": 0.4}  # unknown keys dropped
        (Path(tmp) / "Alice.json").write_text("not json")
        assert load_voice_settings("Alice") == {}
        (Path(tmp) / "Alice.json").write_text("[1, 2]")
        assert load_voice_settings("Alice") == {}


if __name__ == "__main__":
    test_chunk_text()
    test_split_speakers()
    test_strip_tags()
    test_voice_replacement()
    test_speech_bounds()
    test_trim_bounds()
    test_clean_name()
    test_voice_settings()
    print("ok")
