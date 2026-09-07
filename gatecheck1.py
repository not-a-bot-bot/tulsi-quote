import csv, logging, httpx, asyncio, functools, random, json, os
from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Optional
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
import argparse

logger = logging.getLogger(__name__)

class RateFetchError(Exception):
    """"A currency's exchange rate could not be retrieved."""

class InquiryLine(BaseModel):
    part_code: str
    qty: int = Field(gt = 0)
    currency: Optional[str] = Field(min_length = 3, max_length = 3, default = "USD")
    unit_price_foreign: float = Field(gt = 0)

    @field_validator("part_code")
    @classmethod
    def check_code(cls, code):
        valid_prefix = ["CYL", "HYD", "PMP", "SLK"]
        if code[:3].strip().upper() not in valid_prefix or len(code.strip()) > 12:
            raise ValueError(f"{code} is not a valid part code.")
        return code.strip().upper()

    @field_validator("currency")
    @classmethod
    def check_curr(cls, curr):
        if len(curr) != 3:
            raise ValueError(f"{curr} is not a valid currency symbol.")
        return curr.strip().upper()

# class OuterClass:
class QuotedLine(BaseModel):
    part_code: str
    qty: int = Field(gt = 0)
    rate_in_inr: float = Field(gt = 0)
    amount: float = Field(gt = 0)

class Quotation(BaseModel):
    quote: list[QuotedLine] = Field(default_factory = list)

    def __getitem__(self, index):
        return self.quote[index]

    def __len__(self):
        return len(self.quote)

    def __iter__(self):
        return iter(self.quote)

    def append(self, item: QuotedLine):
        self.quote.append(item)

class APIResp(BaseModel):
    amount: float
    base: str
    date: date
    rates: dict[str, float]

def create_inventory(path: Path) -> tuple[list[InquiryLine], int]:
    inventory = []
    REQUIRED = {"part_code", "qty", "currency", "unit_price_foreign"}
    ct = 0
    try:
        with open(path, "r") as f:
            file = csv.DictReader(f, skipinitialspace = True)
            if file.fieldnames is None or not REQUIRED.issubset(file.fieldnames):
                logger.error(f"CSV is missing required columns. Expected {sorted(REQUIRED)}, found {file.fieldnames}")
                return None, ...
            for i, line in enumerate(file, start=2):
                try:
                    valid_line = InquiryLine(**line)
                    inventory.append(valid_line)
                except ValidationError as exc:
                    ct += 1
                    for e in exc.errors():
                        logger.warning(f"Inquiry line {i}: {e['msg']} error for {e['loc'][0]} = {e['input']}, line skipped.")
                    continue
            if inventory == []:
                logger.error(f"No rows found in the csv.")
                return None, 1
            logger.info(f"Total {len(inventory) + ct} lines processed, out of which {ct} lines skipped due to incorrect data or inconsistent formatting.")
            return inventory, ct
    except FileNotFoundError:
        logger.error(f"File not found at the specified path.")
        return None, None

def create_unique_set(inv: list) -> list:
    unique_curr = set()
    for item in inv:
        if item.currency != "INR":
            unique_curr.add(item.currency)
    logger.info(f"The inquiry has {len(unique_curr)} number of unique currency items.")
    return list(unique_curr)

def is_retryable(exc: Exception) -> bool:
    logger.info(f"Checking if {exc} is retryable.")
    RETRY_CODES = {408, 429, 500, 502, 503, 504, 529}
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_CODES
    if isinstance(exc, httpx.TransportError):
        return True
    return False

def retry_after(exc: Exception) -> float:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    header = exc.response.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
        try:
            dt = parsedate_to_datetime(header)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None
    return None

def async_retry(max_attempt:int = 3, base_delay: float = 0.5, max_delay: float = 5):
    if max_attempt < 1:
        raise ValueError(f"Max attempts must be greater than 1")
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):        ### wrapper does not return anything
            for attempt in range(max_attempt):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempt - 1:
                        raise
                    if not is_retryable(exc):
                        raise
                    retryafter = retry_after(exc)
                    if retryafter is not None:
                        if retryafter > max_delay:
                            raise
                        delay = retryafter + random.uniform(0,0.1)
                    else:
                        tot_delay = min(max_delay, base_delay * (2 ** attempt))
                        delay = random.uniform(0, tot_delay)
                    await asyncio.sleep(delay)
        return wrapper
    return decorator

@async_retry()
async def fetch_rate(client: httpx.AsyncClient, url: str, limit: asyncio.Semaphore, base_curr: str, date = "latest") -> APIResp:   ### i don't know what's the returned APIResp datatype
    async with limit:
        resp = await client.get(f"{url}{date}?base={base_curr}&symbols=INR")
        resp.raise_for_status()
        return APIResp(**resp.json())

async def call_curr_exc(unique_curr: list, concurrency: int = 3) -> tuple[list, int]:
    tout = httpx.Timeout(connect = 5, read = 30, write = 10, pool = 5)
    url = os.environ.get("FX_BASE_URL")
    # url = "https://api.frankfurter.dev/v1/"
    limit = asyncio.Semaphore(concurrency)
    results = []
    ct = 0
    async with httpx.AsyncClient(timeout = tout) as client:
        result = await asyncio.gather(
            *(fetch_rate(client, url, limit, curr) for curr in unique_curr),
            return_exceptions = True
        )
        for id, resp in enumerate(result):
            try:
                if isinstance(resp, Exception):
                    raise RateFetchError(f"No rate for {unique_curr[id]} due to {type(resp).__name__}") from resp
                results.append(resp)
                ct += 1
                logger.info(f"{resp.base} to INR exchange rate fetched = {resp.rates['INR']}.")
            except RateFetchError:
                logger.exception(f"Rate fetch failed.")
                # results.append(resp)
    return results, ct

def mapped_rate(inventory: list, curr_rates: list) -> tuple[Quotation, int]:
    rate_dict = {}
    quotation = Quotation()
    for curr in curr_rates:
        rates = curr.rates
        rate_dict[curr.base] = rates['INR']
    valid_curr = len(rate_dict)

    for item in inventory:
        if rate_dict.get(item.currency, None) is None:
            if item.currency != 'INR':
                logger.warning(f"Currency rate not found for item = {item}.")
                continue
        rate_in_inr = float(rate_dict.get(item.currency, 1)) * float(item.unit_price_foreign)
        item_copy = QuotedLine(part_code = item.part_code, qty = int(item.qty), rate_in_inr = rate_in_inr, amount = int(item.qty) * float(rate_in_inr))
        quotation.append(item_copy)
    return quotation, valid_curr

def apply_discount(raw_prices: list, discount:float = 0.0) -> Quotation:
    if discount == 0:
        return raw_prices
    disc_prices = Quotation()
    for item in raw_prices:
        disc_price = float(item.rate_in_inr) * float(1 - discount/100.0)
        new_item = QuotedLine(part_code = item.part_code, qty = item.qty, rate_in_inr = disc_price, amount = int(item.qty) * float(disc_price))
        disc_prices.append(new_item)
    return disc_prices

async def body():
    parser = argparse.ArgumentParser()
    load_dotenv(find_dotenv())

    # fetching from .env file
    # file_path = os.environ.get("FILE_PATH")
    # file_name = Path(file_path)/"nc-singrauli.csv"
    # write_path = os.environ.get("WRITE_PATH")
    log_path = Path(os.environ.get("LOG_PATH"))
    logging.basicConfig(filename = log_path, level = logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    
    gst = float(os.environ.get("GST"))

    # reading from input arguments
    parser.add_argument("input", help="Path to inquiry csv.", type = Path)
    parser.add_argument("--out", help="Specify the output file name for the quote.", type=Path, default = "./quotes")
    parser.add_argument("--discount", help="Mention the discount percentage for this quote.", type=float, default = 0.0)
    parser.add_argument("--concur", help="Give max concurrent workers for processing this quote.", type=int, default=3)
    args = parser.parse_args()
    disc = args.discount
    concur = args.concur
    if args.input is None:
        logger.error(f"Need an input inquiry csv path.")
        return 1
    input_path = args.input
    args.out.mkdir(parents=True, exist_ok=True)
    out_file_name = f"{args.input.stem}-{date.today()}.json"
    write_file = args.out/out_file_name
    return_status = 0

    # basic checks
    if disc < 0 or disc > 100:
        logger.error(f"Incorrect input: Discount must be 0-100.")
        return 1
    if type(concur) != int or concur < 1:
        logger.error(f"Incorrect input: Concurrency must be a positive integer.")
        return 1
    inventory, ct = create_inventory(input_path)
    if inventory is None:
        if ct is None:
            logger.error(f"File not found at specified location. Exiting.")
        else:
            logger.error(f"Inquiry csv is empty. Exiting.")
        return 1
    elif ct > 0:
        return_status = 2

    # main function runs
    unique_curr = create_unique_set(inventory)
    curr_rates, ct = await call_curr_exc(unique_curr, concurrency = concur)
    if ct == 0:
        logger.error(f"No valid rows found. Exiting.")
        return 1
    if ct < len(inventory):
        return_status = 2
    inr_prices, valid_curr = mapped_rate(inventory, curr_rates)
    discounted_prices = apply_discount(inr_prices, discount = disc)
    post_tax_prices = [item.model_copy(update = {"rate_in_inr": item.rate_in_inr * (1 + gst/100.0), "amount": item.amount * (1 + gst/100.0)}) for item in discounted_prices]
    tot_price = round(sum(item.amount for item in post_tax_prices),2)

    # writing out
    prices = [*post_tax_prices]
    with open(write_file, 'w') as file:
        json.dump([item.model_dump() for item in prices], file, indent = 2)

    print(f"Here's the summary for the quotation:")
    print(f"Out of {len(inventory) + ct} items in the quotation, {ct} items were skipped due to inconsistent values - either incorrect part_code, less than 1 quantity or non-positive unit_price.")
    print(f"Amongst the {len(inventory)} items, there were {len(unique_curr)} unique currency codes, out of which {valid_curr} were valid currencies whose exchange rates were fetched from the API.")
    print(f"A discount of {disc}% was applied and additional {gst}% GST was levied as tax.")
    print(f"The final price in INR rates after discount and taxation is INR {tot_price}, and the quotation is stored at {write_file}.")
    
    return return_status


if __name__ == "__main__":
    raise SystemExit(asyncio.run(body()))


