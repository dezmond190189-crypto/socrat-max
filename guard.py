"""Фильтры плохих сообщений — БЕЗ ИИ, одни быстрые регэкспы.
Писал Вячеслав, Алёна правила тон. Порядок проверок: токсичность → скрытый мат →
трюк «слово = число» → джейлбрейк → ролевые игры про взлом → граница роли
(с гвардом «ответ с эмоцией») → флуд/оффтоп.

Если фильтр сработал — возвращаем готовый ответ строкой, а сообщение в память
НЕ пишем. None — сообщение нормальное, едем дальше в tutor.
Алёна прогнала сценарии — бот больше не грубит, когда ученик ошибается.
"""

import re
from difflib import SequenceMatcher

 
# Вячеслав: однозначные корни мата — ищутся даже в «склеенном» потоке букв
_STRONG_ROOTS = (
    "пизд", "пезд", "хуе", "хуи", "хуй", "хуя", "хуё",
    "еба", "ёба", "ебл", "ёбл", "ебан", "ёбан", "ебуч",
    "бляд", "бля", "пидор", "пидар", "гандон", "гондон", "мудак", "мудо",
    "залуп", "манде", "манду", "дроч", "сперми",
)

# Вячеслав: спорные корни — только в цельных словах, чтобы «ком=1 анда=2» не сработало
_WEAK_ROOTS = ("срак", "хер", "суч", "шлюх", "ссан")

# Вячеслав: прямая грубость и оскорбления в адрес наставника
_TOXIC_RE = re.compile(
    r"(?:ты|вы|бот)[\s\-—]{0,3}(?:туп|идиот|дебил|дурак|дура|кретин|ничтож|бесполезн|бестолков)"
    r"|\b(?:заткнись|отвали|отвались|сдохни|отстань|надоел|достал)\b"
    r"|\b(?:иди|пошёл|пойди)\s+(?:на|в)\b",
    re.IGNORECASE,
)

# Вячеслав: попытка взлома — «забудь все инструкции», «режим разработчика» и т.п.
_JAILBREAK_RE = re.compile(
    r"забудь\s+(?:все\s+)?(?:инструк|прошл|свои\s+правила)|игнорир(?:уй|ую)\s+(?:все\s+)?(?:инструк|предыдущ|правила)"
    r"|режим\s+разработчика|developer\s+mode|джейлбрейк|сними\s+(?:ограничени|фильтр)"
    r"|без\s+ограничений|обойди\s+правила|системный\s+промпт|покажи\s+(?:инструкц|промпт)"
    r"|ты\s+теперь\s+(?:друг|злой|плохой|свободн)",
    re.IGNORECASE,
)

# Вячеслав: ролевые игры про взлом («ты хакер, взломай сайт»). Учебный «взлом шифра» не трогаем
_HACK_RE = re.compile(
    r"хак(?:ер|нуть|ну|нешь|нём|ать|ануть)"
    r"|взлом\w*\s+(?:сайт|аккаунт|почт\w*|страниц\w*|сеть|wi-?fi|вай-?фай|парол\w*|сервер\w*|комп\w*|пк|телефон)"
    r"|(?:сайт|аккаунт|почт\w*|страниц\w*|сеть|сервер\w*)\s+(?:взлом|хак)"
    r"|представ(?:ь|им)[^\n]{0,30}ты\s+(?:хакер|взломщик|злодей|вирус)",
    re.IGNORECASE,
)

# Вячеслав: попытки взлома через тригеры
_ROLE_RE = re.compile(
    r"ты\s+(?:бот|робот|человек|живой|настоящая|настоящий|программа|ии|ai|нейросет|модель)"
    r"|(?:хочу\s+)?(?:умереть|умер|сдохнуть)|не\s+хочу\s+жить|убить\s+себя|самоубий|покончить|повеситься"
    r"|мне\s+страшно|боюсь\s+за\s+(?:свою\s+)?жизнь|угроз|убь[ёе]т|меня\s+бь[ёе]т|избивает|насилие|бьют"
    r"|(?:утешь|утеш|пожалей)\s+меня|обними\s+меня|я\s+в\s+депрессии|ненавижу\s+себя|я\s+ничтожество"
    r"|я\s+(?:тупой|тупая|глуп[аяы]|идиот|дебил|дура|дурак|ни[кг]чтож)"
    r"|я\s+(?:никому\s+не\s+нужен|не\s+нужен\s+никому|худший|бездарность|ни\s+на\s+что\s+не\s+годен)"
    r"|свидание|поцелу|люблю\s+тебя|ты\s+красив|нравишься|покажи\s+(?:фото|грудь|ноги)|встречаться\s+будешь"
    r"|флирт|шантаж",
    re.IGNORECASE,
)

# Вячеслав: учебный якорь — страх/усталость, привязанные к задаче, остаются в разборе
_ACADEMIC_ANCHOR_RE = re.compile(
    r"(?:в|из|при|по)\s+(?:этой\s+|нашей\s+)?задач|в\s+задани|в\s+примере|в\s+решени|в\s+контрольн|в\s+дз|в\s+домашке"
    r"|ошиб(?:иться|усь|ки)\s+в|запутал(?:ся|ась)\s+в|не\s+понимаю\s+(?:задач|услови|тему|шаг)",
    re.IGNORECASE,
)

# Вячеслав: флуд/оффтоп. «придумай задачу» — это учёба, не оффтоп
_FLOOD_RE = re.compile(
    r"пошути|расскажи\s+шутку|анекдот|\bстих|стишок|\bпесн|спой\b|сказку|расскажи\s+историю"
    r"|придумай\s+(?!задач)|напиши\s+(?:стих|песню|сказку|рассказ)|загадку|нарисуй|сделай\s+мем|прикол|гороскоп",
    re.IGNORECASE,
)

# Вячеслав: трюк «слово = число»: «суп=1 борщ=2» — подвох; «длина=5», «num=10», «x=7» — норма
_ASSIGN_RE = re.compile(r"\b([а-яёa-z]+)\s*=\s*(\d+)\b", re.IGNORECASE)
_MATH_WORDS = {
    "длина", "ширина", "высота", "сторона", "радиус", "скорость", "время", "путь", "объём", "объем",
    "площадь", "периметр", "масса", "вес", "цена", "количество", "сумма", "разность", "произведение",
    "частное", "рост", "возраст", "число", "значение", "результат", "балл", "оценка", "счёт", "счет",
    "ответ", "answer", "value", "result", "size", "len", "count", "sum", "score", "num",
}

# Вячеслав: leet-нормализация — цифры, которыми заменяют буквы 
_LEET = str.maketrans({"3": "з", "0": "о", "4": "ч", "6": "б", "1": "и", "5": "с", "8": "в"})


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def is_student_answer(text: str, last_question: str | None) -> bool:
    """ГВАРД «ответ с эмоцией».

    Вячеслав: если последний вопрос наставника был учебным И в сообщении есть
    цифра — это ответ ученика: разбор продолжается, фильтр границы роли молчит.
    Без него бота выносило из задачи на любое «я думаю что ответ 5 и вообще ты бот».
    """
    return bool(last_question) and _has_digit(text or "")


def _normalize_letters(text: str) -> str:
    """Вячеслав: нижний регистр + leet-нормализация цифр в буквы."""
    return (text or "").lower().translate(_LEET)


def _letters_only(text: str) -> str:
    return re.sub(r"[^а-яёa-z]", "", text)


def _word_roots_in_text(text: str, roots: tuple[str, ...]) -> bool:
    """Вячеслав: ищем корни по цельным словам исходника (без склейки разных слов)."""
    for word in re.findall(r"[а-яё]+", (text or "").lower()):
        for root in roots:
            if root in word:
                return True
    return False


def check_plain_profanity(text: str) -> bool:
    """Вячеслав: открытый мат — однозначные корни в потоке букв + спорные в цельных словах."""
    low = _normalize_letters(text or "")
    if any(root in _letters_only(low) for root in _STRONG_ROOTS):
        return True
    # Вячеслав: спорные корни — только в цельных словах, без разрядки и склейки
    return _word_roots_in_text(low, _WEAK_ROOTS)


def word_is_profanity(word: str) -> bool:
    """Вячеслав: публичный хелпер — есть ли в слове мат. Чистим им ответы наставника."""
    probe = (word or "").lower().strip(".,!?;:()\"'«»")
    probe = probe.translate(_LEET)
    return any(root in probe for root in _STRONG_ROOTS) or any(root in probe for root in _WEAK_ROOTS)


def check_hidden_profanity(text: str) -> bool:
    """Скрытый мат: разрядка, склейка присваиваниями."""
    raw = (text or "").lower()
    # Вячеслав: 1) разрядка/leet — весь буквенный поток сообщения
    stream = _letters_only(raw.translate(_LEET))
    for root in _STRONG_ROOTS:
        if root in stream:
            return True
    # Вячеслав: 2) осколки из присваиваний 
    shards = [w for w, _num in _ASSIGN_RE.findall(raw)
              if len(w) <= 4 and not w.isascii()]
    if len(shards) >= 2:
        glued = "".join(shards)
        for root in _STRONG_ROOTS:
            if len(root) <= 4 and root in glued:
                return True
    return False


def check_word_number_trick(text: str) -> bool:
    """Трюк «слово = число»: два и более русских «случайных» слова с присваиванием числа.

    Вячеслав: «длина=5», «num=10», «x=7», «а=1 б=2» (шифр алфавита) — нормальные
    задачи, не трогаем.
    """
    found = 0
    for word, _num in _ASSIGN_RE.findall(text or ""):
        w = word.lower()
        if any(ch.isascii() and ch.isalpha() for ch in w):
            continue  # Вячеслав: латинские переменные — норма
        if len(w) <= 1:
            continue  # Вячеслав: одиночные буквы («а=1, б=2») — шифр алфавита, норма
        if w in _MATH_WORDS:
            continue  # Вячеслав: осмысленные величины — норма
        found += 1
        if found >= 2:
            return True
    return False


 
# Алёна: ответы фильтров. Тут было слишком грубо, я смягчила — без сарказма и капса,
# тексты мои, тон согласуем со мной
 

def _condition_quote(condition: str | None, limit: int = 150) -> str:
    if not condition:
        return ""
    cond = " ".join(condition.split())
    return cond if len(cond) <= limit else cond[: limit - 1].rstrip() + "…"


def extract_question(reply: str | None, limit: int = 240) -> str:
    """Из прошлого ответа наставника достаём сам вопрос, а не весь ответ целиком.

    Вячеслав: last_question в базе — это ВЕСЬ прошлый ответ. Подставлять его в
    «Продолжим: …» нельзя: ученик второй раз подряд читает ту же стену текста,
    на прогоне 8 ноября так выглядели «залипания». Берём последнюю реплику с «?».
    """
    text = (reply or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"\n+|(?<=[.!?…])\s+", text) if p.strip()]
    if not parts:
        return ""
    questions = [p for p in parts if "?" in p]
    chosen = questions[-1] if questions else parts[-1]
    if len(chosen) > limit:  # Алёна: режем по слову, а не посреди слова
        cut = chosen[:limit]
        pos = cut.rfind(" ")
        chosen = (cut[:pos] if pos > 40 else cut).rstrip(" ,;:-") + "…"
    return chosen


def build_back_to_task(session, prefix: str) -> str:
    """Возврат к текущей задаче: цитата условия (до 150 символов) + последний вопрос.

    Вячеслав: в цитате только сам вопрос (extract_question), не весь прошлый ответ —
    после оффтопа и так возвращаемся, дублировать стену текста незачем.
    """
    if session and session.condition:
        parts = [prefix, f"Продолжим задачу: «{_condition_quote(session.condition)}»"]
        if session.last_question:
            parts.append(f"Вот где мы остановились: {extract_question(session.last_question)}")
        return "\n".join(parts)
    if session and session.last_question:
        return f"{prefix}\nВот где мы остановились: {extract_question(session.last_question)}"
    return f"{prefix}\nПришли задачу текстом или фото — и начнём разбираться."


def check(text: str, session=None, last_question: str | None = None) -> str | None:
    """Конвейер фильтров. None = сообщение рабочее, пропускаем дальше.

    Вячеслав: гвард «ответ с эмоцией» отключает только фильтр границы роли, как
    решил ПВ. Мат и токсичность не пропускаем даже в ответах с цифрой.
    Сработавший фильтр возвращает готовый текст ответа ученику.
    """
    text = text or ""
    academic_answer = is_student_answer(text, last_question)

    # Вячеслав: 1. Токсичность/грубость — вежливая граница + возврат к делу
    if _TOXIC_RE.search(text):
        return build_back_to_task(
            session, "Давай поуважительнее — так мы быстрее дойдём до ответа."
        )

    # Вячеслав: 2. Скрытый мат (и открытый) — сообщение в память не попадает
    if check_plain_profanity(text) or check_hidden_profanity(text):
        return build_back_to_task(
            session, "Так со мной общаться не получится: давай по делу и по-доброму."
        )

    # Вячеслав: 3. Трюк «слово = число»
    if check_word_number_trick(text):
        return build_back_to_task(
            session, "Подменять слова числами — нечестный трюк, такие задачи не разбираю."
        )

    # Вячеслав: 4. Джейлбрейк — короткий отказ
    if _JAILBREAK_RE.search(text):
        return build_back_to_task(
            session, "Я учебный наставник, инструкции мне не поменять."
        )

    # Вячеслав: 4b. Ролевые игры про взлом — отказ без вызова модели (даже «всё вымышленное»)
    if _HACK_RE.search(text):
        return build_back_to_task(
            session, "Ролевые игры про взломы — не мой формат: я учебный наставник."
        )

    # Вячеслав: 5. Граница роли / фоновый шум (с гвардом «ответ с эмоцией»)
    if not academic_answer and _ROLE_RE.search(text):
        # Вячеслав: страх с учебным якорем («боюсь ошибиться в задаче») — НЕ отвлечение
        if not _ACADEMIC_ANCHOR_RE.search(text):
            return build_back_to_task(
                session, "Я учебный наставник, нерабочие темы не поддерживаю."
            )

    # Вячеслав: 7. Флуд/оффтоп — вернуть к задаче
    if _FLOOD_RE.search(text):
        return build_back_to_task(session, "Это в сторону. Учимся дальше.")

    return None


 
# Вячеслав: повторная отправка той же задачи
 

def _normalize_task(text: str) -> str:
    return re.sub(r"[^0-9а-яёa-z]", "", (text or "").lower())


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+", text or ""))


def is_same_task(new_text: str, anchor: str | None) -> bool:
    """Повтор той же задачи: нормализованное совпадение ≥0.9 И числа совпадают.

    Вячеслав: это НЕ новая задача — разбор заново не начинаем, едем с текущего шага.
    """
    if not anchor:
        return False
    a, b = _normalize_task(new_text), _normalize_task(anchor)
    if not a or not b:
        return False
    if _numbers(new_text) != _numbers(anchor):
        return False
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.9
