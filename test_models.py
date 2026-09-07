import asyncio, logging
import httpx
import pytest
from pydantic import ValidationError

from gatecheck1 import (
    InquiryLine, QuotedLine, APIResp,
    InvalidPartCodeError,
    create_inventory, create_unique_set,
    is_retryable, retry_after, apply_discount, fetch_rate,
)

test_logger = logging.getLogger("test")


@pytest.fixture
def sample_lines():
    return [
        InquiryLine(part_code="CYL-2201", qty=4, currency="USD", unit_price_foreign=150.0),
        InquiryLine(part_code="HYD-3301", qty=2, currency="EUR", unit_price_foreign=80.0),
        InquiryLine(part_code="PMP-4410", qty=1, currency="USD", unit_price_foreign=220.0),
    ]


@pytest.fixture
def bad_request():
    return httpx.Request("GET", "https://example.com/x")


def test_valid_inquiry_line():
    line = InquiryLine(part_code="cyl-2201", qty=4, currency="usd", unit_price_foreign=150.0)
    assert line.part_code == "CYL-2201"
    assert line.currency == "USD"
    assert line.qty == 4


def test_invalid_part_code_raises():
    with pytest.raises(ValueError):
        InquiryLine(part_code="XYZ-9999", qty=4, currency="USD", unit_price_foreign=150.0)


def test_negative_qty_raises():
    with pytest.raises(ValidationError):
        InquiryLine(part_code="CYL-2201", qty=-2, currency="USD", unit_price_foreign=150.0)


def test_zero_price_raises():
    with pytest.raises(ValidationError):
        InquiryLine(part_code="CYL-2201", qty=1, currency="USD", unit_price_foreign=0)


def test_create_inventory_skips_bad_rows(tmp_path):
    csv_file = tmp_path / "enquiry.csv"
    csv_file.write_text(
        "part_code,qty,currency,unit_price_foreign\n"
        "CYL-2201,4,USD,150.0\n"
        "XYZ-9999,4,USD,150.0\n"
        "HYD-3301,-2,USD,80.0\n"
        "PMP-4410,1,EUR,220.0\n"
    )
    inventory, skipped = create_inventory(csv_file)
    assert len(inventory) == 2
    assert skipped == 2


def test_create_inventory_logs_warning(tmp_path, caplog):
    csv_file = tmp_path / "enquiry.csv"
    csv_file.write_text(
        "part_code,qty,currency,unit_price_foreign\n"
        "XYZ-9999,4,USD,150.0\n"
    )
    with caplog.at_level(logging.WARNING):
        create_inventory(csv_file)
    assert "part code" in caplog.text.lower()


def test_create_unique_set_dedupes(sample_lines):
    result = create_unique_set(sample_lines)
    assert sorted(result) == ["EUR", "USD"]


def test_is_retryable_503_true(bad_request):
    exc = httpx.HTTPStatusError(
        "boom", request=bad_request,
        response=httpx.Response(503, request=bad_request))
    assert is_retryable(exc) is True


def test_is_retryable_404_false(bad_request):
    exc = httpx.HTTPStatusError(
        "nf", request=bad_request,
        response=httpx.Response(404, request=bad_request))
    assert is_retryable(exc) is False


def test_is_retryable_transport_error_true():
    assert is_retryable(httpx.ConnectTimeout("timed out")) is True


def test_retry_after_parses_seconds(bad_request):
    exc = httpx.HTTPStatusError(
        "rate limited", request=bad_request,
        response=httpx.Response(429, headers={"Retry-After": "2"}, request=bad_request))
    assert retry_after(exc) == 2.0


def test_retry_after_none_for_transport_error():
    assert retry_after(httpx.ConnectTimeout("timed out")) is None


def test_apply_discount_reduces_price():
    lines = [QuotedLine(part_code="CYL-2201", qty=2, rate_in_inr=100.0, amount =200.0)]
    result = apply_discount(lines, discount=10)
    assert float(result[0].amount) == 180.0


def test_apply_discount_zero_is_noop():
    lines = [QuotedLine(part_code="CYL-2201", qty=2, rate_in_inr=100.0, amount=200.0)]
    assert apply_discount(lines, discount=0) == lines


async def test_fetch_rate_retries_on_503():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={
            "amount": 1.0, "base": "USD", "date": "2026-08-20", "rates": {"INR": 95.7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_rate(client, "https://x/", asyncio.Semaphore(3), "USD")

    assert result.rates["INR"] == 95.7
    assert calls == 3


async def test_fetch_rate_404_does_not_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_rate(client, "https://x/", asyncio.Semaphore(3), "USD")

    assert calls == 1