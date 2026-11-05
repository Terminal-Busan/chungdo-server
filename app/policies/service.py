import asyncio
import os

from fastapi import HTTPException

from app.ai import AIUnavailable, grounded_summary
from app.db import Database
from app.policies.eligibility import (
    check_eligibility,
    criterion_records,
    match_level,
    selected,
)
from app.policies.gov24 import Gov24Client, Gov24Error, normalize_policy
from app.schemas import Alternative


def business_only(policy):
    codes = policy.get("conditions") or {}
    business = any(
        key.startswith(("JA11", "JA12", "JA21", "JA22")) and selected(value)
        for key, value in codes.items()
    )
    personal = any(
        key.startswith(("JA01", "JA02", "JA03", "JA04"))
        and (selected(value) or key in {"JA0110", "JA0111"} and value not in (None, ""))
        for key, value in codes.items()
    )
    return business and not personal


POLICY_COLUMNS = (
    "id",
    "name",
    "field",
    "agency",
    "agency_type",
    "region",
    "purpose_summary",
    "target_text",
    "criteria_text",
    "benefit_text",
    "documents",
    "apply_method",
    "apply_deadline",
    "reception_agency",
    "contact",
    "online_apply_url",
    "detail_url",
    "gov24_updated_at",
    "conditions",
    "is_demo",
)


class PoliciesService:
    def __init__(self, db: Database):
        self.db = db
        self.gov24 = Gov24Client()
        self.demo = os.getenv("WELFARE_DEMO_MODE", "false").lower() == "true"

    async def search(self, q=None, field=None, region=None, limit=20, exclude_ids=None):
        clauses, params = ["is_demo = %s"], [self.demo]
        for value, expression in (
            (q, "name ILIKE %s"),
            (field, "field = %s"),
            (region, "(region IS NULL OR region ILIKE %s)"),
        ):
            if value:
                clauses.append(expression)
                escaped = (
                    value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                params.append(value if expression == "field = %s" else f"%{escaped}%")
        if exclude_ids:
            for policy_id in exclude_ids:
                clauses.append("id <> %s")
                params.append(policy_id)
        params.append(limit)
        return await self.db.query(
            f"SELECT * FROM policies WHERE {' AND '.join(clauses)} ORDER BY name, id LIMIT %s",
            tuple(params),
        )

    async def get(self, policy_id: str, *, live=False):
        rows = await self.db.query(
            "SELECT * FROM policies WHERE id=%s AND is_demo=%s", (policy_id, self.demo)
        )
        if rows:
            return rows[0]
        if live and not self.demo:
            try:
                records = await self.gov24.fetch(
                    "serviceDetail", policy_id=policy_id, per_page=1
                )
                records = [r for r in records if str(r.get("서비스ID")) == policy_id]
                if records:
                    try:
                        conditions = await self.gov24.fetch(
                            "supportConditions", policy_id=policy_id, per_page=1
                        )
                    except Gov24Error:
                        conditions = []
                    matching = next(
                        (r for r in conditions if str(r.get("서비스ID")) == policy_id),
                        {},
                    )
                    policy = normalize_policy(records[0], matching)
                    await self.upsert(policy)
                    return policy
            except Gov24Error:
                raise HTTPException(
                    502, "정부24 상세 정보를 불러오지 못했습니다"
                ) from None
        raise HTTPException(404, "정책을 찾을 수 없습니다")

    async def upsert(self, policy: dict):
        columns = ", ".join(POLICY_COLUMNS)
        placeholders = ", ".join(["%s"] * len(POLICY_COLUMNS))
        updates = ", ".join(
            f"{key}=EXCLUDED.{key}" for key in POLICY_COLUMNS if key != "id"
        )
        await self.db.query(
            f"INSERT INTO policies ({columns}) VALUES ({placeholders}) ON CONFLICT (id) DO UPDATE SET {updates}, synced_at=now()",
            tuple(policy.get(key) for key in POLICY_COLUMNS),
        )

    @staticmethod
    def detail(policy):
        return {
            "id": policy["id"],
            "name": policy["name"],
            "summary": policy.get("purpose_summary"),
            **{
                key: policy.get(key)
                for key in (
                    "target_text",
                    "criteria_text",
                    "conditions",
                    "documents",
                    "apply_method",
                    "apply_deadline",
                    "online_apply_url",
                    "detail_url",
                    "gov24_updated_at",
                )
            },
        }


class PolicySearchSession:
    """One analysis: 3 searches, at most 8 distinct candidate evaluations."""

    def __init__(self, service, profile, applied, rejection, log):
        self.service, self.profile, self.applied = service, profile, applied
        self.rejection, self.log = rejection, log
        self.policies, self.criteria = {}, {}
        self.search_calls = 0
        self.lock = asyncio.Lock()
        preferred = (
            "보육·교육" if profile.employment_status == "student" else "고용·창업"
        )
        other = preferred if preferred != applied.get("field") else "주거·자립"
        if other == applied.get("field"):
            other = "생활안정"
        self.plan = [
            {"field": applied.get("field")},
            {"region": profile.sigungu or profile.sido},
            {"field": other, "region": profile.sido},
        ]

    async def search(self, keyword=None, field=None, region=None, exclude_ids=None):
        async with self.lock:
            if self.search_calls >= 3:
                raise ValueError("검색은 최대 3회입니다")
            planned = self.plan[self.search_calls]
            self.search_calls += 1
            excluded = list(dict.fromkeys([self.applied["id"], *(exclude_ids or [])]))
            args = {
                "q": keyword,
                "field": field,
                "region": region,
                **planned,
                "limit": 50,
                "exclude_ids": excluded,
            }
            await self.log(
                "search_policies",
                {
                    "keyword": keyword,
                    "field": args["field"],
                    "region": args["region"],
                    "exclude_ids": excluded,
                },
            )
            rows = await self.service.search(**args)
            if self.search_calls == 2:
                rows.sort(key=lambda row: row.get("region") is None)
            for row in rows:
                if (
                    row["id"] not in excluded
                    and not business_only(row)
                    and len(self.policies) < 8
                ):
                    self.policies.setdefault(row["id"], row)
            return [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "field": p.get("field"),
                    "region": p.get("region"),
                }
                for p in self.policies.values()
            ]

    async def fill(self):
        while self.search_calls < 3:
            await self.search()
        for policy_id in self.policies:
            if policy_id not in self.criteria:
                await self.check(policy_id)

    async def check(self, policy_id):
        if policy_id not in self.policies:
            raise ValueError("검색된 개인 대상 후보만 판정할 수 있습니다")
        await self.log("check_eligibility", {"policy_id": policy_id})
        if policy_id not in self.criteria:
            self.criteria[policy_id] = check_eligibility(
                self.profile, self.policies[policy_id]
            )
        return criterion_records(self.criteria[policy_id])

    def select(self, answer=None):
        # Pure-code fallback is safe even if the stage exhausted its I/O budget.
        for policy_id, policy in self.policies.items():
            if policy_id not in self.criteria:
                self.criteria[policy_id] = check_eligibility(self.profile, policy)
        proposed = (
            {item.policy_id: item.reason for item in answer.alternatives}
            if answer
            else {}
        )
        ids = list(self.policies)
        ids.sort(
            key=lambda i: (
                match_level(self.criteria[i]) == "unlikely",
                list(proposed).index(i) if i in proposed else 5,
                sum(c.status == "unknown" for c in self.criteria[i]),
                i,
            )
        )
        good = [i for i in ids if match_level(self.criteria[i]) != "unlikely"]
        chosen = (
            (good + [i for i in ids if i not in good])[:5]
            if len(good) < 3
            else good[:5]
        )
        alternatives = []
        failed_names = ", ".join(c.criterion for c in self.rejection.failed_criteria)
        for policy_id in chosen:
            policy, criteria = self.policies[policy_id], self.criteria[policy_id]
            match = match_level(criteria)
            reason = (
                f"기존 탈락 항목인 {failed_names}을 포함해 비교했으며 확인된 미충족 조건이 없어 검토할 수 있습니다"
                if failed_names
                else "현재 입력에서 확인된 미충족 조건이 없어 세부 기준을 확인해 볼 수 있습니다"
            )
            if match == "unlikely":
                reason = "미충족 조건이 남아 있어 해당 조건이 바뀌는 경우에만 검토할 후보입니다"
            generated = False
            if policy_id in proposed:
                try:
                    model_reason = grounded_summary(
                        proposed[policy_id],
                        {
                            "policy": self.service.detail(policy),
                            "criteria": [c.model_dump() for c in criteria],
                        },
                        max_sentences=1,
                    )
                    if match != "unlikely":
                        reason = model_reason
                        if failed_names and not any(
                            c.criterion in reason
                            for c in self.rejection.failed_criteria
                        ):
                            reason = (
                                f"기존 탈락 항목인 {failed_names}과 비교해 보면 "
                                + reason
                            )
                        generated = True
                except AIUnavailable:
                    generated = False
            alternatives.append(
                Alternative(
                    policy_id=policy_id,
                    name=policy["name"],
                    match=match,
                    criteria=criteria,
                    reason=reason,
                    ai_generated=generated,
                )
            )
        return alternatives
