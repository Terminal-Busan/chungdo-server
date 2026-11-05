"""Deterministic checks. Source tags are hints, not a legal eligibility decision."""

import math
import re

from .schemas import Criterion


def mask_personal_information(value: str) -> str:
    value = re.sub(r"(?<!\d)\d{6}\s*[-–]?\s*[1-8]\d{6}(?!\d)", "[주민번호 마스킹]", value)
    return re.sub(
        r"(?<!\d)(?:0\d{1,2}|\+82[ -]?(?:0)?\d{1,2})[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)",
        "[전화번호 마스킹]", value,
    )


def check_eligibility(policy: dict, profile: dict) -> list[Criterion]:
    conditions = policy.get("conditions", {})
    checks = []
    lower, upper = conditions.get("JA0110"), conditions.get("JA0111")
    if lower not in (None, "") or upper not in (None, ""):
        try:
            minimum = float(lower) if lower not in (None, "") else 0
            maximum = float(upper) if upper not in (None, "") else 120
            if not (math.isfinite(minimum) and math.isfinite(maximum) and 0 <= minimum <= maximum):
                raise ValueError("Invalid age range")
            status = "pass" if minimum <= profile["age"] <= maximum else "fail"
            checks.append(Criterion(name="연령", status=status, detail=f"{profile['age']}세 / {minimum:g}~{maximum:g}세"))
        except (TypeError, ValueError):
            checks.append(Criterion(name="연령", status="unknown", detail="지원조건 연령 값을 확인해야 합니다"))

    area = policy.get("region")
    location = " ".join(filter(None, [profile["sido"], profile.get("sigungu")]))
    if area:
        # A missing district cannot establish that the applicant is outside the area.
        if area in location:
            status = "pass"
        elif profile["sido"] not in area:
            status = "fail"
        else:
            status = "unknown"
        checks.append(Criterion(name="지역", status=status, detail=f"거주지 {location} / 담당 지역 {area}"))

    selected = {code for code, value in conditions.items() if value == "Y"}
    # Only a single explicit target can be checked without assuming AND/OR semantics.
    household_codes = selected & {"JA0401", "JA0402", "JA0403", "JA0404", "JA0410", "JA0411", "JA0412", "JA0413", "JA0414"}
    if household_codes == {"JA0404"}:
        status = "pass" if profile["household_size"] == 1 and profile["household_type"] == "single" else "fail"
        checks.append(Criterion(name="가구", status=status, detail=f"가구원 {profile['household_size']}명 / 1인가구 대상 태그"))
        selected -= household_codes

    unhandled = selected
    if unhandled:
        checks.append(Criterion(name="추가 지원조건", status="unknown", detail="기관 확인이 필요한 조건: " + ", ".join(sorted(unhandled))))
    # Income tags use median-income bands. Monthly income alone is insufficient to
    # calculate income recognition, assets, or year-specific household medians.
    if policy.get("criteria_text") or policy.get("target_text") or not checks:
        checks.append(Criterion(name="상세 선정기준", status="unknown", detail="지원대상·선정기준 원문과 소득인정액 등을 담당 기관에 확인해야 합니다"))
    return checks


def match_level(checks: list[Criterion]) -> str:
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "unlikely"
    return "likely" if "unknown" in statuses else "eligible"
