"""Validate A1 against rule results and trace possible causes to their source."""

import json
from pathlib import Path

from app.ai import grounded_summary
from app.policies.eligibility import criterion_records
from app.safety import mask_pii
from app.schemas import ExplanationOutput, FailedCriterion, Glossary, PossibleReason

GLOSSARY = json.loads(
    (Path(__file__).resolve().parents[1] / "config/glossary.json").read_text()
)
CAUSES = {
    "소득인정액": "입력 월 소득과 다른 소득인정액 산정이 영향을 주었을 가능성을 확인할 필요가 있습니다.",
    "재산": "입력에 없는 재산 관련 심사 기준이 영향을 주었을 가능성을 확인할 필요가 있습니다.",
    "가구": "심사에 적용된 가구 구성과 입력 조건이 다른지 확인할 필요가 있습니다.",
    "서류": "제출 서류와 추가 확인 자료가 심사에 영향을 주었는지 확인할 필요가 있습니다.",
    "거주": "실제 거주 요건과 입력 지역이 다른지 확인할 필요가 있습니다.",
}


def failed_criteria(criteria):
    return [
        FailedCriterion(
            criterion=c["criterion"],
            user_value=c["user_value"],
            required_value=c["required_value"],
            explanation="입력 조건이 지원조건 코드와 일치하지 않습니다.",
        )
        for c in criterion_records(criteria)
        if c["status"] == "fail"
    ]


def glossary_for(policy, notice, proposed=()):
    # Field labels are also terms exposed by the official detail tool. They
    # provide two honest explanations even when the notice is empty/plain text.
    source = (
        "지원대상 선정기준 "
        + (notice or "")
        + " "
        + (policy.get("criteria_text") or "")
        + " "
        + (policy.get("target_text") or "")
    )
    preferred = [g.term for g in proposed if g.term in source and g.term in GLOSSARY]
    terms = list(
        dict.fromkeys(preferred + [term for term in GLOSSARY if term in source])
    )[:5]
    return [Glossary(term=term, meaning=GLOSSARY[term]) for term in terms]


def possible_reasons(policy, notice):
    reasons = []
    for keyword, explanation in CAUSES.items():
        for basis, source in (
            ("통지 문구", notice or ""),
            ("선정기준 원문", policy.get("criteria_text") or ""),
        ):
            if keyword in source:
                reasons.append(PossibleReason(reason=explanation, basis=basis))
                break
    return reasons[:3]


def fallback_explanation(policy, notice, criteria):
    failed = failed_criteria(criteria)
    reasons = [] if failed else possible_reasons(policy, notice)
    if failed:
        names = ", ".join(c.criterion for c in failed)
        prefix = (
            "입력하신 월 소득 기준 추정으로는 "
            if any(c.criterion == "소득" for c in failed)
            else "입력 조건에서 "
        )
        summary = f"{prefix}{names} 항목이 지원조건과 맞지 않습니다. 실제 심사 기준은 통지서와 신청처에서 확인해야 합니다. 다른 지원과 다음 신청을 함께 검토해 보세요."
    elif reasons:
        summary = (
            reasons[0].reason
            + " 현재 자료만으로 탈락 원인을 확정할 수 없습니다. 다른 지원과 다음 신청을 함께 검토해 보세요."
        )
    else:
        summary = "현재 자료만으로 탈락 원인을 확정할 수 없습니다. 통지서와 신청처에서 세부 선정기준을 확인하세요. 다른 지원과 다음 신청을 함께 검토해 보세요."
    if "소득" in summary and "입력하신 월 소득 기준 추정" not in summary:
        summary = "입력하신 월 소득 기준 추정에 따르면 " + summary
    return ExplanationOutput(
        failed_criteria=failed,
        possible_reasons=reasons,
        plain_summary=summary,
        glossary=glossary_for(policy, notice),
    )


def validate_explanation(answer, policy, notice, criteria, context):
    corrected = fallback_explanation(policy, notice, criteria)
    # Never accept model eligibility values, including fabricated fail entries.
    summary = grounded_summary(answer.plain_summary, context)
    if "소득" in summary and "입력하신 월 소득 기준 추정" not in summary:
        summary = "입력하신 월 소득 기준 추정에 따르면 " + summary
        summary = grounded_summary(summary, context)
    if not corrected.failed_criteria:
        sources = {
            "통지 문구": notice or "",
            "선정기준 원문": policy.get("criteria_text") or "",
        }
        allowed = []
        for reason in answer.possible_reasons:
            keywords = [
                key
                for key in CAUSES
                if key in reason.reason and key in sources[reason.basis]
            ]
            if keywords:
                # Ground the factual content in the cited input; do not promote
                # an unsupported model hypothesis into a confirmed rejection.
                allowed.append(
                    PossibleReason(reason=CAUSES[keywords[0]], basis=reason.basis)
                )
        if allowed:
            corrected.possible_reasons = list(
                {(p.reason, p.basis): p for p in allowed}.values()
            )[:3]
        if corrected.possible_reasons:
            # Possible causes must actually reach the user without adding fields
            # to the established public rejection DTO.
            summary = (
                corrected.possible_reasons[0].reason
                + " 현재 자료만으로 탈락 원인을 확정할 수 없습니다. 다음 지원을 함께 검토해 보세요."
            )
    if "소득" in summary and "입력하신 월 소득 기준 추정" not in summary:
        summary = "입력하신 월 소득 기준 추정에 따르면 " + summary
    corrected.plain_summary = mask_pii(summary)
    corrected.glossary = glossary_for(policy, notice, answer.glossary)
    return corrected
