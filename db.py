"""База данных: сессии учеников и история переписки.
Писал Артём. Ровно две таблицы: sessions (условие задачи, статус, резюме и т.д.)
и messages (вся переписка). Все остальные модули лезут в базу только через эти
функции — прямого SQL больше нигде нет, так меньше шансов что-то сломать.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

log = logging.getLogger("db")

db_engine: AsyncEngine | None = None
db_factory: async_sessionmaker | None = None


class Base(DeclarativeBase):
    pass


class Session(Base):
    """Артём: сессия ученика — условие задачи, точка возврата, резюме."""

    __tablename__ = "sessions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)   # Артём: якорь — полный текст условия
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # Артём: active | awaiting_close | checking_logic | done
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)  # Артём: последний вопрос наставника
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)     # Артём: резюме старых ходов диалога
    grade: Mapped[str | None] = mapped_column(String(16), nullable=True)  # Артём: уровень 1-5 | 6-8 | 9 | 10-11
    verdict_claim: Mapped[str | None] = mapped_column(Text, nullable=True)  # Артём: ответ ученика, который он оспорил
    dispute_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Артём: сколько раз уже оспорил
    trace: Mapped[str | None] = mapped_column(Text, nullable=True)  # Артём: эталонная трасса решателя
    confirmed_answers: Mapped[str | None] = mapped_column(Text, nullable=True)  # Артём: числа уже закрытых шагов
    drift_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Артём: уходы от задачи подряд
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Message(Base):
    """Артём: история переписки, она же окно для промпта."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # Артём: user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def connect_db(database_url: str) -> None:
    """Артём: коннект к базе. Тесты передают sqlite+aiosqlite:///:memory:, в проде postgres."""
    global db_engine, db_factory
    db_engine = create_async_engine(database_url, pool_pre_ping=True)
    db_factory = async_sessionmaker(db_engine, expire_on_commit=False)


async def create_tables() -> None:
    assert db_engine is not None, "сначала вызови connect_db()"
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Артём: если база уже была создана старой версией — досоздаём новые колонки.
        # андрей, глянь потом, нормально ли миграции летят при апдейте, я в это не углублялся
        if conn.dialect.name == "postgresql":
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS verdict_claim TEXT"))
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS dispute_count INTEGER DEFAULT 0"))
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS trace TEXT"))
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS confirmed_answers TEXT"))
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS drift_count INTEGER DEFAULT 0"))


async def is_db_alive() -> bool:
    """Артём: для /health — жива ли база."""
    if db_engine is None:
        return False
    try:
        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        # Артём: любая ошибка соединения = база недоступна, healthcheck честно скажет, что мы лежим
        log.warning("Проверка БД не прошла: %s", e)
        return False


def _now() -> datetime:
    return datetime.now()


async def get_session(user_id: int) -> Session | None:
    assert db_factory is not None
    async with db_factory() as s:
        return await s.get(Session, user_id)


async def _save_session(user_id: int, **fields) -> None:
    """Артём: апдейт сессии, а если её нет — создаём. Вся запись в sessions живёт тут."""
    assert db_factory is not None
    async with db_factory() as s:
        row = await s.get(Session, user_id)
        if row is None:
            row = Session(user_id=user_id)
            s.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _now()
        await s.commit()


async def set_anchor(user_id: int, condition: str, grade: str | None = None) -> None:
    """Записать новую задачу. Старый якорь стирается сразу (правило 5.1).

    Артём: резюме тоже обнуляем — оно про прежнюю задачу, смешивать нельзя,
    и спор со старым ответом не переносится. Всю старую переписку стираем вместе
    с ними: если старые чаты долго лежат, они приезжают в окно истории, и наставник
    посреди новой задачи вдруг «вспоминает» давнишний разбор (прогон 8 ноября 18:41,
    вспомнил Фано посреди физики). Новая задача = чистый лист, точка.
        """
    fields: dict = {"condition": condition, "status": "active", "last_question": None,
                    "summary": None, "verdict_claim": None, "dispute_count": 0,
                    "trace": None, "confirmed_answers": None, "drift_count": 0}
    if grade:
        fields["grade"] = grade
    await _save_session(user_id, **fields)
    await clear_history(user_id)


async def clear_history(user_id: int) -> None:
    """Артём: стереть всю переписку ученика, не трогая саму сессию (уровень остаётся)."""
    assert db_factory is not None
    async with db_factory() as s:
        await s.execute(delete(Message).where(Message.user_id == user_id))
        await s.commit()


async def drop_task(user_id: int) -> None:
    """Бросить текущую задачу и убрать весь старый диалог (уровень ученика остаётся).

    Артём: для «бросаем и переключаемся на физику». После переключения старый
    разбор не должен ни всплывать в истории, ни висеть якорем в промпте — стираем
    переписку и все поля задачи. Сессию оставляем, чтобы не терять уровень.
        """
    await _save_session(user_id, condition=None, status=None, last_question=None,
                        summary=None, verdict_claim=None, dispute_count=0,
                        trace=None, confirmed_answers=None, drift_count=0)
    await clear_history(user_id)


async def set_last_question(user_id: int, question: str) -> None:
    # Алёна: точка возврата — сюда пишем чистый текст ответа, без служебных префиксов (пишу с телефона, извините за опечатки)
    await _save_session(user_id, last_question=question)


async def set_status(user_id: int, status: str) -> None:
    await _save_session(user_id, status=status)


async def set_verdict_claim(user_id: int, claim: str | None) -> None:
    """Запомнить ответ ученика, который он вправе оспорить; None — сбросить спор.

    Артём: храним с момента, когда проверка объявила ответ ошибочным, до признания,
    закрытия или новой задачи (ПВ запретил спорить с верным учеником).
        """
    await _save_session(user_id, verdict_claim=(claim[:400] if claim else None), dispute_count=0)


async def add_dispute(user_id: int) -> int:
    """Артём: ученик оспорил ответ ещё раз → инкрементим счётчик и возвращаем его."""
    assert db_factory is not None
    async with db_factory() as s:
        row = await s.get(Session, user_id)
        count = ((row.dispute_count or 0) + 1) if row else 1
        if row is None:
            row = Session(user_id=user_id, dispute_count=count)
            s.add(row)
        else:
            row.dispute_count = count
        row.updated_at = _now()
        await s.commit()
        return count


async def set_trace(user_id: int, trace: str | None) -> None:
    """Артём: эталонная трасса решателя для активной задачи; None — убрать."""
    await _save_session(user_id, trace=(trace[:4000] if trace else None))


async def add_confirmed_numbers(user_id: int, numbers: set[str]) -> None:
    """Артём: числа уже подтверждённых шагов — повтор ученика не проверяем заново."""
    row = await get_session(user_id)
    stored = set((row.confirmed_answers or "").split(",")) if row else set()
    stored |= {n for n in numbers if n}
    stored.discard("")
    if len(stored) > 40:  # Артём: растёт только по числам закрытых шагов — старые обрезаем
        stored = set(sorted(stored)[-40:])
    await _save_session(user_id, confirmed_answers=",".join(sorted(stored)))


async def get_confirmed_numbers(user_id: int) -> set[str]:
    row = await get_session(user_id)
    if not row or not row.confirmed_answers:
        return set()
    return {n for n in row.confirmed_answers.split(",") if n}


async def add_drift(user_id: int) -> int:
    """Артём: ученик ушёл от задачи в посторонние темы → счётчик подряд."""
    assert db_factory is not None
    async with db_factory() as s:
        row = await s.get(Session, user_id)
        count = ((row.drift_count or 0) + 1) if row else 1
        if row is None:
            row = Session(user_id=user_id, drift_count=count)
            s.add(row)
        else:
            row.drift_count = count
        row.updated_at = _now()
        await s.commit()
        return count


async def reset_drift(user_id: int) -> None:
    """Артём: вернулся к задаче или закрыл её — счётчик уходов сбрасываем."""
    await _save_session(user_id, drift_count=0)


async def set_grade(user_id: int, grade: str) -> None:
    if grade:
        await _save_session(user_id, grade=grade)


async def add_message(user_id: int, role: str, content: str) -> None:
    assert db_factory is not None
    async with db_factory() as s:
        s.add(Message(user_id=user_id, role=role, content=content, created_at=_now()))
        await s.commit()


async def get_history(user_id: int, limit: int = 10) -> list[Message]:
    """Артём: последние limit сообщений, от старых к новым."""
    assert db_factory is not None
    async with db_factory() as s:
        rows = (await s.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )).scalars().all()
    return list(reversed(rows))


async def update_summary(user_id: int, window: int = 10, max_lines: int = 15, line_max: int = 55) -> None:
    """Сжатие старых ходов в резюме — без нейросети, простым кодом.

    Артём: всё, что старше последних window сообщений, уходит строкой
    «Ученик: …; Наставник: …» в sessions.summary. Пересчитываем каждый раз
    с нуля — так надёжнее, идемпотентно короче.
        """
    assert db_factory is not None
    async with db_factory() as s:
        rows = (await s.execute(
            select(Message).where(Message.user_id == user_id).order_by(Message.id)
        )).scalars().all()
        if len(rows) <= window:
            return  # Артём: ничего не вытеснено
        evicted = rows[:-window]
        lines: list[str] = []
        i = 0
        while i < len(evicted):
            if evicted[i].role == "user":
                user_text = evicted[i].content
                tutor_text = evicted[i + 1].content if i + 1 < len(evicted) and evicted[i + 1].role == "assistant" else ""
                if tutor_text:
                    line = f"Ученик: {user_text}; Наставник: {tutor_text}"
                    i += 2
                else:
                    line = f"Ученик: {user_text}"
                    i += 1
                line = line.replace("\n", " ").strip()
                if len(line) > line_max:
                    line = line[: line_max - 1].rstrip() + "…"
                if line not in lines:  # Артём: дубли не пишем
                    lines.append(line)
            else:
                i += 1  # Артём: ответ наставника без пары (так не бывает, но пусть не падает)
        if lines:
            lines = lines[-max_lines:]
        await s.execute(update(Session).where(Session.user_id == user_id).values(summary="\n".join(lines), updated_at=_now()))
        await s.commit()


async def clear_all(user_id: int) -> None:
    """Артём: полная очистка ученика — сессия + вся история."""
    assert db_factory is not None
    async with db_factory() as s:
        await s.execute(delete(Message).where(Message.user_id == user_id))
        await s.execute(delete(Session).where(Session.user_id == user_id))
        await s.commit()


async def cleanup_idle_sessions(idle_hours: int) -> int:
    """Артём: фоновая зачистка сессий, где тишина больше idle_hours."""
    if db_factory is None or idle_hours <= 0:
        return 0
    border = _now() - timedelta(hours=idle_hours)
    async with db_factory() as s:
        stale = (await s.execute(
            select(Session.user_id).where(Session.updated_at.is_not(None), Session.updated_at < border)
        )).scalars().all()
        if not stale:
            return 0
        for uid in stale:
            await s.execute(delete(Message).where(Message.user_id == uid))
        await s.execute(delete(Session).where(Session.user_id.in_(stale)))
        await s.commit()
    log.info("Зачистка по тишине: удалили %d сессий (тишина > %d ч)", len(stale), idle_hours)
    return len(stale)


async def cleanup_old_history(retention_days: int) -> int:
    """Артём: переписка хранится retention_days дней (0 = не удалять вообще)."""
    if db_factory is None or retention_days <= 0:
        return 0
    border = _now() - timedelta(days=retention_days)
    async with db_factory() as s:
        result = await s.execute(delete(Message).where(Message.created_at.is_not(None), Message.created_at < border))
        await s.commit()
        return result.rowcount or 0
