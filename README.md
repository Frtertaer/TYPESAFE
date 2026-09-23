# jev-consult

Режим: ты даёшь только задачу/план. ИИ-кодер сам смотрит проект. **Jev принимает решения** (TypeSafe System One): подход, keep vs change, библиотека, архитектура, good-enough. Jev не пишет «сделай вот это» текстом — только Choice / Noul / Score. Кодер переводит ответ в действие или эскалирует тебе.

После clone **одна команда** подключает режим в любой сессии **Hermes**, **Claude Code (desktop)**, **Codex**, **Grok Build**. В Cursor / Gemini / прочие установщик не ставит.

```text
python scripts/install.py
```

Windows: `install.cmd`. Unix: `sh install.sh`. То же самое, что команда выше.

Ключ `TYPESAFE_API_KEY` человек кладёт сам (`.env.example` → локальный `.env` или env харнесса). В git ключа нет. Инсталлятор пишет `TYPESAFE_API_KEY: set` или `missing`, значение не печатает.

Открыл этот репозиторий как проект — копировать ничего не нужно: `AGENTS.md`, `CLAUDE.md` и `.hermes.md` уже в git.

## Быстрый старт

```text
git clone <repo-url> && cd TYPESAFE
python scripts/install.py                 # ставит скилл в 4 харнесса
# TYPESAFE_API_KEY=... в .env или env харнесса (значение не печатать)
python skills/jev-consult/scripts/doctor.py        # rc 0 = установка ок
python -m unittest discover -s tests      # прогон всей suite
```

## Что в репозитории

| Путь | Зачем |
| --- | --- |
| `AGENTS.md` / `CLAUDE.md` / `.hermes.md` | Инструкции агенту из коробки |
| `skills/jev-consult/SKILL.md` | Скилл |
| `skills/jev-consult/policy.json` | Единственный файл порогов и шаблонов |
| `skills/jev-consult/scripts/jev.py` | CLI без зависимостей: `ask` / `scaffold` / `decide` / `lint` / `ping` / `env` (`ask --trace` подмешивает план; `env` печатает resolved-конфиг без значения ключа); `--schema` печатает контракт request/response |
| `skills/jev-consult/scripts/question_lint.py` | Статический линт формулировок вопросов J001–J021 (вызывается из `jev lint`); `--schema` печатает контракт ключей request.json |
| `skills/jev-consult/scripts/inventory.py` | Скан установленных skills/plugins/MCP, шортлист; `--schema` печатает контракт ключей payload/item; `--env` печатает resolved-конфиг (task/limit/log/watch/policy) |
| `skills/jev-consult/scripts/inventory_hook.py` | IDF-шортлист, затем один Jev-пикер (fail-open, ничего не ставит) |
| `skills/jev-consult/scripts/peer_fill.py` | Пустой шортлист: Jev выбирает скилл уже на машине, скрипт копирует в текущий харнесс; `--schema` печатает контракт fill-записи, `--env` — резолвнутый конфиг |
| `skills/jev-consult/scripts/catalog_fill.py` | Если локально пусто: поиск Hermes find-skill, Jev выбирает один, inspect, `hermes skills install --yes`; `--schema` печатает контракт fill-записи, `--env` — резолвнутый конфиг |
| `skills/jev-consult/scripts/apply_fill.py` | Если и каталог скиллов пуст: один Hermes-плагин (`--no-enable`) или один официальный MCP. Не npx, не `claude plugin install`; `--schema` печатает контракт fill-записи, `--env` — резолвнутый конфиг |
| `skills/jev-consult/scripts/trace.py` | Файл-память `.jev-trace.json` (план/шаг/попытки) |
| `skills/jev-consult/scripts/compare.py` | Сравнение до/после на липких промптах (`--live`) |
| `skills/jev-consult/scripts/compact.py` | Дефолт — LIVE_FAT (жирный текущий tool result); вырезанная середина сохраняется в spill. Session-drop Tamara только с `--history` (Hermes eval не принял); `--schema` печатает контракт ключей результата/spill; лимиты spill-каталога (`spill_max_files`/`spill_max_bytes`) живут в policy.json, флаги `--spill-max-*` перекрывают |
| `skills/jev-consult/scripts/compact_hook.py` | PostToolUse-хук: урезает жирный `tool_result` (>LIVE_FAT, не ошибка), полный вывод в spill; всегда `{}` fail-open |
| `skills/jev-consult/scripts/decisions.py` | Чтение `decisions.jsonl`: статистика, фильтры (`--status/--harness/--since/--grep/...`), `--watch`, `--jq`, `--schema` печатает контракт ключей записи, `--env` — резолвнутый конфиг (file/source/exists/count/env) |
| `skills/jev-consult/scripts/doctor.py` | Read-only проверка установки по харнессам (skill/hooks/log/ключ) и леджера `.devin/progress.sqlite3` в текущем репо (`--only progress_ledger`); rc 0 = всё ok; `--schema` печатает контракт payload и каталог имён чеков |
| `skills/jev-consult/scripts/policy_lint.py` | Валидация `policy.json` (`--strict`, `--fix` чистит лишние ключи, `--diff OTHER`) |
| `skills/jev-consult/scripts/skill_lint.py` | Sanity SKILL.md-фронтматтера и ссылок на scripts/*.py S001–S009 (`--fix` правит name→dir, `--strict`) |
| `skills/jev-consult/scripts/trigger_lint.py` | Линт триггер-кейсов T001–T011 (`--fix` чинит id-ы и дедуп) |
| `skills/jev-consult/scripts/trigger_eval.py` | Офлайн-оценка покрытия триггеров (`--coverage`, `--uncovered`, `--fail`) |
| `skills/jev-consult/scripts/smoke.py` | Офлайн e2e-прогон без API (`--only`, `--list`, `--junit`, `--verdict`, `--watch`) |
| `skills/jev-consult/scripts/progress.py` | Леджер вклада по этапам: `init` / `lint` / `status` / `history` / `evidence` / `assess` / `invalidate` / `restore` / `review` / `report` / `self-test`; очки только за проверенный чеками diff |
| `skills/jev-consult/scripts/_watch.py` | Общий импорт-хелпер watch-режимов (`dig`/`emit_or_jq`/`cap`/`deadline`/`write_verdict`), без CLI-команд |
| `skills/jev-consult/scripts/progress_core.py` | Движок леджера для `progress.py` и `policy_lint.py` (SQLite + GitEvidence); импортируется, отдельных команд нет |
| `~/.cache/jev-consult/decisions.jsonl` | Журнал решений хука, по строке на промпт (`JEV_CONSULT_LOG=0` выключает, `JEV_CONSULT_LOG=PATH` переадресует) |
| `~/.cache/jev-consult/spill/` | Полные выводы, вырезанные compact (`<sha>.txt`, owner-only каталог, максимум 200 файлов / 256 МБ; `JEV_CONSULT_SPILL=0` выключает, `JEV_CONSULT_SPILL=PATH` переадресует) |
| `scripts/install.py` | Копия скилла только в 4 харнесса |
| `install.cmd` / `install.sh` | Обёртки одной команды |
| `tests/test_jev.py` / `test_inventory.py` / `test_inventory_hook.py` / `test_peer_fill.py` / `test_catalog_fill.py` / `test_apply_fill.py` / `test_trace.py` / `test_compare.py` / `test_compact.py` / `test_compact_hook.py` / `test_skill_evals.py` | Юнит-тесты без живого API |
| `vendor/fast-jev-compaction` | MIT-снимок upstream; рантайм — `compact.py`, не плагин Claude |
| `vendor/awesome-jev` | Снимок каталога (inspect-only) |
| `vendor/typesafeai-cli` | MIT Python CLI; наш клиент остаётся `jev.py` |
| `vendor/awesome-llm-apps-skill-evals` | Apache-2.0 снимок evals-инструментов (inspect-only); рантайм — `skills/jev-consult/scripts/skill_scanner.py` |
| `vendor/jev-skill-suggester` | MIT-снимок upstream (inspect-only); перенесены защиты в `jev.py`/`inventory.py`, их CLI не запускаем |
| `vendor/jevcal` | MIT-снимок upstream (inspect-only); правила линта перенесены в `question_lint.py` |
| `vendor/skill-router` | MIT-снимок upstream (inspect-only); перенесены `strong_pick` и журнал решений |
| `vendor/omp-jev-compaction` | MIT-снимок upstream (inspect-only, TS); перенесён spill в `compact.py` |

Jev не помнит прошлые вызовы. В `state` — факты, план, шаг, счётчик попыток, шортлист. Если кодер не помнит план — читает `.jev-trace.json` и зовёт `ask --trace`. Не спрашивать то, что проверяется инструментом.

Jev **не** убивает галлюцинации «по максимуму» и не факт-чекер. Он режет выдуманные *решения*: кодер не выбирает курс сам. Срыв плана / тупик / «не знаю» / «не помню» / зависание в ответе → `ask` с `on_track` / `next_move` / `stuck_move` / `unknown_move`. Пользователь инструменты не выбирает: хук сам подставляет уже установленные skills/plugins/MCP по тексту промпта (Claude `UserPromptSubmit`, Hermes `pre_llm_call`, Codex `UserPromptSubmit` в `~/.codex/hooks.json` — пока не trusted в `/hooks`, хук не бежит). Grok пишет `.jev-tools.json`. Если sidecar есть, грузить его, иначе fallback:

```text
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/jev.py ask tools.request.json
```

Хук: IDF-шортлист уже установленных, затем **один** вызов Jev (`load_tools` + `need_skill`, 8с, fail-open). Если пользователь сам назвал скилл (`$имя` или точное имя из нескольких слов через дефис), хук берёт его без вызова Jev; Codex-скиллы с `allow_implicit_invocation: false` попадают в шортлист только когда названы явно. Strong pick: p ≥ `strong_pick` (0.85 в `policy.json`) выигрывает даже при неуверенном `need_skill`; все пороги только в `policy.json`. Каждое решение хука дописывается строкой в `~/.cache/jev-consult/decisions.jsonl` (`JEV_CONSULT_LOG=0` выключает; ключ в лог не пишется). Ничего не ставит. Пустой шортлист: агент гоняет `peer_fill.py --from-miss`; если `no_peer` — `catalog_fill.py --from-miss` (один скилл); если `no_catalog` — `apply_fill.py --from-miss` (один Hermes-плагин `--no-enable` или один официальный MCP). npx и `claude plugin install` не ставятся. `--force` нет. Сторожевого процесса нет. Сжатие по умолчанию — только LIVE_FAT текущего жирного результата, не Tamara session-drop. Вырезанная середина сохраняется целиком в `~/.cache/jev-consult/spill/<sha>.txt` — маркер называет файл; каталог owner-only (chmod 700), максимум 200 файлов / 256 МБ, `JEV_CONSULT_SPILL=0` выключает.

Сравнение на одном тестовом запросе (без Jev vs с трассой и Jev):

```text
python skills/jev-consult/scripts/compare.py
python skills/jev-consult/scripts/compare.py --live
```

Политика: высокий confidence / однозначный Choice → делать. Noul `0.5` = «да и нет одинаково», не «средне». На необратимом шаге при низкой уверенности — спросить человека. «Лучший вариант» = max probability.

## После установки

```text
python tests/test_jev.py
python skills/jev-consult/scripts/jev.py ping
```

Куда копируется скилл: `skills/jev-consult/references/harnesses.md`.

После правки `policy.json` снова `python scripts/install.py`.

Снять:

```text
python scripts/install.py --uninstall
```

Опции: `--agents hermes,codex` ставит только в часть харнессов; `--dry-run` показывает план без записи; `--check-key` печатает `TYPESAFE_API_KEY: set|missing` (значение — никогда).

## Новая сессия

Jev — не демон. Он не стартует сам. Новая сессия подхватит режим, если:

1. скилл лежит в user-dir харнесса (это делает `install.py`), **или** агент открыл этот репозиторий (`AGENTS.md` / `CLAUDE.md` / `.hermes.md`);
2. задача про код или план (описание скилла — каждый такой таск);
3. для живого вызова задан `TYPESAFE_API_KEY` в env этого харнесса, в `.env` репо, или в Hermes `.env`.

Кодер обязан вызвать `jev.py ask` до своего keep/change.

## Ревью вклада (opt-in)

`progress.py` — отдельный CLI с локальным SQLite-журналом (`.devin/progress.sqlite3`), не демон и не управление облачной сессией. Включается только на явно согласованный этап: план проверяется и коммитится до `init`, оценка идёт по чистому закоммиченному дереву Git. Шкала zero/small/material/major = 0/1/2/3, контрольная точка — 12 кредитов; пороги и промпты берутся только из `policy.json` и замораживаются внутри этапа. Неизвестное/неуверенное/недоступное/слишком большое — «не оценено», это не ноль. Чекпоинт вызывает ревью, а не завершение проекта: финиш требует свежих проверок и явного `--approve-finish`. Пример плана — `skills/jev-consult/examples/progress-plan.json`, правила — секция «Opt-in contribution review» в `skills/jev-consult/SKILL.md`.

```text
python skills/jev-consult/scripts/progress.py init skills/jev-consult/examples/progress-plan.json
python skills/jev-consult/scripts/progress.py assess reliability client --summary "Describe the verified agreed change"
python skills/jev-consult/scripts/progress.py status reliability
python skills/jev-consult/scripts/progress.py review reliability --reason "Acceptance evidence reviewed" --reviewer "review-reference"
python skills/jev-consult/scripts/progress.py history reliability
```
