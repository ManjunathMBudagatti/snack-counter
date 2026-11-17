"""
Snack counter core logic.
  - Status / Refill: one refill and its lifecycle
  - SnackService: the rules (claim, expire, purchase, budget, HR, report)
  - FakeChat / FakeHR: simulated stand-ins for the chat tool and HR system
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from itertools import count

CLAIM_WINDOW = timedelta(hours=24)
BUDGETS = {"Oslo": (6000, "NOK"), "Bengaluru": (15000, "INR")}


class Status(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    PURCHASED = "purchased"
    SUBMITTED = "submitted"
    REIMBURSED = "reimbursed"


# The only legal moves. Anything else is a bug and raises an error.
ALLOWED_MOVES = {
    Status.OPEN: {Status.CLAIMED},
    Status.CLAIMED: {Status.PURCHASED, Status.OPEN},  # back to OPEN = claim expired
    Status.PURCHASED: {Status.SUBMITTED},
    Status.SUBMITTED: {Status.REIMBURSED},
    Status.REIMBURSED: set(),
}

# States where money has really been spent. These count against the budget.
COMMITTED = {Status.PURCHASED, Status.SUBMITTED, Status.REIMBURSED}


class SnackError(Exception):
    pass


class AlreadyClaimed(SnackError):
    pass


class NotYourRefill(SnackError):
    pass


class BudgetExceeded(SnackError):
    pass


class WrongCurrency(SnackError):
    pass


class InvalidMove(SnackError):
    pass


@dataclass
class Refill:
    id: int
    office: str
    status: Status = Status.OPEN
    version: int = 0               # goes up on every change; used to detect races
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    purchased_at: datetime | None = None
    amount: int = 0
    currency: str = ""
    receipt: str | None = None
    hr_claim_id: int | None = None


class SnackService:
    def __init__(self, hr, chat, clock=datetime.now):
        self.hr = hr
        self.chat = chat
        self.now = clock           # injected so tests can control time
        self.refills: dict[int, Refill] = {}
        self.volunteers: dict[str, dict] = {}
        self._ids = count(1)
        self._lock = threading.Lock()

    # ---------- setup ----------

    def add_volunteer(self, name: str):
        self.volunteers[name] = {"refills_done": 0, "away": False}

    def set_away(self, name: str, away: bool):
        self.volunteers[name]["away"] = away

    def open_refill(self, office: str) -> Refill:
        with self._lock:
            r = Refill(id=next(self._ids), office=office)
            self.refills[r.id] = r
            self.chat.post(f"Snack box at {office} is empty. Claim refill #{r.id}.")
            return r

    # ---------- lifecycle ----------

    def _move(self, r: Refill, new: Status):
        if new not in ALLOWED_MOVES[r.status]:
            raise InvalidMove(f"cannot go from {r.status.value} to {new.value}")
        r.status = new
        r.version += 1

    def _assign(self, r: Refill, person: str):
        r.claimed_by = person
        r.claimed_at = self.now()
        self._move(r, Status.CLAIMED)
        self.chat.post(f"@{person}: please buy refill #{r.id} for {r.office}. Claim lasts 24h.")

    def pick_volunteer(self, exclude=()) -> str | None:
        eligible = [n for n, v in self.volunteers.items()
                    if not v["away"] and n not in exclude]
        if not eligible:
            return None
        # Fairness: whoever has done the fewest refills goes next.
        return min(eligible, key=lambda n: (self.volunteers[n]["refills_done"], n))

    def claim(self, refill_id: int, person: str, seen_version: int) -> Refill:
        with self._lock:
            r = self.refills[refill_id]
            # First claim wins. If someone else got there first, the status
            # or version will no longer match what this person saw.
            if r.status != Status.OPEN or r.version != seen_version:
                raise AlreadyClaimed(f"refill #{refill_id} is already taken")
            if self.volunteers[person]["away"]:
                raise SnackError(f"{person} is marked away")
            self._assign(r, person)
            return r

    def expire_stale_claims(self):
        """Run regularly (e.g. every 10 minutes). Hands expired refills to someone else."""
        with self._lock:
            for r in self.refills.values():
                if r.status == Status.CLAIMED and self.now() - r.claimed_at > CLAIM_WINDOW:
                    previous = r.claimed_by
                    r.claimed_by = None
                    r.claimed_at = None
                    self._move(r, Status.OPEN)
                    nxt = self.pick_volunteer(exclude={previous})
                    if nxt:
                        self._assign(r, nxt)

    # ---------- money ----------

    def _in_this_month(self, dt: datetime | None) -> bool:
        now = self.now()
        return dt is not None and dt.year == now.year and dt.month == now.month

    def remaining_budget(self, office: str) -> int:
        spent = sum(r.amount for r in self.refills.values()
                    if r.office == office and r.status in COMMITTED
                    and self._in_this_month(r.purchased_at))
        return BUDGETS[office][0] - spent

    def record_purchase(self, refill_id: int, person: str, amount: int,
                        currency: str, receipt: str | None = None) -> Refill:
        with self._lock:
            r = self.refills[refill_id]
            if r.status != Status.CLAIMED or r.claimed_by != person:
                raise NotYourRefill(f"{person} does not hold refill #{refill_id}")
            office_currency = BUDGETS[r.office][1]
            if currency != office_currency:
                raise WrongCurrency(f"{r.office} uses {office_currency}, not {currency}")
            remaining = self.remaining_budget(r.office)
            if amount > remaining:
                raise BudgetExceeded(f"only {remaining} {currency} left this month")
            r.amount = amount
            r.currency = currency
            r.receipt = receipt
            r.purchased_at = self.now()
            self._move(r, Status.PURCHASED)
            self.volunteers[person]["refills_done"] += 1
            return r

    def submit_to_hr(self, refill_id: int) -> Refill:
        with self._lock:
            r = self.refills[refill_id]
            if r.status != Status.PURCHASED:
                raise InvalidMove("only purchased refills can go to HR")
            if not r.receipt:
                raise SnackError("a receipt is required before submitting")
            r.hr_claim_id = self.hr.create_claim(
                employee_id=r.claimed_by, amount=r.amount, currency=r.currency,
                description=f"Snack refill {r.office} #{r.id}", receipt=r.receipt)
            self._move(r, Status.SUBMITTED)
            return r

    def sync_with_hr(self):
        with self._lock:
            for r in self.refills.values():
                if r.status == Status.SUBMITTED and self.hr.status(r.hr_claim_id) == "approved":
                    self._move(r, Status.REIMBURSED)

    def monthly_report(self) -> dict:
        """Totals per (office, currency). Never mixes NOK and INR."""
        totals: dict[tuple[str, str], int] = {}
        for r in self.refills.values():
            if r.status in COMMITTED and self._in_this_month(r.purchased_at):
                key = (r.office, r.currency)
                totals[key] = totals.get(key, 0) + r.amount
        return totals


# ---------- simulated stand-ins ----------

class FakeChat:
    """Stands in for the company chat tool. Just keeps a list of messages."""
    def __init__(self):
        self.messages: list[str] = []

    def post(self, text: str):
        self.messages.append(text)


class FakeHR:
    """Stands in for the external HR system. Claims stay 'pending' until approve() is called."""
    def __init__(self):
        self.claims: dict[int, dict] = {}
        self._ids = count(1)

    def create_claim(self, employee_id, amount, currency, description, receipt) -> int:
        cid = next(self._ids)
        self.claims[cid] = {"employee": employee_id, "amount": amount,
                            "currency": currency, "description": description,
                            "receipt": receipt, "status": "pending"}
        return cid

    def status(self, cid: int) -> str:
        return self.claims[cid]["status"]

    def approve(self, cid: int):
        self.claims[cid]["status"] = "approved"