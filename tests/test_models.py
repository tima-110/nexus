"""Tests for nexus.models — enum behavior."""
from __future__ import annotations

from nexus.models import OrderSide, OrderStatus, OrderType, TransactionType


class TestOrderStatus:
    def test_values_are_strings(self):
        assert OrderStatus.filled == "filled"
        assert OrderStatus.pending == "pending"
        assert OrderStatus.submitted == "submitted"
        assert OrderStatus.partially_filled == "partially_filled"
        assert OrderStatus.cancelled == "cancelled"
        assert OrderStatus.cancel_pending == "cancel_pending"
        assert OrderStatus.cancel_failed == "cancel_failed"
        assert OrderStatus.expired == "expired"
        assert OrderStatus.rejected == "rejected"

    def test_all_members_exist(self):
        members = {m.name for m in OrderStatus}
        assert members == {
            "pending",
            "submitted",
            "filled",
            "partially_filled",
            "cancelled",
            "cancel_pending",
            "cancel_failed",
            "expired",
            "rejected",
        }

    def test_string_comparison(self):
        status = "filled"
        assert status == OrderStatus.filled

    def test_enum_is_str_subclass(self):
        assert isinstance(OrderStatus.filled, str)


class TestOrderSide:
    def test_values_are_strings(self):
        assert OrderSide.buy == "buy"
        assert OrderSide.sell == "sell"

    def test_all_members_exist(self):
        members = {m.name for m in OrderSide}
        assert members == {"buy", "sell"}

    def test_string_comparison(self):
        side = "buy"
        assert side == OrderSide.buy

    def test_enum_is_str_subclass(self):
        assert isinstance(OrderSide.buy, str)


class TestOrderType:
    def test_values_are_strings(self):
        assert OrderType.market == "market"
        assert OrderType.limit == "limit"
        assert OrderType.stop == "stop"
        assert OrderType.stop_limit == "stop_limit"
        assert OrderType.trailing_stop == "trailing_stop"

    def test_all_members_exist(self):
        members = {m.name for m in OrderType}
        assert members == {"market", "limit", "stop", "stop_limit", "trailing_stop"}

    def test_enum_is_str_subclass(self):
        assert isinstance(OrderType.market, str)


class TestTransactionType:
    def test_values_are_strings(self):
        assert TransactionType.fill_buy == "fill_buy"
        assert TransactionType.fill_sell == "fill_sell"
        assert TransactionType.deposit == "deposit"
        assert TransactionType.withdrawal == "withdrawal"
        assert TransactionType.adjustment == "adjustment"

    def test_all_members_exist(self):
        members = {m.name for m in TransactionType}
        assert members == {
            "fill_buy",
            "fill_sell",
            "deposit",
            "withdrawal",
            "adjustment",
        }

    def test_enum_is_str_subclass(self):
        assert isinstance(TransactionType.fill_buy, str)
