Сократ.MAX v2.0
Бот-наставник работающий по сократическому методу или как персональный репетитор
Работает в мессенджере MAX, использует GigaChat.

Команда "Чистый код", МБОУ СОШ №2 г. Ливны
Артём Горков - серверная часть, Docker, БД.
Андрей Кузьмин - работа с GigaChat, промпты, решатели.
Вячеслав Зиборов - обработка сообщений, фильтры.
Алёна Пухленкина - педагогические сценарии.
Капитан команды и руководитель - Павел Вячеславович Сергеев.

Файлы проекта:
main.py - точка входа (FastAPI).
tutor.py - логика диалога.
giga.py - вызовы к GigaChat.
db.py - база данных PostgreSQL.
Dockerfile, docker-compose.yml - сборка образа.
requirements.txt - библиотеки.
certs/ - сертификаты Минцифры РФ.

Как запустить:
1. Скопируйте .env.example в .env и заполните токены (MAX_API_KEY, GIGACHAT_AUTH_KEY, POSTGRES_PASSWORD).
2. Запуск через Docker:
docker compose up -d --build
3. Проверка: curl http://localhost:8000/health

Локальный запуск (Python 3.11):
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

Важно: запускайте только один worker (--workers 1), иначе сломаются лимиты.