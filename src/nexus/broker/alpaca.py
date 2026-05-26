from __future__ import annotations

import json
import subprocess
from decimal import Decimal

from nexus.broker.types import BrokerAccount, BrokerOrder, BrokerPosition


class AlpacaBroker:
    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name

    def _run(self, *args: str) -> dict | list:
        try:
            result = subprocess.run(
                ["alpaca", *args, "--profile", self.profile_name],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            raise RuntimeError(f"Alpaca CLI not found or not executable: {e}") from e
        if result.returncode != 0:
            raise RuntimeError(f"Alpaca CLI error: {result.stderr}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse Alpaca response: {e}") from e

    def _order_from_dict(self, data: dict) -> BrokerOrder:
        filled_avg = data.get("filled_avg_price")
        return BrokerOrder(
            broker_order_id=data["id"],
            client_order_id=data["client_order_id"],
            status=data["status"],
            symbol=data["symbol"],
            side=data["side"],
            qty=int(data["qty"]),
            filled_qty=int(data.get("filled_qty", 0)),
            filled_avg_price=Decimal(str(filled_avg)) if filled_avg is not None else None,
            submitted_at=data["submitted_at"],
            filled_at=data.get("filled_at"),
        )

    def submit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str,
        **params,
    ) -> BrokerOrder:
        args = [
            "order", "submit",
            "--symbol", symbol,
            "--qty", str(qty),
            "--side", side,
            "--type", order_type,
        ]
        if "client_order_id" in params:
            args += ["--client-order-id", params["client_order_id"]]
        if params.get("limit_price") is not None:
            args += ["--limit-price", str(params["limit_price"])]
        if params.get("stop_price") is not None:
            args += ["--stop-price", str(params["stop_price"])]
        if params.get("trail_percent") is not None:
            args += ["--trail-percent", str(params["trail_percent"])]
        data = self._run(*args)
        return self._order_from_dict(data)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        data = self._run("order", "get", "--order-id", broker_order_id)
        return self._order_from_dict(data)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder:
        data = self._run("order", "get-by-client-id", "--client-order-id", client_order_id)
        return self._order_from_dict(data)

    def list_orders(self, status: str = "open") -> list[BrokerOrder]:
        data = self._run("order", "list", "--status", status)
        return [self._order_from_dict(item) for item in data]

    def cancel_order(self, broker_order_id: str) -> None:
        self._run("order", "cancel", "--order-id", broker_order_id)

    def get_account(self) -> BrokerAccount:
        data = self._run("account", "get")
        return BrokerAccount(
            cash=Decimal(str(data["cash"])),
            buying_power=Decimal(str(data["buying_power"])),
            equity=Decimal(str(data["equity"])),
        )

    def get_positions(self) -> list[BrokerPosition]:
        data = self._run("position", "list")
        return [
            BrokerPosition(
                symbol=item["symbol"],
                qty=int(item["qty"]),
                avg_entry_price=Decimal(str(item["avg_entry_price"])),
                current_price=Decimal(str(item["current_price"])),
                unrealized_pl=Decimal(str(item["unrealized_pl"])),
            )
            for item in data
        ]

    def get_last_price(self, symbol: str) -> Decimal:
        data = self._run("data", "latest-trade", "--symbol", symbol)
        # Response is a dict with trade data including a "price" or "p" field
        if isinstance(data, dict):
            trade = data.get("trade", data)
            price = trade.get("p") or trade.get("price")
        else:
            raise RuntimeError(f"Failed to parse Alpaca response: unexpected format {type(data)}")
        if price is None:
            raise RuntimeError(f"Failed to parse Alpaca response: no price field in {data}")
        return Decimal(str(price))
