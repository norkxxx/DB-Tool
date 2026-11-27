# SQLi Orchestrator FULL

Автоматизированный SQLi-оркестратор: обход Cloudflare WAF, AI-стратегии (WormGPT
feedback loop), параллельный батч-скан, resume после прерывания, второй эшелон
эксплуатации (Ghauri, sqlninja), экспорт в Burp / DefectDojo / Nessus, живая
строка состояния и логирование с ротацией и gzip-сжатием.

---

## Возможности

| Блок | Что делает |
|---|---|
| **Recon** | GET/POST/Cookie-параметры, baseline ответа, детект Cloudflare |
| **WormGPT Agent** (`--worm`) | Генерирует кастомные тамперы, пейлоады и опции sqlmap под конкретную цель |
| **Feedback loop** (`--worm-loop N`) | Если инъекция не найдена — WormGPT получает отчёт о неудачных пейлоадах и генерирует новую стратегию |
| **Detect DBMS** | MySQL, PostgreSQL, MSSQL, Oracle, SQLite (по ошибкам и time-based) |
| **Fuzz** | Встроенные пейлоады + мутации (space2comment, randomcase, charencode и др.) + тамперы от WormGPT |
| **Exploitation** | sqlmap → (опц.) Ghauri (второй эшелон по WAF) → (опц.) sqlninja (MSSQL post-exploitation) |
| **Батч** (`--list`, `--workers`) | Параллельный скан списка целей |
| **Resume** (`--resume`) | Прерванный батч продолжается с места падения (состояние в JSON) |
| **Логи** | Всё пишется в `logs/scan_*.log`, ротация по 10 МБ, gzip-сжатие, автоочистка старше 30 дней |
| **Статус-строка** | Живой прогресс: `[12/50] ▓▓░░ | воркеры: 8/8 | уязвимо: 4 | чисто: 8 | 00:12:34` |
| **Отчёты** | JSON на цель + сводный HTML-отчёт (`--html`) |
| **Интеграции** | Burp Scanner API, DefectDojo, Nessus (все опциональны) |

---

## Установка

### Требования

- Python 3.9+
- (Рекомендуется) `curl_cffi` — сильнее обход Cloudflare через TLS-имперсонацию Chrome

### Быстрая установка

```bash
pip install -r requirements.txt
```

### Полная установка (со всеми бэкендами)

```bash
pip install -r requirements.txt

# Ghauri — второй эшелон эксплуатации (pip)
pip install ghauri

# sqlninja — MSSQL post-exploitation (Perl)
# Debian/Ubuntu:
sudo apt install sqlninja
# или вручную: https://github.com/Quaker/sqlninja
# (нужен perl в PATH, скрипт sqlninja должен быть в PATH)
```

Проверка, что всё на месте:

```bash
python -c "import curl_cffi; print('curl_cffi OK')"
sqlmap --version
ghauri --version # если нужен --ghauri
sqlninja -h # если нужен --sqlninja
```

### Установка через Docker

```bash
docker build -t sqli-orch.

# одиночная цель
docker run --rm -e WORMGPT_API_KEY="ключ" \
 -v $(pwd)/out:/app/out -v $(pwd)/logs:/app/logs \
 sqli-orch "https://target.com/p?id=1" --worm --worm-loop 2 \
 --log-dir /app/logs

# батч с resume
docker run --rm -e WORMGPT_API_KEY="ключ" \
 -v $(pwd)/out:/app/out -v $(pwd)/logs:/app/logs \
 -v $(pwd)/targets.txt:/app/targets.txt \
 -v $(pwd)/sqli_batch_state.json:/app/sqli_batch_state.json \
 sqli-orch --list /app/targets.txt --workers 8 --resume --html
```

В Docker-образе ghauri и sqlninja уже установлены. Отчёты и логи монтируются как
volume — данные выживают между запусками контейнера (важно для `--resume`).

---

## Переменные окружения

| Переменная | Назначение | Нужна для |
|---|---|---|
| `WORMGPT_API_KEY` | Ключ API WormGPT | `--worm` |
| `DOJO_API_KEY` | API-токен DefectDojo | `--dojo` |
| `NESSUS_ACCESS_KEY` | Access key Nessus | `--nessus` |
| `NESSUS_SECRET_KEY` | Secret key Nessus | `--nessus` |

```bash
export WORMGPT_API_KEY="твой_ключ"
```

---

## Использование

### Одиночная цель

```bash
# Базовый скан (встроенный fuzz + sqlmap)
python sqli_orchestrator.py "https://target.com/page?id=1"

# POST-запрос
python sqli_orchestrator.py "https://target.com/login" \
 --post "user=admin&pass=123"

# С куками и конкретным параметром
python sqli_orchestrator.py "https://target.com/p?id=1&cat=2" \
 --cookie "session=abc123" --param id

# Известная СУБД — пропуск детекта
python sqli_orchestrator.py "https://target.com/p?id=1" --db mysql
```

### С WormGPT (AI-стратегии)

```bash
# Одна стратегия от WormGPT + exploitation
python sqli_orchestrator.py "https://target.com/p?id=1" --worm

# Feedback loop: до 3 итераций, если первая стратегия не сработала
python sqli_orchestrator.py "https://target.com/p?id=1" --worm --worm-loop 3
```

WormGPT получает данные разведки (заголовки, CF-детект, параметры) и возвращает
JSON-стратегию: кастомные тамперы (Python-код), пейлоады, тамперы и опции для
sqlmap. При неудаче `--worm-loop N` отправляет обратно отчёт о проваленных
пейлоадах — модель обязана придумать другой подход.

### Батч-скан

```bash
# Файл целей (по одной на строку, # = комментарий)
python sqli_orchestrator.py --list targets.txt --workers 8 --html

# С прокси-пулом (файл: по одному прокси на строку)
python sqli_orchestrator.py --list targets.txt --proxy proxies.txt
```

### Resume (продолжение прерванного батча)

```bash
# Запуск
python sqli_orchestrator.py --list targets.txt --workers 8 --html
#... Ctrl+C / падение...

# Продолжение: завершённые цели пропускаются, упавшие — ретраятся
python sqli_orchestrator.py --list targets.txt --resume --html
```

Состояние хранится в `sqli_batch_state.json` (меняется через `--state`).
Статусы целей: `pending`, `running`, `done`, `error` (ретраится при `--resume`),
`skipped`. Цели со статусом `running` после Ctrl+C автоматически сбрасываются
в `pending` при следующем запуске.

### Второй эшелон эксплуатации

```bash
# Ghauri — прогон по найденным инъекциям после sqlmap
python sqli_orchestrator.py --list targets.txt --ghauri

# sqlninja — MSSQL post-exploitation (xp_cmdshell, OOB-каналы).
# Автоматически пропускает не-MSSQL находки.
python sqli_orchestrator.py --list mssql_targets.txt --sqlninja

# Всё сразу
python sqli_orchestrator.py --list targets.txt \
 --worm --worm-loop 2 --ghauri --sqlninja --html
```

### Интеграции

```bash
# Burp Scanner REST API (http://host:1337)
python sqli_orchestrator.py --list targets.txt --burp http://127.0.0.1:1337

# DefectDojo
export DOJO_API_KEY="..."
python sqli_orchestrator.py --list targets.txt \
 --dojo https://dojo.example.com --dojo-eng 42

# Nessus
export NESSUS_ACCESS_KEY="..."
export NESSUS_SECRET_KEY="..."
python sqli_orchestrator.py --list targets.txt \
 --nessus https://nessus.example.com:8834
```

Интеграции срабатывают только для целей, где найдены инъекции.

---

## Полный список аргументов

| Аргумент | Описание |
|---|---|
| `url` | Целевой URL (позиционный) |
| `--list FILE` | Файл со списком целей вместо одиночного URL |
| `--post DATA` | POST-данные (`a=1&b=2`) |
| `--cookie STR` | Cookie-строка (`k1=v1; k2=v2`) |
| `--param NAME` | Сканировать только указанный параметр |
| `--db DBMS` | Принудительная СУБД: mysql / postgresql / mssql / oracle / sqlite |
| `--no-sqlmap` | Отключить sqlmap (только встроенный fuzz) |
| `--verbose, -v` | Подробный вывод (все пейлоады — в лог-файл) |
| `--delay SEC` | Базовая задержка между запросами (по умолч. 1.0; при CF — минимум 1.5) |
| `--worm` | Включить WormGPT-агент |
| `--worm-key KEY` | API-ключ WormGPT (или переменная `WORMGPT_API_KEY`) |
| `--worm-loop N` | Число итераций feedback loop при неудаче |
| `--proxy FILE` | Файл со списком прокси для ротации |
| `--html` | Сгенерировать сводный HTML-отчёт `sqli_report.html` |
| `--workers N` | Параллельных воркеров в батче (по умолч. 8) |
| `--resume` | Продолжить прерванный батч |
| `--state FILE` | Файл состояния батча (по умолч. `sqli_batch_state.json`) |
| `--ghauri` | Второй эшелон: Ghauri по найденным инъекциям |
| `--sqlninja` | MSSQL post-exploitation через sqlninja |
| `--burp URL` | Burp Scanner REST API |
| `--dojo URL` | DefectDojo (требует `--dojo-eng` и `DOJO_API_KEY`) |
| `--dojo-eng ID` | Engagement ID в DefectDojo |
| `--nessus URL` | Nessus API (требует ключи в окружении) |
| `--nessus-policy ID` | ID политики Nessus (иначе берётся первая) |
| `--log-dir DIR` | Каталог логов (по умолч. `logs`) |
| `--log-max-mb N` | Размер сегмента лога в МБ до ротации (по умолч. 10) |
| `--log-keep N` | Макс. сжатых сегментов на прогон (по умолч. 5) |
| `--log-days N` | Хранение логов в днях (по умолч. 30) |
| `--version` | Версия инструмента |

---

## Выходные файлы

| Файл | Описание |
|---|---|
| `sqli_report_target_N.json` | Полный отчёт по цели N: находки, стратегия WormGPT, вывод sqlmap/Ghauri/sqlninja, ошибки |
| `sqli_report.html` | Сводный HTML-отчёт по батчу (`--html`) |
| `sqli_batch_state.json` | Состояние батча для resume |
| `logs/scan_YYYYMMDD_HHMMSS.log` | Активный лог (ротация по размеру) |
| `logs/scan_*.log.N.gz` | Сжатые сегменты лога |
| `logs/scan_*.log.gz` | Сжатые завершённые прогоны (>1 МБ) |

Логи старше 30 дней удаляются автоматически при каждом запуске.

---

## Как это работает (пайплайн)

```
1. Recon → доступность, baseline, CF-детект, список параметров
2. WormGPT Agent → стратегия: тамперы + пейлоады + опции sqlmap
3. Detect DBMS → error-based / time-based определение СУБД
4. Fuzz → пейлоады + мутации + WormGPT-тамперы
5. Feedback loop → неудача → WormGPT получает отчёт → новая стратегия
6. Exploitation → sqlmap → [Ghauri] → [sqlninja для MSSQL]
7. Экспорт → JSON + HTML + [Burp / Dojo / Nessus]
```

---

## Советы

- **Cloudflare-цели**: обязательно поставь `curl_cffi` (TLS-имперсонация Chrome)
 и используй `--proxy proxies.txt` — при CF-challenge прокси ротируется автоматически.
- **Тихие цели**: увеличь `--delay` до 2–3, чтобы time-based детект не ловил ложные
 срабатывания на медленных ответах.
- `--verbose` пишет все пейлоады в лог-файл (не в консоль) — консоль остаётся чистой
 для статус-строки.
- Если sqlmap не в PATH, оркестратор пробует `python -m sqlmap`.
- Ghauri и sqlninja полностью опциональны: их отсутствие не ломает скан, просто
 добавляется запись в `errors` отчёта.

---

## Структура проекта

```
.
├── sqli_orchestrator.py # основной скрипт
├── README.md # этот файл
├── requirements.txt # python-зависимости
├── Dockerfile # docker-образ со всеми бэкендами
├──.dockerignore
└──.github/workflows/lint.yml # CI: синтаксис + flake8 + smoke-тесты
```

---

## Правовая заметка

Инструмент предназначен для тестирования на проникновение и аудита **только тех
систем, на которые у вас есть явное разрешение**. Ответственность за использование
лежит на пользователе.