"""Validate A3 action choices and ground factual fields in cached policy data."""

import re
from datetime import date

from app.safety import mask_pii
from app.schemas import AgentAction, NextAction, Pathway

DOCUMENT_CHECK = "신청처에서 구비서류 확인"
APPEAL_CHECK = "통지서에 적힌 이의신청 기한과 방법을 확인하세요"


def deadline_date(policy):
    matches = re.findall(
        r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})",
        policy.get("apply_deadline") or "",
    )
    if not matches:
        return None
    try:
        return date(*map(int, matches[-1]))
    except ValueError:
        return None


class GuidanceService:
    def build(
        self,
        applied: dict,
        alternatives: list[dict],
        proposal=None,
        *,
        criteria=None,
        rejection=None,
        notice="",
    ):
        by_id = {p["id"]: p for p in alternatives}
        policies = {applied["id"]: applied, **by_id}
        criteria = criteria or {}
        appeal_allowed = bool(rejection and rejection.possible_reasons) or bool(
            re.search(r"이의|오류|잘못|정정", notice)
        )

        def canonical(kind, policy_id, requested_documents=None):
            if policy_id is not None and policy_id not in policies:
                return None
            if kind == "appeal" and not appeal_allowed:
                return None
            if kind == "apply_new" and policy_id not in by_id:
                return None
            policy = policies.get(policy_id) or applied
            source = policy.get("documents") or ""
            documents = (
                [d for d in requested_documents if d and d in source]
                if requested_documents is not None
                else [source]
                if source
                else []
            )
            if kind == "documents" and policy_id is None:
                title, description = (
                    "주민센터 복지 상담",
                    "읍·면·동 주민센터에서 현재 조건에 맞는 복지 상담을 받아보세요. "
                    + DOCUMENT_CHECK,
                )
                documents = []
            elif kind == "documents":
                title = "정책 상세와 구비서류 확인"
                description = "정부24 상세조회에서 지원대상과 선정기준을 확인하고 준비할 서류를 확인하세요."
            elif kind == "reapply":
                title = "조건이 바뀌면 재신청 검토"
                description = "소득·가구 상황이 바뀌거나 다음 연도 기준이 바뀌면 재신청 가능 여부를 다시 확인하세요. 날짜나 기준 변화는 통지서 또는 신청처에서 확인이 필요합니다."
            elif kind == "appeal":
                title, description = "이의신청 가능 여부 검토", APPEAL_CHECK
            else:
                title = f"{policy['name']} 신규 신청 검토"
                method = (
                    policy.get("apply_method")
                    or "통지서 또는 신청처에서 확인이 필요합니다"
                )
                description = f"신청방법: {method}"
                if any(c.status == "fail" for c in criteria.get(policy_id, [])):
                    description += " 현재 미충족 조건이 있으므로 조건이 바뀌는 경우에만 신청을 검토하세요."
                else:
                    description += " 세부 조건을 확인한 뒤 신청을 검토하세요."
            if not documents and DOCUMENT_CHECK not in description:
                description += " " + DOCUMENT_CHECK
            return AgentAction(
                type=kind,
                title=mask_pii(title),
                description=mask_pii(description),
                documents=[mask_pii(d) for d in documents],
                policy_id=policy_id,
            )

        defaults = [canonical("documents", applied["id"])]
        if not by_id:
            defaults += [
                canonical("documents", None),
                canonical("reapply", applied["id"]),
            ]
        else:
            defaults += [canonical("apply_new", i) for i in list(by_id)[:3]]
            defaults += [
                canonical("reapply", applied["id"]),
                canonical("appeal", applied["id"]),
            ]
        actions, seen = [], set()
        proposed = proposal.next_actions if proposal else []
        for action in [*proposed, *[a for a in defaults if a is not None]]:
            clean = canonical(action.type, action.policy_id, action.documents)
            if clean is not None and (clean.type, clean.policy_id) not in seen:
                seen.add((clean.type, clean.policy_id))
                actions.append(clean)
        actions = actions[:6]
        actions.sort(
            key=lambda a: {"documents": 0, "appeal": 1, "apply_new": 2, "reapply": 3}[
                a.type
            ]
        )
        proposed_order = (
            {p.policy_id: index for index, p in enumerate(proposal.pathway)}
            if proposal
            else {}
        )

        def priority(policy_id):
            checks = criteria.get(policy_id, [])
            return (
                any(c.status == "fail" for c in checks),
                sum(c.status == "unknown" for c in checks),
                deadline_date(by_id[policy_id]) or date.max,
                proposed_order.get(policy_id, 99),
                policy_id,
            )

        ordered = sorted(by_id, key=priority)
        pathway = []
        for index, policy_id in enumerate(ordered, 1):
            checks = criteria.get(policy_id, [])
            unknown = sum(c.status == "unknown" for c in checks)
            why = (
                "미충족 조건이 남아 있어 조건 변경 여부를 확인한 후 검토합니다"
                if any(c.status == "fail" for c in checks)
                else f"확인 필요 조건이 {unknown}개여서 해당 조건을 확인하며 검토합니다"
            )
            deadline = deadline_date(by_id[policy_id])
            if deadline:
                why += f", 공식 신청기한 {deadline.isoformat()}을 함께 확인하세요"
            pathway.append(Pathway(order=index, policy_id=policy_id, why=why + "."))
        # Null is allowed internally; generic advice is linked to the applied
        # policy when serializing the established public API contract.
        public_actions = [
            NextAction(**{**a.model_dump(), "policy_id": a.policy_id or applied["id"]})
            for a in actions
        ]
        return public_actions, pathway
