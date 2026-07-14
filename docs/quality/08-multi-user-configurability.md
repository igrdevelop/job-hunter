# 08 — Настраиваемость под другого пользователя (candidate-agnostic core)

**Приоритет:** P2 (легче делать ПОСЛЕ 05) · **Усилие:** несколько дней, волнами
**Ветка:** серия `refactor/candidate-config-wave-N`

## Для чего апдейт

Сегодня система формально «настраиваемая» — все личные данные в gitignored
`prompts/` (candidate_profile.md, base_cv_*.md), а конфиг в `.env`. Но чтобы
её реально запустил **другой человек**, этого мало: личность кандидата
захардкожена в **16+ модулях кода** (проверено grep'ом 2026-07-15):

| Где | Что захардкожено |
|-----|-------------------|
| `generate_docs.py` | Имя `"Ihar Petrasheuski"` + шаблон имени файла `Ihar_Petrasheuski_CV_{...}_2026_{lang}` + год |
| `hunter/verdict_refine.py` | `_ALTOROS_FLEXIBLE_PROJECTS` (гибкий работодатель для stretch-раунда) + список «защищённых» работодателей (Atruvia, Fairmarkit, Intel, SII, SolbegSoft) прямо в промпте |
| `hunter/content_qa.py` | Названия ролей/работодателей кандидата для QA-проверок |
| `hunter/filter_config.py` + `hunter/filters.py` | Локационный whitelist (remote/zdalnie/**wrocław**), veto-правила гибрида (Wrocław / weekly Warsaw–Kraków), «немецкий язык = дисквалификатор» (жёсткое допущение о языках кандидата) |
| `hunter/lang_guard.py` | Allowlist польских топонимов; допущение PL/EN как двух языков кандидата |
| `hunter/sources/*` (nofluffjobs, pracuj, theprotocol, thesmartjobs, jobleads, text_utils) | wrocław-токены в прекфильтрах локаций |
| `hunter/apply_api.py` | Карта stack→base_cv файлов (angular/react/ai/fullstack_*) |
| Доменная логика | Doomed gate: «non-EU work authorization = HARD» — допущение про EU-кандидата; «unsupported language» — допущение про PL/EN |

Цели апдейта две (обе — «когда-нибудь», отсюда P2, но проектировать надо уже):
1. Другой пользователь (другое имя, город, стек, языки, работодатели)
   поднимает систему без правки исходников — только конфиги.
2. Сам владелец получает единое место правки этих фактов (сейчас смена
   «гибкого работодателя» = правка промпта в verdict_refine.py).

## Как именно будет происходить

### Центральная идея: `candidate.yaml`

Один машиночитаемый конфиг (gitignored; tracked — `candidate.example.yaml`),
рядом с человекочитаемым `candidate_profile.md` (который остаётся источником
для LLM-промптов):

```yaml
identity:
  full_name: "Ihar Petrasheuski"
  cv_filename_prefix: "Ihar_Petrasheuski_CV"
location:
  home_city: "Wrocław"          # + алиасы wroclaw/vrotslav
  acceptable_hybrid: ["Wrocław"]
  acceptable_weekly_hybrid: ["Warszawa", "Kraków"]
  work_authorization: "EU"       # питает doomed gate
languages:
  spoken: ["pl", "en", "ru"]     # питает language-дисквалификатор (de → skip)
  cv_languages: ["en", "pl"]     # какие CV генерируются/зеркалируются
employers:
  verifiable: ["Atruvia", "Fairmarkit", "Intel", "SII", "SolbegSoft"]
  flexible:
    name: "Altoros"
    period: "2018-2022"
    projects: ["E-commerce", "Insurance", "Healthcare", "Grant Management"]
tracks:                          # см. док 09
  enabled: ["angular"]
  base_cv:
    angular: "base_cv_angular.md"
    react: "base_cv_react.md"
```

Новый модуль `hunter/candidate.py`: ленивый singleton-загрузчик с валидацией
обязательных полей и понятной ошибкой «заполни candidate.yaml по example».
Docker: файл монтируется как prompts/ сейчас.

### Волны миграции (по одной на PR, каждая — чистая замена констант)

1. **Волна 1 — identity**: `generate_docs.py` берёт имя/префикс файла (и год —
   из текущей даты, не константой) из candidate.
2. **Волна 2 — employers**: `verdict_refine.py` (flexible/protected списки в
   промпт подставляются из конфига), `content_qa.py`.
3. **Волна 3 — location/languages**: `filter_config.py` whitelist собирается
   из `location.*`; `filters.py` German-фильтр обобщается до «требуемый язык
   ∉ languages.spoken»; doomed gate authorization — из `work_authorization`;
   `lang_guard` топонимы — из home-страны (или просто конфиг-список).
4. **Волна 4 — sources**: wrocław-токены прекфильтров → из candidate
   (через `text_utils`, чтобы не трогать каждый источник по отдельности).
5. **Волна 5 — документация для «форкера»**: `docs/SETUP_NEW_USER.md` —
   чеклист от клона до первого hunt (env, oauth-туллзы, candidate.yaml,
   prompts/, Telegram-бот).

Явные **не-цели**: multi-tenant (несколько кандидатов в одном инстансе) — не
нужно, инстанс = пользователь; UI для конфига — нет; перевод интерфейса бота —
нет (сообщения остаются как есть).

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `candidate.example.yaml` | **Новый** (tracked): шаблон со всеми полями + комментарии |
| `hunter/candidate.py` | **Новый**: загрузка/валидация/singleton; `pyyaml` в зависимости |
| `generate_docs.py`, `hunter/verdict_refine.py`, `hunter/content_qa.py`, `hunter/filter_config.py`, `hunter/filters.py`, `hunter/lang_guard.py`, `hunter/sources/text_utils.py` (+точечно sources) | Константы → чтение из `candidate.get()` (значения по умолчанию = текущие, поведение владельца не меняется ни на бит) |
| `docker-compose.yml`, `prompts/README.md` | Монтирование/инструкция для candidate.yaml |
| `docs/SETUP_NEW_USER.md` | **Новый**: чеклист развёртывания с нуля |
| `tests/` | Фикстура `fake_candidate`; существующие тесты фильтров получают явный candidate-контекст вместо неявных констант |
| `CLAUDE.md` | Repository Layout + правило «личные факты — только через hunter/candidate.py, не константами» |

## Критерий готовности

- `grep -rniE "ihar|petrasheuski|altoros|wroclaw|wrocław" hunter/ *.py` →
  0 вхождений вне candidate-загрузчика/дефолтов example-файла.
- Смок-тест «второй пользователь»: candidate.yaml с вымышленными данными →
  golden E2E (док 04) генерирует CV с другим именем/городом, фильтры
  используют другой город — без правки исходников.
- Поведение владельца не изменилось: полный suite зелёный без правки ассертов
  (кроме перенацеленных констант).

## Риски

- Расползание «ещё одного конфига»: дисциплина — candidate.yaml только про
  ЛИЧНОСТЬ кандидата; поведение системы (target'ы, интервалы, тогглы) остаётся
  в `.env`/config.py. Граница проста: «сменился человек → меняется candidate.yaml;
  сменился режим работы → меняется .env».
- Волна 3 самая тонкая (фильтры = деньги): каждая замена — с параметризованным
  тестом «старое поведение при дефолтном конфиге бит-в-бит».
