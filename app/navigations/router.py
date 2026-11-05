import asyncio
import json
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.ai import AIUnavailable, grounded_summary
from app.safety import mask_pii
from app.schemas import (
    Answer,
    CreateNavigation,
    Deleted,
    Envelope,
    Question,
    SessionCreated,
    SessionResult,
    SummaryOutput,
)

router = APIRouter(prefix="/api/navigations", tags=["navigations"])


def envelope(data, message="ok"):
    return {"data": data, "message": message}


def sse(event, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("", status_code=201, response_model=Envelope[SessionCreated])
async def create_navigation(body: CreateNavigation, request: Request):
    return envelope(await request.app.state.navigations.create(body), "created")


@router.get("/{id}/stream", response_class=StreamingResponse)
async def stream_navigation(id: UUID, request: Request):
    cursor = request.headers.get("Last-Event-ID", "0")
    try:
        cursor = int(cursor)
        if cursor < 0:
            raise ValueError("negative cursor")
    except ValueError:
        raise HTTPException(400, "Last-Event-ID 값이 올바르지 않습니다") from None
    service = request.app.state.navigations
    navigation_id = str(id)
    already_done = (await service.get(navigation_id))["status"] == "done"
    await service.start(navigation_id)

    async def stream():
        after = cursor
        last_heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                row = await service.get(navigation_id)
                if already_done:
                    yield sse("result", row["result"])
                    break
                events = await service.events(navigation_id, after)
                for item in events:
                    after = item["id"]
                    yield sse(item["content"]["event"], item["content"]["data"], after)
                if row["status"] == "done":
                    yield sse("result", row["result"])
                    break
                if row["status"] == "failed":
                    yield sse(
                        "error",
                        {
                            "agent": "Workflow",
                            "message": "분석을 완료하지 못했습니다",
                            "fallback": False,
                        },
                    )
                    break
                if time.monotonic() - last_heartbeat > 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- Headers are already sent; finish with a safe SSE error.
            yield sse(
                "error",
                {
                    "agent": "Workflow",
                    "message": "분석 연결이 종료되었습니다. 결과를 다시 조회해주세요",
                    "fallback": False,
                },
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{id}", response_model=Envelope[SessionResult])
async def get_navigation(id: UUID, request: Request):
    row = await request.app.state.navigations.get(str(id))
    return envelope(
        {
            "id": str(row["id"]),
            "status": row["status"],
            "result": row["result"] if row["status"] == "done" else None,
            "expires_at": row["expires_at"].isoformat(),
        }
    )


@router.delete("/{id}", response_model=Envelope[Deleted])
async def delete_navigation(id: UUID, request: Request):
    await request.app.state.navigations.delete(str(id))
    return envelope({"deleted": True}, "deleted")


@router.post("/{id}/questions", response_model=Envelope[Answer])
async def ask_question(id: UUID, body: Question, request: Request):
    row = await request.app.state.navigations.get(str(id))
    if row["status"] != "done":
        raise HTTPException(409, "분석이 아직 끝나지 않았습니다")
    context = {"question": mask_pii(body.question), "result": row["result"]}
    try:
        answer = await request.app.state.navigations.ai.generate(
            "Followup", context, SummaryOutput
        )
        await request.app.state.navigations.get(str(id))
        return envelope(
            {
                "answer": grounded_summary(answer.plain_summary, context),
                "ai_generated": True,
            }
        )
    except AIUnavailable:
        raise HTTPException(
            500, "후속 질문에 답변하지 못했습니다. 잠시 후 다시 시도해주세요"
        ) from None
