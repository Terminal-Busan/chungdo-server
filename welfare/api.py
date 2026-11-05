"""Welfare Next HTTP contract and resumable server-sent events."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import time
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .agents import AIUnavailable, AnalysisRunner, CopilotNarrator
from .eligibility import mask_personal_information
from .policies import Gov24, PolicyService
from .schemas import Deleted, Error, Health, NavigationCreate, NavigationCreated
from .schemas import NavigationView, PolicyDetail, PolicySummary, QuestionAnswer, QuestionCreate, Success
from .store import Store

ERRORS = {code: {"model": Error} for code in (400, 404, 409, 410, 500, 502, 503)}


def sse(name: str, data: dict, identifier: int | None = None) -> str:
    prefix = f"id: {identifier}\n" if identifier is not None else ""
    return prefix + f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(*, database_url: str | None = None, gov_client: httpx.AsyncClient | None = None, narrator=None) -> FastAPI:
    url = database_url or os.environ.get("DATABASE_URL") or "sqlite+aiosqlite:///./welfare.db"
    store = Store(url)
    client = gov_client or httpx.AsyncClient(timeout=15)
    policy_service = PolicyService(store, Gov24(os.environ.get("GOV24_SERVICE_KEY", ""), client))
    ai = narrator or CopilotNarrator()
    runner = AnalysisRunner(store, policy_service, ai)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(30)
            try:
                await store.cleanup()
            except SQLAlchemyError:
                # A temporary DB outage must not permanently stop retention cleanup.
                continue

    @asynccontextmanager
    async def lifespan(application):
        if not database_url and os.environ.get("WEBSITE_HOSTNAME") and not os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be configured for Azure deployment")
        try:
            await store.initialize()
            cleanup = asyncio.create_task(cleanup_loop())
            try:
                yield
            finally:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                await runner.close()
        finally:
            await store.close()
            if gov_client is None:
                await client.aclose()

    app = FastAPI(title="Welfare Next API", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.policies = policy_service
    app.state.runner = runner
    origins = {"http://localhost:3000"}
    if front_url := os.environ.get("FRONT_URL", "").strip().rstrip("/"):
        origins.add(front_url)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(origins),
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Content-Type", "Last-Event-ID"])

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/navigations"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        return JSONResponse(status_code=error.status_code, headers=error.headers,
                            content={"statusCode": error.status_code, "message": str(error.detail)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        first = error.errors()[0]
        path = ".".join(str(part) for part in first["loc"] if part not in ("body", "query", "path"))
        if path == "profile.age":
            message = "profile.age는 0~120 사이여야 합니다"
        elif path == "consent":
            message = "개인정보 임시 저장 동의가 필요합니다"
        else:
            message = f"{path or '요청'} 값이 올바르지 않습니다"
        return JSONResponse(status_code=400, content={"statusCode": 400, "message": message})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse(status_code=503, content={"statusCode": 503, "message": "데이터 저장소에 연결할 수 없습니다"})

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return JSONResponse(status_code=500, content={"statusCode": 500, "message": "요청을 처리하지 못했습니다"})

    async def get_navigation(identifier: str):
        row, was_expired = await store.get(identifier)
        if row is None:
            if was_expired:
                raise HTTPException(410, "분석 결과가 만료되었습니다")
            raise HTTPException(404, "분석 결과를 찾을 수 없습니다")
        return row

    @app.get("/api/health", response_model=Success[Health])
    async def health():
        return Success(data=Health())

    @app.get("/api/policies", response_model=Success[list[PolicySummary]], responses=ERRORS)
    async def search_policies(
        q: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
        field: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        region: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        for name, value in (("q", q), ("field", field), ("region", region)):
            if value is not None and not value.strip():
                raise HTTPException(400, f"{name} 값이 올바르지 않습니다")
        result = await policy_service.search(
            q.strip() if q else None, field.strip() if field else None,
            region.strip() if region else None, limit,
        )
        return Success(data=result, message="ok" if result else "검색 결과가 없습니다")

    @app.get("/api/policies/{id}", response_model=Success[PolicyDetail], responses=ERRORS)
    async def policy_detail(id: str):
        return Success(data=PolicyDetail.model_validate(await policy_service.detail(id)))

    @app.post("/api/navigations", status_code=201, response_model=Success[NavigationCreated], responses=ERRORS)
    async def create_navigation(body: NavigationCreate):
        cached = await store.get_policy(body.applied_policy_id)
        if cached is None:
            raise HTTPException(404, "정책을 찾을 수 없습니다")
        # Ensure an analysis never starts from a bare, incomplete search record.
        await policy_service.detail(body.applied_policy_id)
        identifier = await store.create(
            body.applied_policy_id, body.profile.model_dump(),
            mask_personal_information(body.rejection_text),
        )
        return Success(data=NavigationCreated(id=identifier), message="created")

    @app.get("/api/navigations/{id}/stream", response_class=StreamingResponse,
             responses={**ERRORS, 200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    async def navigation_stream(id: str, request: Request,
                                last_event_id: Annotated[str | None, Header()] = None):
        cursor = 0
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
                if cursor < 0:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "Last-Event-ID 값이 올바르지 않습니다") from None
        initial = await get_navigation(id)
        await runner.ensure_started(id)

        async def events():
            nonlocal cursor
            if initial["status"] == "done":
                yield sse("result", initial["result"])
                return
            heartbeat_at = time.monotonic()
            while not await request.is_disconnected():
                row, was_expired = await store.get(id)
                if row is None:
                    yield sse("error", {"agent": "PathPlanner", "message": "expired" if was_expired else "deleted", "fallback": False})
                    return
                for entry in await store.events(id, cursor):
                    cursor = entry["id"]
                    yield sse(entry["event"], entry["data"], cursor)
                    if entry["event"] == "result" or (
                        entry["event"] == "error" and entry["data"].get("fallback") is False
                    ):
                        return
                if row["status"] == "done":
                    yield sse("result", row["result"])
                    return
                if row["status"] == "failed":
                    yield sse("error", {"agent": "PathPlanner", "message": "analysis_failed", "fallback": False})
                    return
                await runner.ensure_started(id)
                if time.monotonic() - heartbeat_at >= 15:
                    yield ": keep-alive\n\n"
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    @app.get("/api/navigations/{id}", response_model=Success[NavigationView], responses=ERRORS)
    async def navigation_detail(id: str):
        row = await get_navigation(id)
        return Success(data=NavigationView(
            id=id, status=row["status"], result=row["result"] if row["status"] == "done" else None,
            expires_at=datetime.fromtimestamp(row["expires_at"], timezone.utc),
        ))

    @app.post("/api/navigations/{id}/questions", response_model=Success[QuestionAnswer], responses=ERRORS)
    async def follow_up(id: str, body: QuestionCreate):
        row = await get_navigation(id)
        if row["status"] != "done":
            raise HTTPException(409, "분석이 아직 끝나지 않았습니다")
        try:
            answer = await ai.explain("FollowUp", {
                "question": mask_personal_information(body.question), "result": row["result"],
            })
            generated = True
        except AIUnavailable:
            answer = row["result"]["rejection"]["plain_summary"] + "\n최종 자격과 추가 질문은 해당 정책 담당 기관에 확인하세요."
            generated = False
        # Do not return a response after consent storage has expired or been deleted.
        await get_navigation(id)
        return Success(data=QuestionAnswer(answer=answer, ai_generated=generated))

    @app.delete("/api/navigations/{id}", response_model=Success[Deleted], responses=ERRORS)
    async def delete_navigation(id: str):
        if not await store.delete(id):
            raise HTTPException(404, "분석 결과를 찾을 수 없습니다")
        await runner.cancel(id)
        return Success(data=Deleted(), message="deleted")

    # FastAPI documents 422 by default. This contract uses 400 for all validation errors.
    original_openapi = app.openapi

    def contract_openapi():
        document = original_openapi()
        for path in document["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict):
                    operation.get("responses", {}).pop("422", None)
        return document

    app.openapi = contract_openapi
    return app
