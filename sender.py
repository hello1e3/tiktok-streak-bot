"""Отправка сообщений в TikTok через браузер.

Правила этого модуля:
- капчу, окно лимита времени и другие проверки TikTok мы НЕ закрываем и не обходим:
  запуск останавливается, а бот сообщает об этом владельцу;
- браузер не маскируется;
- "отправлено" считается только тогда, когда сообщение появилось в чате нужного человека.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

SENT = "sent"  # сообщение появилось в чате
UNCONFIRMED = "unconfirmed"  # нажали отправку, но в чате сообщения не видно
FAILED = "failed"  # не дошли до отправки
SKIPPED = "skipped"  # не пробовали: запуск остановлен раньше

MESSAGE_BUTTON = ['[data-e2e="message-button"]', 'button:has-text("Message")', 'button:has-text("Сообщение")']
INPUT_BOX = ['[data-e2e="message-input-area"] [contenteditable="true"]', 'div[contenteditable="true"]']

BLOCKERS = {
    "captcha": ['[id*="captcha-verify"]', "text=/Drag the slider|Перетащите ползунок/i"],
    "screen_time": ["text=/Ready to close TikTok|Готовы закрыть TikTok/i"],
    "logged_out": ['[data-e2e="top-login-button"]', "text=/Log in to TikTok|Войти в TikTok/i"],
}

SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": "Lax"}

# Считает текст в переписке, не учитывая поле ввода.
COUNT_JS = """(m) => {
  let n = 0;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const p = w.currentNode.parentElement;
    if (w.currentNode.nodeValue.trim() === m && !(p && p.closest('[contenteditable="true"]'))) n++;
  }
  return n;
}"""


class Blocked(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class StepFailed(Exception):
    pass


@dataclass
class Result:
    username: str
    status: str
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    blocker: str | None = None
    screenshot: bytes | None = None

    @property
    def all_sent(self) -> bool:
        return bool(self.results) and all(r.status == SENT for r in self.results)


def parse_cookies(raw: str) -> list[dict]:
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("cookies", [])
    cookies = []
    for c in data:
        ck = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or ".tiktok.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            ck["expires"] = float(exp)
        if ck["sameSite"] == "None":
            ck["secure"] = True
        cookies.append(ck)
    if not any(c["name"] == "sessionid" for c in cookies):
        raise ValueError("в cookies нет sessionid")
    return cookies


async def find_blocker(page) -> str | None:
    for kind, selectors in BLOCKERS.items():
        for sel in selectors:
            try:
                if await page.locator(sel).first.is_visible():
                    return kind
            except Exception:
                pass
    return None


async def wait_visible(page, selectors: list[str], timeout: float):
    """Ждёт элемент. Если появилась проверка TikTok, сразу прерывает запуск."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        kind = await find_blocker(page)
        if kind:
            raise Blocked(kind)
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None


async def send_one(page, username: str, message: str) -> Result:
    await page.goto(f"https://www.tiktok.com/@{username}", wait_until="domcontentloaded")
    btn = await wait_visible(page, MESSAGE_BUTTON, 20)
    if btn is None:
        raise StepFailed("нет кнопки «Сообщение»")
    await btn.click(timeout=8000)

    # Проверяем, что открыт чат именно этого человека.
    header = await wait_visible(page, [f'text="@{username}"'], 20)
    if header is None or "/messages" not in page.url:
        raise StepFailed("чат не открылся")

    box = await wait_visible(page, INPUT_BOX, 20)
    if box is None:
        raise StepFailed("нет поля ввода")

    before = await page.evaluate(COUNT_JS, message)
    await box.click(timeout=8000)
    await page.keyboard.insert_text(message)
    await page.wait_for_timeout(random.randint(400, 900))
    await page.keyboard.press("Enter")

    for _ in range(20):
        await page.wait_for_timeout(500)
        if await page.evaluate(COUNT_JS, message) > before:
            return Result(username, SENT)
    kind = await find_blocker(page)
    if kind:
        raise Blocked(kind)
    return Result(username, UNCONFIRMED, "в чате сообщения не видно")


async def send_with_retry(page, username: str, message: str) -> Result:
    """Сразу после открытия сайт может ещё не подхватить сессию, поэтому вход проверяем дважды."""
    try:
        return await send_one(page, username, message)
    except Blocked as e:
        if e.kind != "logged_out":
            raise
    await page.wait_for_timeout(5000)
    return await send_one(page, username, message)


async def run(cookies: list[dict], recipients: list[str], message: str) -> Report:
    report = Report()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-US", viewport={"width": 1366, "height": 850})
        await context.add_cookies(cookies)
        page = await context.new_page()
        try:
            for i, username in enumerate(recipients):
                try:
                    report.results.append(await send_with_retry(page, username, message))
                except Blocked as e:
                    report.blocker = e.kind
                    report.screenshot = await page.screenshot()
                    report.results.append(Result(username, FAILED, "остановлено проверкой TikTok"))
                    report.results += [Result(u, SKIPPED) for u in recipients[i + 1:]]
                    break
                except Exception as e:  # noqa: BLE001
                    detail = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
                    report.results.append(Result(username, FAILED, detail))
                    report.screenshot = await page.screenshot()
                await page.wait_for_timeout(random.randint(3000, 7000))
        finally:
            await browser.close()
    return report
