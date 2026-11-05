"""FastAPI entry point for the Chungdo API."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

import certifi
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from psycopg.conninfo import conninfo_to_dict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import Database
from app.navigations.router import router as navigations_router
from app.navigations.service import NavigationsService
from app.policies.router import router as policies_router
from app.schemas import Envelope, ErrorResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database()
    app.state.navigations = NavigationsService(app.state.db)

    async def cleanup():
        while True:
            if app.state.db.url:
                try:
                    await app.state.db.purge_expired()
                    # A killed process cannot finish its analysis; surface a retryable failure.
                    await app.state.db.query("""UPDATE navigations SET status='failed', result=NULL
                        WHERE status='running' AND started_at < now() - INTERVAL '5 minutes'""")
                except HTTPException:
                    logger.warning("Analysis cleanup unavailable")
            await asyncio.sleep(60)

    janitor = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        janitor.cancel()
        with suppress(asyncio.CancelledError):
            await janitor
        await app.state.navigations.close()


app = FastAPI(
    title="Welfare Next API",
    version="0.0.1",
    lifespan=lifespan,
    responses={
        code: {"model": ErrorResponse} for code in (400, 404, 409, 410, 500, 502, 503)
    },
)
origins = list(
    dict.fromkeys(
        [
            "http://localhost:3000",
            *[
                value.strip()
                for value in os.getenv("FRONT_URL", "").split(",")
                if value.strip()
            ],
        ]
    )
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)
app.include_router(policies_router)
app.include_router(navigations_router)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/navigations"):
        response.headers["Cache-Control"] = "no-store"
    return response


def contract_openapi():
    if app.openapi_schema is None:
        document = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path in document["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict) and "responses" in operation:
                    # FastAPI normally advertises 422; our agreed validation status is 400.
                    operation["responses"].pop("422", None)
        app.openapi_schema = document
    return app.openapi_schema


app.openapi = contract_openapi


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    first = error.errors()[0]
    field = ".".join(str(p) for p in first["loc"] if p not in ("body", "query", "path"))
    # Never echo Pydantic's input or ctx, which may contain private text.
    message = f"{field or '요청'} 입력값을 확인해주세요"
    if field == "profile.age":
        message = "profile.age는 0~120 사이 정수여야 합니다"
    return JSONResponse(
        status_code=400, content={"statusCode": 400, "message": message}
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, error: StarletteHTTPException):
    return JSONResponse(
        status_code=error.status_code,
        content={"statusCode": error.status_code, "message": error.detail},
        headers=error.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    logger.error("API request failed")
    return JSONResponse(
        status_code=500, content={"statusCode": 500, "message": "일시적인 오류입니다"}
    )


@app.get("/api/health", tags=["health"], response_model=Envelope[dict[str, str]])
async def health():
    return {"data": {"status": "ok"}, "message": "ok"}


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "Hello World!"


@app.get("/health/ready", response_model=None)
async def readiness() -> dict[str, str] | JSONResponse:
    connection_string = os.environ.get("DATABASE_URL")
    if connection_string:
        try:
            connection_options = conninfo_to_dict(connection_string)
            ssl_mode = connection_options.get("sslmode", os.environ.get("PGSSLMODE"))
            if (
                ssl_mode in {"verify-ca", "verify-full"}
                and "sslrootcert" not in connection_options
                and not os.environ.get("PGSSLROOTCERT")
            ):
                connection_options["sslrootcert"] = certifi.where()
            connection_options["connect_timeout"] = 5
            connection_options["options"] = (
                connection_options.get("options", "") + " -c statement_timeout=5000"
            ).strip()
            async with await psycopg.AsyncConnection.connect(
                **connection_options,
            ) as connection:
                await connection.execute("SELECT 1")
            return {"status": "ok"}
        except (psycopg.Error, OSError):
            # Connection errors can contain credentials or database hostnames.
            logger.warning("Database readiness check failed")

    return JSONResponse(
        status_code=503,
        content={
            "message": "Database is unavailable",
            "error": "Service Unavailable",
            "statusCode": 503,
        },
    )
