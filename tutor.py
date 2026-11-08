"""Конвейер одного сообщения ученика — один читаемый проход сверху вниз.

Главный принцип (ПВ повторяет его на каждой встрече): все решения о состоянии
принимает КОД, а GigaChat только пишет следующий вопрос-подсказку. Модель не
решает, новая это задача или продолжение — это решают регэкспы и база. Так
предсказуемо и легко отлаживать.

Два правила, которые нельзя ломать:
1) сессия в базе всегда соответствует тому, что ученик УЖЕ увидел в ответе;
2) условие активной задачи — якорь, входит в каждый промпт первым блоком.

Файл ведут Вячеслав и Алёна, Андрей помогал с вызовами модели.
"""

import logging
import re
from difflib import SequenceMatcher

import db
import guard
import prompts
import solver
from config import cfg
from guard import extract_question
from giga import failure_hint, has_table_or_graph, transcribe_photo

log = logging.getLogger("tutor")

GREETING = (
    "Привет! Я Сократ — наставник по информатике. Присылай задачу текстом или фото, "
    "и будем разбираться вопросами: я не решаю за тебя, но помогу дойти до ответа самому.\n"
    "/help — как я работаю, /reset — начать с чистого листа."
)

HELP_TEXT = (
    "Я веду разбор вопросами и подсказками, пока ты сам не назовёшь ответ.\n"
    "• Пришли условие задачи текстом или фото — начнём разбор.\n"
    "• Отвечай шагами: я буду подсказывать наводящими вопросами.\n"
    "• Готовый ответ я не выдаю — так ты поймёшь и запомнишь.\n"
    "• /reset — забыть текущую задачу и начать заново."
)

RESET_TEXT = "Готово: всё забыл, лист чистый. Пришли новую задачу — и начнём."

# Вячеслав: «Рестарт» и синонимы без слэша — то же, что /reset (ученики так и пишут)
RESTART_RE = re.compile(
    r"^\s*(?:рестарт|перезапуск|(?:давай\s+)?(?:начать|начн[её]м|начни)\s+(?:заново|с\s+нуля)|заново)[\s.!]*$",
    re.IGNORECASE,
)

TECH_REPLY = "У меня технические сложности, попробуй через минуту."
# Вячеслав: эти три текста дергает main.py, когда сработали лимиты или таймаут.
# раньше их тут не было, и бот падал с ошибкой вместо ответа
RATE_MINUTE_REPLY = "Ты пишешь слишком часто. Подожди минуту — и продолжим разбор."
RATE_DAY_REPLY = "На сегодня лимит сообщений закончился. Завтра продолжим!"
TIMEOUT_REPLY = "Что-то я задумался дольше обычного и не успел. Попробуй ещё раз."

NEW_TASK_PREFIX = "Берём новую!"
NEW_TASK_FALLBACK = NEW_TASK_PREFIX + " Давай начнём с простого: выпиши, что дано и что найти."
CONTINUE_AFTER_NO = "Продолжаем! "
CLOSE_CONFIRM = "Понял. Точно закрываем задачу? (да/нет)"
CLOSED_TEXT = "Задача закрыта, всё забыли. Пришли новую — начнём разбираться."

# Алёна: строгий режим для посторонних тем — на постороннее бот не болтает.
# В активной задаче вернём к разбору; в теме/после закрытия — попросим задачу
OFF_TOPIC_ACTIVE_PREFIX = "Это в сторону — я веду только учебный разбор."
OFF_TOPIC_TEXT = (
    "Я учебный наставник, посторонние темы не поддерживаю. "
    "Пришли задачу текстом или фото — и начнём разбираться."
)
OFF_TOPIC_DONE_TEXT = (
    "Посторонние темы — не мой формат: я учебный наставник. "
    "Пришли новую задачу — и начнём разбор. Вернуться к решённой — напиши «продолжим»."
)

# Вячеслав: ученик несколько раз ушёл от задачи — на третьем уходе предлагаем определиться
DRIFT_OFFER = (
    "Ты уже несколько раз ушёл в сторону. Давай определимся: закрываем текущую задачу? (да/нет) "
    "Если продолжаем разбор — ответь «нет», и вернёмся к делу."
)

# Вячеслав: шаг уже закрыт — повтор подтверждённого ответа не подтверждаем заново
STEP_CLOSED_PREFIX = "Этот шаг мы уже разобрали, и он верный. Давай следующий: "

# Вячеслав: шаг закрыт, а нового вопроса нет — проверяющий не заполнил «СЛЕДУЮЩИЙ ШАГ» или
# модель повторила старый. Прошлый вопрос сюда подставлять НЕЛЬЗЯ — на прогоне
# 8 ноября из-за этого ученик два раза подряд получал свой же вопрос целиком
# («залипание»). Двигаем вперёд общим текстом без повтора.
STEP_DONE_QUESTION = "что по условию нужно найти на следующем шаге?"
NEXT_STEP_FALLBACK = "Верно. Давай следующий: " + STEP_DONE_QUESTION

# Вячеслав: явное притязание ученика, что число — ИТОГОВЫЙ ответ задачи: «ответ 2048»,
# «ответ к задаче: 2048», «итоговый ответ — 2048», «это и есть ответ».
# Прогон 8 ноября 19:20: «ответ 2048» четырежды получал отписку «этот шаг мы
# уже разобрали» (число попадало в подтверждённые), и задача не могла закрыться
FINAL_CLAIM_RE = re.compile(
    r"(?:итогов\w*\s+|мой\s+|наш\s+|это\s+(?:и\s+есть\s+)?)?"
    r"ответ\w*\s*(?:к\s+задач\w*|задач\w*)?\s*[:\-–—]?\s*\d"
    r"|это\s+(?:и\s+есть\s+)?(?:итогов\w*\s+)?ответ\b"
    r"|итог\w*\s*[:\-–—]\s*\d",
    re.IGNORECASE,
)

# Андрей: модель застряла и повторяет свой предыдущий вопрос — двигаем её вперёд общим текстом
REPETITION_FALLBACK = (
    "Давай дальше: напиши, что у тебя получается на текущем шаге, и что должно выйти после него."
)

# Вячеслав: задача решена самим учеником (вердикт «верно-финал») — похвала и автозакрытие
PRAISE_TEXT = (
    "Верно! Ты дошёл до ответа сам — так и держать. Задача решена и закрыта.\n"
    "Пришли новую задачу — продолжим. Захочешь вернуться к этой — напиши «продолжим»."
)
DONE_TEXTS = (
    "Эта задача уже решена и закрыта. Пришли новую задачу — начнём разбор. "
    "Захочешь вернуться к этой — напиши «продолжим».",
    "Разбор этой задачи завершён. Жду новую задачу или слово «продолжим», чтобы вернуться.",
    "Задача закрыта, её ходы остались в истории. Присылай следующую — или напиши «продолжим», "
    "если нужно освежить решение.",
)
# Алёна: короткое «спасибо/ок» после закрытия — без вызова модели,
# но не дословно дважды подряд (ПВ запретил повторять свои фразы)
DONE_ACK_RE = re.compile(
    r"^\s*(?:оук?ей|ок|хор(ошо)?|ладно|понятно|понял[а]?|ясно|спасибо|спс|благодарю|пасиб\w*|"
    r"всё|все|ничего|ни\W*чё|нич[её])[\s!.,]*$",
    re.IGNORECASE,
)
WRONG_FINAL_ASK = (
    "В ответе есть ошибка. Давай найдём её вместе: расскажи, как ты получил этот ответ, "
    "по шагам — что делал первым, что получалось дальше."
)
REOPEN_RE = re.compile(r"продолжим|верн[её]мся|вернуться|вернись|ещ[её] раз", re.IGNORECASE)

# Вячеслав: явное закрытие задачи: «закрываем задачу», «задача закрыта», «конец», «хватит»…
# (раньше «закрыли задачу» не распознавалось и бот продолжал говорить о закрытой)
CLOSE_RE = re.compile(
    r"закр\w*\s+(?:эту\s+|свою\s+|нашу\s+)?(?:задач|тему|вс[её])"
    r"|(?:задач|тем)\w*\s+закр\w*"
    r"|^\s*(?:и\s+)?(?:эту\s+|её\s+|ее\s+)?(?:закрыли|закрыто|закрываем|конец|хватит|достаточно)[\s.!]*$"
    r"|(?:закончили|достаточно|хватит)\s+(?:с\s+)?(?:этой\s+|с\s+этой\s+)?задач",
    re.IGNORECASE,
)
YES_RE = re.compile(r"^\s*(?:да|ага|ок|окей|давай|конечно|закрываем|yes|y)\s*[.!]?\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(?:нет|неа|продолжаем|no|n)\s*[.!]?\s*$", re.IGNORECASE)

# Вячеслав: ученик бросает задачу и переключается («бросаем и переключаемся», «хочу физику»).
# Прогон 8 ноября 18:41: бот дважды отказал («давай сначала закончим информатику»)
# и потом сам вернул старую задачу посреди физики. Теперь бросаем сразу, без уговоров
SWITCH_AWAY_RE = re.compile(
    r"броса(?:ем|ть|й)\b|брось\s+(?:эту\s+)?задач|давай\s+(?:её\s+)?бросать"
    r"|не\s?интересн\w*|не\s+хочу\s+(?:эту\s+|её\s+|решать|больше)"
    r"|надоел[ао]?\b[^.?!\n]{0,30}(?:задач|разбор|тем)|задач\w*\s+надоел|мне\s+надоел"
    r"|переключ(?:аемся|имся|ись|иться)|смен\w*\s+(?:тем\w*|разбор)"
    r"|хочу\s+(?:физику|математик\w*|хими\w*|биолог\w*|русск\w*\s*язык|литератур\w*|обществознан\w*|географ\w*|истори\w*|англ\w*|другую\s+тем\w*|другой\s+предмет)"
    r"|давай\s+(?:другую\s+тем\w*|про\s+другое|о\s+другом)",
    re.IGNORECASE,
)
# Вячеслав: «неинтересно, НО давай продолжим» — бросать не надо
SWITCH_KEEP_GOING_RE = re.compile(
    r"продолж\w*|дореша\w*|давай\s+дальше|оста(?:ём|ем)ся|не\s+бросаем|все\s+(?:же|равно)\s+реш",
    re.IGNORECASE,
)

# Вячеслав: просьба объяснить тему/другой предмет — такие реплики могут быть и продолжением
# разбора, и сменой темы, поэтому отдаём их классификатору, а не решаем регэкспом
TOPIC_REQUEST_RE = re.compile(
    r"объясни|расскажи|что\s+такое|как\s+(?:работает|устроен\w*|действует)"
    r"|поговорим\s+(?:о|про)|давай\s+(?:о|про)\s|хочу\s+(?:про|другую|другой)",
    re.IGNORECASE,
)
# Вячеслав: слова других школьных предметов — середина информатики + «закон Ньютона» = смена темы
OTHER_SUBJECT_RE = re.compile(
    r"физик|ньютона|архимеда|закон\w*\s+ом\w*|хими\w*|биолог\w*|географ\w*|истори\w*"
    r"|литератур\w*|обществознан\w*|русск\w*\s+язык|английск\w*|математик\w*|геометри\w*|алгебр\w*",
    re.IGNORECASE,
)

# Алёна: ученик закрывает консультацию: «я разобрался», «всё понял», «не надо», «спасибо»…
# Прогон 8 ноября 18:40: «спасибо я разобрался» поймал оффтоп-фильтр, и бот ответил
# отказом «посторонние темы — не мой формат». Тему закрываем сразу, одной фразой
CONSULT_CLOSE_RE = re.compile(
    r"(?:я\s+)?вс[её]\s+понял\w*|вс[её]\s+(?:понятно|ясно|чётко|четко)"
    r"|(?:я\s+)?(?:вс[её]\s+)?разобрал\w*"
    r"|^\s*(?:да\s+)?(?:не\s+(?:надо|нужно|над)|хватит|достаточно)"
    r"|больше\s+не\s+(?:надо|нужно)"
    r"|закрыв\w*\s+(?:тем\w*|разбор)|(?:тем\w*|разбор)\s+закр\w*"
    r"|^\s*(?:спасибо|спс|благодарю|пасиб\w*)\b",
    re.IGNORECASE,
)
# Алёна: а это признак того, что консультация продолжается, хоть и выглядит как закрытие:
# «спасибо, объясни ещё про X» или «не понял, расскажи по-другому» — не закрываем
CONSULT_CONTINUE_RE = re.compile(
    r"объясн\w*|расскаж\w*|что\s+такое|почему|зачем|как\s+|пример|задач|продолж\w*"
    r"|не\s+понял\w*|непонятн\w*|ещ[её]\s+(?:раз|пример)",
    re.IGNORECASE,
)
CONSULT_CLOSE_TEXTS = (
    "Отлично, рад, что разобрался! Когда будет задача по информатике — присылай условие, разберём по шагам.",
    "Пожалуйста! Если понадобится ещё одна тема или задача — я тут, присылай.",
    "Хорошо, тему закрыли. Жду новую задачу или вопрос — продолжим.",
)

# Алёна: ответ при переключении на другую тему — старый диалог стёрт, дальше консультант
SWITCHED_PREFIX = "Хорошо, эту задачу бросаем — старый разбор убрал совсем. "
SWITCH_ACK = (SWITCHED_PREFIX
              + "Чем займёмся? Пришли новую задачу или скажи, какую тему разобрать.")

# Вячеслав: маркеры задачи и предметные слова информатики
TASK_MARKER_RE = re.compile(
    r"задач|услови|найди|определи|вычисли|переведи|реши|сколько|построй|составь|дано|напишите|вычислить",
    re.IGNORECASE,
)
SUBJECT_WORD_RE = re.compile(
    r"числ|бит|байт|кбайт|систем.{0,12}счислен|двоичн|десятичн|шестнадцат|код|алгоритм|цикл|массив"
    r"|логик|исполнител|программ|переменн|таблиц|граф|ребр|вершин|файл|каталог|папк|инф|объ[её]м"
    r"|исполн|редактор|черепах|робот|кумир|эксел|электрон.{0,12}таблиц|шифр|перевод|степен|сумм",
    re.IGNORECASE,
)
EXPLICIT_TASK_RE = re.compile(r"(?:вот|новая|следующая|держи|лови)\s+(?:полная\s+)?задача", re.IGNORECASE)
ASK_CONDITION_RE = re.compile(
    r"(?:пришли|прислать|отправь|напиши|перешли)[^\n]{0,40}(?:условие|текст|задачу)", re.IGNORECASE
)

# Вячеслав: служебные префиксы, которые НЕ попадают в last_question
_SERVICE_PREFIX_RE = re.compile(
    r"^(?:прошлая задача закрыта[.!]*\s*|берём новую[.!]*\s*|новая задача[.!]*\s*)+",
    re.IGNORECASE,
)


 
# Алёна: санитизация и контроль стиля
 

def latex_to_text(reply: str) -> str:
    """LaTeX → читаемый текст: дроби через «/», степени словами, без разметки.

    Алёна: модель обожает латех с frac, а школьник в мессенджере это не прочитает.
    """
    text = reply or ""
    text = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\^\{?2\}?", " в квадрате", text)
    text = re.sub(r"\^\{?3\}?", " в кубе", text)
    text = re.sub(r"\^\{?([0-9]+)\}?", r" в степени \1", text)
    text = re.sub(r"\\times|\\cdot", "*", text)
    text = re.sub(r"\\(?:left|right|,|;|!|quad|qquad|displaystyle)", "", text)
    text = re.sub(r"\\(?:le|leq)", "≤", text)
    text = re.sub(r"\\(?:ge|geq)", "≥", text)
    text = re.sub(r"\\text\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)          # Алёна: прочие латех-команды убираем
    text = text.replace("$", "").replace("\\(", "").replace("\\)", "")
    text = text.replace("\\[", "").replace("\\]", "")
    text = text.replace("**", "").replace("__", "")   # Алёна: разметка markdown — нет
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # Алёна: заголовки — нет
    text = re.sub(r"```[a-z]*\n?", "", text)          # Алёна: ограждения кода — нет
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Алёна: утечки бренда GigaChat → нейтральные слова (не афишируем, чьей моделью отвечаем)
_BRAND_RE = re.compile(
    r"gigachat|гигачат|нейросет(?:ь|и|ью|ям|ями|ей|ях)"
    r"|языков(?:ая|ую|ой|ые|ым)\s+модел(?:ь|и|ью|ями)"
    r"|искусственный\s+интеллект|\bllm\b|модель\s+(?:от\s+)?(?:сбер|giga)",
    re.IGNORECASE,
)


def sanitize_reply(reply: str) -> str:
    """Алёна: LaTeX → текст, утечки бренда → «наставник», скрытый мат → «***»."""
    text = latex_to_text(reply)
    text = _BRAND_RE.sub("наставник", text)
    text = " ".join("***" if guard.word_is_profanity(w) else w for w in text.split())
    return text.strip()


def split_sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text.strip())


def trim_questions(text: str, max_questions: int = 1) -> str:
    """Больше одного вопроса в ответе — обрезаем до последнего.

    Алёна: иначе модель задаст три вопроса сразу, и ученик не знает,
    на какой отвечать.
    """
    sentences = split_sentences(text)
    question_indexes = [i for i, s in enumerate(sentences) if "?" in s]
    if len(question_indexes) <= max_questions:
        return text
    keep_last = set(question_indexes[-max_questions:])
    kept = [s for i, s in enumerate(sentences) if i not in question_indexes or i in keep_last]
    return " ".join(kept)


ANSWER_NUMBER_RE = re.compile(r"ответ\w*\s*(?:будет|задачи|равен)?\s*[:=]\s*\d", re.IGNORECASE)
FINAL_NUMBER_RE = re.compile(
    r"\b(?:получается|получится|итог)\w*\s*[:=]?\s*\d", re.IGNORECASE
)
CODE_LINE_RE = re.compile(
    r"^\s*(?:def\s|class\s|print\s*\(|import\s|from\s+\w+\s+import|for\s+\w+\s+in\s|while\s+)", re.IGNORECASE
)
_CONFIRMATION_RE = re.compile(r"верн|правильн|молодец|сходится|совпада|отлично", re.IGNORECASE)

# Алёна: утечка служебного текста проверяющего в ответ ученику — бот говорил о ребёнке
# в третьем лице, как отчёт учителю, а не как разговор с учеником
_THIRD_PERSON_RE = re.compile(r"\bученик\w*", re.IGNORECASE)
# Алёна: служебные маркеры формата проверки — только в верхнем регистре, как в инструкции,
# чтобы не ловить обычную речь («Здесь есть ошибка: …»)
_VERDICT_TOKEN_RE = re.compile(r"ВЕРДИКТ\s*:|СЛЕДУЮЩИЙ\s+ШАГ\s*:|УТВЕРЖДЕНИЕ\s+УЧЕНИКА")

# Алёна: обрыв ответа — не начатое или не законченное предложение
_COMPLETE_END_RE = re.compile(r"[.!?…)»\"…]$")
_GLUE_RE = re.compile(r"(?:тоже|же|также|опять)\s+(?:Давай|Вот|Итак)\b", re.IGNORECASE)
# Алёна: ответ-«лекция» списком — школьнику в чате читать перечисления тяжело
# (жалоба с прогона 8 ноября: «слишком расписанные шаги»)
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-•*–]|\d+[\).])\s+\S", re.MULTILINE)


def echo_violation(reply: str, sources) -> str | None:
    """Дословное эхо мета-инструкции в ответе: ищем длинные (≥40 символов)
    куски инструкций в тексте ответа. None — чисто.
    """
    reply = reply or ""
    for source in sources or ():
        src = source or ""
        for i in range(0, max(0, len(src) - 40), 20):
            chunk = src[i:i + 40]
            if chunk in reply:
                return f"эхо мета-инструкции в ответе ({chunk[:30]}…)"
    return None


def style_violations(reply: str, instructions=None) -> list[str]:
    """Регэксп-контроль стиля: готовое решение, код, лекция без вопроса, утечка,
    эхо инструкций, обрывы. Пустой список — ответ можно отправлять.
    """
    violations: list[str] = []
    if ANSWER_NUMBER_RE.search(reply):
        violations.append("готовое решение (числовой ответ)")
    if FINAL_NUMBER_RE.search(reply) and not _CONFIRMATION_RE.search(reply):
        # Алёна: подтверждение верного ответа ученика — это не «выдача решения»
        violations.append("итоговое число посреди разбора")
    if sum(1 for ln in reply.splitlines() if CODE_LINE_RE.match(ln)) >= 3:
        violations.append("готовый код решения (≥3 строк)")
    if len(reply) > 600 and "?" not in reply:
        violations.append("лекция >600 символов без вопроса")
    if len(_BULLET_LINE_RE.findall(reply)) >= 3:
        violations.append("ответ построен списком-перечислением вместо обычного текста")
    if _THIRD_PERSON_RE.search(reply):
        # Алёна: наставник говорит с учеником на «ты»: «ученик…» — служебный текст уехал в ответ
        violations.append("утечка служебного текста (обращение к ученику в третьем лице)")
    if _VERDICT_TOKEN_RE.search(reply):
        violations.append("в ответе служебные токены проверки (ВЕРДИКТ/ОШИБКА/СЛЕДУЮЩИЙ ШАГ)")
    stripped = (reply or "").strip()
    if stripped:
        if not _COMPLETE_END_RE.search(stripped[-2:]):
            violations.append("обрыв ответа (нет завершающего знака)")
        if stripped[0].islower():
            violations.append("обрыв ответа (начинается со строчной буквы)")
    if _GLUE_RE.search(reply):
        violations.append("склейка обрывков в ответе")
    echo = echo_violation(reply, instructions)
    if echo:
        violations.append(echo)
    return violations


 
# Андрей: вызовы GigaChat — старт задачи / продолжение / тема
 

def add_instruction(messages: list[dict], instruction: str | None) -> list[dict]:
    """API GigaChat допускает system-сообщение ТОЛЬКО первым: инструкцию дописываем
    в первый system-блок (или ставим первой), а не в конец списка — иначе 400
    «system message must be the first message».
    """
    if not instruction:
        return messages
    result = [dict(m) for m in messages]
    if result and result[0].get("role") == "system":
        result[0]["content"] = result[0]["content"].rstrip() + "\n\n" + instruction
    else:
        result.insert(0, {"role": "system", "content": instruction})
    return result


def last_bot_message(messages: list[dict]) -> str:
    """Андрей: последняя реплика наставника в окне истории — для контроля повторов."""
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return ""


async def ask_giga(giga, messages: list[dict], instruction: str | None,
                   fallback: str) -> str:
    """Один вызов модели + контроль стиля + максимум один перезапрос.

    Андрей: основной вызов, потом чистим ответ и проверяем регэкспами. Не
    прошло — переспрашиваем модель с указанием нарушения. Опять не прошло —
    отдаём запасной ответ. Если модель тупит и повторяет свой вопрос — двигаем
    вперёд общим текстом, а не старым.
    """
    system_text = (messages[0].get("content") or "") if messages else ""
    # Андрей: эхо ищем только в системном блоке — инструкции поведения сознательно
    # подсказывают модели формулировки для ученика — их «эхо» легитимно
    sources = (system_text,)
    try:
        reply = await giga.chat(add_instruction(messages, instruction),
                                temperature=0.5, max_tokens=400)
    except Exception as e:
        log.error("GigaChat недоступен: %s", e)
        return TECH_REPLY

    prev_assistant = last_bot_message(messages)
    violations: list[str] = []
    for attempt in range(2):  # Андрей: основной вызов + максимум один перезапрос
        reply = sanitize_reply(reply)
        reply = trim_questions(reply, max_questions=1)
        violations = style_violations(reply, instructions=sources)
        # Андрей: модель переиграла уже заданный шаг — дословный повтор своего вопроса
        if not violations and prev_assistant:
            ratio = SequenceMatcher(None, reply[:400], prev_assistant[:400]).ratio()
            if ratio > 0.72:
                violations = [f"повтор уже заданного вопроса (совпадение {ratio:.0%})"]
        if not violations:
            break
        log.info("Ответ забракован контролем стиля (попытка %d): %s", attempt + 1, violations)
        if attempt == 0:
            try:
                retry_messages = add_instruction(
                    messages,
                    prompts.RETRY_INSTRUCTION.format(violation="; ".join(violations)),
                )
                reply = await giga.chat(retry_messages, temperature=0.5, max_tokens=400)
            except Exception as e:
                log.error("Перезапрос GigaChat не удался: %s", e)
                break
        else:
            break
    if violations:
        # Андрей: повтор шага не залечился — уходим вперёд общим вопросом, а не старым текстом
        if any("повтор" in v for v in violations):
            return REPETITION_FALLBACK
        return fallback  # Андрей: «Давай продолжим: <открытый вопрос>»

    # Андрей: опциональный LLM-контролёр (env LLM_CHECKER, по умолчанию выкл) — одна проверка
    if cfg.LLM_CHECKER:
        try:
            verdict = await giga.chat(
                [{"role": "system", "content": prompts.CHECKER_INSTRUCTION},
                 {"role": "user", "content": reply}],
                temperature=0.1, max_tokens=400,
            )
            if verdict and verdict.strip().upper() != "OK":
                reply = sanitize_reply(trim_questions(verdict.strip(), max_questions=1))
        except Exception as e:
            log.error("LLM-контролёр упал, оставляем регэксп-проверку: %s", e)
    return reply


 
# Андрей: проверка вывода/ответа ученика (отдельный вызов-проверка)
 

# Андрей: маркеры результата в реплике ученика: «ответ 4073», «получится строка …», «х = 5»
_CLAIM_MARK_RE = re.compile(
    r"ответ|получ(?:ится|ается)|выйдет|выйти|итог|равно|будет|значит|следовательно|должно|=",
    re.IGNORECASE,
)


def _looks_like_answer(text: str) -> bool:
    """Андрей: похоже на вывод/ответ — есть числа И маркер результата, либо короткая числовая реплика."""
    if not re.search(r"\d", text):
        return False
    return bool(_CLAIM_MARK_RE.search(text)) or len(text) <= 80


def _clean_reply(text: str) -> tuple[str, bool]:
    """Санитизация + трим вопросов + контроль стиля; True, если текст чистый.

    Алёна: тексты проверки могут притащить в полях куски мета-инструкций —
    проверяем эхо.
    """
    clean = trim_questions(sanitize_reply(text), max_questions=1)
    violations = style_violations(clean, instructions=(
        prompts.VERDICT_INSTRUCTION, prompts.VERDICT_RECHECK_INSTRUCTION,
        prompts.VERDICT_CONFIRM_INSTRUCTION,
    ))
    return clean, not violations


def _parse_verdict(raw: str) -> dict | None:
    """Разбор ответа проверяющего: {verdict, error, next_step}; None — не разобрались."""
    if not raw:
        return None
    m = re.search(r"ВЕРДИКТ\s*:\s*([^\n]+)", raw, re.IGNORECASE)
    if not m:
        return None
    token = m.group(1).lower().replace("ё", "е").replace(" ", "-").strip(" .!-")
    verdict = None
    for needle, name in (
        ("неверно-финал", "wrong_final"), ("неверный-финал", "wrong_final"),
        ("неверно-шаг", "wrong_step"), ("неверный-шаг", "wrong_step"),
        ("не-вывод", "not_claim"), ("невывод", "not_claim"),
        ("не-уверен", "unsure"), ("неуверен", "unsure"),
        ("верно-финал", "correct_final"), ("верный-финал", "correct_final"),
        ("верно-шаг", "correct_step"), ("верный-шаг", "correct_step"),
    ):
        if needle in token:
            verdict = name
            break
    if verdict is None:  # Андрей: модель ответила без точного токена — грубая классификация
        if "неверно" in token or "ошиб" in token:
            verdict = "wrong_step"
        elif "уверен" in token:  # Андрей: «не уверен»/«не-уверен» без дефиса
            verdict = "unsure"
        elif "верно" in token or "правильн" in token:
            verdict = "correct_step"
        elif "вывод" in token:
            verdict = "not_claim"
        else:
            return None

    def field(label: str) -> str:
        m2 = re.search(label + r"\s*:\s*([^\n]*)", raw, re.IGNORECASE)
        if not m2:
            return ""
        value = m2.group(1).strip()
        if len(value) > 300:
            # Алёна: раньше резали ровно на 300-м символе, и ученик получал обрубок
            # «4 бита не п.» вместо «не помещается» — теперь по границе
            cut = value[:300]
            for stop in (".", "!", "?"):
                pos = cut.rfind(stop)
                if pos >= 120:
                    return cut[: pos + 1]
            pos = cut.rfind(" ")
            if pos > 0:
                cut = cut[:pos]
            return cut.rstrip(" ,;:-")
        return value

    m3 = re.search(r"СЛЕДУЮЩ\w*\s+ШАГ\s*:\s*(.*)", raw, re.IGNORECASE | re.DOTALL)
    next_step = (m3.group(1).strip()[:300] if m3 else "")
    return {"verdict": verdict, "error": field("ОШИБКА"), "next_step": next_step}


async def _check_prompt(sess, text: str, claim: str | None = None,
                        open_question: str | None = None,
                        insist: bool = False) -> str:
    """Общий промпт проверяющего: условие + эталон решателя + резюме + окно диалога
    + текущий вопрос + сообщение.

    Андрей: память проверяющего должна дотягивать до самого финального ответа.
    Без текущего вопроса и резюме он видел только обрывок диалога и принимал
    ответ на промежуточный вопрос («147» секунд) за финал (прогон 8 ноября, 18:05).
    """
    history = await db.get_history(sess.user_id, 10)
    lines = [f"{'Ученик' if row.role == 'user' else 'Наставник'}: {row.content[:400]}"
             for row in history]
    prompt = f"УСЛОВИЕ ЗАДАЧИ:\n{sess.condition}\n\n"
    if getattr(sess, "trace", None):
        # Андрей: эталонная трасса посчитана программой — сверяем утверждения ученика с ней
        prompt += f"{sess.trace}\n\n"
    if getattr(sess, "summary", None):
        prompt += f"РЕЗЮМЕ РАННИХ ХОДОВ (давно пройденное):\n{sess.summary}\n\n"
    if open_question:
        prompt += (f"ТЕКУЩИЙ ВОПРОС НАСТАВНИКА, НА КОТОРЫЙ ОТВЕЧАЕТ УЧЕНИК:\n{open_question}\n\n"
                   "Это вопрос разбора, а не требование условия задачи.\n\n")
    if lines:
        prompt += "ПОСЛЕДНИЕ СООБЩЕНИЯ ДИАЛОГА:\n" + "\n".join(lines) + "\n\n"
    prompt += f"СООБЩЕНИЕ УЧЕНИКА:\n{text}"
    if claim:
        prompt += f"\n\nУТВЕРЖДЕНИЕ УЧЕНИКА, КОТОРОЕ ПРОВЕРЯЕМ:\n{claim}"
    if insist:
        prompt += (
            "\n\nВНИМАНИЕ: ученик повторяет этот ответ и настаивает на нём. Проверь особенно "
            "пристально, не является ли это число ответом на ТРЕБОВАНИЕ УСЛОВИЯ. Если требование "
            "условия этим числом уже выполнено и содержательных шагов дальше нет — ставь "
            "ВЕРНО-ФИНАЛ, не придумывай новых шагов. Если число противоречит эталонному расчёту — "
            "ставь НЕВЕРНО-ФИНАЛ и объясняй, где именно разошлись вычисления."
        )
    return prompt


async def _check_student_answer(giga, sess, text: str,
                                open_question: str | None = None,
                                insist: bool = False) -> dict | None:
    """Отдельный вызов: проверяющий решает задачу молча и классифицирует сообщение ученика.

    Андрей: обвинение (НЕВЕРНО-*) требует подтверждения ВТОРОЙ независимой
    проверкой — один проверяющий однажды неверно трассировал машину Тюринга
    и дважды браковал верный ответ ученика. Вторая не сошлась — обвинения нет.

    insist=True — ученик повторяет ответ или прямо назвал его итоговым («ответ 2048»):
    проверяющему это говорится прямо, чтобы повтор не классифицировался как очередной шаг.

    None — проверка не удалась (сеть/формат): тогда обычный ход разбора.
    """
    prompt = await _check_prompt(sess, text, open_question=open_question, insist=insist)
    try:
        raw = await giga.chat(
            [{"role": "system", "content": prompts.VERDICT_INSTRUCTION},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=600, kind="verdict",
        )
    except Exception as e:
        log.error("Проверка вывода не удалась: %s", e)
        return None
    parsed = _parse_verdict(raw)
    if parsed is None:
        log.warning("Вердикт не разобран, обычное продолжение: %r", (raw or "")[:200])
        return parsed

    if parsed["verdict"] in ("wrong_final", "wrong_step"):
        try:
            raw2 = await giga.chat(
                [{"role": "system", "content": prompts.VERDICT_CONFIRM_INSTRUCTION},
                 {"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=600, kind="verdict",
            )
            second = _parse_verdict(raw2)
        except Exception as e:
            log.error("Подтверждающая проверка не удалась: %s", e)
            second = None
        if second is None:
            # Андрей: подтвердить обвинение не удалось — обвинять нельзя
            parsed = {"verdict": "unsure", "error": "", "next_step": ""}
        elif second["verdict"] in ("correct_final", "correct_step"):
            parsed = second  # Андрей: вторая проверка оправдала ученика
        elif second["verdict"] == "not_claim":
            parsed = second  # Андрей: это вообще не вывод — обычное продолжение
        elif second["verdict"] == "unsure":
            parsed = second
        # Андрей: иначе вторая тоже «неверно» — обвинение подтверждено, остаёмся при parsed
    return parsed


 
# Вячеслав: спор с проверкой — ученик настаивает на своём ответе (не спорь с верным)
 

_DISPUTE_RE = re.compile(
    r"сошл\w*|я\s+прав|прав\s+я|я\s+же\s+(?:прав|говорил)|ошибаешься|не\s+согласен|"
    r"проверь\s+(?:ещ|снова|повторн)|перепроверь|пересчитай|пересчитал|"
    r"я\s+уверен|ответ\s+(?:верн\w*|правил\w*)|вс[её]\s+(?:верно|правильно)|"
    r"я\s+не\s+(?:ошибл|спорю)",
    re.IGNORECASE,
)

DISPUTE_ADMIT_FINAL = (
    "Ты прав, извини: я ошибся в проверке, а твой ответ верный. Молодец, что не сдался. "
    "Задача решена и закрыта.\nПришли новую задачу — продолжим."
)
DISPUTE_WALKTHROUGH = (
    "Давай сверим решение по шагам: напиши свою цепочку с первого шага — что делал и что "
    "получалось после каждого шага?"
)


def _numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+", text or ""))


def _repeat_streak(history, num: str) -> int:
    """Сколько последних сообщений ученика подряд содержат то же число.

    Вячеслав: сообщения наставника пропускаем (ученик всегда повторяет через
    наш ответ), а любое другое сообщение ученика обрывает серию. Текущее
    сообщение ещё не сохранено: streak=1 — ученик уже называл это число один раз.
    """
    streak = 0
    for row in reversed(history):
        if row.role != "user":
            continue
        if num in _numbers_in(row.content):
            streak += 1
        else:
            break
    return streak


def _final_answer_numbers(trace: str | None) -> set[str]:
    """Числа из строки эталонной трассы с итоговым ответом («Итог: …», «Ответ: …»).

    Страховка от преждевременного закрытия: если проверяющий объявил ВЕРНО-ФИНАЛ,
    а среди чисел ученика нет итогового ответа решателя — значит проверяющий слил
    промежуточный шаг с финалом (прогон 8 ноября 18:05: «147» секунд закрыли задачу
    про объём файла). Пустое множество — эталона нет или итог без чисел: сверять
    нечем, верим проверяющему.
    """
    if not trace:
        return set()
    numbers: set[str] = set()
    for line in trace.splitlines():
        if re.search(r"\b(?:Ответ|Итог)\s*:", line):
            numbers.update(re.findall(r"\d+", line))
    return numbers


def _is_dispute(text: str, claim: str | None) -> bool:
    """Вячеслав: ученик оспаривает прежний вердикт — слова несогласия или повтор своего числа."""
    if not claim:
        return False
    if _DISPUTE_RE.search(text or ""):
        return True
    nums = _numbers_in(text)
    return bool(nums and nums & _numbers_in(claim))


async def _handle_dispute(giga, sess, text: str, open_question: str) -> str | None:
    """Независимый пересчёт при споре. None — спора нет или перепроверка не удалась.

    Ученик повторил свой ответ или сказал «сошлось» — прежнее утверждение об
    ошибке НЕ повторяем: пересчитываем с нуля и, если ученик прав, признаём
    ошибку проверки.
    """
    claim = sess.verdict_claim
    if not claim or not _is_dispute(text, claim):
        return None
    user_id = sess.user_id
    count = await db.add_dispute(user_id)
    if count > 2:  # Вячеслав: дальше не проверяем моделью — сверяемся руками
        await db.set_verdict_claim(user_id, None)
        await _save_exchange(user_id, user_text=text, reply=DISPUTE_WALKTHROUGH, update_question=False)
        return DISPUTE_WALKTHROUGH
    prompt = await _check_prompt(sess, text, claim=claim, open_question=open_question)
    try:
        raw = await giga.chat(
            [{"role": "system", "content": prompts.VERDICT_RECHECK_INSTRUCTION},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=600, kind="verdict_recheck",
        )
    except Exception as e:
        log.error("Перепроверка не удалась: %s", e)
        return None  # Вячеслав: не спорим на упавшей проверке — обычный ход
    v = _parse_verdict(raw)
    if v is None:
        log.warning("Перепроверка не разобрана: %r", (raw or "")[:200])
        return None
    kind = v["verdict"]
    if kind == "not_claim":
        await db.set_verdict_claim(user_id, None)
        return None  # Вячеслав: упомянул число без утверждения — обычное продолжение
    if kind in ("correct_final", "correct_step"):
        # Вячеслав: проверка ошиблась, ученик прав — признаём и сразу закрываем, без «закрываем?»
        await db.set_verdict_claim(user_id, None)
        if kind == "correct_final":
            await _save_exchange(user_id, user_text=text, reply=DISPUTE_ADMIT_FINAL, update_question=False)
            await db.set_status(user_id, "done")
            return DISPUTE_ADMIT_FINAL
        clean = "Ты прав, извини: я ошибся в проверке. " + (v["next_step"] or open_question)
        clean, _ = _clean_reply(clean)
        await _save_exchange(user_id, user_text=text, reply=clean)
        return clean
    if kind == "unsure":
        await db.set_verdict_claim(user_id, None)
        clean, _ = _clean_reply("Пересчитать однозначно не получается, спорить не буду. " + DISPUTE_WALKTHROUGH)
        await _save_exchange(user_id, user_text=text, reply=clean, update_question=False)
        return clean
    # Вячеслав: остаёмся при своём, но теперь с обоснованием (промежуточные значения для сверки)
    base = ("Пересчитал ещё раз, с нуля, — остаюсь при своём. Моя проверка: "
            + (v["error"] or "сверь свои шаги с моим ходом") + " Давай сверим: "
            + (v["next_step"] or open_question))
    clean, ok = _clean_reply(base)
    if not ok:
        clean = f"Давай сверим шаг за шагом: {open_question}"
    await _save_exchange(user_id, user_text=text, reply=clean)
    return clean


async def _apply_verdict(giga, user_id: int, sess, text: str, v: dict, open_question: str,
                         insist: bool = False) -> str:
    """Действия по вердикту проверяющего: верно-финал → похвала и автозакрытие (без
    вопроса «закрываем?»); неверно-финал → сначала просьба объяснить логику;
    вывод верный/ошибочный → подтверждение или опровержение с местом ошибки.

    insist=True — ученик повторяет ответ или прямо назвал его итоговым. Тогда
    работает и обратная страховка к проверке 18:05: проверяющий назвал ответ
    «шагом», а эталонный решатель знает итоговый ответ, и он среди чисел ученика →
    ученик прав, проверяющий занырнул в бесконечные шаги. Повышаем до финала
    и закрываем (прогон 8 ноября 19:20: «2048» нельзя было закрыть никак).
    Без настойчивости не повышаем: итоговый ответ может совпасть с промежуточным
    числом, названным мимоходом.
    """
    kind = v["verdict"]
    error = v["error"]
    next_step = v["next_step"]
    in_logic_check = sess.status == "checking_logic"

    # Вячеслав: проверяющий объявил финал, но эталон решателя знает итоговый ответ, и его
    # среди чисел ученика нет → слил промежуточный шаг с финалом. Разбор идёт дальше
    student_numbers = _numbers_in(text)
    if kind == "correct_final" and student_numbers:
        final_numbers = _final_answer_numbers(getattr(sess, "trace", None))
        if final_numbers and not (student_numbers & final_numbers):
            log.info("Финал отбракован сверкой с эталоном: ученик назвал %s, эталон %s",
                     sorted(student_numbers), sorted(final_numbers))
            v = dict(v, verdict="correct_step")
            kind = "correct_step"

    if kind == "correct_step" and insist and student_numbers:
        final_numbers = _final_answer_numbers(getattr(sess, "trace", None))
        if final_numbers and (student_numbers & final_numbers):
            log.info("Шаг повышен до финала: ученик настаивает, эталон подтверждает %s",
                     sorted(student_numbers & final_numbers))
            v = dict(v, verdict="correct_final")
            kind = "correct_final"

    if kind == "correct_final":
        await db.set_verdict_claim(user_id, None)
        await db.add_confirmed_numbers(user_id, _numbers_in(text))
        await _save_exchange(user_id, user_text=text, reply=PRAISE_TEXT, update_question=False)
        await db.set_status(user_id, "done")
        return PRAISE_TEXT

    if kind == "wrong_final" and not in_logic_check:
        # Алёна: неверный финальный ответ — сразу называем, где искать расхождение,
        # затем просим логику и проверяем её; ответ запоминаем — ученик вправе
        # оспорить, и тогда будет независимый пересчёт
        await db.set_verdict_claim(user_id, text)
        ask = WRONG_FINAL_ASK
        if error:
            hint, ok = _clean_reply("Проверь такой момент: " + error.rstrip(". ") + ".")
            if ok:
                if not hint.endswith((".", "!", "?")):
                    hint += "."
                ask = (f"В ответе есть ошибка. {hint} "
                       "А теперь расскажи, как ты получил свой ответ, по шагам — "
                       "что делал первым, что получалось дальше.")
        await _save_exchange(user_id, user_text=text, reply=ask, update_question=False)
        await db.set_status(user_id, "checking_logic")  # Вячеслав: после save — он ставит active
        return ask

    if kind == "unsure":
        # Вячеслав: проверяющий не смог пересчитать — не утверждаем ничего, сверяемся вместе
        await db.set_verdict_claim(user_id, None)
        clean, _ = _clean_reply(
            "Я не смог однозначно пересчитать — давай сверим вместе. "
            "Напиши свою цепочку с первого шага: что делал и что получалось?"
        )
        await _save_exchange(user_id, user_text=text, reply=clean, update_question=False)
        if in_logic_check:
            await db.set_status(user_id, "active")
        return clean

    if kind == "correct_step":
        await db.set_verdict_claim(user_id, None)
        await db.add_confirmed_numbers(user_id, _numbers_in(text))
        if next_step:
            base = "Верно. " + next_step
            clean, ok = _clean_reply(base)
            if not ok:
                clean = f"Давай продолжим: {open_question}"
        else:
            # Вячеслав: проверяющий не дал следующий шаг. Раньше здесь подставлялся весь
            # прошлый ответ («Продолжим: <весь прошлый вопрос>») — ученик второй
            # раз подряд читал свой же вопрос, это и есть «залипание» из прогона
            # 8 ноября. Теперь спрашиваем модель про следующий шаг, а если и она
            # повторяется или недоступна — двигаем вперёд общим текстом.
            messages = await _build_messages(user_id, text)
            reply = await ask_giga(giga, messages, prompts.NEXT_STEP_INSTRUCTION,
                                   fallback=NEXT_STEP_FALLBACK)
            if reply == TECH_REPLY:
                clean = NEXT_STEP_FALLBACK
            else:
                clean = _SERVICE_PREFIX_RE.sub("", reply).strip()
    else:  # Вячеслав: wrong_step или wrong_final после объяснения логики
        await db.set_verdict_claim(user_id, text)
        base = "Здесь есть ошибка. " + (error or "Проверь последний шаг.")
        base += " Давай исправимся с этого места: " + (next_step or open_question)
        clean, ok = _clean_reply(base)
        if not ok:
            clean = f"Давай вернёмся: {open_question}"
    if in_logic_check:
        await db.set_status(user_id, "active")
    await _save_exchange(user_id, user_text=text, reply=clean)
    return clean


 
# Андрей: сборка промптов
 

async def _build_messages(user_id: int, user_text: str, closed: bool = False) -> list[dict]:
    """Системный промпт (якорь + профиль + резюме + эталон) + окно истории + текущее сообщение.

    Андрей: closed=True — задача уже закрыта, условие идёт справочником, а не
    «неприкосновенным якорем», иначе модель сама таскает решённую задачу в новые разговоры.
    """
    sess = await db.get_session(user_id)
    history = await db.get_history(user_id, cfg.HISTORY_WINDOW)
    system = prompts.build_system_prompt(
        subject=cfg.ACTIVE_SUBJECT,
        condition=sess.condition if sess else None,
        grade=sess.grade if sess else None,
        summary=sess.summary if sess else None,
        reference=sess.trace if sess else None,
        closed=closed,
    )
    messages = [{"role": "system", "content": system}]
    for row in history:
        messages.append({"role": "user" if row.role == "user" else "assistant", "content": row.content})
    messages.append({"role": "user", "content": user_text})
    return messages


async def _drop_and_switch(giga, user_id: int, text: str) -> str:
    """Бросить активную задачу и ответить на новую тему ученика.

    Прогон 8 ноября 18:41: ученик ушёл в физику, бот согласился, но условие Фано
    осталось якорем в каждом промпте — и посреди физики он сам «вернулся» к задаче.
    Теперь старый разбор стирается целиком (история, якорь, резюме), дальше
    отвечает консультант. Уровень ученика сохраняется.
    """
    await db.drop_task(user_id)
    messages = await _build_messages(user_id, text)
    reply = await ask_giga(giga, messages, prompts.TOPIC_INSTRUCTION, fallback=SWITCH_ACK)
    if reply == TECH_REPLY:
        # Андрей: задача уже брошена, модель недоступна — отвечаем своим текстом, не «техошибкой»
        clean = SWITCH_ACK
    else:
        clean = SWITCHED_PREFIX + _SERVICE_PREFIX_RE.sub("", reply).strip()
    await _save_exchange(user_id, user_text=text, reply=clean)
    return clean


 
# Вячеслав: определение типа сообщения — ТОЛЬКО кодом, без модели
 

def looks_like_new_task(text: str) -> bool:
    """Вячеслав: НОВАЯ ЗАДАЧА = (текст ≥120 символов И похоже на условие) ИЛИ явная фраза."""
    text = text or ""
    if len(text) >= 120:
        if TASK_MARKER_RE.search(text) and SUBJECT_WORD_RE.search(text):
            return True
    if EXPLICIT_TASK_RE.search(text) and len(text) >= 40:
        return True
    return False


def asked_for_condition(last_question: str | None) -> bool:
    """Вячеслав: бот в прошлом ответе просил прислать условие."""
    return bool(last_question and ASK_CONDITION_RE.search(last_question))


 
# Вячеслав: команды
 

async def handle_command(user_id: int, text: str) -> str:
    command = text.strip().split()[0].split("@")[0].lower()
    if command == "/start":
        return GREETING
    if command == "/help":
        return HELP_TEXT
    if command == "/reset":
        await db.clear_all(user_id)  # Вячеслав: очистить всё — сессия + история + резюме
        return RESET_TEXT
    return "Не знаю такую команду. /help — подсказка."


 
# Вячеслав: сохранение хода
 

async def _save_exchange(user_id: int, user_text: str, reply: str, update_question: bool = True) -> None:
    """Вячеслав: оба сообщения в историю; last_question = ответ без префиксов; резюме."""
    await db.add_message(user_id, "user", user_text)
    await db.add_message(user_id, "assistant", reply)
    if update_question:
        await db.set_last_question(user_id, reply)
    else:
        await db.set_status(user_id, "active")  # Вячеслав: только обновить updated_at
    await db.update_summary(
        user_id,
        window=cfg.HISTORY_WINDOW,
        max_lines=cfg.SUMMARY_MAX_LINES,
        line_max=cfg.SUMMARY_LINE_MAX,
    )


def _pick_variant(texts: tuple[str, ...], history) -> str:
    """Вариант текста, не совпадающий дословно с прошлым ответом наставника.

    Алёна: повторять свои фразы дословно нельзя (запрет ПВ): если прошлый ответ
    совпал с первым вариантом, берём следующий.
    """
    prev = ""
    for row in reversed(history):
        if row.role == "assistant":
            prev = row.content
            break
    for variant in texts:
        if variant != prev:
            return variant
    return texts[0]


def _pick_done_text(history) -> str:
    """Алёна: заглушка после закрытия задачи — но не дословно тот же текст, что прошлый ответ."""
    return _pick_variant(DONE_TEXTS, history)


async def _ask_if_study(giga, text: str, last_question: str | None = None,
                        condition: str | None = None) -> str:
    """Классификатор «задача / тема / общение» — отдельный дешёвый запрос.

    Тип сообщения про ЗАДАЧИ (новая/повтор/продолжение) по-прежнему определяет
    только код; классификатор решает одно: продолжение ли это текущего разбора
    (task), просьба другой темы/предмета (topic) или посторонний разговор (chat).
    Ему передаётся последний вопрос наставника и текущее условие: без них «объясни
    законы Ньютона» посреди Фано неотличимо от «объясни, что за коды в задаче».
    Любая ошибка или непонятный ответ = «task» (безопасный дефолт: ничего не бросаем).
    """
    content = prompts.INTENT_INSTRUCTION
    if condition:
        content += f"\n\nТекущий разбор идёт по задаче: {condition[:300]}"
    if last_question:
        content += f"\n\nПоследнее сообщение наставника: {last_question[:300]}"
    content += f"\n\nСообщение: {text}"
    try:
        raw = await giga.chat(
            [{"role": "user", "content": content}],
            temperature=0.1, max_tokens=10, kind="intent",
        )
    except Exception as e:
        log.warning("Классификатор намерения недоступен: %s", e)
        return "task"
    token = (raw or "").strip().upper()
    if "ОБЩЕНИЕ" in token:
        return "chat"
    if "ТЕМА" in token:
        return "topic"
    return "task"


def _needs_intent_check(text: str) -> bool:
    """Нужен ли классификатор «задача / тема / общение»: короткая реплика без
    маркеров задачи и предметных слов, либо явная просьба «объясни/расскажи»,
    либо упоминание другого предмета. Последние две бывают и продолжением
    разбора, и сменой темы — решает классификатор, а не регэксп.
    """
    t = text or ""
    if TOPIC_REQUEST_RE.search(t) or OTHER_SUBJECT_RE.search(t):
        return True
    return len(t) < 120 and not TASK_MARKER_RE.search(t) and not SUBJECT_WORD_RE.search(t)


def _wants_topic_close(text: str) -> bool:
    """Ученик закрывает консультацию («я разобрался», «всё понял», «не надо»)?

    Алёна: тему закрываем сразу и без модели. Просьбы продолжить («спасибо,
    объясни ещё про range») закрыванием не считаем.
    """
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    if CONSULT_CONTINUE_RE.search(t):
        return False
    return bool(CONSULT_CLOSE_RE.search(t))


def _off_topic_reply(sess, status: str | None = None) -> str:
    """Алёна: строгий отказ от посторонней темы — на постороннее бот не болтает."""
    if status == "done":
        return OFF_TOPIC_DONE_TEXT
    if sess and sess.condition:
        return guard.build_back_to_task(sess, OFF_TOPIC_ACTIVE_PREFIX)
    return OFF_TOPIC_TEXT


 
# Вячеслав: главный конвейер
 

async def handle_message(giga, user_id: int, text: str, photo_bytes: bytes | None = None) -> str:
    """Вячеслав: полный конвейер одного сообщения ученика. Возвращает текст ответа."""
    text = (text or "").strip()

    # Вячеслав: 1. Команды
    if text.startswith("/"):
        return await handle_command(user_id, text)
    if RESTART_RE.match(text):
        return await handle_command(user_id, "/reset")

    sess = await db.get_session(user_id)
    last_question = sess.last_question if sess else None
    photo_task = False

    # Вячеслав: 2. Фото — расшифровать через GigaChat
    if photo_bytes is not None:
        if not cfg.VISION_ENABLED:
            return "Чтение фото выключено. Пришли условие задачи текстом, и продолжим."
        result = await transcribe_photo(giga, photo_bytes)
        if result.status == "fail":
            return failure_hint(result.reason)
        if result.status == "no_task":
            return guard.build_back_to_task(sess, "На фото я не нашёл задачи.")
        # Андрей: расшифровка длинная — это норма, лимит длины на неё не действует
        text = (text + "\n\n" if text else "") + "Текст с фото ученика: " + result.text
        photo_task = True

    if not text:
        return "Напиши условие текстом или пришли фото задачи."

    # Вячеслав: 3. Фильтры (с гвардом «ответ с эмоцией») — до всякого GigaChat
    verdict = guard.check(text, session=sess, last_question=last_question)
    if verdict is not None:
        return verdict  # Вячеслав: сработало — ответ, в память не пишем, конец

    # Вячеслав: 4. Явное «закрываем задачу» → переспрос/закрытие
    if sess and sess.status == "awaiting_close":
        # Вячеслав: «да» и «бросаем и переключаемся» — закрываем без доп. вопросов
        if YES_RE.match(text) or CLOSE_RE.search(text) or SWITCH_AWAY_RE.search(text):
            await db.clear_all(user_id)  # Вячеслав: «да» → полная очистка: сессия + история + резюме
            return CLOSED_TEXT
        if NO_RE.match(text):
            await db.set_status(user_id, "active")
            await db.reset_drift(user_id)
            # Алёна: только сам вопрос, без повторной стены текста (extract_question)
            follow = extract_question(last_question) or "Выпишем, что дано и что найти."
            await _save_exchange(user_id, user_text=text, reply=follow, update_question=False)
            return CONTINUE_AFTER_NO + follow
        # Вячеслав: в awaiting_close пришёл содержательный текст — считаем, что ученик продолжил разбор
        await db.set_status(user_id, "active")
        sess = await db.get_session(user_id)
    elif CLOSE_RE.search(text):
        await db.set_status(user_id, "awaiting_close")
        return CLOSE_CONFIRM

    # Вячеслав: 4b. «Бросаем и переключаемся»: бросаем сразу, без уговоров «сначала закончим
    # информатику» (прогон 8 ноября 18:41 бот дважды отказал ученику) и без
    # переспроса. Старый диалог стирается целиком, дальше отвечает консультант.
    # Реплики с цифрами не переключаем: «неинтересно, но ответ 147» — это ответ
    # по задаче, а не просьба бросить
    if (sess and sess.condition and sess.status in ("active", "checking_logic")
            and len(text) < 120 and not any(ch.isdigit() for ch in text)
            and SWITCH_AWAY_RE.search(text) and not SWITCH_KEEP_GOING_RE.search(text)):
        return await _drop_and_switch(giga, user_id, text)

    # Вячеслав: 5. Определение типа сообщения — ТОЛЬКО кодом.
    # В закрытой (решённой) сессии повтор того же текста — «прорешать заново», не повтор
    is_repeat = bool(sess and sess.condition and sess.status != "done"
                     and guard.is_same_task(text, sess.condition))
    is_new = False
    if not is_repeat:
        if photo_task:
            is_new = True  # Вячеслав: расшифровка фото с задачей — новая задача
        elif looks_like_new_task(text):
            is_new = True
        elif sess and not sess.condition and asked_for_condition(sess.last_question) \
                and TASK_MARKER_RE.search(text) and len(text) >= 20:
            is_new = True  # Вячеслав: после просьбы прислать условие текст прикрепляется без вопросов

    # Вячеслав: уровень ученика обновляем по названному классу/маркерам (детерминированно)
    detected = prompts.detect_grade(text)
    new_grade = detected if detected != "unknown" else None
    if new_grade and new_grade != (sess.grade if sess else None):
        await db.set_grade(user_id, new_grade)

    if is_new and not is_repeat:
        # Вячеслав: 6. Новая задача → якорь записывается В ЭТОМ ЖЕ ХОДУ
        condition = text[: cfg.CONDITION_MAX]
        await db.set_anchor(user_id, condition, grade=new_grade)

        # Андрей: эталонный прогон до диалога — детерминированный решатель (МТ/кодирование)
        ref = solver.try_solve(condition)
        await db.set_trace(user_id, ref.trace if ref else None)

        # Алёна: старт разбора — таблица/граф → попросить выписать все связи
        instruction = (prompts.TABLE_START_INSTRUCTION
                       if has_table_or_graph(condition) else prompts.START_INSTRUCTION)
        messages = await _build_messages(user_id, condition)
        reply = await ask_giga(giga, messages, instruction, fallback=NEW_TASK_FALLBACK)
        if reply == TECH_REPLY:
            return TECH_REPLY  # Вячеслав: якорь записан, но ход в память не попадает

        clean = _SERVICE_PREFIX_RE.sub("", reply).strip()
        await _save_exchange(user_id, user_text=condition, reply=clean)
        return NEW_TASK_PREFIX + " " + clean

    if sess and sess.condition:
        # Вячеслав: 7. Продолжение разбора (любая реплика: «не понял», ответ шагом, «запутался»)
        # open_question — это сам вопрос из прошлого ответа, а не весь ответ целиком
        open_question = extract_question(last_question) or "выпиши, что дано и что найти."

        if sess.status == "done":
            # Алёна: задача решена: «продолжим» возвращает, короткое «спасибо» — заглушкой,
            # всё остальное — живой ответ наставника
            if REOPEN_RE.search(text) and len(text) <= 60:
                # Вячеслав: возврат к решённой задаче — recap места остановки и явный выход
                await db.set_status(user_id, "active")
                await db.reset_drift(user_id)
                recap = "Хорошо, возвращаемся: " + open_question
                recap += "\nЕсли нужна другая задача — просто пришли её условие текстом или фото."
                await _save_exchange(user_id, user_text=text, reply=recap, update_question=False)
                return recap
            if DONE_ACK_RE.match(text):
                done_reply = _pick_done_text(await db.get_history(user_id, 4))
                await _save_exchange(user_id, user_text=text, reply=done_reply, update_question=False)
                await db.set_status(user_id, "done")  # Вячеслав: save ставит active — восстанавливаем закрытие
                return done_reply
            # Алёна: ученик закрывает консультацию («спасибо я разобрался») — прощаемся сразу.
            # Раньше это ловил оффтоп-фильтр, и бот отвечал отказом «не мой формат»
            if _wants_topic_close(text):
                close_reply = _pick_variant(CONSULT_CLOSE_TEXTS, await db.get_history(user_id, 4))
                await _save_exchange(user_id, user_text=text, reply=close_reply, update_question=False)
                await db.set_status(user_id, "done")  # Вячеслав: save ставит active — восстанавливаем
                return close_reply
            # Алёна: посторонний разговор после закрытия не поддерживаем — детерминированный отказ
            if _needs_intent_check(text):
                intent = await _ask_if_study(giga, text, last_question, condition=sess.condition)
                if intent == "chat":
                    await _save_exchange(user_id, user_text=text, reply=OFF_TOPIC_DONE_TEXT,
                                         update_question=False)
                    await db.set_status(user_id, "done")  # Вячеслав: save ставит active — восстанавливаем
                    return OFF_TOPIC_DONE_TEXT
            messages = await _build_messages(user_id, text, closed=True)
            fallback = _pick_done_text(await db.get_history(user_id, 4))
            reply = await ask_giga(
                giga, messages, prompts.DONE_TOPIC_INSTRUCTION, fallback=fallback)
            if reply == TECH_REPLY:
                return TECH_REPLY
            clean = _SERVICE_PREFIX_RE.sub("", reply).strip()
            await _save_exchange(user_id, user_text=text, reply=clean, update_question=False)
            await db.set_status(user_id, "done")  # Вячеслав: беседа не переоткрывает задачу
            return clean

        # Вячеслав: 7a. Спор с прежним вердиктом: ученик настаивает на своём ответе →
        # независимый пересчёт с нуля, а не повтор того же утверждения
        disputed = await _handle_dispute(giga, sess, text, open_question)
        if disputed is not None:
            return disputed

        # Вячеслав: 7a-v2. Повтор уже закрытого шага: ученик повторно прислал число, которое
        # мы уже подтвердили → не подтверждаем заново и не бракуем, а двигаем дальше.
        # Но повтор одного и того же числа два раза подряд (а тем более «ответ 2048»)
        # — это не залипание, а настойчивое притязание на итог: прогон 8 ноября 19:20,
        # «ответ 2048» четырежды получал одну и ту же отписку, и задача не могла
        # закрыться никогда. Первый повтор отписываем, дальше — полная проверка (7b)
        final_claim = bool(FINAL_CLAIM_RE.search(text))
        insist_repeat = False
        if not sess.verdict_claim and len(text) <= 80 and not final_claim:
            nums = _numbers_in(text)
            confirmed = nums & await db.get_confirmed_numbers(user_id)
            if confirmed:
                history_tail = await db.get_history(user_id, 12)
                streak = max(_repeat_streak(history_tail, num) for num in confirmed)
                if streak < 2:
                    await db.reset_drift(user_id)
                    # Вячеслав: старый вопрос не подставляем — он уже закрыт, повтор это залипание
                    reply = STEP_CLOSED_PREFIX + STEP_DONE_QUESTION
                    await _save_exchange(user_id, user_text=text, reply=reply, update_question=False)
                    return reply
                insist_repeat = True  # Вячеслав: настаивает — пусть проверяющий решает всерьёз.
                # Андрей, если найдёшь кейс, где это лишнее — пиши в тг, не коммить молча в мою зону)

        # Вячеслав: 7b. Вывод/ответ ученика → отдельная проверка: проверяющий решает задачу
        # молча и классифицирует реплику. Не получилось — обычный ход разбора.
        # Просьбы «объясни/расскажи» и чужие предметы («закон Ньютона») проверкой
        # вывода не являются — отправляем их в 7c, пусть классификатор решает.
        # Настойчивый повтор числа проверяем всегда: он может быть финалом
        topic_request = bool(TOPIC_REQUEST_RE.search(text) or OTHER_SUBJECT_RE.search(text))
        insist = final_claim or insist_repeat
        if insist or ((sess.status == "checking_logic" or _looks_like_answer(text))
                      and not topic_request):
            v = await _check_student_answer(giga, sess, text,
                                            open_question=open_question, insist=insist)
            if v and v["verdict"] != "not_claim":
                return await _apply_verdict(giga, user_id, sess, text, v,
                                            open_question, insist=insist)
            if sess.status == "checking_logic":
                await db.set_status(user_id, "active")  # Вячеслав: проверки не случилось — обычный ход

        # Вячеслав: 7c. Продолжение или смена темы: реплика без маркеров задачи/предмета (и
        # всякая просьба «объясни») проверяется классификатором. Просьба другой
        # темы/предмета → бросаем задачу и помогаем тем, чем занят ученик сейчас;
        # постороннее → строгий отказ и возврат к задаче. Три ухода подряд →
        # предлагаем закрыть задачу
        if _needs_intent_check(text):
            intent = await _ask_if_study(giga, text, last_question, condition=sess.condition)
            if intent == "topic":
                return await _drop_and_switch(giga, user_id, text)
            if intent == "chat":
                drift = await db.add_drift(user_id)
                if drift >= 3:
                    await _save_exchange(user_id, user_text=text, reply=DRIFT_OFFER,
                                         update_question=False)
                    await db.set_status(user_id, "awaiting_close")  # Вячеслав: после save — он ставит active
                    return DRIFT_OFFER
                return _off_topic_reply(sess)
        await db.reset_drift(user_id)  # Вячеслав: учебная реплика — счётчик уходов обнуляем

        messages = await _build_messages(user_id, text)
        fallback = f"Давай продолжим: {open_question}"
        reply = await ask_giga(giga, messages, None, fallback=fallback)
        if reply == TECH_REPLY:
            return TECH_REPLY

        clean = _SERVICE_PREFIX_RE.sub("", reply).strip()
        if clean == fallback:
            # Вячеслав: запасной ответ — точку возврата не трогаем, открытый вопрос остаётся прежним
            await _save_exchange(user_id, user_text=text, reply=clean, update_question=False)
        else:
            await _save_exchange(user_id, user_text=text, reply=clean)
        return clean

    # Алёна: 5c. Режим консультанта — якоря нет, текст короткий без маркеров задачи.
    # «Я разобрался / не надо» закрывает тему сразу, без вызова модели; прочее
    # разбирает классификатор: учёба — консультант, постороннее — строгий отказ
    # (CHAT_MODE=friendly возвращает вежливое свободное общение)
    if _wants_topic_close(text):
        close_reply = _pick_variant(CONSULT_CLOSE_TEXTS, await db.get_history(user_id, 4))
        await _save_exchange(user_id, user_text=text, reply=close_reply)
        return close_reply
    intent = await _ask_if_study(giga, text, last_question)
    if intent == "chat" and cfg.CHAT_MODE != "friendly":
        return _off_topic_reply(sess)
    messages = await _build_messages(user_id, text)
    if intent == "chat":
        instruction = prompts.CHAT_INSTRUCTION
        fallback = "Понял тебя. Если захочешь задачу — пришли условие текстом или фото, разберём по шагам."
    else:
        instruction = prompts.TOPIC_INSTRUCTION
        fallback = "Давай продолжим: пришли условие задачи текстом или фото — и начнём разбираться."
    reply = await ask_giga(giga, messages, instruction, fallback=fallback)
    if reply == TECH_REPLY:
        return TECH_REPLY

    clean = _SERVICE_PREFIX_RE.sub("", reply).strip()
    await _save_exchange(user_id, user_text=text, reply=clean)
    return clean
