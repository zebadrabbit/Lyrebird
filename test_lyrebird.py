"""Run with `python test_lyrebird.py` (or pytest). Needs no model or GPU."""
from lyrebird import chunk_text


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


if __name__ == "__main__":
    test_chunk_text()
    print("ok")
