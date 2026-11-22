# Ключи и константы. .env лежит в корне MyAgent, в git его нет.
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

FOLDER_ID = (os.environ.get("folder_id") or os.environ.get("YANDEX_FOLDER_ID") or "").strip()
API_KEY = (os.environ.get("api_key") or os.environ.get("YANDEX_API_KEY") or "").strip()

BASE_URL = "https://ai.api.cloud.yandex.net/v1"

# Как в ЛР2: сначала flash, если молчит — Qwen.
MODEL_CANDIDATES = [
    "deepseek-v4.1-flash",
    "qwen3.6-35b-a3b/latest",
]

WEATHER_MCP_URL = (
    os.environ.get("WEATHER_MCP_URL")
    or "https://db8u5d643l0rm9a2aapa.6q7pzfrg.mcpgw.serverless.yandexcloud.net/sse"
).strip()

WEATHER_CITY = "Архангельск"
# точка «Камень» на трассе — рядом с Архангельском, для Open-Meteo
ARKHANGELSK = (64.54, 40.54)

SHIFT_START = "20:00"
SHIFT_END = "08:00"
FREQUENCY = "124.7"
CENTER_NAME = "Север-Вышка"
# часы на табло: Сокол-17 молчит с 21:14
BOARD_CLOCK = "21:16"


def model_uri(name: str) -> str:
    return f"gpt://{FOLDER_ID}/{name}"
