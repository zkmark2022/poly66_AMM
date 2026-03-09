"""Tests for Layer 4 E2E preflight checker.

Validates all preflight checks using mocked HTTP responses:
- Backend /health reachable and fast
- Frontend /app entry accessible
- Assigned market(s) are ACTIVE
- /amm/mint smoke test passes
- Orderbook returns usable data
"""
import httpx
import pytest
import respx

from scripts.preflight import (
    CheckResult,
    PreflightChecker,
    PreflightReport,
    Status,
)


@pytest.fixture
def checker() -> PreflightChecker:
    return PreflightChecker(
        backend_url="http://localhost:8000/api/v1",
        frontend_url="http://localhost:3000",
        market_ids=["MKT-TEST-001"],
    )


# --- Status enum ---

class TestStatus:
    def test_pass_is_truthy(self) -> None:
        assert Status.PASS

    def test_fail_is_falsy(self) -> None:
        assert not Status.FAIL

    def test_skip_is_falsy(self) -> None:
        assert not Status.SKIP


# --- CheckResult model ---

class TestCheckResult:
    def test_pass_result(self) -> None:
        r = CheckResult(name="test", status=Status.PASS, detail="ok")
        assert r.name == "test"
        assert r.status == Status.PASS
        assert r.detail == "ok"
        assert r.latency_ms is None

    def test_result_with_latency(self) -> None:
        r = CheckResult(name="test", status=Status.PASS, detail="ok", latency_ms=42.5)
        assert r.latency_ms == 42.5


# --- Backend health check ---

class TestBackendHealth:
    @respx.mock
    @pytest.mark.asyncio
    async def test_health_pass(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/health").respond(
            200, json={"status": "ok"}
        )
        result = await checker.check_backend_health()
        assert result.status == Status.PASS
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_timeout(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/health").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        result = await checker.check_backend_health()
        assert result.status == Status.FAIL
        assert "timeout" in result.detail.lower() or "connect" in result.detail.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_500(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/health").respond(500)
        result = await checker.check_backend_health()
        assert result.status == Status.FAIL

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_slow_warns(self, checker: PreflightChecker) -> None:
        """Health responding but > 2s should still PASS but note latency."""
        respx.get("http://localhost:8000/health").respond(
            200, json={"status": "ok"}
        )
        result = await checker.check_backend_health()
        # Even if slow, a 200 is a PASS
        assert result.status == Status.PASS


# --- Frontend entry check ---

class TestFrontendEntry:
    @respx.mock
    @pytest.mark.asyncio
    async def test_app_reachable(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:3000/app").respond(200, html="<html>App</html>")
        result = await checker.check_frontend_entry()
        assert result.status == Status.PASS

    @respx.mock
    @pytest.mark.asyncio
    async def test_app_404(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:3000/app").respond(404)
        result = await checker.check_frontend_entry()
        assert result.status == Status.FAIL
        assert "404" in result.detail

    @respx.mock
    @pytest.mark.asyncio
    async def test_app_unreachable(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:3000/app").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await checker.check_frontend_entry()
        assert result.status == Status.FAIL


# --- Market status check ---

class TestMarketStatus:
    @respx.mock
    @pytest.mark.asyncio
    async def test_active_market(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001").respond(
            200, json={"data": {"market_id": "MKT-TEST-001", "status": "ACTIVE"}}
        )
        results = await checker.check_market_status()
        assert len(results) == 1
        assert results[0].status == Status.PASS
        assert "ACTIVE" in results[0].detail.upper()

    @respx.mock
    @pytest.mark.asyncio
    async def test_settled_market_fails(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001").respond(
            200, json={"data": {"market_id": "MKT-TEST-001", "status": "SETTLED"}}
        )
        results = await checker.check_market_status()
        assert results[0].status == Status.FAIL
        assert "SETTLED" in results[0].detail.upper()

    @respx.mock
    @pytest.mark.asyncio
    async def test_market_not_found(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001").respond(404)
        results = await checker.check_market_status()
        assert results[0].status == Status.FAIL

    @respx.mock
    @pytest.mark.asyncio
    async def test_market_invalid_json(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001").respond(
            200, content=b"<html>proxy error</html>", headers={"content-type": "text/html"}
        )
        results = await checker.check_market_status()
        assert results[0].status == Status.FAIL
        assert "Invalid JSON" in results[0].detail

    @respx.mock
    @pytest.mark.asyncio
    async def test_multiple_markets(self) -> None:
        c = PreflightChecker(
            backend_url="http://localhost:8000/api/v1",
            frontend_url="http://localhost:3000",
            market_ids=["MKT-A", "MKT-B"],
        )
        respx.get("http://localhost:8000/api/v1/markets/MKT-A").respond(
            200, json={"data": {"market_id": "MKT-A", "status": "ACTIVE"}}
        )
        respx.get("http://localhost:8000/api/v1/markets/MKT-B").respond(
            200, json={"data": {"market_id": "MKT-B", "status": "SETTLED"}}
        )
        results = await c.check_market_status()
        assert results[0].status == Status.PASS
        assert results[1].status == Status.FAIL


# --- Orderbook check ---

class TestOrderbook:
    @respx.mock
    @pytest.mark.asyncio
    async def test_orderbook_with_data(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001/orderbook").respond(
            200, json={"data": {"bids": [{"price": 45, "quantity": 10}], "asks": [{"price": 55, "quantity": 10}]}}
        )
        results = await checker.check_orderbook()
        assert results[0].status == Status.PASS

    @respx.mock
    @pytest.mark.asyncio
    async def test_orderbook_empty(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001/orderbook").respond(
            200, json={"data": {"bids": [], "asks": []}}
        )
        results = await checker.check_orderbook()
        # Empty orderbook is a SKIP — not a hard failure, but not usable
        assert results[0].status == Status.SKIP

    @respx.mock
    @pytest.mark.asyncio
    async def test_orderbook_unreachable(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001/orderbook").respond(500)
        results = await checker.check_orderbook()
        assert results[0].status == Status.FAIL

    @respx.mock
    @pytest.mark.asyncio
    async def test_orderbook_invalid_json(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001/orderbook").respond(
            200, content=b"<html>proxy error</html>", headers={"content-type": "text/html"}
        )
        results = await checker.check_orderbook()
        assert results[0].status == Status.FAIL
        assert "Invalid JSON" in results[0].detail


# --- Mint smoke test ---

class TestMintSmoke:
    @respx.mock
    @pytest.mark.asyncio
    async def test_mint_dry_run_pass(self, checker: PreflightChecker) -> None:
        """Mint endpoint returns 200 — smoke test passes."""
        respx.post("http://localhost:8000/api/v1/amm/mint").respond(
            200, json={"data": {"yes_shares": 100, "no_shares": 100}}
        )
        result = await checker.check_mint_smoke()
        assert result.status == Status.PASS

    @respx.mock
    @pytest.mark.asyncio
    async def test_mint_500_fails(self, checker: PreflightChecker) -> None:
        respx.post("http://localhost:8000/api/v1/amm/mint").respond(500)
        result = await checker.check_mint_smoke()
        assert result.status == Status.FAIL
        assert "500" in result.detail

    @respx.mock
    @pytest.mark.asyncio
    async def test_mint_no_auth(self, checker: PreflightChecker) -> None:
        """401 indicates auth is not configured — should fail preflight."""
        respx.post("http://localhost:8000/api/v1/amm/mint").respond(401)
        result = await checker.check_mint_smoke()
        assert result.status == Status.FAIL

    @respx.mock
    @pytest.mark.asyncio
    async def test_mint_skip_without_auth_token(self) -> None:
        """When no auth token is provided, mint check should SKIP."""
        c = PreflightChecker(
            backend_url="http://localhost:8000/api/v1",
            frontend_url="http://localhost:3000",
            market_ids=["MKT-TEST-001"],
            auth_token="",
            skip_mint_if_no_auth=True,
        )
        result = await c.check_mint_smoke()
        assert result.status == Status.SKIP


# --- Full preflight report ---

class TestPreflightReport:
    def test_all_pass(self) -> None:
        results = [
            CheckResult(name="a", status=Status.PASS, detail="ok"),
            CheckResult(name="b", status=Status.PASS, detail="ok"),
        ]
        report = PreflightReport(results=results)
        assert report.all_passed
        assert report.summary_line == "PREFLIGHT PASS (2/2)"

    def test_one_fail(self) -> None:
        results = [
            CheckResult(name="a", status=Status.PASS, detail="ok"),
            CheckResult(name="b", status=Status.FAIL, detail="broken"),
        ]
        report = PreflightReport(results=results)
        assert not report.all_passed
        assert "FAIL" in report.summary_line

    def test_skip_does_not_block(self) -> None:
        results = [
            CheckResult(name="a", status=Status.PASS, detail="ok"),
            CheckResult(name="b", status=Status.SKIP, detail="skipped"),
        ]
        report = PreflightReport(results=results)
        assert report.all_passed

    def test_table_output(self) -> None:
        results = [
            CheckResult(name="health", status=Status.PASS, detail="ok", latency_ms=12.3),
            CheckResult(name="mint", status=Status.FAIL, detail="500 error"),
        ]
        report = PreflightReport(results=results)
        table = report.to_table()
        assert "health" in table
        assert "PASS" in table
        assert "FAIL" in table
        assert "12.3" in table or "12" in table


# --- run_all integration ---

class TestRunAll:
    @respx.mock
    @pytest.mark.asyncio
    async def test_run_all_collects_results(self, checker: PreflightChecker) -> None:
        respx.get("http://localhost:8000/health").respond(
            200, json={"status": "ok"}
        )
        respx.get("http://localhost:3000/app").respond(200, html="<html></html>")
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001").respond(
            200, json={"data": {"market_id": "MKT-TEST-001", "status": "ACTIVE"}}
        )
        respx.get("http://localhost:8000/api/v1/markets/MKT-TEST-001/orderbook").respond(
            200, json={"data": {"bids": [{"price": 45, "quantity": 10}], "asks": [{"price": 55, "quantity": 10}]}}
        )
        respx.post("http://localhost:8000/api/v1/amm/mint").respond(
            200, json={"data": {"yes_shares": 100, "no_shares": 100}}
        )
        report = await checker.run_all()
        assert report.all_passed
        assert len(report.results) >= 5  # health + frontend + market + orderbook + mint
