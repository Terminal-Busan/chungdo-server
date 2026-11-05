"""Bounded official API access. Credentials are never included in errors."""

import os
from urllib.parse import unquote

import httpx


class Gov24Error(Exception):
    pass


class Gov24Client:
    base_url = "https://api.odcloud.kr/api/gov24/v3"

    async def fetch(self, resource: str, *, page=1, per_page=100, policy_id=None):
        key = os.getenv("GOV24_API_KEY") or os.getenv("GOV24_SERVICE_KEY")
        if not key:
            raise Gov24Error("정부24 인증키 설정이 필요합니다")
        params = {"page": page, "perPage": per_page, "returnType": "JSON"}
        if policy_id:
            params["cond[서비스ID::EQ]"] = policy_id
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(
                    f"{self.base_url}/{resource}",
                    params=params,
                    headers={"Authorization": f"Infuser {unquote(key)}"},
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body.get("data"), list):
                    raise TypeError("invalid response")
                return body["data"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise Gov24Error("정부24 조회에 실패했습니다") from None


def normalize_policy(row: dict, conditions: dict | None = None) -> dict:
    agency = row.get("소관기관명") or ""
    # Central institutions have nationwide coverage. Unknown locality stays
    # searchable nationwide rather than excluding potentially eligible people.
    region = None
    if row.get("소관기관유형") == "지자체":
        region = agency.strip() or None
    return {
        "id": str(row["서비스ID"]),
        "name": row["서비스명"],
        "field": row.get("서비스분야"),
        "agency": agency,
        "agency_type": row.get("소관기관유형"),
        "region": region,
        "purpose_summary": row.get("서비스목적요약"),
        "target_text": row.get("지원대상"),
        "criteria_text": row.get("선정기준"),
        "benefit_text": row.get("지원내용"),
        "documents": row.get("구비서류"),
        "apply_method": row.get("신청방법"),
        "apply_deadline": row.get("신청기한"),
        "reception_agency": row.get("접수기관명"),
        "contact": row.get("전화문의"),
        "online_apply_url": row.get("온라인신청사이트URL"),
        "detail_url": row.get("상세조회URL"),
        "gov24_updated_at": row.get("수정일시"),
        "conditions": conditions or {},
        "is_demo": False,
    }
