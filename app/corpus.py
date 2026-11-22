# Журнал, позывные, регламент. File Search в облаке + поиск по файлам,
# если индекс ещё не готов или шлюз молчит.
from __future__ import annotations

import re
import time
from pathlib import Path

from openai import OpenAI

from .config import ROOT

CORPUS_DIR = ROOT / "corpus"


def corpus_files() -> list[Path]:
    return sorted(CORPUS_DIR.glob("*.md"))


def read_all() -> str:
    parts = []
    for path in corpus_files():
        parts.append(f"# файл {path.name}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def parse_callsigns() -> list[dict]:
    text = (CORPUS_DIR / "pozvynye.md").read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        name, _, rest = body.partition(" — ")
        rows.append({"name": name.strip(), "about": rest.strip() or body})
    return rows


def lookup_callsign(query: str) -> str:
    q = query.strip().lower()
    hits = []
    for row in parse_callsigns():
        blob = f"{row['name']} {row['about']}".lower()
        if q in blob or row["name"].lower() in q:
            hits.append(f"{row['name']} — {row['about']}")
    if not hits:
        return (
            f"Позывной «{query}» в списке района «Север» не найден. "
            "Не подтверждаем. Записываем и сверяем с журналом."
        )
    return "\n".join(hits)


def local_search(query: str, limit: int = 5) -> str:
    # Простой поиск по кускам: заголовок ## и абзац под ним.
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    chunks = []
    for path in corpus_files():
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"(?=^## )", text, flags=re.M)
        if len(parts) == 1:
            parts = [text]
        for part in parts:
            score = 0
            low = part.lower()
            for w in words:
                score += low.count(w)
            if score:
                chunks.append((score, path.name, part.strip()))
    chunks.sort(key=lambda x: -x[0])
    if not chunks:
        return "В корпусе пусто по этому запросу. Не выдумывать."
    out = []
    for score, name, part in chunks[:limit]:
        snippet = part[:900]
        out.append(f"[{name}]\n{snippet}")
    return "\n\n---\n\n".join(out)


def upload_vector_store(client: OpenAI) -> str | None:
    # Как в лекции: files.create → vector_stores.create → чанки 800/100.
    files = corpus_files()
    if not files:
        return None
    uploaded = []
    for path in files:
        with path.open("rb") as fh:
            uploaded.append(client.files.create(file=fh, purpose="assistants"))
    store = client.vector_stores.create(name="duty_mcc_journal")
    for item in uploaded:
        client.vector_stores.files.create(
            vector_store_id=store.id,
            file_id=item.id,
            chunking_strategy={
                "type": "static",
                "static": {
                    "max_chunk_size_tokens": 800,
                    "chunk_overlap_tokens": 100,
                },
            },
        )
    # индекс не мгновенный
    time.sleep(3)
    return store.id
