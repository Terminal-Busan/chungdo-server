from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request

from app.policies.service import PoliciesService
from app.schemas import Envelope, PolicyDetail, PolicySummary

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("", response_model=Envelope[list[PolicySummary]])
async def search_policies(
    request: Request,
    q: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    field: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    region: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    if any(value is not None and not value.strip() for value in (q, field, region)):
        raise HTTPException(400, "검색 조건을 확인해주세요")
    rows = await PoliciesService(request.app.state.db).search(q, field, region, limit)
    return {
        "data": [
            {key: row.get(key) for key in ("id", "name", "field", "agency", "region")}
            for row in rows
        ],
        "message": "ok" if rows else "검색 결과가 없습니다",
    }


@router.get("/{id}", response_model=Envelope[PolicyDetail])
async def policy_detail(
    request: Request, id: Annotated[str, Path(min_length=1, max_length=64)]
):
    service = PoliciesService(request.app.state.db)
    policy = await service.get(id, live=True)
    return {"data": service.detail(policy), "message": "ok"}
