Run the script with input inquiry csv as positional argument, followed by discount (float, defaulted as 0%), concurrency (int, defaulted as 3) and output folder name which will house the Tulsi-quote upon completion.
Example - python3 gatecheck1.py nc-singrauli.csv --out ./quotes --discount 10 --concur 2
Sample output - 
Here's the summary for the quotation:
Out of 21 items in the quotation, 7 items were skipped due to inconsistent values - either incorrect part_code, less than 1 quantity or non-positive unit_price.
Amongst the 14 items, there were 4 unique currency codes, out of which 4 were valid currencies whose exchange rates were fetched from the API.
A discount of 0.0% was applied and additional 18.0% GST was levied as tax.
The final price in INR rates after discount and taxation is INR 11515385.68, and the quotation is stored at quotes/nc-singrauli-2026-09-07.json.

Concurrency is chosen to be 3 to ensure max 3 async workers can try working on the same thread during the API fetch calls.
Errors received from the API calls are retried for certain transient (network/transport) errors mentioned under RETRY_CODES, other errors need human input for subsequent success calls so not retried automatically.