"""Deterministic eligibility checks; unsupported criteria always stay unknown."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.schemas import Criterion, Profile

MEDIAN = json.loads(
    (Path(__file__).resolve().parents[1] / "config/median_income.json").read_text()
)
HOUSEHOLD = {
    "JA0404": "single",
    "JA0403": "single_parent",
    "JA0411": "multi_child",
    "JA0401": "multicultural",
}
EMPLOYMENT = {"JA0326": "employed", "JA0327": "job_seeker", "JA0320": "student"}
LABELS = {
    "single": "1인 가구",
    "couple": "부부 가구",
    "single_parent": "한부모·조손 가구",
    "multi_child": "다자녀 가구",
    "multicultural": "다문화 가구",
    "other": "기타 가구",
    "employed": "근로자·직장인",
    "job_seeker": "구직자·실업자",
    "student": "대학생·대학원생",
    "none": "해당 없음",
}
INCOME_RANGES = ("0~50%", "51~75%", "76~100%", "101~200%", "200% 초과")


def selected(value) -> bool:
    return value is True or str(value).strip().upper() in {"Y", "YES", "TRUE", "1"}


def criterion_records(criteria):
    """Exact values for internal agent tools; public Criterion DTO stays stable."""
    return [
        {
            **c.model_dump(),
            "criterion": c.name,
            "user_value": c.detail.split(" / ")[0],
            "required_value": c.detail.split(" / ", 1)[-1],
        }
        for c in criteria
    ]


def match_level(criteria: list[Criterion]) -> str:
    states = {c.status for c in criteria}
    return (
        "unlikely"
        if "fail" in states
        else "likely"
        if "unknown" in states or not states
        else "eligible"
    )


def check_eligibility(profile: Profile, policy: dict) -> list[Criterion]:
    conditions = policy.get("conditions") or {}
    result = []

    def add(name, status, detail):
        result.append(Criterion(name=name, status=status, detail=detail))

    bounds = []
    for key in ("JA0110", "JA0111"):
        value = conditions.get(key)
        try:
            bounds.append(
                int(value)
                if value not in (None, "") and not isinstance(value, bool)
                else None
            )
        except (ValueError, TypeError):
            bounds.append(None)
    low, high = bounds
    if any(
        conditions.get(k) not in (None, "") and bounds[i] is None
        for i, k in enumerate(("JA0110", "JA0111"))
    ):
        add("연령", "unknown", "정부24 대상 연령 값 확인 필요")
    elif low is not None or high is not None:
        invalid = low is not None and high is not None and low > high
        passed = (low is None or profile.age >= low) and (
            high is None or profile.age <= high
        )
        add(
            "연령",
            "unknown" if invalid else "pass" if passed else "fail",
            f"{profile.age}세 / {low if low is not None else '하한 없음'}~{high if high is not None else '상한 없음'}세",
        )

    income_codes = [f"JA020{i}" for i in range(1, 6)]
    if any(selected(conditions.get(k)) for k in income_codes):
        current_year = datetime.now(ZoneInfo("Asia/Seoul")).year
        if current_year != MEDIAN["year"]:
            add("소득", "unknown", "해당 연도의 기준 중위소득 자료 확인 필요")
        else:
            size = profile.household_size
            base = MEDIAN["households"].get(
                str(size),
                MEDIAN["households"]["7"] + (size - 7) * MEDIAN["additional_member"],
            )
            ratio = Decimal(profile.income_monthly_manwon * 10000) * 100 / Decimal(base)
            index = next(
                (i for i, bound in enumerate((50, 75, 100, 200), 1) if ratio <= bound),
                5,
            )
            # Monthly income is not income-recognition amount; avoid false certainty.
            passed = selected(conditions.get(f"JA020{index}"))
            status = "pass" if passed else "fail"
            if "소득인정액" in (policy.get("criteria_text") or "") + (
                policy.get("target_text") or ""
            ):
                status = "unknown"
            add(
                "소득",
                status,
                f"월 {profile.income_monthly_manwon}만원 (중위소득 약 {ratio:.1f}%) / 정부24 대상 구간 "
                + ", ".join(
                    INCOME_RANGES[i]
                    for i, key in enumerate(income_codes)
                    if selected(conditions.get(key))
                )
                + f". {MEDIAN['year']}년 기준 참고 비율이며 소득인정액은 별도 확인 필요",
            )

    # Codes within a target group are alternatives, not cumulative requirements.
    for name, mapping, actual in (
        ("가구 형태", HOUSEHOLD, profile.household_type),
        ("취업·학업", EMPLOYMENT, profile.employment_status),
    ):
        active = [k for k in mapping if selected(conditions.get(k))]
        if active:
            # Other active codes in the same family can represent an alternative
            # target the collected profile cannot establish.
            family = "JA04" if name == "가구 형태" else "JA03"
            unmapped = any(
                k.startswith(family) and k not in mapping and selected(v)
                for k, v in conditions.items()
            )
            passed = actual in [mapping[k] for k in active]
            status = "pass" if passed else "unknown" if unmapped else "fail"
            add(
                name,
                status,
                f"{LABELS[actual]} / 대상: {', '.join(LABELS[mapping[key]] for key in active)}",
            )

    if selected(conditions.get("JA0412")):
        add(
            "주거",
            "fail" if profile.housing_type == "owned" else "unknown",
            "자가 보유 / 무주택세대 조건"
            if profile.housing_type == "owned"
            else "임차 등 거주 / 세대 전체의 무주택 여부 확인 필요",
        )
    if selected(conditions.get("JA0328")):
        other_targets = any(
            k.startswith("JA03") and k != "JA0328" and selected(v)
            for k, v in conditions.items()
        )
        add(
            "장애 여부",
            "pass"
            if profile.has_disability
            else "unknown"
            if other_targets
            else "fail",
            f"장애 여부 {'예' if profile.has_disability else '아니오'} / 장애인 및 다른 지원대상 조건 확인",
        )

    known = {
        "JA0110",
        "JA0111",
        "JA0412",
        "JA0328",
        *income_codes,
        *HOUSEHOLD,
        *EMPLOYMENT,
    }
    if any(k not in known and selected(v) for k, v in conditions.items()):
        add(
            "기타 지원조건",
            "unknown",
            "현재 입력으로 판정할 수 없는 정부24 지원조건 확인 필요",
        )
    if policy.get("region"):
        region = policy["region"]
        possible = region in {
            profile.sido,
            profile.sigungu,
            f"{profile.sido} {profile.sigungu or ''}".strip(),
        }
        add(
            "거주지역",
            "pass" if possible else "unknown",
            f"입력 지역 {profile.sido} {profile.sigungu or ''} / 정책 지역 {region}. 실제 거주 요건은 원문 확인",
        )
    # Free text may contain additional selection criteria; never infer them.
    if policy.get("criteria_text") or policy.get("target_text") or not result:
        add("세부 선정기준", "unknown", "선정기준 원문과 신청처에서 추가 확인 필요")
    return result
