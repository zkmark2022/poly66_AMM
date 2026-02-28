"""AMM configuration models. Aligned with config handbook v1.3."""
from dataclasses import dataclass, field

GAMMA_TIERS: dict[str, float] = {
    "EARLY": 0.1,
    "MID": 0.3,
    "LATE": 0.8,
    "MATURE": 1.5,
}


@dataclass
class GlobalConfig:
    """Global AMM settings (not per-market)."""

    base_url: str = "http://localhost:8000/api/v1"
    redis_url: str = "redis://localhost:6379/0"
    amm_username: str = "amm_market_maker"
    amm_password: str = ""  # from env var, never in YAML

    quote_interval_seconds: float = 2.0
    reconcile_interval_seconds: float = 300.0
    trade_poll_interval_seconds: float = 2.0
    balance_poll_interval_seconds: float = 30.0

    max_concurrent_markets: int = 50
    log_level: str = "INFO"


@dataclass
class MarketConfig:
    """Per-market AMM configuration. 87+ parameters."""

    market_id: str

    # Pricing
    gamma_tier: str = "MID"
    kappa: float = 1.5  # market depth / order arrival intensity
    anchor_price_cents: int = 50  # initial anchor price
    spread_min_cents: int = 2  # minimum spread
    spread_max_cents: int = 20  # maximum spread

    # Inventory
    initial_mint_quantity: int = 1000
    auto_reinvest_enabled: bool = True
    auto_merge_threshold: float = 0.3  # merge when skew drops below this

    # Gradient
    gradient_levels: int = 3  # number of price levels per side
    gradient_quantity_decay: float = 0.5  # each level has 50% of previous
    gradient_price_step_cents: int = 1  # price step between levels

    # Risk
    max_daily_loss_cents: int = 100_00  # $100
    max_per_market_loss_cents: int = 50_00  # $50
    inventory_skew_widen: float = 0.3  # trigger WIDEN
    inventory_skew_one_side: float = 0.6  # trigger ONE_SIDE
    inventory_skew_kill: float = 0.8  # trigger KILL_SWITCH
    widen_factor: float = 1.5  # spread multiplier in WIDEN mode
    defense_cooldown_cycles: int = 5  # cycles before de-escalation

    # Phase
    exploration_duration_hours: float = 24.0
    stabilization_volume_threshold: int = 100  # trades to trigger STABILIZATION

    # Timing
    remaining_hours_override: float | None = None  # for testing

    @property
    def gamma(self) -> float:
        return GAMMA_TIERS.get(self.gamma_tier, 0.3)
