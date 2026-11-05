"""Explicit schema setup and bounded Gov24 sync; never print credentials."""

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from app.db import Database
from app.policies.gov24 import Gov24Client, normalize_policy
from app.policies.service import PoliciesService


async def synchronize(args):
    db = Database()
    if args.init:
        await db.initialize()
    service = PoliciesService(db)
    if args.demo:
        if not service.demo:
            raise ValueError(
                "데모 데이터는 WELFARE_DEMO_MODE=true에서만 적재할 수 있습니다"
            )
        demo = Path(__file__).resolve().parents[1] / "seed/demo.json"
        records = json.loads(demo.read_text())
        for row in records:
            await service.upsert(row)
        return len(records)
    if args.init_only:
        return 0
    client = Gov24Client()
    count = 0
    # 3 requests per selected policy + paged lists, bounded well below daily quota.
    for page in range(1, args.pages + 1):
        records = await client.fetch("serviceList", page=page, per_page=args.per_page)
        for row in records:
            if count >= args.max_policies:
                return count
            if row.get("사용자구분") and "개인" not in row["사용자구분"]:
                continue
            if not any(
                field in (row.get("서비스분야") or "")
                for field in args.fields.split(",")
            ):
                continue
            policy_id = str(row["서비스ID"])
            conditions = await client.fetch(
                "supportConditions", policy_id=policy_id, per_page=1
            )
            detail = await client.fetch(
                "serviceDetail", policy_id=policy_id, per_page=1
            )
            matching_detail = next(
                (r for r in detail if str(r.get("서비스ID")) == policy_id), {}
            )
            matching_conditions = next(
                (r for r in conditions if str(r.get("서비스ID")) == policy_id), {}
            )
            await service.upsert(
                normalize_policy({**row, **matching_detail}, matching_conditions)
            )
            count += 1
        if len(records) < args.per_page:
            break
    return count


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create tables if absent")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--pages", type=int, choices=range(1, 21), default=3)
    parser.add_argument("--per-page", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--max-policies", type=int, choices=range(1, 501), default=100)
    parser.add_argument("--fields", default="주거,고용,생활안정")
    args = parser.parse_args()
    try:
        count = asyncio.run(synchronize(args))
        print(f"정책 동기화 완료: {count}건")
    except Exception:  # noqa: BLE001 -- CLI failures must not print keys or connection strings.
        # Deliberately omit raw external/DB exceptions and their connection URLs.
        print("동기화 실패: DB 설정·스키마·정부24 인증 및 연결을 확인하세요")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
