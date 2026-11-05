"""Government24 v3 adapter and a persistent cache of source records."""

import asyncio
import time
from urllib.parse import unquote

import httpx
from fastapi import HTTPException

from .schemas import PolicySummary
from .store import Store

GOV24_BASE_URL = "https://api.odcloud.kr/api/gov24/v3"
LOCAL_TYPES = ("지방", "교육청", "시군구", "광역시도", "시·군·구", "지자체")


def summary_from_source(row: dict) -> dict:
    agency = str(row.get("소관기관명") or "")
    agency_type = str(row.get("소관기관유형") or "")
    # Gov24 has no region column. Local-government agency names identify the area.
    region = agency if any(value in agency_type for value in LOCAL_TYPES) else None
    return {
        "id": str(row["서비스ID"]), "name": str(row.get("서비스명") or ""),
        "field": str(row.get("서비스분야") or ""), "agency": agency, "region": region,
        "detail_url": str(row.get("상세조회URL") or ""),
    }


def detail_from_source(row: dict, conditions: dict, summary: dict) -> dict:
    mapping = {
        "summary": "서비스목적", "target_text": "지원대상", "criteria_text": "선정기준",
        "documents": "구비서류", "apply_method": "신청방법", "apply_deadline": "신청기한",
        "online_apply_url": "온라인신청사이트URL", "gov24_updated_at": "수정일시",
    }
    payload = {**summary, "id": str(row["서비스ID"]), "name": str(row.get("서비스명") or summary.get("name", ""))}
    payload.update({name: str(row.get(source) or "") for name, source in mapping.items()})
    payload["conditions"] = {key: value for key, value in conditions.items() if key.startswith("JA")}
    return payload


class Gov24:
    def __init__(self, key: str, client: httpx.AsyncClient):
        self.key = unquote(key)
        self.client = client

    async def page(self, endpoint: str, *, page: int = 1, identifier: str | None = None):
        if not self.key:
            raise HTTPException(502, "정부24 API 인증키가 설정되지 않았습니다")
        params = {"serviceKey": self.key, "page": page, "perPage": 1000, "returnType": "JSON"}
        if identifier is not None:
            params["cond[서비스ID::EQ]"] = identifier
        try:
            response = await self.client.get(f"{GOV24_BASE_URL}/{endpoint}", params=params)
            response.raise_for_status()
            payload = response.json()
            rows = payload["data"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid Gov24 records")
            count = int(payload.get("matchCount", payload.get("totalCount", len(rows))))
            if count < 0:
                raise ValueError("Invalid Gov24 count")
            return rows, count
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # Never include the authenticated request URL in an error or log.
            raise HTTPException(502, "정부24 상세 정보를 불러오지 못했습니다") from None

    async def listing(self):
        records = []
        for page in range(1, 101):
            rows, total = await self.page("serviceList", page=page)
            records.extend(rows)
            if len(records) >= total:
                try:
                    if any(not row.get("서비스ID") or not row.get("서비스명") for row in records):
                        raise ValueError("Invalid Gov24 policy identity")
                    return [summary_from_source(row) for row in records]
                except (KeyError, ValueError, TypeError):
                    raise HTTPException(502, "정부24 정책 목록을 불러오지 못했습니다") from None
            if not rows:
                break
        raise HTTPException(502, "정부24 정책 목록을 불러오지 못했습니다")

    async def detail(self, identifier: str, summary: dict):
        rows, _ = await self.page("serviceDetail", identifier=identifier)
        row = next((row for row in rows if str(row.get("서비스ID")) == identifier), None)
        if row is None:
            raise HTTPException(404, "정책을 찾을 수 없습니다")
        if not summary.get("name"):
            listed, _ = await self.page("serviceList", identifier=identifier)
            metadata = next((item for item in listed if str(item.get("서비스ID")) == identifier), None)
            if metadata:
                summary = summary_from_source(metadata)
        conditions, _ = await self.page("supportConditions", identifier=identifier)
        condition = next((row for row in conditions if str(row.get("서비스ID")) == identifier), {})
        return detail_from_source(row, condition, summary)


class PolicyService:
    def __init__(self, store: Store, gov: Gov24):
        self.store = store
        self.gov = gov
        self.refresh_lock = asyncio.Lock()
        self.refreshed_at = 0.0

    async def search(self, q: str | None, field: str | None, region: str | None, limit: int):
        async with self.refresh_lock:
            cached = await self.store.list_policies()
            if time.time() - self.refreshed_at > 3600:
                try:
                    payloads = await self.gov.listing()
                    await self.store.cache_policies(payloads)
                    self.refreshed_at = time.time()
                    cached = await self.store.list_policies()
                except HTTPException:
                    if not cached:
                        raise
        found = []
        for row in cached:
            policy = PolicySummary.model_validate(row["payload"])
            if q and q.casefold() not in policy.name.casefold():
                continue
            if field and field != policy.field:
                continue
            if region and policy.region and region not in policy.region and policy.region not in region:
                continue
            found.append(policy)
            if len(found) == limit:
                break
        return found

    async def detail(self, identifier: str):
        cached = await self.store.get_policy(identifier)
        if cached and cached["detail_at"] > time.time() - 86400:
            return cached["payload"]
        summary = cached["payload"] if cached else {"id": identifier, "name": ""}
        try:
            payload = await self.gov.detail(identifier, summary)
            await self.store.cache_policies([payload], detailed=True)
            return payload
        except HTTPException as error:
            if cached and cached["detail_at"]:
                return cached["payload"]
            if error.status_code == 404:
                raise
            raise HTTPException(502, "정부24 상세 정보를 불러오지 못했습니다") from None

    async def candidates(self, excluded: str, profile: dict):
        # Restrict to the same service field and region before doing detail lookups.
        applied = await self.store.get_policy(excluded)
        field = applied["payload"].get("field") if applied else None
        rows = await self.store.list_policies()
        candidates = []
        region = " ".join(filter(None, [profile["sido"], profile.get("sigungu")]))
        for row in rows:
            payload = row["payload"]
            if payload["id"] == excluded or (field and payload.get("field") != field):
                continue
            area = payload.get("region")
            if area and area not in region and region not in area:
                continue
            candidates.append(payload["id"])
        return candidates[:20]
