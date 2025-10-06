"""Tests for parser token counting helpers."""

from __future__ import annotations

import pytest

from raggd.modules.parser.tokenizer import (
    DEFAULT_ENCODER,
    TokenEncoderError,
    get_token_encoder,
)


def test_get_token_encoder_counts_tokens() -> None:
    pytest.importorskip("tiktoken")
    encoder = get_token_encoder(DEFAULT_ENCODER)
    assert encoder.count("Hello world!") > 0


def test_get_token_encoder_unknown_name_raises() -> None:
    with pytest.raises(TokenEncoderError):
        get_token_encoder("not-a-real-encoder")
