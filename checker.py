"""Поиск товара на сайтах из sites.json и определение наличия.

Можно запускать без бота для проверки:  python checker.py "термофор ангара"
"""
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import aiohttp
from bs4 import BeautifulSoup

SITES_FILE = Path(__file__).with_name("sites.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
}
TIMEOUT = aiohttp.ClientTimeout(total=25)


@dataclass
class Item:
    title: str
    url: str
    in_stock: bool


@dataclass
class SiteResult:
    site: str
    items: list[Item] = field(default_factory=list)
    error: str | None = None

    @property
    def in_stock(self) -> list[Item]:
        return [i for i in self.items if i.in_stock]


def load_sites() -> list[dict]:
    return json.loads(SITES_FILE.read_text(encoding="utf-8"))


def save_sites(sites: list[dict]) -> None:
    SITES_FILE.write_text(json.dumps(sites, ensure_ascii=False, indent=2), encoding="utf-8")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def matches(query: str, title: str) -> bool:
    """Все слова запроса должны встретиться в названии.

    Слово считается найденным, если какое-то слово названия начинается с его
    основы (без последних 2 букв у длинных слов): «печи» найдёт «печь»,
    «ангару» найдёт «ангара». Числа сравниваются точно: «9» не найдёт «90».
    """
    title_words = _words(title)
    for q in _words(query):
        if q.isdigit():
            if q not in title_words:
                return False
            continue
        stem = q[:-2] if len(q) > 5 else q[:-1] if len(q) > 3 else q
        if not any(w.startswith(stem) for w in title_words):
            return False
    return True


def _stock(card_text: str, site: dict) -> bool:
    text = card_text.lower()
    if any(w.lower() in text for w in site["out_of_stock"]):
        return False
    return any(w.lower() in text for w in site["in_stock"])


async def _search(session: aiohttp.ClientSession, site: dict, search: str, query: str) -> list[Item]:
    url = site["search_url"].format(query=quote_plus(search))
    async with session.get(url) as resp:
        html = await resp.text(errors="ignore")

    items = []
    for card in BeautifulSoup(html, "lxml").select(site["card"]):
        link = next((a for a in card.select(site["title"]) if a.get_text(strip=True)), None)
        if link is None:
            continue
        title = link.get("title") or link.get_text(" ", strip=True)
        if matches(query, title):
            items.append(Item(
                title=title,
                url=urljoin(url, link.get("href", "")),
                in_stock=_stock(card.get_text(" ", strip=True), site),
            ))
    return items


async def check_site(session: aiohttp.ClientSession, site: dict, query: str) -> SiteResult:
    result = SiteResult(site["name"])
    try:
        result.items = await _search(session, site, query, query)
        # Поиск сайта часто не находит ничего по запросу целиком («ставр 9»),
        # тогда ищем по самому длинному слову и фильтруем сами.
        words = [w for w in _words(query) if not w.isdigit()]
        if not result.items and len(_words(query)) > 1 and words:
            result.items = await _search(session, site, max(words, key=len), query)
    except Exception as e:  # сеть, таймаут — сайт просто пропускаем
        result.error = f"{type(e).__name__}: {e}"
    return result


async def check(query: str) -> list[SiteResult]:
    sites = [s for s in load_sites() if s.get("enabled", True)]
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
        return await asyncio.gather(*(check_site(session, s, query) for s in sites))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    q = " ".join(sys.argv[1:]) or "термофор"
    for r in asyncio.run(check(q)):
        print(f"== {r.site}: найдено {len(r.items)}, в наличии {len(r.in_stock)}"
              + (f"  [ошибка: {r.error}]" if r.error else ""))
        for i in r.items[:5]:
            print(f"   {'+' if i.in_stock else '-'} {i.title}  {i.url}")
