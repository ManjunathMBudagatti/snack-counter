Snack Counter Automation

Automates the snack counter refill process for the Oslo and Bengaluru offices.

What it does

- Opens a refill when a snack box is empty and assigns it to a volunteer.
- Prevents two people from claiming the same refill (first claim wins).
- Reassigns refills that are not bought within 24 hours.
- Checks each office's monthly budget in its own currency (NOK or INR).
- Submits receipts to HR and tracks approval.
- Produces a monthly report, with NOK and INR kept separate.

Files

- `snack_core.py`: the rules and the simulated chat and HR stand-ins.
- `test_snack_core.py`: automated tests for the rules.
- `design.md` or the design document: explains the design and assumptions.

How to run the tests

Requires Python 3.10 or newer.

    pip install pytest
    pytest -v

Design

See the design document, which covers the refill lifecycle, the components, how each problem is prevented, the assumptions, and the trade-offs.