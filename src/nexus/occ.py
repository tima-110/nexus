"""OCC option symbol parsing utilities."""

from __future__ import annotations

import re


OCC_PATTERN = re.compile(
    r"^[A-Z]{1,6}\d{6}[CP]\d{8}$"
)


def is_occ_symbol(symbol: str) -> bool:
    """Return True if symbol matches OCC format.

    OCC format: ROOT (1-6 chars) + YYMMDD + C/P + 8-digit strike (×1000)
    Example: NKE260718P00040000
    """
    if not isinstance(symbol, str):
        return False
    return bool(OCC_PATTERN.match(symbol))


def parse_occ_symbol(symbol: str) -> dict:
    """Parse OCC option symbol into components.

    Args:
        symbol: OCC format symbol (e.g., NKE260718P00040000)

    Returns:
        Dict with keys: root, expiry (YYYY-MM-DD), option_type ("C" or "P"),
        strike (float), right ("call" or "put")

    Raises:
        ValueError: If symbol doesn't match OCC format
    """
    if not is_occ_symbol(symbol):
        raise ValueError(f"Invalid OCC symbol format: {symbol}")

    # Find the position of C/P (right indicator)
    # It's at position len(root) + 6
    # Root length is variable (1-6 chars), but C/P is always at index -9
    right_char = symbol[-9]
    if right_char not in ("C", "P"):
        raise ValueError(f"Invalid OCC symbol: missing C/P at position -9: {symbol}")

    root = symbol[:-15]  # everything before YYMMDD + C/P + 8 digits
    yymmdd = symbol[-15:-9]
    strike_raw = symbol[-8:]

    # Parse date
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    expiry = f"{year:04d}-{month:02d}-{day:02d}"

    # Parse strike (stored as ×1000)
    strike = int(strike_raw) / 1000.0

    option_type = right_char
    right = "call" if right_char == "C" else "put"

    return {
        "root": root,
        "expiry": expiry,
        "option_type": option_type,
        "strike": strike,
        "right": right,
    }


def occ_to_underlying(symbol: str) -> str:
    """Extract underlying symbol from OCC format.

    Args:
        symbol: OCC format symbol

    Returns:
        Root symbol (e.g., NKE from NKE260718P00040000)
    """
    if is_occ_symbol(symbol):
        return symbol[:-15]
    return symbol