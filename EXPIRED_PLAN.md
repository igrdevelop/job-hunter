# Plan: Expired Job Detection in apply_agent

## Цель
Перед вызовом LLM проверять текст вакансии на признаки истечения.
Если вакансия истекла — уведомить в Telegram, записать EXPIRED в tracker, выйти.
LLM не вызывается, документы не генерируются, время не тратится.

---

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `hunter/expired_check.py` | Создать — паттерны + функция детекции |
| `apply_agent.py` | Добавить вызов в `main_api()` и `main_cli()` |
| `hunter/tracker.py` | Добавить `add_expired()` — запись строки EXPIRED |

---

## Шаг 1 — `hunter/expired_check.py`

Новый файл. Содержит:
- `EXPIRED_PATTERNS` — список regex на EN + PL
- `is_job_expired(text: str) -> bool` — проверка текста

Паттерны (case-insensitive):

```python
EXPIRED_PATTERNS = [
    # English
    r"\boffer\s+expired\b",
    r"\bthis\s+(?:job\s+)?(?:offer|posting|position)\s+(?:has\s+)?expired\b",
    r"\bjob\s+(?:no\s+longer\s+)?available\b",
    r"\bposition\s+(?:has\s+been\s+)?filled\b",
    r"\bapplication\s+(?:period\s+)?(?:has\s+)?closed\b",
    # Polish
    r"\boferta\b.{0,40}\bwygasła\b",
    r"\bwygasła\b.{0,40}\boferta\b",
    r"\bta\s+oferta\s+(?:pracy\s+)?wygasła\b",
    r"\boferta\s+pracy\b.{0,80}\bwygasła\b",
    r"\bogloszenie\s+wygaslo\b",
    r"\boferta\s+jest\s+nieaktywna\b",
    r"\boferta\s+zostala\s+zakonczona\b",
    # English — "no longer accepting"
    r"\bno\s+longer\s+accepting\s+applications\b",
    r"\bapplications?\s+(?:are\s+)?(?:now\s+)?closed\b",
    r"\bthis\s+(?:job\s+)?(?:listing|role|position)\s+(?:is\s+)?(?:no\s+longer\s+)?(?:active|available)\b",
    # Polish — "pracodawca zakończył zbieranie zgłoszeń"
    r"\bpracodawca\s+zakończył\s+zbieranie\s+zgłoszeń\b",
    r"\bzakończył\s+zbieranie\s+zgłoszeń\b",
    r"\bzgłoszenia\s+(?:na\s+tę\s+ofertę\s+)?(?:zostały\s+)?zamknięte\b",
]
```

Функция:
```python
def is_job_expired(text: str) -> bool:
    """Return True if job text contains expiry indicators."""
    if not text:
        return False
    for pattern in EXPIRED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return True
    return False
```

---

## Шаг 2 — `hunter/tracker.py`

Добавить функцию `add_expired(url: str, company: str = "", title: str = "") -> None`.

Пишет строку в tracker.xlsx с:
- `ATS = "EXPIRED"`
- `Company`, `Job Title` — если известны (пустые если нет, будет прочерк)
- `URL` — кликабельная ссылка
- `Date` — сегодня

Аналогично уже существующим `add_skipped`, `add_react_skipped`.

---

## Шаг 3 — `apply_agent.py`, функция `main_api()`

Место вставки: **после** `fetch_job_text(url)`, **до** `call_llm(...)`.

```python
# Step 1.5 — Check for expired offer
from hunter.expired_check import is_job_expired
if is_job_expired(job_text):
    notify(
        f"⏭ <b>Expired — skipped</b>\n"
        f"🔗 {url}"
    )
    print(f"[apply_agent] EXPIRED — offer no longer active: {url}")
    try:
        from hunter.tracker import add_expired
        add_expired(url)
    except Exception as e:
        print(f"[apply_agent] Warning: could not write EXPIRED to tracker: {e}")
    return
```

---

## Шаг 4 — `apply_agent.py`, функция `main_cli()`

Место вставки: после `job_text = fetch_job_text(url)` в pre-fetch блоке.

```python
if job_text and len(job_text) > 100:
    from hunter.expired_check import is_job_expired
    if is_job_expired(job_text):
        notify(f"⏭ <b>Expired — skipped</b>\n🔗 {url}")
        print(f"[apply_agent] EXPIRED — offer no longer active: {url}")
        try:
            from hunter.tracker import add_expired
            add_expired(url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write EXPIRED to tracker: {e}")
        return
```

---

## Детали реализации `add_expired()`

Смотреть как устроены `add_skipped()` / `add_react_skipped()` в `hunter/tracker.py` —
использовать тот же паттерн открытия tracker.xlsx через openpyxl и дописывания строки.

Колонки tracker.xlsx (из tracker_service.py или читать из файла):
`Date | Company | Job Title | Stack | ATS % | URL | Folder | Sent | Re-application | To Learn`

Значения для EXPIRED строки:
- Date: сегодня
- Company: "" или из аргумента
- Job Title: "" или из аргумента  
- Stack: ""
- ATS %: "EXPIRED"
- URL: url (кликабельная ссылка)
- Folder: ""
- Sent: ""
- Re-application: ""
- To Learn: ""

---

## Порядок реализации

```
Шаг 1 → Шаг 2 → Шаг 3 → Шаг 4
expired_check.py   tracker.py   main_api()   main_cli()
```

## Тестирование

После реализации запустить:
```
python apply_agent.py "https://justjoin.it/job-offer/codepole-principal-engineer-javascript--wroclaw-javascript"
```
Ожидаемый результат в Telegram:
```
⏭ Expired — skipped
🔗 https://justjoin.it/...
```
И строка EXPIRED в tracker.xlsx.
