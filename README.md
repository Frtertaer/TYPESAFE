# jev-consult

Режим: ты даёшь только задачу/план. ИИ-кодер сам смотрит проект и на развилках спрашивает **Jev** (TypeSafe System One). Jev не пишет «сделай вот это» текстом — только Choice / Noul / Score. Кодер переводит ответ в действие или эскалирует тебе.

После clone **одна команда** подключает режим в любой сессии **Hermes**, **Claude Code (desktop)**, **Codex**, **Grok Build**. В Cursor / Gemini / прочие установщик не ставит.

```text
python scripts/install.py
```

Windows: `install.cmd`. Unix: `sh install.sh`. То же самое, что команда выше.

Ключ `TYPESAFE_API_KEY` человек кладёт сам (`.env.example` → локальный `.env` или env харнесса). В git ключа нет. Инсталлятор пишет `TYPESAFE_API_KEY: set` или `missing`, значение не печатает.

Открыл этот репозиторий как проект — копировать ничего не нужно: `AGENTS.md`, `CLAUDE.md` и `.hermes.md` уже в git.

## Что в репозитории

| Путь | Зачем |
| --- | --- |
| `AGENTS.md` / `CLAUDE.md` / `.hermes.md` | Инструкции агенту из коробки |
| `skills/jev-consult/SKILL.md` | Скилл |
| `skills/jev-consult/policy.json` | Единственный файл порогов и шаблонов |
| `skills/jev-consult/scripts/jev.py` | CLI без зависимостей: `ask` / `decide` / `ping` |
| `scripts/install.py` | Копия скилла только в 4 харнесса |
| `install.cmd` / `install.sh` | Обёртки одной команды |
| `tests/test_jev.py` | Юнит-тесты без живого API |

Jev не помнит прошлые вызовы. В `state` — факты и куски кода. Не спрашивать то, что проверяется инструментом.

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

## Новая сессия

Jev — не демон. Он не стартует сам. Новая сессия подхватит режим, если:

1. скилл лежит в user-dir харнесса (это делает `install.py`), **или** агент открыл этот репозиторий (`AGENTS.md` / `CLAUDE.md` / `.hermes.md`);
2. задача похожа на код/архитектуру (описание скилла);
3. для живого вызова задан `TYPESAFE_API_KEY` в env этого харнесса.

Hermes на этой машине читает ключ из `%HERMES_HOME%\\.env`. Claude / Codex / Grok без своего ключа скилл увидят, API не вызовут.
