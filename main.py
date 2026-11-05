"""FastAPI entry point for the Chungdo API."""

import logging
import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)
app = FastAPI(title="Chungdo API", version="0.0.1")


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "Hello World!"


@app.get("/health/ready", response_model=None)
async def readiness() -> dict[str, str] | JSONResponse:
    connection_string = os.environ.get("DATABASE_URL")
    if connection_string:
        try:
            async with await psycopg.AsyncConnection.connect(
                connection_string,
                connect_timeout=5,
                options="-c statement_timeout=5000",
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
