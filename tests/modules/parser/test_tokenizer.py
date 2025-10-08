"""Tests for parser token counting helpers."""

from __future__ import annotations

from math import ceil

import pytest

from raggd.modules.parser.tokenizer import (
    DEFAULT_ENCODER,
    TokenEncoderError,
    get_token_encoder,
)
import raggd.modules.parser.tokenizer as tokenizer


def test_get_token_encoder_counts_tokens() -> None:
    pytest.importorskip("tiktoken")
    encoder = get_token_encoder(DEFAULT_ENCODER)
    assert encoder.count("Hello world!") > 0


def test_get_token_encoder_unknown_name_raises() -> None:
    with pytest.raises(TokenEncoderError):
        get_token_encoder("not-a-real-encoder")


def test_get_token_encoder_fallback_when_tiktoken_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokenizer, "tiktoken", None, raising=False)
    monkeypatch.setattr(tokenizer, "_FALLBACK_NOTICE_EMITTED", False, raising=False)
    tokenizer._load_encoding.cache_clear()

    encoder = get_token_encoder(DEFAULT_ENCODER)
    sample = "Hello world!"
    expected = max(1, ceil(len(sample) / 4))

    assert encoder.count(sample) == expected
    assert len(encoder.encoding.encode(sample)) == expected

    tokenizer._load_encoding.cache_clear()
