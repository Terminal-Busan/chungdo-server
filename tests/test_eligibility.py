import copy

import pytest

from app.policies.eligibility import check_eligibility, match_level
from app.schemas import Criterion, Profile

PROFILE = {
    "age": 27,
    "sido": "경상북도",
    "sigungu": "청도군",
    "household_type": "single",
    "household_size": 1,
    "income_monthly_manwon": 230,
    "housing_type": "monthly_rent",
    "employment_status": "employed",
    "has_disability": False,
}


def checks(conditions, **extra):
    return check_eligibility(Profile(**PROFILE), {"conditions": conditions, **extra})


@pytest.mark.parametrize(
    "age,expected", [(18, "fail"), (19, "pass"), (34, "pass"), (35, "fail")]
)
def test_age_inclusive_boundaries(age, expected):
    profile = Profile(**{**PROFILE, "age": age})
    assert (
        check_eligibility(profile, {"conditions": {"JA0110": 19, "JA0111": 34}})[
            0
        ].status
        == expected
    )


def test_unparseable_and_reversed_ages_are_unknown():
    assert checks({"JA0110": "unknown"})[0].status == "unknown"
    assert checks({"JA0110": 40, "JA0111": 20})[0].status == "unknown"


def test_unknown_conditions_never_pass():
    assert checks({})[0].status == "unknown"
    assert any(c.status == "unknown" for c in checks({"JA9999": "Y"}))


def test_income_rule_and_income_recognition_distinction():
    assert checks({"JA0201": "Y"})[0].status == "fail"
    assert "정부24 대상 구간 0~50%" in checks({"JA0201": "Y"})[0].detail
    assert checks({"JA0203": "Y"})[0].status == "pass"
    assert (
        checks({"JA0201": "Y"}, criteria_text="소득인정액 기준")[0].status == "unknown"
    )


def test_household_and_employment_alternatives():
    assert checks({"JA0404": "Y", "JA0403": "Y"})[0].status == "pass"
    assert checks({"JA0326": "Y", "JA0327": "Y"})[0].status == "pass"
    assert checks({"JA0327": "Y"})[0].status == "fail"
    assert checks({"JA0327": "Y", "JA0330": "Y"})[0].status == "unknown"


def test_renting_does_not_prove_no_home_ownership_in_household():
    assert checks({"JA0412": "Y"})[0].status == "unknown"


@pytest.mark.parametrize(
    "states,expected",
    [
        (["pass"], "eligible"),
        (["pass", "unknown"], "likely"),
        (["fail", "unknown"], "unlikely"),
        ([], "likely"),
    ],
)
def test_match_level(states, expected):
    assert (
        match_level([Criterion(name="test", status=s, detail="test") for s in states])
        == expected
    )


def test_rules_do_not_mutate_government_data():
    original = {"JA0110": 19, "JA0201": "Y"}
    snapshot = copy.deepcopy(original)
    checks(original)
    assert original == snapshot
