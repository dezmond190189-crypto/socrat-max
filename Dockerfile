FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Корневой и промежуточный сертификаты Минцифры идут в комплекте (папка certs/).
# Ими подписан *.max.ru и API GigaChat; во встроенном наборе Python их нет,
# поэтому ставим их в системное хранилище (по нему и сверяется httpx).
COPY certs/russian_trusted_root_ca.crt certs/russian_trusted_sub_ca.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates || echo "update-ca-certificates не прошёл"

# сами модули лежат прямо в корне (файлов немного, в отдельную папку не прятали)
COPY *.py ./

# ВАЖНО: ровно один worker — лимиты и дедупликация живут в памяти процесса.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
