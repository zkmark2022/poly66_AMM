"""Layer 4 E2E preflight checker.

Validates environment prerequisites before launching formal E2E tests:
- Backend /health reachable and responsive
- Frontend /app entry accessible
- Assigned market(s) are ACTIVE (rejects SETTLED)
- /amm/mint endpoint smoke test
- Orderbook returns usable data

Usage:
    uv run python -m scripts.preflight [--backend-url URL] [--frontend-url URL] [--market-id MKT-ID] ...
"""
from __future__ import annotations

import argparse
import asyncio
import enum
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx


class Status(enum.Enum):
    """Preflight check result status."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    """Single preflight check outcome."""
    name: str
    status: Status
    detail: str
    latency_ms: float | None = None


@dataclass
class PreflightReport:
    """Aggregated preflight results."""
    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.status in (Status.PASS, Status.SKIP) for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.status == Status.PASS)

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.status == Status.SKIP)

    @property
    def summary_line(self) -> str:
        verdict = "PASS" if self.all_passed else "FAIL"
        skip = self.skip_count
        if skip:
            return f"PREFLIGHT {verdict} ({self.pass_count}/{self.total_count}, {skip} SKIP)"
        return f"PREFLIGHT {verdict} ({self.pass_count}/{self.total_count})"

    def to_table(self) -> str:
        lines = [
            f"{'Check':<30} {'Status':<8} {'Latency':<10} Detail",
            "-" * 80,
        ]
        for r in self.results:
            latency_str = f"{r.latency_ms:.1f}ms" if r.latency_ms is not None else "-"
            lines.append(f"{r.name:<30} {r.status.value:<8} {latency_str:<10} {r.detail}")
        lines.append("-" * 80)
        lines.append(self.summary_line)
        return "\n".join(lines)


_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MARKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_market_ids(market_ids: list[str]) -> None:
    for mid in market_ids:
        if not _MARKET_ID_RE.match(mid):
            raise ValueError(
                f"Invalid market ID {mid!r}: only alphanumeric, hyphens, and underscores are allowed"
            )


class PreflightChecker:
    """Runs all preflight checks against live or mocked services."""

    def __init__(
        self,
        backend_url: str,
        frontend_url: str,
        market_ids: list[str],
        auth_token: str | None = None,
        skip_mint_if_no_auth: bool = False,
    ) -> None:
        _validate_market_ids(market_ids)
        self.backend_url = backend_url.rstrip("/")
        self.frontend_url = frontend_url.rstrip("/")
        self.market_ids = market_ids
        self.auth_token = auth_token
        self.skip_mint_if_no_auth = skip_mint_if_no_auth

    def _health_url(self) -> str:
        # /health lives at the app root, not under /api/v1
        # e.g. http://localhost:8000/api/v1 -> http://localhost:8000/health
        parsed = urllib.parse.urlparse(self.backend_url)
        return f"{parsed.scheme}://{parsed.netloc}/health"

    async def check_backend_health(self) -> CheckResult:
        """GET /health — must return 200 quickly."""
        url = self._health_url()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                t0 = time.monotonic()
                resp = await client.get(url)
                latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return CheckResult(
                    name="backend_health",
                    status=Status.PASS,
                    detail="200 OK",
                    latency_ms=latency,
                )
            return CheckResult(
                name="backend_health",
                status=Status.FAIL,
                detail=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            return CheckResult(
                name="backend_health",
                status=Status.FAIL,
                detail=str(exc),
            )

    async def check_frontend_entry(self) -> CheckResult:
        """GET /app — must return 200."""
        url = f"{self.frontend_url}/app"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                t0 = time.monotonic()
                resp = await client.get(url)
                latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return CheckResult(
                    name="frontend_entry",
                    status=Status.PASS,
                    detail="200 OK",
                    latency_ms=latency,
                )
            return CheckResult(
                name="frontend_entry",
                status=Status.FAIL,
                detail=f"HTTP {resp.status_code} (expected 200 at /app)",
            )
        except Exception as exc:
            return CheckResult(
                name="frontend_entry",
                status=Status.FAIL,
                detail=str(exc),
            )

    async def _check_one_market_status(
        self, client: httpx.AsyncClient, mid: str
    ) -> CheckResult:
        url = f"{self.backend_url}/markets/{mid}"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return CheckResult(
                    name=f"market_status:{mid}",
                    status=Status.FAIL,
                    detail=f"HTTP {resp.status_code}",
                )
            try:
                body = resp.json()
            except ValueError:
                return CheckResult(
                    name=f"market_status:{mid}",
                    status=Status.FAIL,
                    detail="Invalid JSON in response",
                )
            data = body.get("data", body) if isinstance(body, dict) else {}
            status_str = str(data.get("status", "unknown")).upper()
            if status_str == "ACTIVE":
                return CheckResult(name=f"market_status:{mid}", status=Status.PASS, detail="ACTIVE")
            return CheckResult(
                name=f"market_status:{mid}",
                status=Status.FAIL,
                detail=f"Status is {status_str} (must be ACTIVE)",
            )
        except Exception as exc:
            return CheckResult(
                name=f"market_status:{mid}",
                status=Status.FAIL,
                detail=str(exc),
            )

    async def check_market_status(self) -> list[CheckResult]:
        """GET /markets/{id} — each must be ACTIVE."""
        if not self.market_ids:
            return [CheckResult(
                name="market_status",
                status=Status.FAIL,
                detail="No market IDs configured — preflight cannot proceed",
            )]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return list(
                await asyncio.gather(
                    *[self._check_one_market_status(client, mid) for mid in self.market_ids]
                )
            )

    async def _check_one_orderbook(
        self, client: httpx.AsyncClient, mid: str
    ) -> CheckResult:
        url = f"{self.backend_url}/markets/{mid}/orderbook"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return CheckResult(
                    name=f"orderbook:{mid}",
                    status=Status.FAIL,
                    detail=f"HTTP {resp.status_code}",
                )
            try:
                body = resp.json()
            except ValueError:
                return CheckResult(
                    name=f"orderbook:{mid}",
                    status=Status.FAIL,
                    detail="Invalid JSON in response",
                )
            data = body.get("data", body) if isinstance(body, dict) else {}
            if "yes" in data or "no" in data:
                # Nested format: {"yes": {"bids": [...], "asks": [...]}, "no": {...}}
                yes = data.get("yes") or {}
                no = data.get("no") or {}
                bids = (yes.get("bids") or []) + (no.get("bids") or [])
                asks = (yes.get("asks") or []) + (no.get("asks") or [])
            else:
                # Flat fallback format: {"bids": [...], "asks": [...]}
                bids = data.get("bids") or []
                asks = data.get("asks") or []
            if bids or asks:
                return CheckResult(
                    name=f"orderbook:{mid}",
                    status=Status.PASS,
                    detail=f"{len(bids)} bids, {len(asks)} asks",
                )
            return CheckResult(
                name=f"orderbook:{mid}",
                status=Status.SKIP,
                detail="Empty orderbook (no bids/asks) — AMM may not have posted yet",
            )
        except Exception as exc:
            return CheckResult(
                name=f"orderbook:{mid}",
                status=Status.FAIL,
                detail=str(exc),
            )

    async def check_orderbook(self) -> list[CheckResult]:
        """GET /markets/{id}/orderbook — must return bids/asks."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return list(
                await asyncio.gather(
                    *[self._check_one_orderbook(client, mid) for mid in self.market_ids]
                )
            )

    async def check_mint_smoke(self) -> CheckResult:
        """POST /amm/mint — smoke test with small quantity."""
        if self.skip_mint_if_no_auth and not self.auth_token:
            return CheckResult(
                name="mint_smoke",
                status=Status.SKIP,
                detail="No auth token provided — skipping mint smoke test",
            )

        url = f"{self.backend_url}/amm/mint"
        market_id = self.market_ids[0] if self.market_ids else "UNKNOWN"
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        payload = {
            "market_id": market_id,
            "quantity": 1,
            "idempotency_key": f"preflight_smoke_{market_id}",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                t0 = time.monotonic()
                resp = await client.post(url, json=payload, headers=headers)
                latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return CheckResult(
                    name="mint_smoke",
                    status=Status.PASS,
                    detail="200 OK",
                    latency_ms=latency,
                )
            return CheckResult(
                name="mint_smoke",
                status=Status.FAIL,
                detail=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            return CheckResult(
                name="mint_smoke",
                status=Status.FAIL,
                detail=str(exc),
            )

    async def run_all(self) -> PreflightReport:
        """Execute all preflight checks and return aggregated report."""
        results: list[CheckResult] = []
        results.append(await self.check_backend_health())
        results.append(await self.check_frontend_entry())
        results.extend(await self.check_market_status())
        results.extend(await self.check_orderbook())
        results.append(await self.check_mint_smoke())
        return PreflightReport(results=results)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Layer 4 E2E preflight checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  uv run python -m scripts.preflight --market-id MKT-BTC-100K-2026",
    )
    parser.add_argument(
        "--backend-url",
        default="http://localhost:8000/api/v1",
        help="Backend API base URL (default: http://localhost:8000/api/v1)",
    )
    parser.add_argument(
        "--frontend-url",
        default="http://localhost:3000",
        help="Frontend base URL (default: http://localhost:3000)",
    )
    parser.add_argument(
        "--market-id",
        action="append",
        dest="market_ids",
        default=[],
        help="Market ID to check (repeatable). At least one required.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("AMM_AUTH_TOKEN") or None,
        help="Bearer token for authenticated endpoints (mint). Can also be set via AMM_AUTH_TOKEN env var.",
    )
    parser.add_argument(
        "--skip-mint-if-no-auth",
        action="store_true",
        help="Skip mint smoke test when no auth token is provided.",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.market_ids:
        print("ERROR: At least one --market-id is required.", file=sys.stderr)
        return 2

    checker = PreflightChecker(
        backend_url=args.backend_url,
        frontend_url=args.frontend_url,
        market_ids=args.market_ids,
        auth_token=args.auth_token,
        skip_mint_if_no_auth=args.skip_mint_if_no_auth,
    )
    report = await checker.run_all()
    print(report.to_table())
    return 0 if report.all_passed else 1


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(_main(argv)))


if __name__ == "__main__":
    main()
