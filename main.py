"""FastAPI entry point for the Chungdo API."""

import logging
import os

import certifi
import psycopg
from psycopg.conninfo import conninfo_to_dict
from welfare.api import create_app
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)
app = create_app()


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
