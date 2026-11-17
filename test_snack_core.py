from datetime import datetime, timedelta

import pytest

from snack_core import (
    AlreadyClaimed, BudgetExceeded, FakeChat, FakeHR, NotYourRefill,
    SnackError, SnackService, Status, WrongCurrency,
)


class FakeClock:
    """Lets tests move time forward instantly."""
    def __init__(self):
        self.now = datetime(2026, 11, 2, 9, 0)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def hr():
    return FakeHR()


@pytest.fixture
def svc(clock, hr):
    s = SnackService(hr=hr, chat=FakeChat(), clock=clock)
    for name in ["Asha", "Ola", "Ravi"]:
        s.add_volunteer(name)
    return s


# ---------- double-buying ----------

def test_two_people_cannot_both_claim_the_same_refill(svc):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    with pytest.raises(AlreadyClaimed):
        svc.claim(r.id, "Ola", seen_version=0)
    assert r.claimed_by == "Asha"


# ---------- nobody finishes ----------

def test_unfinished_refill_is_handed_to_someone_else(svc, clock):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    clock.advance(hours=25)
    svc.expire_stale_claims()
    assert r.status == Status.CLAIMED
    assert r.claimed_by in {"Ola", "Ravi"}


def test_claim_within_24_hours_is_not_taken_away(svc, clock):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    clock.advance(hours=23)
    svc.expire_stale_claims()
    assert r.claimed_by == "Asha"


def test_only_the_person_who_claimed_can_record_a_purchase(svc):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    with pytest.raises(NotYourRefill):
        svc.record_purchase(r.id, "Ola", 500, "NOK", "receipt.jpg")


# ---------- budgets ----------

def test_purchase_over_remaining_budget_is_rejected(svc):
    r1 = svc.open_refill("Oslo")
    svc.claim(r1.id, "Asha", seen_version=r1.version)
    svc.record_purchase(r1.id, "Asha", 4000, "NOK", "r1.jpg")

    r2 = svc.open_refill("Oslo")
    svc.claim(r2.id, "Ola", seen_version=r2.version)
    with pytest.raises(BudgetExceeded):
        svc.record_purchase(r2.id, "Ola", 3000, "NOK", "r2.jpg")


def test_submitted_but_unapproved_claims_still_count_against_budget(svc):
    r1 = svc.open_refill("Oslo")
    svc.claim(r1.id, "Asha", seen_version=r1.version)
    svc.record_purchase(r1.id, "Asha", 4000, "NOK", "r1.jpg")
    svc.submit_to_hr(r1.id)  # HR has NOT approved it yet

    r2 = svc.open_refill("Oslo")
    svc.claim(r2.id, "Ola", seen_version=r2.version)
    with pytest.raises(BudgetExceeded):
        svc.record_purchase(r2.id, "Ola", 3000, "NOK", "r2.jpg")


def test_purchase_in_the_wrong_currency_is_rejected(svc):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    with pytest.raises(WrongCurrency):
        svc.record_purchase(r.id, "Asha", 500, "INR", "r.jpg")


def test_budgets_and_totals_are_kept_separate_for_nok_and_inr(svc):
    o = svc.open_refill("Oslo")
    svc.claim(o.id, "Asha", seen_version=o.version)
    svc.record_purchase(o.id, "Asha", 1200, "NOK", "o.jpg")

    b = svc.open_refill("Bengaluru")
    svc.claim(b.id, "Ola", seen_version=b.version)
    svc.record_purchase(b.id, "Ola", 12000, "INR", "b.jpg")

    assert svc.monthly_report() == {("Oslo", "NOK"): 1200, ("Bengaluru", "INR"): 12000}
    # Oslo still has its full remaining budget (6000 - 1200 = 4800 NOK).
    assert svc.remaining_budget("Oslo") == 4800
    assert svc.remaining_budget("Bengaluru") == 3000


# ---------- fairness and the full flow ----------

def test_next_volunteer_is_the_one_who_has_done_fewest_refills(svc):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    svc.record_purchase(r.id, "Asha", 500, "NOK", "r.jpg")
    assert svc.pick_volunteer() in {"Ola", "Ravi"}


def test_receipt_is_required_before_hr_submission(svc):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    svc.record_purchase(r.id, "Asha", 500, "NOK", receipt=None)
    with pytest.raises(SnackError):
        svc.submit_to_hr(r.id)


def test_full_lifecycle_from_empty_to_reimbursed(svc, hr):
    r = svc.open_refill("Oslo")
    svc.claim(r.id, "Asha", seen_version=r.version)
    svc.record_purchase(r.id, "Asha", 800, "NOK", "r.jpg")
    svc.submit_to_hr(r.id)
    assert r.status == Status.SUBMITTED

    hr.approve(r.hr_claim_id)
    svc.sync_with_hr()
    assert r.status == Status.REIMBURSED