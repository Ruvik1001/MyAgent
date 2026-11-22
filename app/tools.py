# Инструменты дежурного: журнал, погода, скилл, строка в ленту.
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from agents import RunContextWrapper, function_tool
from openai import AsyncOpenAI

from . import corpus
from .config import ARKHANGELSK, BOARD_CLOCK, ROOT, WEATHER_CITY


@dataclass
class ShiftContext:
    session_id: str
    queue: asyncio.Queue
    log: list[dict] = field(default_factory=list)
    last_weather: str = ""
    silence: bool = True
    last_skill: str = ""
    vector_store_id: str | None = None
    model: str = ""
    client: AsyncOpenAI | None = None
    weather_mcp: dict | None = None


def _now_stamp() -> str:
    try:
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        return now.strftime("%H:%M")
    except Exception:
        return BOARD_CLOCK


async def _emit(ctx: ShiftContext, payload: dict[str, Any]) -> None:
    await ctx.queue.put(payload)


@function_tool
async def write_log(
    wrapper: RunContextWrapper[ShiftContext],
    text: str,
    kind: str = "ДЕЖ",
) -> str:
    """Пишет строку в журнал текущей смены. kind: ДЕЖ, СТАР, МЕТЕО, ПРИЁМ."""
    ctx = wrapper.context
    entry = {
        "time": _now_stamp(),
        "kind": kind[:8],
        "text": text.strip()[:500],
    }
    ctx.log.append(entry)
    await _emit(ctx, {"type": "log", "entry": entry})
    return f"в журнале {entry['time']} {entry['kind']}: {entry['text']}"


@function_tool
def lookup_callsign(query: str) -> str:
    """Сверяет позывной со списком района «Север». Не подтверждает чужие."""
    return corpus.lookup_callsign(query)


@function_tool
def shift_clock() -> str:
    """Часы вахты и что уже известно без журнала: частота, рамка смены."""
    return (
        f"Табло: {BOARD_CLOCK}. Вахта 20:00–08:00. Частота 124.7, Север-Вышка. "
        "Сокол-17 молчит с 21:14. Срок на приём до 21:31 (час сорок от 19:51). "
        "Между вызовами — минута. Тревогу до третьего неответа не орём."
    )


@function_tool
def load_skill(skill_id: str = "radio_watch") -> str:
    """Читает скилл вахты. Перед решением «вызвать / тишина / тревога» бери radio_watch."""
    sid = (skill_id or "radio_watch").strip()
    path = ROOT / "skills" / sid / "SKILL.md"
    if not path.exists():
        # на всякий случай — единственный скилл лежит тут
        path = ROOT / "skills" / "radio_watch" / "SKILL.md"
    if not path.exists():
        return "скилл не найден"
    return path.read_text(encoding="utf-8")


@function_tool
def search_corpus(query: str) -> str:
    """Запасной поиск по файлам журнала, если File Search ещё молчит."""
    return corpus.local_search(query)


def _bind_runtime(ctx: ShiftContext | None) -> ShiftContext | None:
    # вложенный агент иногда приходит без клиента — берём то, что подняли при старте
    if ctx is None:
        return None
    if ctx.client and ctx.model:
        return ctx
    try:
        from .agent import CURRENT
    except Exception:
        return ctx
    if CURRENT is None:
        return ctx
    ctx.client = CURRENT.async_client
    ctx.model = CURRENT.model
    ctx.vector_store_id = CURRENT.vector_store_id
    ctx.weather_mcp = CURRENT.weather_mcp
    return ctx


async def file_search_hosted(ctx: ShiftContext, query: str) -> str:
    ctx = _bind_runtime(ctx)
    if not ctx or not ctx.client or not ctx.vector_store_id or not ctx.model:
        return corpus.local_search(query)
    try:
        resp = await ctx.client.responses.create(
            model=ctx.model,
            instructions=(
                "Ищи только в приложенных файлах. Цитируй коротко. "
                "Если в файлах нет — так и скажи, не выдумывай."
            ),
            input=query,
            tools=[
                {
                    "type": "file_search",
                    "vector_store_ids": [ctx.vector_store_id],
                    "max_num_results": 5,
                }
            ],
        )
        text = (getattr(resp, "output_text", None) or "").strip()
        if len(text) < 20:
            return corpus.local_search(query)
        return text
    except Exception as err:
        return f"{corpus.local_search(query)}\n\n(File Search: {err})"


@function_tool
async def search_journal(
    wrapper: RunContextWrapper[ShiftContext],
    query: str,
) -> str:
    """Ищет в журнале связи, регламенте и списке позывных через File Search."""
    ctx = wrapper.context
    await _emit(ctx, {"type": "tool", "name": "search_journal", "status": "start"})
    text = await file_search_hosted(ctx, query)
    await _emit(ctx, {"type": "tool", "name": "search_journal", "status": "end"})
    return text


async def weather_from_mcp(ctx: ShiftContext, place: str) -> str:
    if not ctx.client or not ctx.model or not ctx.weather_mcp:
        return ""
    try:
        resp = await ctx.client.responses.create(
            model=ctx.model,
            instructions="Запроси погоду и перескажи коротко: температура, ветер, видимость, осадки. Без таблицы.",
            input=f"Какая сейчас погода в городе {place}? Нужны ветер, видимость, осадки.",
            tools=[ctx.weather_mcp],
        )
        text = (getattr(resp, "output_text", None) or "").strip()
        return text
    except Exception:
        return ""


def weather_open_meteo() -> str:
    lat, lon = ARKHANGELSK
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,wind_speed_10m,wind_direction_10m,"
        "relative_humidity_2m,visibility,weather_code"
        "&wind_speed_unit=ms"
        "&timezone=Europe%2FMoscow"
    )
    try:
        with httpx.Client(timeout=12) as http:
            data = http.get(url).json()
        cur = data.get("current") or {}
        t = cur.get("temperature_2m")
        wind = cur.get("wind_speed_10m")
        vis = cur.get("visibility")
        hum = cur.get("relative_humidity_2m")
        vis_km = None
        if isinstance(vis, (int, float)):
            vis_km = round(vis / 1000, 1)
        return (
            f"{WEATHER_CITY}: {t} °C, ветер {wind} м/с, "
            f"видимость {vis_km} км, влажность {hum}%. "
            "Сводка Open-Meteo, не на глаз."
        )
    except Exception as err:
        return f"Сводки нет ({err}). Погоду на глаз не называем."


@function_tool
async def get_weather(
    wrapper: RunContextWrapper[ShiftContext],
    place: str = WEATHER_CITY,
) -> str:
    """Сводка погоды по месту. Сначала MCP weathernow, если молчит — Open-Meteo."""
    ctx = wrapper.context
    await _emit(ctx, {"type": "tool", "name": "get_weather", "status": "start"})
    asked = place or WEATHER_CITY
    # «Камень» у нас точка трассы, не город в Германии
    if "камен" in asked.lower():
        asked = WEATHER_CITY
    text = await weather_from_mcp(ctx, asked)
    if len(text) < 40 or "германи" in text.lower() or "city not found" in text.lower():
        text = weather_open_meteo()
    for i, ch in enumerate(text):
        if "а" <= ch.lower() <= "я" or ch in "ёЁ":
            text = text[i:].lstrip()
            break
    text = text.replace("**", "")
    ctx.last_weather = text
    await _emit(ctx, {"type": "board", "weather": text})
    await _emit(ctx, {"type": "tool", "name": "get_weather", "status": "end"})
    return text
