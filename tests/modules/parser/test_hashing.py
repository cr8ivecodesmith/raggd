"""Tests for parser hashing utilities."""

from __future__ import annotations

from pathlib import Path

from raggd.modules.parser.hashing import hash_file, hash_stream, hash_text


def test_hash_stream_includes_handler_version() -> None:
    baseline = hash_stream(
        handler_version="1.0",
        chunks=[b"example"],
    )
    updated = hash_stream(
        handler_version="2.0",
        chunks=[b"example"],
    )
    assert baseline != updated


def test_hash_file_matches_text(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world", encoding="utf-8")

    file_digest = hash_file(file_path, handler_version="1.0")
    text_digest = hash_text("hello world", handler_version="1.0")

    assert file_digest == text_digest


def test_hash_stream_with_extra_payload() -> None:
    digest_a = hash_stream(
        handler_version="1.0",
        chunks=[b"alpha"],
        extra=[b"beta"],
    )
    digest_b = hash_stream(
        handler_version="1.0",
        chunks=[b"alpha"],
    )

    assert digest_a != digest_b

