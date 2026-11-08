"""Написал Артём. Магии тут ноль: читаем переменные, валидируем и отдаём cfg.
Если в .env чего-то не хватает, бот при старте сам скажет, ЧЕГО именно не хватает.
"""

import base64
import logging
import os
from pathlib import Path

log = logging.getLogger("config")

# Артём: .env лежит рядом с кодом, но ищем и в текущей папке — вдруг кто-то запускает из другого места
ENV_PLACES = [
    Path(__file__).resolve().parent / ".env",
    Path.cwd() / ".env",
]

# Артём: если в .env осталась подсказка вместо ключа (типа «вставьте_токен_сюда») — предупреждение, а не падение
PLACEHOLDER_HINTS = ("только это", "токен здесь", "<пароль>", "пароль", "заполн", "xxx", "...")

_TRUE = {"1", "true", "yes", "on", "да"}


def parse_env_file(path: Path) -> dict:
    """Артём: читаем .env руками, если вдруг python-dotenv не установлен."""
    result = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Артём: срезаем хвостовые комменты вида " GigaChat-3-Ultra #версия обязательна", иначе они уедут в ключ
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    except OSError as e:
        # Артём: файл не открылся — не страшно, переменные могут лежать прямо в окружении
        log.warning("Не удалось прочитать %s: %s", path, e)
    return result


def load_dotenv() -> None:
    # Артём: уже заданные переменные не трогаем — окружение главнее файла, так проще переопределить
    for place in ENV_PLACES:
        if place.is_file():
            try:
                from dotenv import load_dotenv
                load_dotenv(place, override=False)
                return
            except ImportError:
                for key, value in parse_env_file(place).items():
                    os.environ.setdefault(key, value)
                return


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    """Артём: все настройки бота в одном классе."""

    def __init__(self) -> None:
        load_dotenv()

        # Артём: --- блок GigaChat ---
        self.GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
        self.GIGACHAT_SECRET = os.getenv("GIGACHAT_SECRET", "").strip()
        self.GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
        self.GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra").strip()
        self.GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        self.GIGACHAT_VERIFY_SSL = _bool("GIGACHAT_VERIFY_SSL", False)
        # Артём: 60 секунд таймаута на вызов модели. ПВ сказал сделать надёжнее,
        # чтобы длинные ответы не обрывались на середине (и они реально обрывались)
        self.GIGACHAT_TIMEOUT = _int("GIGACHAT_TIMEOUT", 60)
        self.GIGACHAT_VISION_MODEL = os.getenv("GIGACHAT_VISION_MODEL", "").strip()
        # Артём: сверять расшифровку фото вторым проходом: always / true (только таблицы) / false
        self.GIGACHAT_VISION_VERIFY = os.getenv("GIGACHAT_VISION_VERIFY", "always").strip().lower()
        self.GIGACHAT_VISION_MAX_TOKENS = _int("GIGACHAT_VISION_MAX_TOKENS", 1500)
        self.LLM_CHECKER = _bool("LLM_CHECKER", False)  # Артём: доп. контролёр стиля, по умолчанию выкл

        # Артём: посторонние темы: off = строгий отказ, friendly = вежливое свободное общение
        self.CHAT_MODE = os.getenv("CHAT_MODE", "off").strip().lower()
        if self.CHAT_MODE not in ("off", "friendly"):
            self.CHAT_MODE = "off"

        # Артём: --- блок MAX ---
        self.MAX_API_KEY = os.getenv("MAX_API_KEY", "").strip()
        self.MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").strip().rstrip("/")
        self.MAX_VERIFY_SSL = _bool("MAX_VERIFY_SSL", True)
        self.WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
        self.MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()

        # Артём: --- блок базы данных ---
        self.DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
        if not self.DATABASE_URL:
            # Артём: если готовой строки нет — собираем её из частей
            user = os.getenv("POSTGRES_USER", "").strip()
            password = os.getenv("POSTGRES_PASSWORD", "").strip()
            db = os.getenv("POSTGRES_DB", "socrat").strip()
            if user and password:
                self.DATABASE_URL = f"postgresql+asyncpg://{user}:{password}@db:5432/{db}"
        self.POSTGRES_USER = os.getenv("POSTGRES_USER", "").strip()
        self.POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "").strip()
        self.POSTGRES_DB = os.getenv("POSTGRES_DB", "socrat").strip()

        # Артём: --- блок поведения ---
        self.VISION_ENABLED = _bool("VISION_ENABLED", True)
        self.ACTIVE_SUBJECT = os.getenv("ACTIVE_SUBJECT", "informatics").strip().lower() or "informatics"
        self.SESSION_IDLE_HOURS = _int("SESSION_IDLE_HOURS", 4)
        self.HISTORY_RETENTION_DAYS = _int("HISTORY_RETENTION_DAYS", 30)
        self.RATE_PER_MINUTE = _int("RATE_PER_MINUTE", 8)
        self.RATE_PER_DAY = _int("RATE_PER_DAY", 200)
        self.THINKING_MESSAGE_AFTER = _int("THINKING_MESSAGE_AFTER", 20)
        self.PROCESS_TIMEOUT = _int("PROCESS_TIMEOUT", 300)

        # Артём: эти числа ПВ велел зафиксировать прямо в коде, в .env их не выносить
        self.HISTORY_WINDOW = 10      # Артём: сколько последних сообщений идет в промпт
        self.SUMMARY_MAX_LINES = 15   # Артём: максимум строк в резюме
        self.SUMMARY_LINE_MAX = 55    # Артём: максимум символов в строке резюме
        self.CONDITION_MAX = 4000     # Артём: предел длины условия задачи
        self.MESSAGE_MAX = 4000       # Артём: предел длины сообщения MAX

    @property
    def gigachat_credentials(self) -> str | None:
        """Артём: ключ авторизации, либо base64 от client_id:client_secret. Пусто — None."""
        if self.GIGACHAT_AUTH_KEY:
            return self.GIGACHAT_AUTH_KEY
        if self.GIGACHAT_CLIENT_ID and self.GIGACHAT_SECRET:
            raw = f"{self.GIGACHAT_CLIENT_ID}:{self.GIGACHAT_SECRET}".encode("utf-8")
            return base64.b64encode(raw).decode("ascii")
        return None

    def ca_bundle_if_present(self) -> str | None:
        """Путь к системному набору сертов, если он есть.

        Артём: в докер-образе кладём туда корень Минцифры (см Dockerfile и certs/),
        потому что certifi про российские сертификаты не знает. Вне контейнера
        файла может не быть — вернём None, сработает дефолтный бандл.
                """
        path = Path("/etc/ssl/certs/ca-certificates.crt")
        return str(path) if path.is_file() else None


def validate(c: Config) -> tuple[list[str], list[str]]:
    """Проверка настроек. Возвращает (ошибки, предупреждения).

    Артём: ошибки не дают боту стартовать, предупреждения — просто ругань в лог.
        """
    errors: list[str] = []
    warnings: list[str] = []

    if not c.MAX_API_KEY:
        errors.append("MAX_API_KEY пуст — возьми токен бота в MAX и впиши в .env.")
    if not c.DATABASE_URL:
        errors.append("DATABASE_URL пуст — заполни её или POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB в .env.")
    if not c.gigachat_credentials:
        errors.append(
            "Нет ключей GigaChat: заполни GIGACHAT_AUTH_KEY "
            "или пару GIGACHAT_CLIENT_ID + GIGACHAT_SECRET в .env (https://developers.sber.ru/studio)."
        )
    if c.WEBHOOK_URL and not c.MAX_WEBHOOK_SECRET:
        errors.append("Задан WEBHOOK_URL, но нет MAX_WEBHOOK_SECRET — вебхук без секрета небезопасен.")

    # Артём: мягкие предупреждения — похоже, в .env остались подсказки вместо секретов
    for name, value in (("GIGACHAT_AUTH_KEY", c.GIGACHAT_AUTH_KEY), ("MAX_API_KEY", c.MAX_API_KEY)):
        if value and any(h in value.lower() for h in PLACEHOLDER_HINTS):
            warnings.append(f"{name} выглядит как заглушка («{value}») — впиши настоящий ключ, иначе бот будет молчать.")
    # Артём: токены только из латиницы — русские буквы в ключе ломают HTTP-заголовки.
    # сам на этом погорел: час ловил 500 и не понимал, откуда он берётся
    for name, value in (("MAX_API_KEY", c.MAX_API_KEY), ("GIGACHAT_AUTH_KEY", c.GIGACHAT_AUTH_KEY)):
        if value and not value.isascii():
            errors.append(
                f"{name} содержит не-латинские символы — это подсказка из шаблона, а не ключ. "
                "Впиши настоящий токен (только латиница/цифры)."
            )
    if c.GIGACHAT_VERIFY_SSL:
        warnings.append(
            "GIGACHAT_VERIFY_SSL=true: сертификаты Минцифры лежат в certs/ и устанавливаются в образ при сборке."
        )
    return errors, warnings


# Артём: синглтон — все остальные модули просто импортируют cfg
cfg = Config()
