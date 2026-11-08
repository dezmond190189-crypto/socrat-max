"""Эталонные решатели-подпорки: программные расчёты для задач
Писал Андрей. Принцип: только полная уверенность в разборе условия. Не смог
разобрать таблицу, ленту или числа — возвращаем None, и бот работает как раньше,
на чистой модели. Решатель НИКОГДА не отвечает ученику сам — его результат идет
в промпты наставника и проверяющего как эталонная трасса. Если модель тупит,
повторяем запрос с температурой 0.1, а тут мы просто даём ей правильный ответ,
чтобы сверялась.
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("solver")

MAX_STEPS = 5000          # Андрей: защита от вечного цикла при симуляции
TRACE_MAX_LINES = 60


@dataclass
class SolverResult:
    kind: str        # Андрей: "turing" | "coding"
    answer: str      # Андрей: короткий итог (для лога и проверяющих)
    reference: str   # Андрей: компактный блок для промпта — итог + ключевые шаги
    trace: str       # Андрей: полная трасса/расчёт для проверяющего


 
# Андрей: машина Тьюринга — разбор программы и ленты + симуляция
 

_CMD_RE = re.compile(r"([λΛa-zа-яё0-9])\s*,\s*([LRNSlrns])\s*,\s*(q\s?\d+)", re.IGNORECASE)
_ROW_FORMAT_A_RE = re.compile(r"^\s*(?:строка\s*:?\s*)?(q\s?\d+)\s*:\s*([^=\n]+?)\s*=\s*(.+?)\s*$",
                              re.IGNORECASE)
_ROW_STATE_RE = re.compile(r"^\s*(q\s?\d+)\b", re.IGNORECASE)


def _norm_state(s: str) -> str:
    return "q" + re.sub(r"\D", "", s)


def _norm_symbol(s: str) -> str:
    s = (s or "").strip()
    if s in ("λ", "Λ", "л", "пусто"):
        return "λ"
    return s.lower()


def _parse_command(cell_text: str):
    """Андрей: текст ячейки → (записать, сдвиг, состояние); None — ячейка пустая."""
    text = (cell_text or "").strip().lower()
    if not text or text in ("пусто", "пустая", "-", "—", "нет команды"):
        return None
    m = _CMD_RE.search(text)
    if not m:
        return None
    return _norm_symbol(m.group(1)), m.group(2).upper(), _norm_state(m.group(3))


def _parse_program_format_a(text: str) -> dict | None:
    """Андрей: формат нашей расшифровки фото — строки «q1: 0 = 0, L, q1», пусто = «пусто»."""
    program: dict = {}
    for line in text.splitlines():
        m = _ROW_FORMAT_A_RE.match(line)
        if not m or not _ROW_STATE_RE.match(line):
            continue
        row = _norm_state(m.group(1))
        col = _norm_symbol(m.group(2))
        cmd = _parse_command(m.group(3))
        if cmd is None and (m.group(3) or "").strip().lower() not in (
                "пусто", "пустая", "-", "—", "нет команды", ""):
            continue  # Андрей: ячейка не распарсилась — не наша уверенность
        program.setdefault(row, {})[col] = cmd
    return program or None


def _parse_program_format_b(text: str) -> dict | None:
    """Таблица, вставленная текстом: строки «q1 1, L, q2 0, L, q1 …» с командами
    по столбцам слева направо, над ними — строка заголовка с символами.

    Андрей: фраза «Программа работы исполнителя:» часто склеена с абзацем
    условия, поэтому строки команд ищем по всему тексту, а заголовок —
    строкой перед первой из них.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    row_indexes: list[int] = []
    parsed_rows: dict[int, tuple[str, list]] = {}
    for i, ln in enumerate(lines):
        m = _ROW_STATE_RE.match(ln)
        if not m:
            continue
        cells = _CMD_RE.findall(ln)
        if not cells:
            continue
        row_indexes.append(i)
        parsed_rows[i] = (_norm_state(m.group(1)), cells)
    if len(row_indexes) < 2:
        return None

    # Андрей: заголовок — ближайшая строка ПЕРЕД первой строкой команд из коротких символов
    header: list[str] = []
    for j in range(row_indexes[0] - 1, max(row_indexes[0] - 4, -1), -1):
        ln = lines[j]
        if _CMD_RE.search(ln) or _ROW_STATE_RE.match(ln):
            continue  # Андрей: это тоже строка программы — заголовок дальше
        toks = [t for t in re.split(r"[\s|;:,]+", ln.rstrip(":")) if t]
        if len(toks) >= 2 \
                and all(re.fullmatch(r"[λΛa-zа-яё0-9]{1,2}", t, re.IGNORECASE) for t in toks) \
                and not any(t.lower().startswith("q") for t in toks):
            header = [_norm_symbol(t) for t in toks]
            break
    if not header:
        return None

    program: dict = {}
    for i in row_indexes:
        row, cells = parsed_rows[i]
        if len(cells) > len(header):
            return None  # Андрей: столбцов больше, чем в заголовке — разбор ненадежен
        program.setdefault(row, {})
        for col_sym, cmd in zip(header, cells):
            program[row][col_sym] = _norm_symbol(cmd[0]), cmd[1].upper(), _norm_state(cmd[2])
    return program or None


def parse_turing_program(text: str) -> dict | None:
    text = text or ""
    return _parse_program_format_a(text) or _parse_program_format_b(text)


def parse_tape(condition: str) -> str | None:
    """Андрей: начальная лента — двоичная запись числа или явная строка из 0/1."""
    condition = condition or ""
    m = re.search(r"двоичн\w+\s+(?:представление|запись|записи)\s+числа\s+(\d+)", condition, re.IGNORECASE)
    if m:
        return bin(int(m.group(1)))[2:]
    m = re.search(r"(?:записано|записана|на\s+ленте)[^.\n]{0,40}?\b([01]{3,})\b", condition, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


_HEAD_CONTEXT_RE = re.compile(r"головк|расположен|находится|начальный момент|старт", re.IGNORECASE)


def parse_head_start(condition: str, tape_len: int) -> int | None:
    """Начальная позиция головки — строго по формулировке условия; не распознали — None.

    ВАЖНО (Андрей): описание ленты тоже содержит «справа и слева от последовательности»
    («ячейки справа и слева заполнены λ»), поэтому совпадение принимаем только если
    рядом (±80 символов) говорится о головке/начальном положении.
    """
    condition = condition or ""
    patterns = (
        (r"ближайш\w+[^.\n]{0,20}справа|справа\s+от\s+(?:последовательности|записи|числа)", tape_len),
        (r"ближайш\w+[^.\n]{0,20}слева|слева\s+от\s+(?:последовательности|записи|числа)", -1),
        (r"на\s+(?:самой\s+)?(?:первой|начальн\w+)\s+(?:цифр|символ|ячейк|единиц)|в\s+начале\s+записи", 0),
        (r"на\s+(?:самой\s+)?(?:последн\w+|правой)\s+(?:цифр|символ|ячейк|единиц|единице)", tape_len - 1),
    )
    for pattern, pos in patterns:
        for m in re.finditer(pattern, condition, re.IGNORECASE):
            context = condition[max(0, m.start() - 80): m.end() + 40]
            if _HEAD_CONTEXT_RE.search(context):
                return pos
    return None


def simulate_turing(program: dict, tape: str, head_pos: int) -> tuple[str, list[str]] | None:
    """Симуляция по тактам. Возвращает (итоговая лента, строки трассы) или None,
    если понадобится команда из пустой ячейки программы выше MAX_STEPS без останова.
    Останов — символ S в команде или пустая ячейка таблицы. Зацикливание фиксируем.
    """
    cells: dict[int, str] = {i: ch for i, ch in enumerate(tape)}
    pos, state = head_pos, "q0"
    trace: list[str] = []
    seen: set = set()
    for step in range(1, MAX_STEPS + 1):
        sym = cells.get(pos, "λ")
        row = program.get(state, {})
        cmd = row.get(sym)
        key = (state, pos, tuple(sorted(cells.items())))
        if cmd is None:
            trace.append(f"Такт {step}: ({state}; {sym}) — ячейка таблицы ПУСТА → исполнитель остановился")
            break
        if key in seen:
            trace.append(f"Такт {step}: конфигурация ({state}; позиция {pos}) повторилась → зацикливание")
            break
        seen.add(key)
        write, move, new_state = cmd
        cells[pos] = write
        snippet = _tape_window(cells, pos)
        trace.append(f"Такт {step}: ({state}; {sym}) → {write}, {move}, {new_state} | лента около головки: {snippet}")
        if move == "S":
            trace.append(f"Такт {step}: команда с S → исполнитель завершил работу")
            break
        if move == "L":
            pos -= 1
        elif move == "R":
            pos += 1
        state = new_state
    else:
        return None
    lo = min(cells) if cells else 0
    hi = max(cells) if cells else -1
    final = "".join(cells.get(i, "λ") for i in range(lo, hi + 1))
    return final.strip("λ"), trace


def _tape_window(cells: dict, pos: int, radius: int = 9) -> str:
    return "".join(cells.get(i, "λ") for i in range(pos - radius, pos + radius + 1))


def try_solve_turing(condition: str) -> SolverResult | None:
    program = parse_turing_program(condition)
    if not program:
        return None
    tape = parse_tape(condition)
    if not tape:
        return None
    head = parse_head_start(condition, len(tape))
    if head is None:
        return None
    result = simulate_turing(program, tape, head)
    if result is None:
        return None
    final, trace = result
    answer = final
    if final and set(final) <= {"0", "1"}:
        answer = str(int(final, 2)) + " (" + final + "₂)"
    program_str = "; ".join(
        f"({st}; {sym})→{cmd[0]},{cmd[1]},{cmd[2]}"
        for st, cols in sorted(program.items()) for sym, cmd in sorted(cols.items())
        if cmd
    )
    reference = (
        f"ЭТАЛОН (посчитано программой-симулятором, не моделью):\n"
        f"Программа: {program_str}\n"
        f"Начало: лента {tape}, головка на позиции {head}, состояние q0.\n"
        f"Итог: лента «{final}»"
        + (f" = {answer}" if answer != final else "")
        + ("" if final else " (пустая — исполнитель ничего не записал)")
    )
    return SolverResult("mt", answer, reference,
                        reference + "\nТакты:\n" + "\n".join(trace[:TRACE_MAX_LINES]))


 
# Андрей: задачи на кодирование/объём — «не более V» → целая часть (floor), перебор b
 

_UNIT_BYTES = {"кбайт": 1024, "кб": 1024, "мбайт": 1024 ** 2, "мб": 1024 ** 2, "байт": 1, "б": 1}
# Вячеслав: регексп Андрея не брал «187 564 серийных номеров» — прилагательное
# между числом и словом (прогон 8 ноября 19:13: из-за этого эталон не посчитался
# вовсе, и диалог ушёл в неверное округление без сверки). Расширил до {0,2}? слов —
# точку/запятую класс [а-яё-] не пересекает, за границу предложения не уедет.
# Андрей глянь, вроде как старые форматы не сломал
_RECORDS_RE = re.compile(
    r"(\d[\d\s]{0,11})\s*(?:[а-яё-]+\s+){0,2}?(?:номер|запис|парол|логин|пользовател|фото|файл)",
    re.IGNORECASE,
)
_LENGTH_RE = re.compile(r"(?:состоит\s+)?из\s+(\d+)\s*(?:различных\s+)?символ", re.IGNORECASE)
_VOLUME_RE = re.compile(r"(\d[\d\s]{0,11})\s*(кбайт|кб|мбайт|мб|байт|б)\b", re.IGNORECASE)


def _num(raw: str) -> int:
    return int(raw.replace(" ", "").replace("\u202f", "").replace("\xa0", ""))


def try_solve_coding(condition: str) -> SolverResult | None:
    """Классическое задание: N объектов по L символов, лимит V «не более»,
    на объект — минимально возможное ЦЕЛОЕ число байт. Найти максимум b бит на символ
    (мощность алфавита 2^b) или сам b. Всё считаем программно: floor по «не более»,
    ceil по байтам на один объект. Короче, школьная математика, никакой магии.
    """
    condition = condition or ""
    if not re.search(r"символ", condition, re.IGNORECASE):
        return None
    m = _RECORDS_RE.search(condition)
    if not m:
        return None
    records = _num(m.group(1))
    m = _LENGTH_RE.search(condition)
    if not m:
        return None
    length = int(m.group(1))
    m = _VOLUME_RE.search(condition)
    if not m:
        return None
    volume = _num(m.group(1))
    unit = m.group(2).lower()
    v_bytes = volume * _UNIT_BYTES.get(unit, 1)
    if records <= 0 or length <= 0 or v_bytes <= 0:
        return None

    # Андрей: «не более V на все записи» и целые байты на одну запись → floor
    bytes_per_max = v_bytes // records
    bits_per_symbol = (8 * bytes_per_max) // length      # Андрей: наибольшее b: ceil(L·b/8) ≤ bytes_per_max
    if bits_per_symbol <= 0:
        return None

    asked_bits = bool(re.search(r"сколько\s+бит|минимальн\w+\s+бит", condition, re.IGNORECASE))
    answer_num = bits_per_symbol if asked_bits else 2 ** bits_per_symbol
    units_word = "бит на символ" if asked_bits else "(мощность алфавита)"
    reference = (
        "ЭТАЛОННЫЙ РАСЧЁТ (программа, не модель):\n"
        f"Дано: {records} объектов, длина {length} символов, лимит {volume} {unit} = {v_bytes} байт.\n"
        f"Шаг 1 («не более» → ЦЕЛАЯ ЧАСТЬ, floor — это корректный шаг, а не ошибка округления): "
        f"целых байт на один объект максимум floor({v_bytes} / {records}) = {bytes_per_max} байт.\n"
        f"Шаг 2 (округление ВВЕРХ — только байты на один объект): ceil({length}·b/8) ≤ {bytes_per_max} "
        f"⇔ {length}·b ≤ {8 * bytes_per_max} → максимум b = {bits_per_symbol} бит на символ.\n"
        f"Ответ: {answer_num} {units_word}."
    )
    return SolverResult("coding", f"{answer_num} {units_word}", reference, reference)


def try_solve(condition: str) -> SolverResult | None:
    """Андрей: единая точка входа — сначала исполнитель, потом кодирование. Не разобрали — None."""
    for attempt in (try_solve_turing, try_solve_coding):
        try:
            result = attempt(condition)
        except Exception as e:  # Андрей: решатель не должен ронять диалог
            log.warning("Решатель упал на условии: %s", e)
            result = None
        if result:
            log.info("Решатель: %s, ответ %s", result.kind, result.answer)
            return result
    return None
