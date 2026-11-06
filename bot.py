"""Telegram-бот для продления серий TikTok. Отвечает только владельцу."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import sender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "state.json"
COOKIES_FILE = DATA_DIR / "cookies.json"
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
JOB = "daily-send"

DEFAULTS = {"time": "13:00", "recipients": [], "message": "🔥", "enabled": False}

STATUS_ICON = {sender.SENT: "✅", sender.UNCONFIRMED: "⚠️", sender.FAILED: "❌", sender.SKIPPED: "⏭"}
STATUS_TEXT = {
    sender.SENT: "дошло",
    sender.UNCONFIRMED: "не подтверждено",
    sender.FAILED: "не отправлено",
    sender.SKIPPED: "не отправлено",
}
BLOCKER_TEXT = {
    "captcha": "TikTok показал капчу. Бот её не проходит, поэтому остановился.",
    "screen_time": "TikTok показал окно лимита экранного времени. Бот его не закрывает, поэтому остановился.",
    "logged_out": "TikTok не видит вход в аккаунт. Пришлите боту свежий cookies.json.",
}

HELP = """Команды:
/status — настройки и состояние
/send — отправить сейчас
/time 13:00 — время отправки
/add ник1 ник2 — добавить людей
/remove ник — убрать человека
/message 🔥 — текст (варианты через |)
/on и /off — включить и выключить расписание
/id — показать ваш Telegram ID

Чтобы обновить cookies, пришлите файл cookies.json."""


def load_state() -> dict:
    if STATE_FILE.exists():
        return {**DEFAULTS, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    return dict(DEFAULTS)


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def parse_time(text: str) -> dt.time | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return dt.time(int(m[1]), int(m[2]), tzinfo=TZ)


def clean_username(text: str) -> str | None:
    name = text.strip().lstrip("@").rstrip(",")
    return name if re.fullmatch(r"[A-Za-z0-9_.]{2,24}", name) else None


def format_report(report: sender.Report) -> str:
    lines = [f"{STATUS_ICON[r.status]} @{r.username}: {STATUS_TEXT[r.status]}" + (f" ({r.detail})" if r.detail else "")
             for r in report.results]
    done = sum(r.status == sender.SENT for r in report.results)
    head = f"Дошло {done} из {len(report.results)}."
    if not report.all_sent:
        head += " Отправьте остальным вручную, иначе серия может сгореть."
    if report.blocker:
        head = BLOCKER_TEXT[report.blocker] + "\n" + head
    return head + "\n\n" + "\n".join(lines)


def schedule(app: Application, state: dict) -> None:
    for job in app.job_queue.get_jobs_by_name(JOB):
        job.schedule_removal()
    if state["enabled"]:
        app.job_queue.run_daily(scheduled_send, parse_time(state["time"]), name=JOB)


async def do_send(app: Application) -> None:
    owner = app.bot_data["owner"]
    lock: asyncio.Lock = app.bot_data["lock"]
    if lock.locked():
        await app.bot.send_message(owner, "Отправка уже идёт.")
        return
    async with lock:
        state = load_state()
        if not state["recipients"]:
            await app.bot.send_message(owner, "Список пуст. Добавьте людей: /add ник")
            return
        if not COOKIES_FILE.exists():
            await app.bot.send_message(owner, "Нет cookies. Пришлите боту файл cookies.json.")
            return
        try:
            cookies = sender.parse_cookies(COOKIES_FILE.read_text(encoding="utf-8-sig"))
            variants = [m.strip() for m in state["message"].split("|") if m.strip()]
            report = await sender.run(cookies, state["recipients"], random.choice(variants))
        except Exception as e:  # noqa: BLE001
            logging.exception("send failed")
            await app.bot.send_message(owner, f"❌ Запуск упал, ничего не подтверждено: {type(e).__name__}")
            return
        await app.bot.send_message(owner, format_report(report))
        if report.screenshot:
            await app.bot.send_photo(owner, BytesIO(report.screenshot), caption="Экран в момент остановки")


async def scheduled_send(context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_send(context.application)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def cmd_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = load_state()
    people = ", ".join("@" + u for u in s["recipients"]) or "пусто"
    await update.message.reply_text(
        f"Расписание: {'включено' if s['enabled'] else 'выключено'}\n"
        f"Время: {s['time']}\nТекст: {s['message']}\nЛюди: {people}\n"
        f"Cookies: {'есть' if COOKIES_FILE.exists() else 'нет'}"
    )


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Запускаю. Это займёт несколько минут.")
    context.application.create_task(do_send(context.application))


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or parse_time(context.args[0]) is None:
        await update.message.reply_text("Формат: /time 13:00")
        return
    s = load_state()
    t = parse_time(context.args[0])
    s["time"] = f"{t.hour:02d}:{t.minute:02d}"
    save_state(s)
    schedule(context.application, s)
    await update.message.reply_text(f"Время: {s['time']}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    names = [clean_username(a) for a in context.args]
    if not names or None in names:
        await update.message.reply_text("Формат: /add ник1 ник2")
        return
    s = load_state()
    s["recipients"] += [n for n in dict.fromkeys(names) if n not in s["recipients"]]
    save_state(s)
    await update.message.reply_text("Люди: " + ", ".join("@" + u for u in s["recipients"]))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    names = {clean_username(a) for a in context.args}
    s = load_state()
    s["recipients"] = [u for u in s["recipients"] if u not in names]
    save_state(s)
    await update.message.reply_text("Люди: " + (", ".join("@" + u for u in s["recipients"]) or "пусто"))


async def cmd_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Формат: /message 🔥")
        return
    s = load_state()
    s["message"] = text
    save_state(s)
    await update.message.reply_text(f"Текст: {text}")


async def set_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    s = load_state()
    s["enabled"] = enabled
    save_state(s)
    schedule(context.application, s)
    await update.message.reply_text(f"Расписание включено, {s['time']}" if enabled else "Расписание выключено")


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_enabled(update, context, True)


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_enabled(update, context, False)


async def on_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    file = await update.message.document.get_file()
    raw = bytes(await file.download_as_bytearray()).decode("utf-8-sig", errors="replace")
    await update.message.delete()  # файл даёт доступ к аккаунту, в истории чата он не нужен
    try:
        sender.parse_cookies(raw)
    except Exception:  # noqa: BLE001
        await context.bot.send_message(update.effective_chat.id, "Файл не подошёл: нужен JSON из Cookie-Editor с sessionid.")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(raw, encoding="utf-8")
    COOKIES_FILE.chmod(0o600)
    await context.bot.send_message(update.effective_chat.id, "Cookies сохранены, файл из чата удалён.")


async def post_init(app: Application) -> None:
    schedule(app, load_state())
    await app.bot.set_my_commands([
        BotCommand("status", "Настройки"), BotCommand("send", "Отправить сейчас"),
        BotCommand("time", "Время отправки"), BotCommand("add", "Добавить людей"),
        BotCommand("remove", "Убрать человека"), BotCommand("message", "Текст"),
        BotCommand("on", "Включить расписание"), BotCommand("off", "Выключить расписание"),
    ])


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    owner_raw = os.environ.get("OWNER_ID", "").strip()
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["lock"] = asyncio.Lock()

    if not owner_raw:
        # Режим настройки: бот только сообщает ID и ничего больше не умеет.
        logging.warning("OWNER_ID не задан: бот работает только в режиме /id")
        app.add_handler(CommandHandler(["start", "id"], cmd_id))
        app.bot_data["owner"] = None
        app.post_init = None
        app.run_polling()
        return

    owner = int(owner_raw)
    app.bot_data["owner"] = owner
    only_owner = filters.User(user_id=owner)  # остальным бот просто не отвечает
    for name, handler in {
        "start": cmd_start, "help": cmd_start, "id": cmd_id, "status": cmd_status, "send": cmd_send,
        "time": cmd_time, "add": cmd_add, "remove": cmd_remove, "message": cmd_message,
        "on": cmd_on, "off": cmd_off,
    }.items():
        app.add_handler(CommandHandler(name, handler, filters=only_owner))
    app.add_handler(MessageHandler(only_owner & filters.Document.ALL, on_cookies))
    app.run_polling()


if __name__ == "__main__":
    main()
