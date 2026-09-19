# jev-consult

Режим: ты даёшь только задачу/план. ИИ-кодер сам смотрит проект и на развилках спрашивает **Jev** (TypeSafe System One). Jev не пишет «сделай вот это» текстом — только Choice / Noul / Score. Кодер переводит ответ в действие или эскалирует тебе.

Работает в любой сессии **Hermes**, **Claude Code (desktop)**, **Codex**, **Grok Build** после `install.py`. В другие агенты (Cursor, Gemini, …) установщик не ставит.

## Что в репозитории

| Путь | Зачем |
| --- | --- |
| `skills/jev-consult/SKILL.md` | Скилл для агента |
| `skills/jev-consult/policy.json` | Единственный файл порогов и шаблонов вопросов |
| `skills/jev-consult/scripts/jev.py` | CLI без зависимостей: `ask` / `decide` / `ping` |
| `scripts/install.py` | Копия скилла только в 4 харнесса |
| `docs/for-agents.md` | Короткий текст для `AGENTS.md` / `CLAUDE.md` |
| `tests/test_jev.py` | Юнит-тесты без живого API |

Jev не помнит прошлые вызовы. В `state` — факты и куски кода, не одни имена файлов. Не спрашивать то, что проверяется инструментом (файл есть, тесты красные, grep).

Политика: высокий confidence / однозначный Choice → делать. Noul `0.5` = «да и нет одинаково», не «средне». На необратимом шаге при низкой уверенности — спросить человека. «Лучший вариант» = max probability, не порог на все опции.

## Установка на этой машине

Ключ только в окружении или локальном `.env`, **не в git**. Для Hermes он уже в `%HERMES_HOME%\.env` как `TYPESAFE_API_KEY`.

```text
python tests/test_jev.py
python scripts/install.py --dry-run
python scripts/install.py
python skills/jev-consult/scripts/jev.py ping
```

Куда копируется скилл: `skills/jev-consult/references/harnesses.md`.

После правки `policy.json` снова запусти `python scripts/install.py`.

Снять:

```text
python scripts/install.py --uninstall
```

## Ключ в другом харнессе

Скопируй `.env.example` → `.env` рядом с проектом **или** задай `TYPESAFE_API_KEY` в окружении того агента. Не клади ключ в чат и не коммить `.env`.

## Сохранить в git

```text
cd Desktop/jev-consult
git init
git add .
git status
git commit -m "Add jev-consult portable coder-Jev mode"
```

Потом `git remote add origin <url>` и push. Проверь, что `.env` в `.gitignore` и в индексе нет ключа.
