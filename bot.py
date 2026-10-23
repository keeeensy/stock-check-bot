"""Телеграм-бот проверки наличия товара на сайтах из sites.json."""
import asyncio
import csv
import html
import io
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from checker import check, load_sites, save_sites

BASE = Path(__file__).parent
DB_FILE = BASE / "requests.db"


def load_env() -> None:
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}

dp = Dispatcher()


# ---------- база запросов ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, user_id INTEGER, username TEXT, query TEXT,
        in_stock INTEGER, details TEXT)""")
    return conn


def log_request(user_id: int, username: str, query: str, in_stock: bool, details: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO requests (ts, user_id, username, query, in_stock, details) VALUES (?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, username, query, int(in_stock), details),
        )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------- пользователь ----------

@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Здравствуйте! Напишите название товара, и я проверю его наличие.\n"
        "Например: <i>Термофор Ставр 9</i>"
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_query(message: Message, bot: Bot) -> None:
    query = message.text.strip()
    if len(query) < 3:
        await message.answer("Слишком короткий запрос, напишите название подробнее.")
        return

    wait = await message.answer("🔎 Проверяю наличие…")
    results = await check(query)
    found = [(r.site, i) for r in results for i in r.in_stock]
    in_stock = bool(found)

    await wait.edit_text("✅ В наличии" if in_stock else "❌ Нет в наличии")

    user = message.from_user
    username = f"@{user.username}" if user.username else user.full_name
    details = "; ".join(f"{site}: {i.title} {i.url}" for site, i in found)
    log_request(user.id, username, query, in_stock, details)

    if in_stock:
        lines = [f"✅ <b>Товар в наличии</b>\nЗапрос: «{html.escape(query)}»\n"
                 f"Пользователь: {html.escape(username)} (id {user.id})\n"]
        for site, item in found[:15]:
            lines.append(f"• <b>{html.escape(site)}</b>: <a href=\"{item.url}\">{html.escape(item.title)}</a>")
        if len(found) > 15:
            lines.append(f"…и ещё {len(found) - 15}")
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin, "\n".join(lines), disable_web_page_preview=True)
            except Exception as e:
                logging.warning("Не удалось отправить админу %s: %s", admin, e)


# ---------- админ ----------

@dp.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Команды администратора</b>\n"
        "/log — последние 20 запросов (/log 50 — последние 50)\n"
        "/export — все запросы файлом CSV (открывается в Excel)\n"
        "/sites — список сайтов, включить или отключить"
    )


@dp.message(Command("log"))
async def show_log(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    limit = int(command.args) if command.args and command.args.isdigit() else 20
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, username, query, in_stock FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        await message.answer("Запросов пока нет.")
        return
    text = "\n".join(f"{'✅' if s else '❌'} {ts} {html.escape(u)}: «{html.escape(q)}»" for ts, u, q, s in rows)
    await message.answer(text[:4000])


@dp.message(Command("export"))
async def export(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    with db() as conn:
        rows = conn.execute("SELECT ts, user_id, username, query, in_stock, details FROM requests ORDER BY id").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата", "ID", "Пользователь", "Запрос", "В наличии", "Где найдено"])
    for ts, uid, u, q, s, d in rows:
        writer.writerow([ts, uid, u, q, "да" if s else "нет", d])
    data = buf.getvalue().encode("utf-8-sig")
    await message.answer_document(BufferedInputFile(data, "requests.csv"), caption=f"Всего запросов: {len(rows)}")


def sites_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢' if s.get('enabled', True) else '⚪'} {s['name']}", callback_data=f"site:{n}")]
        for n, s in enumerate(load_sites())
    ])


@dp.message(Command("sites"))
async def sites(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Сайты для проверки (нажмите, чтобы включить или отключить).\n"
                         "Новый сайт добавляется блоком в sites.json.", reply_markup=sites_keyboard())


@dp.callback_query(F.data.startswith("site:"))
async def toggle_site(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    sites_list = load_sites()
    site = sites_list[int(call.data.split(":")[1])]
    site["enabled"] = not site.get("enabled", True)
    save_sites(sites_list)
    await call.message.edit_reply_markup(reply_markup=sites_keyboard())
    await call.answer(f"{site['name']}: {'включён' if site['enabled'] else 'отключён'}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
