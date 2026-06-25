"""Domain types shared across Nexus modules."""

from enum import Enum


class OrderSide(str, Enum):
    buy = "buy"
    sell = "sell"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"
    stop = "stop"
    stop_limit = "stop_limit"
    trailing_stop = "trailing_stop"


class OrderStatus(str, Enum):
    pending = "pending"
    submitted = "submitted"
    filled = "filled"
    partially_filled = "partially_filled"
    cancelled = "cancelled"
    cancel_pending = "cancel_pending"
    cancel_failed = "cancel_failed"
    expired = "expired"
    rejected = "rejected"


class TransactionType(str, Enum):
    fill_buy = "fill_buy"
    fill_sell = "fill_sell"
    deposit = "deposit"
    withdrawal = "withdrawal"
    adjustment = "adjustment"


class OptionRight(str, Enum):
    call = "call"
    put = "put"


class AssetClass(str, Enum):
    equity = "equity"
    option = "option"
