"""Runs against a disposable PostgreSQL database when PG_TEST_URL is set."""

import asyncio
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from welfare.store import RunCancelled, Store, navigations, steps


@pytest.mark.skipif(not os.environ.get("PG_TEST_URL"), reason="PG_TEST_URL is not configured")
def test_postgres_persistence_claim_recovery_expiry_and_cascade():
    async def check():
        url = os.environ["PG_TEST_URL"]
        first, second = Store(url), Store(url)
        try:
            await asyncio.gather(first.initialize(), second.initialize())
            policy_id = "test-" + str(uuid4())
            await first.cache_policies([{"id": policy_id, "name": "원본", "conditions": {"JA0110": 19}}], detailed=True)
            await second.cache_policies([{"id": policy_id, "name": "수정됨"}])
            cached = await first.get_policy(policy_id)
            assert cached["payload"]["name"] == "수정됨"
            assert cached["payload"]["conditions"] == {"JA0110": 19}
            identifier = await first.create(policy_id, {"age": 27}, "masked")
            tokens = await asyncio.gather(first.claim(identifier), second.claim(identifier))
            assert sum(token is not None for token in tokens) == 1
            token = next(token for token in tokens if token)
            await first.emit(identifier, token, "step", {"agent": "PolicyMatcher", "status": "started"})
            async with first.engine.begin() as conn:
                await conn.execute(update(navigations).where(navigations.c.id == identifier).values(lease_until=0))
            replacement = await second.claim(identifier)
            assert replacement and replacement != token
            with pytest.raises(RunCancelled):
                await first.emit(identifier, token, "result", {}, result={})
            await second.emit(identifier, replacement, "result", {"stored": True}, result={"stored": True})
            row, _ = await first.get(identifier)
            assert row["status"] == "done" and row["result"] == {"stored": True}
            assert len(await first.events(identifier, 0)) == 2
            async with first.engine.begin() as conn:
                await conn.execute(update(navigations).where(navigations.c.id == identifier).values(expires_at=time.time() - 1))
            await asyncio.gather(first.cleanup(), second.cleanup())
            row, was_expired = await second.get(identifier)
            assert row is None and was_expired
            async with first.engine.connect() as conn:
                assert await conn.scalar(select(func.count()).select_from(steps).where(steps.c.navigation_id == identifier)) == 0
            assert await first.delete(identifier)
        finally:
            await first.close()
            await second.close()
    asyncio.run(check())
