"""Poll trades endpoint to sync AMM inventory into Redis."""
import logging

from src.amm.connector.api_client import AMMApiClient
from src.amm.cache.inventory_cache import InventoryCache

logger = logging.getLogger(__name__)


class TradePoller:
    def __init__(self, api: AMMApiClient, cache: InventoryCache) -> None:
        self._api = api
        self._cache = cache
        self._cursors: dict[str, str] = {}
        self._processed_ids: set[str] = set()

    async def poll(self, market_id: str) -> int:
        """Poll for new trades, update Redis inventory. Returns count of new trades."""
        cursor = self._cursors.get(market_id, "")
        resp = await self._api.get_trades(cursor=cursor, limit=50)
        trades = resp.get("data", {}).get("trades", [])

        new_count = 0
        for trade in trades:
            trade_id = trade["id"]
            if trade_id in self._processed_ids:
                continue
            self._processed_ids.add(trade_id)
            await self._apply_trade(market_id, trade)
            new_count += 1

        if trades:
            self._cursors[market_id] = trades[-1]["id"]

        return new_count

    async def _apply_trade(self, market_id: str, trade: dict) -> None:
        from src.pm_account.domain.constants import AMM_USER_ID

        scenario = trade["scenario"]
        quantity = trade["quantity"]
        price = trade["price_cents"]
        is_buyer = trade["buy_user_id"] == AMM_USER_ID

        if is_buyer:
            fee = trade.get("buyer_fee_cents", 0)
        else:
            fee = trade.get("seller_fee_cents", 0)

        if scenario == "MINT":
            cost = quantity * 100
            await self._cache.adjust(market_id, yes_delta=quantity, no_delta=quantity,
                                     cash_delta=-cost,
                                     yes_cost_delta=quantity * 50,
                                     no_cost_delta=quantity * 50)
        elif scenario == "BURN":
            recovery = quantity * 100
            await self._cache.adjust(market_id, yes_delta=-quantity, no_delta=-quantity,
                                     cash_delta=recovery,
                                     yes_cost_delta=-(quantity * 50),
                                     no_cost_delta=-(quantity * 50))
        elif scenario == "TRANSFER_YES":
            trade_value = price * quantity
            if is_buyer:
                await self._cache.adjust(market_id, yes_delta=quantity,
                                         cash_delta=-(trade_value + fee),
                                         yes_cost_delta=trade_value)
            else:
                await self._cache.adjust(market_id, yes_delta=-quantity,
                                         cash_delta=(trade_value - fee),
                                         yes_cost_delta=-trade_value)
        elif scenario == "TRANSFER_NO":
            no_price = 100 - price
            trade_value = no_price * quantity
            if is_buyer:
                await self._cache.adjust(market_id, no_delta=quantity,
                                         cash_delta=-(trade_value + fee),
                                         no_cost_delta=trade_value)
            else:
                await self._cache.adjust(market_id, no_delta=-quantity,
                                         cash_delta=(trade_value - fee),
                                         no_cost_delta=-trade_value)
