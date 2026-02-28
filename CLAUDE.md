# CLAUDE.md - AMM Market Maker Bot

## Project Overview
This is an independent AMM (Automated Market Maker) bot that connects to the prediction market matching engine via REST API. It implements the Avellaneda-Stoikov pricing model with three-layer pricing.

## Tech Stack
- Python 3.12, asyncio, httpx, redis.asyncio, PyYAML, pydantic, pytest-asyncio
- Package manager: `uv`

## Architecture
Three-layer module architecture:
1. **Connector Layer** - API client, order management, inventory sync
2. **Strategy Layer** - Three-layer pricing, A-S model, gradient ladder
3. **Risk Middleware** - Three-line defense, budget management

## Directory Structure (Target)
```
src/amm/
├── config/         # YAML + Redis config loader
├── connector/      # REST API client, trade poller, order manager
├── strategy/       # Pricing engines, A-S model, phase manager
│   └── pricing/    # Three-layer pricing components
├── risk/           # Defense stack, budget manager, sanitizer
├── lifecycle/      # Initializer, reconciler, shutdown
├── cache/          # Redis cache layer
├── models/         # Data models (Inventory, MarketContext)
└── utils/          # Integer math, logging
```

## Key Formulas (Avellaneda-Stoikov)
```
r = s - q · γ · σ² · τ(h) × 100    # Reservation price
δ = (γ · σ² · τ(h) + (2/γ) · ln(1 + γ/κ)) × 100  # Optimal spread
σ = sqrt(p(1-p)) / 100              # Bernoulli sigma
```

## Critical Rules
1. All amounts in integer cents
2. AMM NEVER issues BUY orders - only SELL YES and SELL NO
3. Bid ladder maps to SELL NO: bid_yes @ P → sell_no @ (100-P)
4. Use math.ceil() for ask, math.floor() for bid (NOT round())

## Commands
```bash
uv run pytest tests/ -v           # Run tests
uv run python -m src.amm.main     # Run AMM bot
```

## Design Documents
- `AMM_Bot_Design/` - Architecture and algorithms
- `Implementation_Plan/2026-02-28-amm-bot-plan.md` - Detailed tasks
- `Implementation_Plan/2026-02-28-amm-bot-design.md` - Design doc

## Backend Dependency
Phase A prerequisites must be implemented in the backend first.
See `Implementation_Plan/2026-02-28-amm-prerequisites-plan.md`.
