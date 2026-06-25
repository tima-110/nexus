"""Tests for OCC option symbol parsing."""
from __future__ import annotations

import pytest

from nexus.occ import is_occ_symbol, occ_to_underlying, parse_occ_symbol


class TestIsOccSymbol:
    def test_valid_put(self):
        assert is_occ_symbol("NKE260718P00040000") is True

    def test_valid_call(self):
        assert is_occ_symbol("AAPL260821C00225000") is True

    def test_valid_long_root(self):
        assert is_occ_symbol("SPXW260718C05000000") is True

    def test_equity_symbol_false(self):
        assert is_occ_symbol("AAPL") is False

    def test_empty_false(self):
        assert is_occ_symbol("") is False

    def test_none_false(self):
        assert is_occ_symbol(None) is False

    def test_lowercase_false(self):
        assert is_occ_symbol("nke260718p00040000") is False


class TestParseOccSymbol:
    def test_parse_put(self):
        result = parse_occ_symbol("NKE260718P00040000")
        assert result["root"] == "NKE"
        assert result["expiry"] == "2026-07-18"
        assert result["option_type"] == "P"
        assert result["strike"] == 40.00
        assert result["right"] == "put"

    def test_parse_call(self):
        result = parse_occ_symbol("AAPL260821C00225000")
        assert result["root"] == "AAPL"
        assert result["expiry"] == "2026-08-21"
        assert result["option_type"] == "C"
        assert result["strike"] == 225.00
        assert result["right"] == "call"

    def test_parse_long_root(self):
        result = parse_occ_symbol("SPXW260718C05000000")
        assert result["root"] == "SPXW"
        assert result["strike"] == 5000.00

    def test_invalid_symbol(self):
        with pytest.raises(ValueError, match="Invalid OCC symbol"):
            parse_occ_symbol("AAPL")


class TestOccToUnderlying:
    def test_occ_symbol(self):
        assert occ_to_underlying("NKE260718P00040000") == "NKE"

    def test_equity_symbol(self):
        assert occ_to_underlying("AAPL") == "AAPL"