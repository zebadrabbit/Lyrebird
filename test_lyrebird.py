"""Run with `python test_lyrebird.py` (or pytest). Needs no model or GPU."""
import tempfile
from pathlib import Path

import lyrebird
from lyrebird import chunk_text, drop_older_versions, split_speakers, strip_tags, voice_files


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


if __name__ == "__main__":
    test_chunk_text()
    test_split_speakers()
    test_strip_tags()
    test_voice_replacement()
    print("ok")
