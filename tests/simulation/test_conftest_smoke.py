"""Smoke tests: verify all simulation fixtures are importable and yield usable values."""
import httpx
import pytest


def test_mock_exchange_yields_dict(mock_exchange: dict) -> None:
    assert isinstance(mock_exchange, dict)
    assert "orders_placed" in mock_exchange
    assert "orders_cancelled" in mock_exchange
    assert "call_log" in mock_exchange
    assert isinstance(mock_exchange["orders_placed"], list)
    assert isinstance(mock_exchange["orders_cancelled"], list)
    assert isinstance(mock_exchange["call_log"], list)


def test_mock_exchange_has_httpx_client(mock_exchange: dict) -> None:
    client = mock_exchange["client"]
    assert isinstance(client, httpx.AsyncClient)


def test_fake_redis_sync(fake_redis_sync) -> None:  # type: ignore[no-untyped-def]
    fake_redis_sync.set("k", "v")
    assert fake_redis_sync.get("k") == b"v"


async def test_fake_redis_async(fake_redis_async) -> None:  # type: ignore[no-untyped-def]
    await fake_redis_async.set("k", "v")
    val = await fake_redis_async.get("k")
    assert val == b"v"


def test_make_market_config_defaults(make_market_config) -> None:  # type: ignore[no-untyped-def]
    from src.amm.config.models import MarketConfig

    cfg = make_market_config()
    assert isinstance(cfg, MarketConfig)
    assert cfg.market_id == "test-market-001"
    assert cfg.anchor_price_cents == 50


def test_make_market_config_custom(make_market_config) -> None:  # type: ignore[no-untyped-def]
    cfg = make_market_config(market_id="custom-mkt", mid_price=0.65, cash_cents=80_000)
    assert cfg.market_id == "custom-mkt"
    assert cfg.anchor_price_cents == 65
