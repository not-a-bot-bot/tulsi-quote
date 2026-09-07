from pydantic import ValidationError
from gatecheck1 import QuotedLine, apply_discount, create_inventory, fetch_rate, Quotation
import asyncio, httpx, pytest, logging

logger = logging.getLogger(__name__)

# def test_first():
def test_apply_discount_reduces_price():
    lines = Quotation()
    quote = [QuotedLine(part_code="CYL-2201", qty=2, rate_in_inr=100.0, amount=200.0)]
    lines.append(quote)
    result = apply_discount(lines, discount=10)
    assert result[0].amount == 180.0

def test_create_inventory_skips_bad_rows(tmp_path):
    csv_file = tmp_path / "enquiry.csv"
    csv_file.write_text(
        "part_code,qty,currency,unit_price_foreign\n"
        "CYL-2201,4,USD,150.0\n"
        "XYZ-9999,4,USD,150.0\n"      # bad prefix
        "HYD-3301,-2,USD,150.0\n"     # bad qty
    )
    inventory, skipped = create_inventory(csv_file)
    assert len(inventory) == 1
    assert skipped == 2

def test_gst_read_from_env(monkeypatch):
    monkeypatch.setenv("GST", "10.0")
    assert load_config().gst == 18.0

async def test_fetch_rate_retries_on_503():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"amount": 1.0, "base": "USD",
                                         "date": "2026-08-20", "rates": {"INR": 95.7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_rate(client, "https://x/", asyncio.Semaphore(3), "USD")

    assert result.rates["INR"] == 95.7
    assert calls == 3                      # proves it retried twice

async def test_404_does_not_retry():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_rate(client, "https://x/", asyncio.Semaphore(3), "USD")

    assert calls == 1                      # proves fail-fast