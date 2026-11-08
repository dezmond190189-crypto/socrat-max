"""Точка входа: сеть, long polling / вебхук, лимиты, дедупликация
Писал Артём. Сюда же переехал max_api.py — обёртка над MAX Bot
API, чтобы файлов было меньше. Запуск: uvicorn main:app.
"""

import asyncio
import hmac
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import date

import httpx
from fastapi import FastAPI, HTTPException, Request

import db
import giga
import tutor
from config import cfg, validate
from giga import GigaClient

log = logging.getLogger("main")

# Артём: правило ПВ — логи построчно и обязательно с user_id
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

MAX_SEEN_MIDS = 10000  # Артём: размер кеша дедупликации по body.mid
APP_VERSION = "2.0"    # Артём: виден в /health и в логе запуска

UPDATES_HTTP_TIMEOUT = 50.0   # Артём: HTTP-таймаут запроса updates (long polling 30 c внутри)
DEFAULT_HTTP_TIMEOUT = 30.0


class MaxApi:
    """Обёртка над MAX Bot API.

    Артём: авторизация — заголовок Authorization <токен> БЕЗ слова Bearer.
    У MAX так в документации, я час тупил, почему 401, пока не дочитал доки.
    """

    def __init__(self) -> None:
        # Артём: при включённой проверке сверяемся с системным набором CA — в образе он
        # содержит корень Минцифры, которым подписан *.max.ru.
        verify = cfg.MAX_VERIFY_SSL and (cfg.ca_bundle_if_present() or True)
        self._client = httpx.AsyncClient(
            base_url=cfg.MAX_API_URL,
            headers={"Authorization": cfg.MAX_API_KEY},  # Артём: без Bearer, по документации MAX
            verify=verify,
            timeout=DEFAULT_HTTP_TIMEOUT,
            follow_redirects=True,  # Артём: вложения (payload.url) отвечают 30x с CDN — идём за редиректами
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict | None:
        """Артём: проверка связи при старте — GET /me."""
        try:
            r = await self._client.get("/me")
            r.raise_for_status()
            data = r.json()
            log.info("MAX: бот подключён: %s", data.get("name") or data.get("username") or "?")
            return data
        except Exception as e:
            log.error("GET /me не удался: %s", e)
            return None

    async def send_message(self, chat_id: int, text: str) -> None:
        """Артём: POST /messages?chat_id=<id>; текст обрезается до 4000 символов."""
        text = (text or "")[: cfg.MESSAGE_MAX]
        try:
            r = await self._client.post("/messages", params={"chat_id": chat_id}, json={"text": text})
            if r.status_code >= 400:
                log.error("Отправка в chat_id=%s не удалась: HTTP %d %s", chat_id, r.status_code, r.text[:300])
        except Exception as e:
            log.error("Отправка в chat_id=%s упала: %s", chat_id, e)

    async def download_image(self, url: str) -> bytes | None:
        """Артём: скачивание картинки-вложения по готовому url."""
        try:
            r = await self._client.get(url)
            if r.status_code >= 400:
                log.error("Скачивание картинки не удалось: HTTP %d", r.status_code)
                return None
            return r.content
        except Exception as e:
            log.error("Скачивание картинки упало: %s", e)
            return None

    async def download_image_token(self, token: str) -> bytes | None:
        """Штатный путь MAX для картинок: GET /images?token=<token>.

        Артём: большинство вложений приходит только с payload.token — без этого
        эндпоинта фото терялось ещё до GigaChat.
        """
        try:
            r = await self._client.get("/images", params={"token": token})
            if r.status_code >= 400:
                log.error("GET /images: HTTP %d %s", r.status_code, r.text[:200])
                return None
            return r.content
        except Exception as e:
            log.error("GET /images упал: %s", e)
            return None

    async def get_updates(self, marker: str | None, types: str = "message_created,bot_started") -> dict:
        """Артём: long polling — GET /updates?timeout=30&limit=100. При ошибке пустой результат."""
        params = {"timeout": 30, "limit": 100, "types": types}
        if marker:
            params["marker"] = marker
        try:
            r = await self._client.get("/updates", params=params, timeout=UPDATES_HTTP_TIMEOUT)
            if r.status_code >= 400:
                log.error("GET /updates: HTTP %d %s", r.status_code, r.text[:300])
                return {}
            return r.json()
        except Exception as e:
            log.error("GET /updates упал: %s", e)
            return {}

    # Артём: --- подписки на вебхук ---

    async def list_subscriptions(self) -> list[dict]:
        try:
            r = await self._client.get("/subscriptions")
            r.raise_for_status()
            return r.json().get("subscriptions", []) or []
        except Exception as e:
            log.error("GET /subscriptions не удался: %s", e)
            return []

    async def delete_subscription(self, url: str) -> None:
        try:
            r = await self._client.delete("/subscriptions", params={"url": url})
            if r.status_code >= 400:
                log.error("DELETE /subscriptions url=%s: HTTP %d", url, r.status_code)
        except Exception as e:
            log.error("DELETE /subscriptions url=%s упал: %s", url, e)

    async def add_subscription(self, url: str) -> bool:
        """Артём: POST /subscriptions с update_types и secret."""
        body = {
            "url": url,
            "update_types": ["message_created", "bot_started"],
        }
        if cfg.MAX_WEBHOOK_SECRET:
            body["secret"] = cfg.MAX_WEBHOOK_SECRET
        try:
            r = await self._client.post("/subscriptions", json=body)
            if r.status_code >= 400:
                log.error("POST /subscriptions: HTTP %d %s", r.status_code, r.text[:300])
                return False
            return True
        except Exception as e:
            log.error("POST /subscriptions упал: %s", e)
            return False

    async def reset_subscriptions(self) -> None:
        """Артём: перед запуском polling удалить старые подписки на вебхук."""
        for sub in await self.list_subscriptions():
            url = sub.get("url")
            if url:
                log.info("Удаляю старую подписку: %s", url)
                await self.delete_subscription(url)


class RateLimiter:
    """Артём: лимиты из .env — RATE_PER_MINUTE и RATE_PER_DAY на каждого ученика."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._timestamps: dict[int, deque] = {}
        self._day: dict[int, tuple[date, int]] = {}

    def allow(self, user_id: int) -> bool:
        """Артём: False = превышен минутный или дневной лимит."""
        import time as _time

        now = _time.monotonic()
        dq = self._timestamps.setdefault(user_id, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= self.per_minute:
            return False
        today = date.today()
        last_day, count = self._day.get(user_id, (today, 0))
        count = count if last_day == today else 0
        if count >= self.per_day:
            return False
        dq.append(now)
        self._day[user_id] = (today, count + 1)
        return True


class Bot:
    def __init__(self) -> None:
        self.max: MaxApi | None = None
        self.giga: GigaClient | None = None
        self.limiter = RateLimiter(cfg.RATE_PER_MINUTE, cfg.RATE_PER_DAY)
        self._locks: dict[int, asyncio.Lock] = {}
        self._seen_mids: set[str] = set()
        self._mid_order: deque = deque()
        self._polling_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None

    def lock_for(self, user_id: int) -> asyncio.Lock:
        # Артём: на каждого ученика свой замок — его сообщения обрабатываются строго по очереди.
        # Артём: Исправил. СКОЛЬКО РАЗ ГОВОРИТЬ НЕ ЛЕЗЬТЕ В МОЮ ЗОНУ ВЫ ЛОМАЕТЕ ЛОГИКУ!!!!
        # и пишите АРТЁМ через Ё пожалуйста, «артем» — это вообще другое имя
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _remember_mid(self, mid: str) -> bool:
        """Артём: дедупликация по body.mid. True — сообщение новое."""
        if mid in self._seen_mids:
            return False
        self._seen_mids.add(mid)
        self._mid_order.append(mid)
        while len(self._mid_order) > MAX_SEEN_MIDS:
            old = self._mid_order.popleft()
            self._seen_mids.discard(old)
        return True

    # ------------------------------------------------------------------
    # Артём: обработка одного события
    # ------------------------------------------------------------------

    async def process_update(self, update: dict) -> None:
        update_type = update.get("update_type")
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        body = message.get("body") or {}

        chat_id = recipient.get("chat_id")
        if chat_id is None:
            return
        if update_type == "bot_started":
            await self.max.send_message(chat_id, tutor.GREETING)
            return
        if update_type != "message_created":
            return
        if sender.get("is_bot"):
            return  # Артём: сообщения других ботов не обрабатываем
        if recipient.get("chat_type") == "chat":
            return  # Артём: группы игнорируем, работаем только в личных чатах

        mid = body.get("mid") or ""
        if mid and not self._remember_mid(mid):
            return  # Артём: повтор не обрабатываем

        user_id = sender.get("user_id")
        if user_id is None:
            return

        text = body.get("text") or ""
        attachments = body.get("attachments") or []
        photo_bytes = None
        photo_failed = False
        extra_photos = 0
        for att in attachments:
            payload = att.get("payload") or {}
            att_type = att.get("type") or ""
            mime = str(payload.get("mime_type") or att.get("mime_type") or "")
            if att_type not in ("image", "photo") and not mime.startswith("image/"):
                continue  # Артём: не картинка (в т.ч. фото, присланное как «файл»)
            url, token = payload.get("url"), payload.get("token")
            if not (url or token):
                photo_failed = True
                log.error("Вложение-картинка без url и token: тип=%s", att_type)
                continue
            if photo_bytes is not None:
                extra_photos += 1
                continue
            # Артём: MAX отдаёт вложение по-разному: где-то готовый url, чаще только token —
            # штатный путь GET /images?token=. Если по url скачался мусор — пробуем token
            data = await self.max.download_image(url) if url else None
            if not giga.looks_like_image(data or b"") and token:
                data = await self.max.download_image_token(token)
            if data and giga.looks_like_image(data):
                photo_bytes = data
                log.info("Фото скачано: %d байт (url=%s, token=%s)",
                         len(data), bool(url), bool(token))
            else:
                photo_failed = True
                log.error("Фото не скачалось или мусор: url=%s, token=%s, %d байт",
                          bool(url), bool(token), len(data or b""))
        note = f"\n(ещё {extra_photos} фото не смотрел)" if extra_photos else ""
        if extra_photos:
            text = (text + "\n" + note.strip()).strip()

        if photo_failed and photo_bytes is None:
            # Артём: фото было, но скачать не получилось — честно просим повторить
            await self.max.send_message(chat_id, giga.failure_hint("download"))
            return

        reply_text = await self.process_message(user_id, chat_id, text, photo_bytes)
        if reply_text:
            await self.max.send_message(chat_id, reply_text)

    async def process_message(self, user_id: int, chat_id: int,
                              text: str, photo_bytes: bytes | None) -> str | None:
        """Лимиты + личный замок + «⏳ Думаю…» + гарантированный ответ.

        Артём: ученик никогда не должен смотреть в молчащий чат. Модель думает
        дольше THINKING_MESSAGE_AFTER — шлём «⏳ Думаю…», зависла совсем —
        честное «не успел».
        """
        if not self.limiter.allow(user_id):
            if len(self.limiter._timestamps[user_id]) >= self.limiter.per_minute:
                return tutor.RATE_MINUTE_REPLY
            return tutor.RATE_DAY_REPLY

        async with self.lock_for(user_id):
            async def thinking() -> None:
                await asyncio.sleep(cfg.THINKING_MESSAGE_AFTER)
                await self.max.send_message(chat_id, "⏳ Думаю…")

            think_task = asyncio.create_task(thinking())
            try:
                reply = await asyncio.wait_for(
                    tutor.handle_message(self.giga, user_id, text, photo_bytes),
                    timeout=cfg.PROCESS_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.error("Обработка для user_id=%s превысила %d c", user_id, cfg.PROCESS_TIMEOUT)
                reply = tutor.TIMEOUT_REPLY
            except Exception as e:
                log.error("Ошибка обработки для user_id=%s: %s", user_id, e)
                reply = tutor.TECH_REPLY
            finally:
                think_task.cancel()
            return reply

    # ------------------------------------------------------------------
    # Артём: режимы получения сообщений
    # ------------------------------------------------------------------

    async def polling_loop(self) -> None:
        marker: str | None = None
        log.info("Запущен long polling")
        while True:
            data = await self.max.get_updates(marker)
            if not data:
                await asyncio.sleep(5)  # Артём: при ошибке лог и повтор через 5 секунд
                continue
            marker = data.get("marker") or marker
            for update in data.get("updates") or []:
                try:
                    await self.process_update(update)
                except Exception as e:
                    log.error("Ошибка обработки апдейта: %s", e)

    async def setup_connection(self) -> None:
        """Артём: long polling (если WEBHOOK_URL пуст) или вебхук."""
        if cfg.WEBHOOK_URL:
            ok = await self.max.add_subscription(cfg.WEBHOOK_URL)
            log.info("Вебхук %s оформлен", "успешно" if ok else "НЕ оформлен — проверь MAX_WEBHOOK_SECRET/URL")
        else:
            await self.max.reset_subscriptions()  # Артём: снести старые подписки на вебхук
            self._polling_task = asyncio.create_task(self.polling_loop())

    async def cleanup_loop(self) -> None:
        """Артём: фоновая зачистка — тишина SESSION_IDLE_HOURS и история HISTORY_RETENTION_DAYS."""
        while True:
            try:
                await db.cleanup_idle_sessions(cfg.SESSION_IDLE_HOURS)
                await db.cleanup_old_history(cfg.HISTORY_RETENTION_DAYS)
            except Exception as e:
                log.error("Фоновая зачистка упала: %s", e)
            await asyncio.sleep(600)


bot = Bot()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Артём: валидация настроек — по-человечески ругаемся, чего не хватает
    errors, warnings = validate(cfg)
    for w in warnings:
        log.warning("ВНИМАНИЕ: %s", w)
    if errors:
        for e in errors:
            log.error("НЕ ХВАТАЕТ НАСТРОЙКИ: %s", e)
        raise RuntimeError("Проверь .env — см. ошибки в логе выше")

    # Артём: база — подключаемся с ретраями, postgres в compose может подниматься параллельно
    db.connect_db(cfg.DATABASE_URL)
    for attempt in range(10):
        try:
            await db.create_tables()
            break
        except Exception as e:
            log.warning("БД ещё не готова (попытка %d): %s", attempt + 1, e)
            await asyncio.sleep(3)

    bot.giga = GigaClient()
    bot.max = MaxApi()  # Артём: создаём после валидации — токен должен быть корректным
    await bot.max.get_me()  # Артём: проверка связи с MAX
    await bot.setup_connection()
    bot._maintenance_task = asyncio.create_task(bot.cleanup_loop())
    log.info("Бот v%s запущен: %s", APP_VERSION, "webhook" if cfg.WEBHOOK_URL else "long polling")
    try:
        yield
    finally:
        for task in (bot._polling_task, bot._maintenance_task):
            if task:
                task.cancel()
        if bot.max:
            await bot.max.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    """Артём: 200/503 по доступности БД — healthcheck контейнера."""
    if await db.is_db_alive():
        return {"status": "ok", "version": APP_VERSION}
    raise HTTPException(status_code=503, detail={"status": "db unavailable"})


@app.post("/webhook")
async def webhook(request: Request):
    """Артём: входящий POST — проверяем секрет, отвечаем {"ok": true} сразу, обработка в фоне."""
    if cfg.WEBHOOK_URL:
        secret = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not cfg.MAX_WEBHOOK_SECRET or not hmac.compare_digest(secret, cfg.MAX_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="bad secret")
    payload = await request.json()
    asyncio.create_task(_safe_process(payload))
    return {"ok": True}


async def _safe_process(payload: dict) -> None:
    try:
        for update in payload.get("updates") or [payload]:
            await bot.process_update(update)
    except Exception as e:
        log.error("Фоновая обработка вебхука упала: %s", e)
