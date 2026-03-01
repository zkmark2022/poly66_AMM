"""Poll trades endpoint to sync AMM inventory into Redis."""
import logging

from src.amm.connector.api_client import AMMApiClient, _sanitize_id
from src.amm.cache.inventory_cache import InventoryCache
from src.pm_account.domain.constants import AMM_USER_ID

logger = logging.getLogger(__name__)


class TradePoller:
    def __init__(self, api: AMMApiClient, cache: InventoryCache,
                 amm_user_id: str = AMM_USER_ID) -> None:
        self._api = api
        self._cache = cache
        self._amm_user_id = amm_user_id
        self._cursors: dict[str, str] = {}
        self._processed_ids: set[str] = set()

    async def poll(self, market_id: str) -> list[dict]:
        """Poll for new trades, update Redis inventory. Returns new AMM trades processed."""
        market_id = _sanitize_id(market_id)
        cursor = self._cursors.get(market_id, "")
        resp = await self._api.get_trades(market_id=market_id, cursor=cursor, limit=50)
        trades = resp.get("data", {}).get("trades", [])

        new_trades: list[dict] = []
        for trade in trades:
            trade_id = _sanitize_id(trade["id"])
            if trade_id in self._processed_ids:
                continue
            self._processed_ids.add(trade_id)
            # Only apply trades that belong to this AMM account
            if not self._is_amm_trade(trade):
                logger.warning("Ignoring third-party trade %s", trade_id)
                continue
            await self._apply_trade(market_id, trade)
            new_trades.append(trade)

        if trades:
            self._cursors[market_id] = trades[-1]["id"]

        return new_trades

    def _is_amm_trade(self, trade: dict) -> bool:
        """Return True if this trade involves the AMM account as buyer or seller."""
        return (trade.get("buy_user_id") == self._amm_user_id
                or trade.get("sell_user_id") == self._amm_user_id)

    async def _apply_trade(self, market_id: str, trade: dict) -> None:
        scenario = trade["scenario"]
        quantity = trade["quantity"]
        price = trade["price_cents"]
        is_buyer = trade["buy_user_id"] == self._amm_user_id

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
                # Release proportional acquisition cost, not sale price
                inv = await self._cache.get(market_id)
                if inv and inv.yes_volume > 0:
                    # Integer arithmetic: avoid float precision errors in financial math
                    cost_basis = inv.yes_cost_sum_cents * quantity // inv.yes_volume
                else:
                    cost_basis = trade_value
                await self._cache.adjust(market_id, yes_delta=-quantity,
                                         cash_delta=(trade_value - fee),
                                         yes_cost_delta=-cost_basis)
        elif scenario == "TRANSFER_NO":
            no_price = 100 - price
            trade_value = no_price * quantity
            if is_buyer:
                await self._cache.adjust(market_id, no_delta=quantity,
                                         cash_delta=-(trade_value + fee),
                                         no_cost_delta=trade_value)
            else:
                # Release proportional acquisition cost, not sale price
                inv = await self._cache.get(market_id)
                if inv and inv.no_volume > 0:
                    # Integer arithmetic: avoid float precision errors in financial math
                    cost_basis = inv.no_cost_sum_cents * quantity // inv.no_volume
                else:
                    cost_basis = trade_value
                await self._cache.adjust(market_id, no_delta=-quantity,
                                         cash_delta=(trade_value - fee),
                                         no_cost_delta=-cost_basis)
