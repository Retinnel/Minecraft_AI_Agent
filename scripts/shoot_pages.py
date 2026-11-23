"""Снимки страниц для записки: report/picture/ui_*.png с работающего сервера.

Страницы снимаются браузером без окна через протокол отладки Chrome. Сценарий вводит
реплики теми же функциями страницы, что вызывает кнопка «Отправить», ждёт ответа сервера и
снимает страницу целиком при масштабе 1,5. Модель отвечает по-настоящему, поэтому тексты
ответов от снимка к снимку различаются; снимок поиска тратит один кредит Tavily.

Сервер должен быть запущен. После съёмки сессия возвращается в исходное состояние:
творческий режим, Minecraft, пустой инвентарь, кампании нет.

    .venv/Scripts/python.exe scripts/shoot_pages.py                 # все снимки
    .venv/Scripts/python.exe scripts/shoot_pages.py search tree     # выборочно
    .venv/Scripts/python.exe scripts/shoot_pages.py --base http://127.0.0.1:8002
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "report" / "picture"

BROWSERS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
DEBUG_PORT = 9333
WIDTH = 1400          # ширина окна в точках CSS; при масштабе 1,5 снимок — 2100 пикселей
SCALE = 1.5


class Page:
    """Вкладка браузера, управляемая по протоколу отладки."""

    def __init__(self, ws, base: str) -> None:
        self.ws = ws
        self.base = base
        self._id = 0

    async def send(self, method: str, **params):
        self._id += 1
        call = self._id
        await self.ws.send(json.dumps({"id": call, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == call:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def js(self, expression: str):
        result = await self.send("Runtime.evaluate", expression=expression,
                                 awaitPromise=True, returnByValue=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(f"ошибка в странице: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    async def open(self, path: str) -> None:
        await self.send("Page.navigate", url=self.base + path)
        for _ in range(100):
            await asyncio.sleep(0.2)
            if await self.js("document.readyState") == "complete":
                break
        await asyncio.sleep(1.5)

    async def ask(self, text: str) -> None:
        """Реплика игрока — та же функция, что вызывает кнопка «Отправить»."""
        await self.js(f"ask({json.dumps(text, ensure_ascii=False)})")

    async def click(self, container: str, label: str) -> None:
        await self.js(
            f"[...document.querySelectorAll('#{container} button')]"
            f".find(b => b.textContent.trim() === {json.dumps(label, ensure_ascii=False)}).click()")

    async def shoot(self, name: str, selector: str | None = None) -> Path:
        """Снимок страницы целиком либо одного элемента."""
        metrics = await self.send("Page.getLayoutMetrics")
        height = int(metrics["cssContentSize"]["height"])
        await self.send("Emulation.setDeviceMetricsOverride", width=WIDTH, height=height,
                        deviceScaleFactor=SCALE, mobile=False)
        await asyncio.sleep(0.8)
        params = {"format": "png", "captureBeyondViewport": True}
        if selector:
            rect = await self.js(
                f"(() => {{ const r = document.querySelector({json.dumps(selector)})"
                f".getBoundingClientRect(); return {{x: r.x + scrollX, y: r.y + scrollY, "
                f"width: r.width, height: r.height}}; }})()")
            params["clip"] = {**rect, "scale": 1}
        data = (await self.send("Page.captureScreenshot", **params))["data"]
        target = OUT / f"{name}.png"
        target.write_bytes(base64.b64decode(data))
        await self.send("Emulation.setDeviceMetricsOverride", width=WIDTH, height=900,
                        deviceScaleFactor=SCALE, mobile=False)
        print(f"снят {target.relative_to(ROOT)}")
        return target


async def reset(page: Page) -> None:
    """Исходное состояние сессии и пустой журнал страницы чата."""
    await page.open("/chat")
    await page.js("""(async () => {
        const post = (url, body) => fetch(url, {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        await post("/api/rp/mode", {mode: "creative"});
        await post("/api/rp/game", {game: "Minecraft"});
        await post("/api/campaign/cancel", {});
        inventory = {};
        await sendInventory();
        for (let i = 0; i < 5; i++) await fetch("/api/rp/");   // накопленные объявления
    })()""")
    await page.ask("персонаж: кузнец")
    await page.open("/chat")        # журнал реплик живёт в странице: перезагрузка его очищает


async def chat(page: Page) -> None:
    await reset(page)
    for text in ["мне бы 10 наковален", "дай меч", "алмазный", "призови дракона края",
                 "как сделать алмазную кирку"]:
        await page.ask(text)
    await page.shoot("ui_chat")


async def events(page: Page) -> None:
    await reset(page)
    await page.ask("режим выживания")
    await page.ask("веди меня к каменной кирке")
    await asyncio.sleep(6)                     # объявление первого этапа приходит опросом
    await page.js('inventory["minecraft:crafting_table"] = 1; sendInventory()')
    await asyncio.sleep(12)                    # награда за этап и объявление второго
    await page.js('inventory["minecraft:oak_planks"] = 6; inventory["minecraft:iron_ingot"] = 1; '
                  'sendInventory()')
    await asyncio.sleep(4)
    await page.click("sim-hostile", "рядом: крипер")
    await asyncio.sleep(6)
    await page.click("sim-death", "смерть: взрыв")
    await asyncio.sleep(8)
    await page.shoot("ui_events")


async def tree(page: Page) -> None:
    await reset(page)
    await page.open("/tree")
    await page.js("""(async () => {
        inventory = {"minecraft:stone_pickaxe": 1, "minecraft:crafting_table": 1,
                     "minecraft:furnace": 1, "minecraft:iron_ingot": 8, "minecraft:stick": 4,
                     "minecraft:birch_planks": 6};
        await sendInventory();
    })()""")
    await asyncio.sleep(3)
    await page.js("select(Object.keys(groups).find(id => id.endsWith('diamond_pickaxe')))")
    await asyncio.sleep(3)
    await page.shoot("ui_tree")


async def search(page: Page) -> None:
    await reset(page)
    for text in ["я играю в Elden Ring", "как победить Маргита", "дай мне меч"]:
        await page.ask(text)
    await page.shoot("ui_search", selector="#log")


SCENARIOS = {"chat": chat, "events": events, "tree": tree, "search": search}


def launch_browser() -> tuple[subprocess.Popen, str]:
    browser = next((path for path in BROWSERS if path.exists()), None)
    if browser is None:
        raise SystemExit("Не найден ни Chrome, ни Edge")
    profile = tempfile.mkdtemp(prefix="shoot-pages-")
    process = subprocess.Popen(
        [str(browser), "--headless=new", f"--remote-debugging-port={DEBUG_PORT}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check", f"--window-size={WIDTH},900",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, profile


def page_socket() -> str:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/list") as answer:
                targets = json.load(answer)
            for target in targets:
                if target.get("type") == "page":
                    return target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit("Браузер не открыл порт отладки")


async def run(names: list[str], base: str) -> None:
    process, profile = launch_browser()
    try:
        async with websockets.connect(page_socket(), max_size=None) as ws:
            page = Page(ws, base.rstrip("/"))
            await page.send("Page.enable")
            await page.send("Emulation.setDeviceMetricsOverride", width=WIDTH, height=900,
                            deviceScaleFactor=SCALE, mobile=False)
            try:
                for name in names:
                    await SCENARIOS[name](page)
            finally:
                await reset(page)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help=f"какие снимки снять: {', '.join(SCENARIOS)}; "
                                                 f"по умолчанию все")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="адрес сервера")
    args = parser.parse_args()
    unknown = [name for name in args.names if name not in SCENARIOS]
    if unknown:
        parser.error(f"нет таких снимков: {', '.join(unknown)}")
    asyncio.run(run(args.names or list(SCENARIOS), args.base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
