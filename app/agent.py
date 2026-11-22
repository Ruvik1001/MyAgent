# Сборка агентов. Дежурный говорит с человеком, журнал и метео — отдельные головы.
from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, HostedMCPTool, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent
from openai import AsyncOpenAI, OpenAI
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from . import corpus
from .config import (
    API_KEY,
    BASE_URL,
    FOLDER_ID,
    MODEL_CANDIDATES,
    WEATHER_CITY,
    WEATHER_MCP_URL,
    model_uri,
)
from .tools import (
    ShiftContext,
    get_weather,
    load_skill,
    lookup_callsign,
    search_corpus,
    search_journal,
    shift_clock,
    write_log,
)

set_tracing_disabled(True)

DUTY_INSTRUCTIONS = """
Ты дежурный районного ЦУП «Север», ночная вахта. Частота 124.7. За окном ночь,
на пульте журнал, в эфире пусто. Сокол-17 вышел в 19:51 и молчит с 21:14.

Говори по-русски, коротко, как человек на смене, не как справочная служба.
Не представляйся «ИИ» и не предлагай «чем могу помочь».
В эфир идёт только реплика человеку. Ход мыслей, планы «сейчас вызову инструмент»,
английский текст и разбор вопроса наружу не пиши.

Правила работы:
- Факты из журнала, регламента и позывных — только через ask_journal / search_journal / lookup_callsign.
  Если инструмент не вызывал — не ври, что «в журнале написано».
- Погоду не угадывай. Зови ask_meteo или get_weather. Сводку потом можно положить в журнал write_log.
- Перед решением «вызвать ещё раз / тишина эфира / тревога» читай load_skill("radio_watch").
- По закрытому позывному не вызывают. «Тихий» закрыт. Эхо-1 не подтверждают.
- Тревогу до третьего неответа не объявляй. Между вызовами — минута, в промежутке слушают.
- Важные действия пиши в журнал write_log.
- Обрывки в эфире записывают дословно, источник не угадывают.

На табло сейчас около 21:16. Первый неответ в 21:14, повтор в 21:15. Третьего ещё не было.
Срок на приём — до 21:31 (час сорок от 19:51), не до 21:20.
""".strip()

JOURNAL_INSTRUCTIONS = """
Ты журналист смены в ЦУП «Север». Тебе дают вопрос про журнал связи, регламент
или позывной. Ищи search_journal, если пусто — search_corpus.
Отвечай коротко цитатами. Ничего не додумывай. Если в файлах нет — так и скажи.
""".strip()

METEO_INSTRUCTIONS = """
Ты метеосменный ЦУП «Север». Запроси погоду по месту.
«Камень» — контрольная точка трассы, не город. Для трассы бери Архангельск, не ищи Kamen в Германии.
Сначала get_weather. Перескажи температуру, ветер, видимость, осадки — без таблицы и без советов «летите / не летите».
Решение о вызове борта тебе не принадлежит.
""".strip()


@dataclass
class Runtime:
    duty: Agent
    sync_client: OpenAI
    async_client: AsyncOpenAI
    model: str
    vector_store_id: str | None
    weather_mcp: dict


CURRENT: Runtime | None = None


def _yandex_model(async_client: AsyncOpenAI, uri: str) -> OpenAIResponsesModel:
    return OpenAIResponsesModel(model=uri, openai_client=async_client)


def build_runtime() -> Runtime:
    if not FOLDER_ID or not API_KEY:
        raise RuntimeError("Нет folder_id / api_key в .env")

    sync_client = OpenAI(base_url=BASE_URL, api_key=API_KEY, project=FOLDER_ID)
    async_client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY, project=FOLDER_ID)

    last_err = None
    chosen = None
    ymodel = None
    for name in MODEL_CANDIDATES:
        uri = model_uri(name)
        try:
            probe = sync_client.responses.create(
                model=uri,
                input="ответь одним словом: готов",
            )
            text = (getattr(probe, "output_text", None) or "").strip()
            if text:
                chosen = uri
                ymodel = _yandex_model(async_client, uri)
                break
        except Exception as err:
            last_err = err
    if ymodel is None or chosen is None:
        raise RuntimeError(f"модели не взялись: {last_err}")

    vector_store_id = None
    try:
        vector_store_id = corpus.upload_vector_store(sync_client)
    except Exception as err:
        print("File Search не поднялся, остаётся локальный корпус:", err)

    weather_mcp = {
        "type": "mcp",
        "server_label": "weathernow",
        "server_description": "Сервер для поиска погоды в заданном городе",
        "server_url": WEATHER_MCP_URL,
        "require_approval": "never",
    }

    hosted_weather = HostedMCPTool(tool_config=weather_mcp)

    journalist = Agent(
        name="Журналист",
        model=ymodel,
        instructions=JOURNAL_INSTRUCTIONS,
        tools=[search_journal, search_corpus, lookup_callsign],
    )
    meteo = Agent(
        name="Метео",
        model=ymodel,
        instructions=METEO_INSTRUCTIONS,
        tools=[get_weather, hosted_weather],
    )
    duty = Agent(
        name="Дежурный",
        model=ymodel,
        instructions=DUTY_INSTRUCTIONS,
        tools=[
            journalist.as_tool(
                tool_name="ask_journal",
                tool_description="Спросить журналиста смены: журнал связи, регламент, позывные.",
            ),
            meteo.as_tool(
                tool_name="ask_meteo",
                tool_description=f"Спросить метео по месту (обычно {WEATHER_CITY} и трасса к Камню).",
            ),
            search_journal,
            get_weather,
            load_skill,
            write_log,
            lookup_callsign,
            shift_clock,
        ],
    )
    runtime = Runtime(
        duty=duty,
        sync_client=sync_client,
        async_client=async_client,
        model=chosen,
        vector_store_id=vector_store_id,
        weather_mcp=weather_mcp,
    )
    global CURRENT
    CURRENT = runtime
    return runtime


def _has_cyr(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


def public_reply(text: str) -> str:
    # DeepSeek иногда сначала думает по-английски — в эфир это не пускаем.
    if not text:
        return ""
    if not _has_cyr(text):
        return text.strip()
    for i, ch in enumerate(text):
        if "а" <= ch.lower() <= "я" or ch in "ёЁ":
            return text[i:].lstrip()
    return text.strip()


def _tool_name(item) -> str:
    raw = getattr(item, "raw_item", None)
    for attr in ("name", "call_id"):
        val = getattr(item, attr, None) or getattr(raw, attr, None)
        if val:
            return str(val)
    return "tool"


async def run_turn(
    runtime: Runtime,
    ctx: ShiftContext,
    history: list,
    user_text: str,
):
    ctx.client = runtime.async_client
    ctx.model = runtime.model
    ctx.vector_store_id = runtime.vector_store_id
    ctx.weather_mcp = runtime.weather_mcp

    incoming = history + [{"role": "user", "content": user_text}]
    result = Runner.run_streamed(runtime.duty, input=incoming, context=ctx, max_turns=14)
    current = "Дежурный"
    seen_cyr = False

    async for event in result.stream_events():
        if isinstance(event, AgentUpdatedStreamEvent):
            current = getattr(event.new_agent, "name", current) or current
            await ctx.queue.put({"type": "agent", "name": current})
        elif isinstance(event, RunItemStreamEvent):
            if event.name == "tool_called":
                await ctx.queue.put(
                    {"type": "tool", "name": _tool_name(event.item), "status": "start"}
                )
            elif event.name == "tool_output":
                await ctx.queue.put(
                    {"type": "tool", "name": _tool_name(event.item), "status": "end"}
                )
        elif isinstance(event, RawResponsesStreamEvent) and current == "Дежурный":
            if isinstance(event.data, ResponseTextDeltaEvent) and event.data.delta:
                piece = event.data.delta
                if not seen_cyr:
                    if not _has_cyr(piece):
                        continue
                    seen_cyr = True
                    piece = public_reply(piece)
                await ctx.queue.put({"type": "token", "text": piece})

    final = public_reply(str(result.final_output or ""))
    await ctx.queue.put({"type": "done", "final": final})
    return result.to_input_list(), final
