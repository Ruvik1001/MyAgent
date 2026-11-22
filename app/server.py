# Пульт: статика из web/ и SSE на /api/chat.
from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import Runtime, build_runtime, run_turn
from .config import BOARD_CLOCK, CENTER_NAME, FREQUENCY, ROOT, SHIFT_END, SHIFT_START
from .corpus import parse_callsigns
from .tools import ShiftContext

SESSIONS: dict[str, dict[str, Any]] = {}
RUNTIME: Runtime | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global RUNTIME
    print("поднимаем агентов и журнал...")
    RUNTIME = build_runtime()
    print("модель:", RUNTIME.model)
    print("vector store:", RUNTIME.vector_store_id)
    yield


app = FastAPI(title="Дежурный ЦУП", lifespan=lifespan)


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


def _session(session_id: str | None) -> dict[str, Any]:
    sid = session_id or uuid.uuid4().hex
    if sid not in SESSIONS:
        SESSIONS[sid] = {
            "id": sid,
            "history": [],
            "transcript": [],
            "log": [],
            "weather": "",
            "silence": True,
        }
    return SESSIONS[sid]


@app.get("/api/health")
def health():
    return {
        "ok": RUNTIME is not None,
        "model": None if RUNTIME is None else RUNTIME.model,
        "file_search": None if RUNTIME is None else bool(RUNTIME.vector_store_id),
    }


@app.get("/api/status")
def status(session_id: str | None = None):
    sess = _session(session_id)
    return {
        "session_id": sess["id"],
        "center": CENTER_NAME,
        "frequency": FREQUENCY,
        "clock": BOARD_CLOCK,
        "shift": f"{SHIFT_START}–{SHIFT_END}",
        "silence": sess["silence"],
        "weather": sess["weather"],
        "callsigns": parse_callsigns(),
        "log": sess["log"],
        "transcript": sess.get("transcript") or [],
    }


@app.post("/api/reset")
def reset(session_id: str | None = None):
    sess = _session(session_id)
    sid = sess["id"]
    SESSIONS[sid] = {
        "id": sid,
        "history": [],
        "transcript": [],
        "log": [],
        "weather": "",
        "silence": True,
    }
    return {"session_id": sid}


@app.post("/api/chat")
async def chat(body: ChatIn):
    if RUNTIME is None:
        raise HTTPException(503, "агент ещё не готов")
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "пусто")
    sess = _session(body.session_id)
    queue: asyncio.Queue = asyncio.Queue()
    ctx = ShiftContext(session_id=sess["id"], queue=queue, log=sess["log"])
    ctx.last_weather = sess["weather"]
    ctx.silence = sess["silence"]

    async def produce():
        try:
            new_history, final = await run_turn(RUNTIME, ctx, sess["history"], text)
            sess["history"] = new_history
            sess["log"] = ctx.log
            sess["weather"] = ctx.last_weather
            sess["silence"] = ctx.silence
            sess.setdefault("transcript", []).append({"role": "user", "text": text})
            if final:
                sess["transcript"].append({"role": "duty", "text": final})
        except Exception as err:
            traceback.print_exc()
            await queue.put({"type": "error", "message": str(err)})
            await queue.put({"type": "done", "final": ""})
        finally:
            await queue.put(None)

    async def events():
        task = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Session-Id": sess["id"],
        },
    )


app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")


def main():
    import uvicorn

    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
