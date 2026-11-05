"""Durable sessions, policy cache and SSE events shared by API workers."""

import time
import os

import certifi
from uuid import uuid4

from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, MetaData, String, Table, Text
from sqlalchemy import and_, delete, event, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url

metadata = MetaData()
policies = Table(
    "welfare_policies", metadata,
    Column("id", String(200), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("summary_at", Float, nullable=False),
    Column("detail_at", Float, nullable=False, default=0),
)
navigations = Table(
    "welfare_navigations", metadata,
    Column("id", String(36), primary_key=True),
    Column("policy_id", String(200), nullable=False),
    Column("profile", JSON, nullable=False),
    Column("rejection_text", Text, nullable=False),
    Column("status", String(12), nullable=False),
    Column("result", JSON),
    Column("expires_at", Float, nullable=False, index=True),
    Column("lease_until", Float, nullable=False, default=0),
    Column("lease_token", String(36)),
)
steps = Table(
    "welfare_agent_steps", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("navigation_id", String(36), ForeignKey("welfare_navigations.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("event", String(16), nullable=False),
    Column("data", JSON, nullable=False),
)
expired = Table(
    "welfare_expired_navigations", metadata,
    Column("id", String(36), primary_key=True),
    Column("purge_at", Float, nullable=False, index=True),
)


class Store:
    def __init__(self, url: str):
        if url.startswith(("postgresql://", "postgres://")):
            url = "postgresql+psycopg://" + url.split("://", 1)[1]
        if url.startswith("sqlite://"):
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        connect_args = {"connect_timeout": 5} if url.startswith("postgresql+") else {}
        if connect_args:
            query = make_url(url).query
            ssl_mode = query.get("sslmode", os.environ.get("PGSSLMODE"))
            if ssl_mode in {"verify-ca", "verify-full"} and "sslrootcert" not in query and not os.environ.get("PGSSLROOTCERT"):
                connect_args["sslrootcert"] = certifi.where()
        self.engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self.upsert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

    async def initialize(self):
        async with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                from sqlalchemy import text
                await conn.execute(text("SELECT pg_advisory_xact_lock(198237456)"))
            await conn.run_sync(metadata.create_all)
        await self.cleanup()

    async def close(self):
        await self.engine.dispose()

    async def cleanup(self):
        now = time.time()
        async with self.engine.begin() as conn:
            # Lock rows before expiry cleanup so concurrent replicas cannot race writes.
            rows = (await conn.execute(select(navigations.c.id).where(
                navigations.c.expires_at <= now,
            ).with_for_update())).scalars().all()
            for identifier in rows:
                await conn.execute(self.upsert(expired).values(
                    id=identifier, purge_at=now + 7 * 86400,
                ).on_conflict_do_nothing(index_elements=["id"]))
            await conn.execute(delete(navigations).where(navigations.c.expires_at <= now))
            await conn.execute(delete(expired).where(expired.c.purge_at <= now))

    async def cache_policies(self, payloads: list[dict], *, detailed: bool = False):
        now = time.time()
        async with self.engine.begin() as conn:
            for payload in sorted(payloads, key=lambda item: item["id"]):
                existing = (await conn.execute(select(policies).where(
                    policies.c.id == payload["id"],
                ).with_for_update())).mappings().first()
                if existing:
                    # Serialize detail/list refreshes so metadata stays current while
                    # the detailed conditions survive a list-only refresh.
                    merged = {**existing["payload"], **payload}
                    values = {"payload": merged, "summary_at": now}
                    if detailed:
                        values["detail_at"] = now
                    await conn.execute(update(policies).where(
                        policies.c.id == payload["id"],
                    ).values(**values))
                else:
                    statement = self.upsert(policies).values(
                        id=payload["id"], payload=payload, summary_at=now,
                        detail_at=now if detailed else 0,
                    )
                    if detailed:
                        statement = statement.on_conflict_do_update(
                            index_elements=["id"], set_={
                                "payload": payload, "summary_at": now, "detail_at": now,
                            },
                        )
                    else:
                        statement = statement.on_conflict_do_nothing(index_elements=["id"])
                    await conn.execute(statement)

    async def list_policies(self):
        async with self.engine.connect() as conn:
            return (await conn.execute(select(policies).order_by(policies.c.id))).mappings().all()

    async def get_policy(self, identifier: str):
        async with self.engine.connect() as conn:
            return (await conn.execute(select(policies).where(policies.c.id == identifier))).mappings().first()

    async def create(self, policy_id: str, profile: dict, rejection_text: str):
        identifier = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(insert(navigations).values(
                id=identifier, policy_id=policy_id, profile=profile,
                rejection_text=rejection_text, status="pending", expires_at=time.time() + 86400,
            ))
        return identifier

    async def get(self, identifier: str):
        await self.cleanup()
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(navigations).where(navigations.c.id == identifier))).mappings().first()
            was_expired = (await conn.execute(select(expired.c.id).where(expired.c.id == identifier))).first()
        return row, bool(was_expired)

    async def delete(self, identifier: str):
        # Also permits explicit deletion of an expired ID and its tombstone.
        async with self.engine.begin() as conn:
            removed = await conn.execute(delete(navigations).where(navigations.c.id == identifier))
            tombstone = await conn.execute(delete(expired).where(expired.c.id == identifier))
            return bool(removed.rowcount or tombstone.rowcount)

    async def claim(self, identifier: str):
        now = time.time()
        token = str(uuid4())
        async with self.engine.begin() as conn:
            result = await conn.execute(update(navigations).where(
                navigations.c.id == identifier,
                navigations.c.expires_at > now,
                or_(navigations.c.status == "pending", and_(
                    navigations.c.status == "running", navigations.c.lease_until < now,
                )),
            ).values(status="running", lease_token=token, lease_until=now + 120))
            return token if result.rowcount else None

    async def release(self, identifier: str, token: str):
        async with self.engine.begin() as conn:
            await conn.execute(update(navigations).where(
                navigations.c.id == identifier, navigations.c.lease_token == token,
                navigations.c.status == "running",
            ).values(status="pending", lease_until=0, lease_token=None))

    async def emit(self, identifier: str, token: str, name: str, data: dict, *, result: dict | None = None, failed: bool = False):
        now = time.time()
        values = {"lease_until": now + 120}
        if result is not None:
            values.update(status="done", result=result)
        if failed:
            values.update(status="failed", result=None)
        async with self.engine.begin() as conn:
            touched = await conn.execute(update(navigations).where(
                navigations.c.id == identifier, navigations.c.lease_token == token,
                navigations.c.status == "running", navigations.c.expires_at > now,
            ).values(**values))
            if not touched.rowcount:
                raise RunCancelled()
            await conn.execute(insert(steps).values(navigation_id=identifier, event=name, data=data))

    async def events(self, identifier: str, after: int):
        async with self.engine.connect() as conn:
            return (await conn.execute(select(steps).where(
                steps.c.navigation_id == identifier, steps.c.id > after,
            ).order_by(steps.c.id))).mappings().all()


class RunCancelled(Exception):
    """The session was deleted, expired, or claimed by a newer worker."""
