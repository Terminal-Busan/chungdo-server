import asyncio
import os

import httpx
import pytest

from app.policies.gov24 import Gov24Client, Gov24Error, normalize_policy


@pytest.mark.skipif(os.getenv("WELFARE_RUN_LIVE_GOV24") != "true", reason="Explicit live Gov24 integration only")
def test_live_gov24_list_detail_and_conditions():
    async def run():
        client = Gov24Client()
        rows = await client.fetch("serviceList", per_page=1)
        assert rows, "Official policy list was empty"
        policy_id = str(rows[0]["서비스ID"])
        detail = await client.fetch("serviceDetail", policy_id=policy_id, per_page=1)
        conditions = await client.fetch("supportConditions", policy_id=policy_id, per_page=1)
        assert detail and str(detail[0]["서비스ID"]) == policy_id
        assert conditions and str(conditions[0]["서비스ID"]) == policy_id
        policy = normalize_policy({**rows[0], **detail[0]}, conditions[0])
        assert policy["is_demo"] is False and policy["id"] == policy_id
        assert policy["name"]
    asyncio.run(run())


def test_official_request_auth_filter_and_response(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params, headers):
            captured.update(url=url, params=params, headers=headers)
            return httpx.Response(
                200,
                json={"data": [{"서비스ID": "official-id"}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setenv("GOV24_API_KEY", "encoded%2Bkey")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    rows = asyncio.run(Gov24Client().fetch("serviceDetail", policy_id="official-id"))
    assert rows == [{"서비스ID": "official-id"}]
    assert captured["headers"]["Authorization"] == "Infuser encoded+key"
    assert "serviceKey" not in captured["params"]
    assert captured["params"]["cond[서비스ID::EQ]"] == "official-id"


def test_external_failure_hides_private_details(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            raise httpx.ReadTimeout("secret-token-and-url")

    monkeypatch.setenv("GOV24_API_KEY", "secret-token")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(Gov24Error) as error:
        asyncio.run(Gov24Client().fetch("serviceList"))
    assert "secret" not in str(error.value)


def test_normalization_keeps_official_ids_and_raw_conditions():
    policy = normalize_policy(
        {
            "서비스ID": "official-id",
            "서비스명": "정책",
            "소관기관명": "경상북도 청도군",
            "소관기관유형": "지자체",
        },
        {"JA0110": 19},
    )
    assert policy["id"] == "official-id" and policy["conditions"] == {"JA0110": 19}
    assert policy["region"] == "경상북도 청도군" and policy["is_demo"] is False
