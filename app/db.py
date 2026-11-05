"""Small PostgreSQL gateway shared by the three business modules."""

import os
from pathlib import Path

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class Database:
    def __init__(self, url: str | None = None):
        self.url = url or os.getenv("DATABASE_URL")

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self.url:
            raise HTTPException(503, "데이터베이스 연결 설정이 필요합니다")
        try:
            async with await psycopg.AsyncConnection.connect(
                self.url,
                row_factory=dict_row,
                connect_timeout=5,
            ) as connection:
                await connection.execute("SET LOCAL statement_timeout = '10s'")
                cursor = (
                    await connection.execute(
                        sql,
                        tuple(
                            Jsonb(p) if isinstance(p, (dict, list)) else p
                            for p in params
                        ),
                    )
                    if params
                    else await connection.execute(sql)
                )
                return list(await cursor.fetchall()) if cursor.description else []
        except (psycopg.Error, OSError):
            raise HTTPException(500, "일시적인 오류입니다") from None

    async def initialize(self):
        # Explicit CLI operation, not an automatic migration of the team's DB.
        schema = Path(__file__).resolve().parents[1] / "seed/schema.sql"
        await self.query(schema.read_text())

    async def purge_expired(self):
        # Keep only an anonymous tombstone so expired IDs continue returning 410.
        await self.query("""DELETE FROM agent_steps WHERE navigation_id IN
            (SELECT id FROM navigations WHERE expires_at <= now())""")
        await self.query("""UPDATE navigations SET rejection_text=NULL,
            profile='{}'::jsonb, result=NULL, status='failed'
            WHERE expires_at <= now() AND (profile <> '{}'::jsonb OR result IS NOT NULL)""")
