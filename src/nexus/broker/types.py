from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    status: str
    symbol: str
    side: str
    qty: int
    filled_qty: int
    filled_avg_price: Decimal | None
    submitted_at: str
    filled_at: str | None


@dataclass
class BrokerAccount:
    cash: Decimal
    buying_power: Decimal
    equity: Decimal


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pl: Decimal


@dataclass
class BrokerOptionPosition:
    symbol: str
    underlying: str
    side: str           # "short" | "long"
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pl: Decimal
    strike: Decimal
    expiry: str
    option_right: str   # "call" | "put"
