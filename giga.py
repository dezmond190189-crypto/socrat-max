"""Писал Андрей. Два этажа:
1) GigaClient — тупо «спроси модель, получи ответ»;
2) расшифровка фото (бывший photo.py) — картинка с задачей → текст.

За токеном следит сам SDK gigachat, свой цикл обновления не писали (и не надо).
Импорт SDK ленивый, внутри класса — чтобы тесты на заглушках шли без него.
"""

import asyncio
import base64
import io
import logging
from dataclasses import dataclass

from config import cfg

log = logging.getLogger("giga")


def make_credentials() -> str | None:
    """Андрей: ключ авторизации или base64(client_id:client_secret) — что задано, то и берём."""
    if cfg.GIGACHAT_AUTH_KEY:
        return cfg.GIGACHAT_AUTH_KEY
    if cfg.GIGACHAT_CLIENT_ID and cfg.GIGACHAT_SECRET:
        raw = f"{cfg.GIGACHAT_CLIENT_ID}:{cfg.GIGACHAT_SECRET}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")
    return None


def file_id(file) -> str:
    """Андрей: id загруженного файла — у SDK 0.1.x поле id_string, у 0.2.x просто id_."""
    return getattr(file, "id_string", None) or getattr(file, "id_", None) \
        or str(getattr(file, "id", "")) or ""


def detect_image_format(data: bytes) -> tuple[str, str]:
    """По первым байтам определяем (mime_type, расширение). JPEG — запасной вариант.

    Андрей: API GigaChat не принимает файлы без настоящего MIME («File format
    application/octet-stream is not supported»), поэтому mime определяем сами,
    по магическим байтам, а не по расширению.
    """
    data = data or b""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "image/jpeg", "jpg"


class _GigaSdk:
    """Один интерфейс над двумя поколениями SDK gigachat.

    Андрей: у команды стоят разные версии либы, поэтому держим обе: старую 0.1.x
    (класс AsyncGigaChat, методы chat/upload_file) и новую 0.2.x+ (GigaChatAsyncClient,
    методы achat/aupload_file). Короче, один интерфейс — два бэкенда.
    """

    def __init__(self, credentials: str) -> None:
        try:
            from gigachat import AsyncGigaChat as client_cls  # Андрей: старый SDK
        except ImportError:
            from gigachat import GigaChatAsyncClient as client_cls  # Андрей: новый SDK
        params: dict = {
            "credentials": credentials,
            "scope": cfg.GIGACHAT_SCOPE,
            "verify_ssl_certs": cfg.GIGACHAT_VERIFY_SSL,
            "timeout": cfg.GIGACHAT_TIMEOUT,  # Андрей: модель отвечает медленно, это нормально, не уменьшать
        }
        if cfg.GIGACHAT_VERIFY_SSL and cfg.ca_bundle_if_present():
            # Андрей: certifi не знает российский корень, сверяемся с системным бандлом, иначе ssl истерит
            params["ca_bundle_file"] = cfg.ca_bundle_if_present()
        try:
            self._client = client_cls(**params)
        except TypeError:
            params.pop("ca_bundle_file", None)  # Андрей: совсем старый SDK без этого параметра
            self._client = client_cls(**params)
        # Андрей: исправил определение ветки SDK (Славик не обижайся, но так реально лучше, проверь).
        # По имени класса нельзя — оно менялось, смотрим по факту: chat = старый, achat = новый.
        # Вячеслав: спасибо люблю халяву)
        self._modern = not hasattr(self._client, "chat")
        log.info("GigaChat SDK: %s", "новый API (achat/aupload_file)" if self._modern else "старый API (chat/upload_file)")

    async def chat(self, payload: dict):
        """Диалоговый вызов: словарь payload → ответ модели.

        Андрей: wait_for поверх SDK. Некоторые ветки SDK просто игнорируют параметр
        timeout и висят вечно, а подвисший вызов блокирует всю очередь чата.
        ПВ сказал сделать надёжнее — этот внешний asyncio.wait_for наш последний
        рубеж обороны.
        """
        return await asyncio.wait_for(self._call(payload), timeout=cfg.GIGACHAT_TIMEOUT + 10)

    async def _call(self, payload: dict):
        if self._modern:
            return await self._client.achat(payload)
        return await self._client.chat(payload)

    async def upload_file(self, image_bytes: bytes):
        """Загрузка картинки: bytes → id файла для поля attachments.

        Андрей: GigaChat определяет формат по ИМЕНИ файла и Content-Type части
        multipart. Голые байты SDK упаковывает как filename="upload" +
        application/octet-stream, и API отвечает 400. Поэтому отдаём кортежем
        (имя, байты, MIME) — так ест обе ветки SDK; purpose=general SDK добавляет
        сам. Совсем старым SDK грузим файл-объектом с настоящим .name.
        """
        mime, ext = detect_image_format(image_bytes)
        payload = (f"photo.{ext}", image_bytes, mime)
        try:
            if self._modern:
                return await asyncio.wait_for(
                    self._client.aupload_file(payload),
                    timeout=cfg.GIGACHAT_TIMEOUT + 10)
            return await asyncio.wait_for(
                self._client.upload_file(payload),
                timeout=cfg.GIGACHAT_TIMEOUT + 10)
        except TypeError:
            bio = io.BytesIO(image_bytes)
            bio.name = f"photo.{ext}"  # Андрей: httpx возьмёт имя из .name и MIME по расширению
            if self._modern:
                return await asyncio.wait_for(
                    self._client.aupload_file(bio), timeout=cfg.GIGACHAT_TIMEOUT + 10)
            return await asyncio.wait_for(
                self._client.upload_file(bio), timeout=cfg.GIGACHAT_TIMEOUT + 10)


def _attachments_rejected(e: Exception) -> bool:
    """Андрей: похоже ли, что API отказался принимать поле attachments — 400/422
    с жалобой на поле/схему, но не на сеть или квоту.
    """
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    return status in (400, 422) and any(
        word in text
        for word in ("attachments", "unknown", "validation", "field", "unsupported")
    )


class GigaClient:
    """Андрей: клиент GigaChat со всеми настройками из .env."""

    def __init__(self) -> None:
        credentials = make_credentials()
        if not credentials:
            raise RuntimeError("Нет ключей GigaChat: заполни GIGACHAT_AUTH_KEY или CLIENT_ID/SECRET в .env")
        self._giga = _GigaSdk(credentials)

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """Страховка от 400 «system message must be the first message».

        Андрей: API принимает system-сообщение только первым (и по факту одно):
        если порядок нарушен или system несколько — переставляем и сливаем в один.
        Корректный список возвращаем как есть.
        """
        if not messages:
            return messages
        if messages[0].get("role") == "system" and \
                all(m.get("role") != "system" for m in messages[1:]):
            return messages
        systems = [m.get("content") or "" for m in messages if m.get("role") == "system"]
        rest = [dict(m) for m in messages if m.get("role") != "system"]
        log.warning("Payload без system первым (%d system-блоков) — исправляю", len(systems))
        return [{"role": "system", "content": "\n\n".join(systems)}] + rest

    async def chat(self, messages: list[dict], temperature: float = 0.5,
                   max_tokens: int = 400, model: str | None = None,
                   kind: str | None = None) -> str:
        """Обычный вызов: наставник отвечает коротко, 2-4 предложения.

        Андрей: kind — служебная метка вызова («verdict» = проверка ответа
        ученика). На запрос к API не влияет, нужна заглушке тестов и логам.
        """
        payload = {
            "messages": self._normalize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "model": model or cfg.GIGACHAT_MODEL,
        }
        response = await self._giga.chat(payload)
        return (response.choices[0].message.content or "").strip()

    async def vision(self, image_bytes: bytes, prompt: str,
                     max_tokens: int | None = None) -> str:
        """Вызов с картинкой: файл грузится в /files, его id идет в поле attachments.

        Андрей: это схема текущего API (developers.sber.ru, «Работа с файлами»).
        Если бэкенд поле attachments не знает (400/422) — повтор по старой схеме
        через тег <img src>.
        """
        if not image_bytes:
            raise ValueError("Пустые байты картинки")
        file = await self._giga.upload_file(image_bytes)
        image_id = file_id(file)
        if not image_id:
            # Андрей: пустой id → модель «не видит» картинку и начинает выдумывать.
            # Андрей: молчаливая галлюцинация хуже честной ошибки, поэтому кидаем исключение
            raise RuntimeError("GigaChat не вернул id загруженного файла — картинка не отправлена")
        payload = {
            "messages": [{"role": "user", "content": prompt, "attachments": [image_id]}],
            "max_tokens": max_tokens or cfg.GIGACHAT_VISION_MAX_TOKENS,
            "model": cfg.GIGACHAT_VISION_MODEL or cfg.GIGACHAT_MODEL,
        }
        try:
            response = await self._giga.chat(payload)
        except Exception as e:
            if not _attachments_rejected(e):
                raise
            log.warning("API не принял attachments (%s) — повторяю через тег <img src>", e)
            legacy = dict(payload)
            legacy["messages"] = [
                {"role": "user", "content": f"<img src='{image_id}'>{prompt}"}]
            response = await self._giga.chat(legacy)
        return (response.choices[0].message.content or "").strip()


 
# Андрей: расшифровка фото с условием задачи (бывший photo.py)
 

NO_TASK_TEXT = "NO_TASK_TEXT"  # маркер «это не учебная задача», который просит промпт

# Андрей: промпт-расшифровщик — переписать условие ДОСЛОВНО, включая вопрос задачи.
# Таблицу просим по каждой ячейке: пустые ячейки (как в таблице машины Тюринга)
# НЕ пропускаем, помечаем «пусто» — иначе условие искажается и потом бот
# неверно решает задачу.
# Алёна, глянь, нормально ли формулировка звучит для школьников, ты у нас по текстам отвечаешь.
TRANSCRIBE_PROMPT = (
    "Перепиши условие задачи с картинки ДОСЛОВНО, включая весь текст и вопрос задачи "
    "(что нужно найти или вычислить). Числа, индексы и обозначения переписывай символ в символ. "
    "Таблицу перепиши по каждой ячейке в формате «строка: столбец = значение»; "
    "пустые ячейки помечай словом «пусто» — НЕ пропускай их. "
    "Граф — списком всех рёбер. Неразборчивое помечай "
    "[неразборчиво], не угадывай. Если это не учебная задача — ответь ровно " + NO_TASK_TEXT + "."
)

# Андрей: второй проход-сверка — «проверь каждое число и каждое ребро»
VERIFY_PROMPT = (
    "На картинке учебная задача. Проверь каждое число, каждый индекс и каждое ребро в "
    "предварительной расшифровке по картинке; все ячейки таблицы должны быть на месте, "
    "пустые — помечены словом «пусто». Исправь ошибки и выведи финальную версию "
    "расшифровки целиком."
)

# Андрей: маркеры таблицы/графа в расшифровке — по ним решаем, нужна ли сверка
_TABLE_GRAPH_MARKERS = ("|", "рёбер", "ребро", "рёбра", "граф", "таблиц", "строка:", "строк:")


@dataclass
class PhotoResult:
    """Андрей: итог расшифровки фото."""
    status: str            # Андрей: ok | no_task | fail
    text: str = ""         # Андрей: расшифровка (для ok)
    reason: str = ""       # Андрей: download | format | size | recognize | api (для fail)


def has_table_or_graph(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _TABLE_GRAPH_MARKERS)


def _verify_mode() -> str:
    """Андрей: always = сверять каждую расшифровку, true = только таблицы/графы, false = никогда."""
    mode = cfg.GIGACHAT_VISION_VERIFY.lower()
    return mode if mode in ("true", "always", "false") else "true"


def looks_like_image(data: bytes) -> bool:
    """Проверяем настоящие магические байты известного формата.

        Андрей: MAX иногда отдаёт по вложению не картинку, а HTML-страницу ошибки
        или мусор. Такой файл GigaChat отвергнет с «File format … is not supported»,
        и ученик получит ложную отговорку про формат. Ловим это ДО вызова модели.
    """
    data = data or b""
    return (
        data.startswith(b"\xff\xd8\xff")                # Андрей: JPEG
        or data.startswith(b"\x89PNG\r\n\x1a\n")        # Андрей: PNG
        or data.startswith((b"GIF87a", b"GIF89a"))      # Андрей: GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # Андрей: WEBP
    )


MAX_PHOTO_BYTES = 15 * 1024 * 1024  # Андрей: 15 МБ, больше — просим сжать


def prepare_image(data: bytes) -> bytes:
    """GigaChat надёжно принимает JPEG и PNG: всё прочее (WEBP, GIF, BMP, TIFF —
        их отдают часть телефонов и мессенджеров) конвертируем в JPEG по magic-байтам.

        Андрей: тут был баг с фото, вроде починили конвертацией в JPEG (см переписку
        в чате: Слава скидывал скрин с телефона, API его не брал). Pillow не установлен
        или файл не открылся — отдаём как есть, пусть решает API.
    """
    data = data or b""
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data  # Андрей: JPEG/PNG — нужная пара, не трогаем
    try:
        import io
        from PIL import Image  # Андрей: pillow в requirements; нет либы — просто пропускаем конвертацию

        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=92)
            converted = out.getvalue()
            log.info("Картинка конвертирована в JPEG: %d -> %d байт", len(data), len(converted))
            return converted
    except Exception as e:
        log.warning("Конвертация картинки в JPEG не удалась, отправляю как есть: %s", e)
        return data


async def transcribe_photo(giga, image_bytes: bytes | None) -> PhotoResult:
    """Расшифровка фото + опциональная сверка вторым проходом.

        Андрей: ошибки наружу не кидаем — возвращаем статус, чтобы бот честно
        объяснил ученику, что случилось.
    """
    if not image_bytes:
        return PhotoResult("fail", reason="download")
    if not looks_like_image(image_bytes):
        log.error("Вложение не картинка (нет магических байтов JPEG/PNG/GIF/WEBP)")
        return PhotoResult("fail", reason="download")
    if len(image_bytes) > MAX_PHOTO_BYTES:
        log.error("Картинка слишком большая: %d байт", len(image_bytes))
        return PhotoResult("fail", reason="size")
    image_bytes = prepare_image(image_bytes)
    try:
        text = await giga.vision(image_bytes, TRANSCRIBE_PROMPT)
    except Exception as e:  # Андрей: недоступен GigaChat, таймаут, сеть, отказ формата — всё в статус
        log.error("Расшифровка фото не удалась: %s", e)
        msg = str(e).lower()
        if "not supported" in msg or "file format" in msg:
            # Андрей: API отверг сам файл — это не «сервис не отвечает», а формат картинки
            return PhotoResult("fail", reason="format")
        return PhotoResult("fail", reason="api")

    if NO_TASK_TEXT in (text or "").strip().upper():
        return PhotoResult("no_task")

    # Андрей: сверка — если в расшифровке таблица/граф, второй проход той же картинкой
    mode = _verify_mode()
    need_verify = mode == "always" or (mode == "true" and has_table_or_graph(text))
    if need_verify:
        try:
            verified = await giga.vision(
                image_bytes,
                f"{VERIFY_PROMPT}\n\nПредварительная расшифровка:\n{text}",
            )
            if verified and verified.strip():
                text = verified
        except Exception as e:
            log.error("Сверка фото не удалась, берём первую расшифровку: %s", e)

    return PhotoResult("ok", text=text.strip())


def failure_hint(reason: str) -> str:
    """Андрей: конкретная подсказка по причине неудачи — не молчим и не пугаем."""
    hints = {
        "download": "Не смог скачать картинку из чата. Попробуй отправить её ещё раз.",
        "format": "Это не похоже на картинку или формат не поддерживается. Пришли фото в PNG или JPG, или напиши условие словами.",
        "size": "Картинка слишком большая. Сожми её и пришли ещё раз.",
        "recognize": "Не смог разобрать фото. Перепиши условие словами — и продолжим разбор.",
        "api": "Не смог разобрать фото: сервис распознавания не отвечает. Попробуй ещё раз через минуту или пришли условие словами.",
    }
    return hints.get(reason, hints["recognize"])
