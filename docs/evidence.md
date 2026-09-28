# Evidence — детали измерений

Прод-слепок на README — сокращённая таблица; здесь первичные выводы и методика.
История изменений по PR: схема v2 + IDF-фикс — #19, снятие need-гейта и
Evidence v2 — #20, explicit-consult bypass — #21, floor 0.30 + алиасы — #22,
winner promotion на consult-route — #23/#26, dashboard + drift→PR — #25.

## Калибровка confidence_floor — раунд 3, применена

Verbatim `--calibrate` на кумулятивном логе (433 записи, политика до правки):

```text
calibrate: 154 replayable entries (279 skipped, 0 health-excluded)
threshold           current  recommended    delta
confidence_floor     0.5500       0.1500  -0.4000
strong_pick          0.8500       0.8500  +0.0000
tight_gap            0.0800       0.7250  +0.6450
rates under recommended: escalate=0.026 weak_winner=0.1818 strong_winner=0.3506 none=0.4416
cost: current=0.2922 recommended=0.2078
pick_confidence recorded on 115/154 entries — confidence_floor replayed against the real operand
```

Рекомендация инструмента (0.15) **отклонена**: cost-функция verdict-blind —
минимизирует долю escalate+weak_winner и не различает, был ли подавленный
пик нужен. При 0.15 всплыл бы verdicted-**rejected** пик `mcp_sqlite` на
conf 0.23 («keep JSONL or move to sqlite?» — хотели jev-consult, не sqlite).
По вердиктам 12 floor-suppressed записей r3 распадаются на два класса:

- **item-suppressed (5)** — argmax был реальным скиллом/тулом: accepted
  0.38 / 0.43 / 0.44 (jev-consult ×2, changelog-writer), rejected
  0.07 / 0.23 (mcp_sqlite ×2). Вердикт-чистое окно — **(0.23, 0.38]**:
  взяли **0.30** (max-margin), `escalate_if.confidence_below` синхронно
  (правило P005).
- **none-suppressed (7)** — argmax был `none` (низкоуверенный abstain):
  1 accepted / 6 rejected. Пол корректно блокирует слабые abstains.

Flips при 0.30 по кумулятивному логу: 23 записи — из них 11 всплывают
item-пиками (3 verdicted-accepted + 8 unverdicted r2), 12 остаются `none`.
Оба verdicted-rejected пика (0.07, 0.23) остаются под полом.

## Consult-route: reach → promotion

Плумбинг `route: explicit_consult`: все 33 consult-запроса r3 отроучены с
jev-consult в шортлисте, 0% false-trigger. Первая версия не давала пиков:
`none` выигрывал argmax (0.51–0.92 против 0.08–0.34 у jev-consult).
С #23/#26 на consult-route при `none`-argmax jev-consult продвигается
winner'ом при вероятности ≥ `consult_min_conf` (0.08, lint-инвариант
< `confidence_floor`); ниже — честный `none`. Записи несут
`promoted`/`model_top`, `--acceptance` показывает `consult_promotions`
отдельной строкой — в pick-rate не входят. Проверено живым вызовом:
`promoted: true`, winner `jev-consult` при p=0.45.

## IDF-покрытие после расширения алиасов

В `core_skill_tokens.jev-consult` добавлены только наблюдавшиеся промахи
r2/r3: «torn between», «on the fence», «settle on», «debating whether»,
«two paths», «hold or fold». Все 10 вербатим-промптов промахов теперь
попадают в шортлист (parity-тест `test_observed_miss_phrases_surface`).
Сознательно **не** алиасились «can» и «table» — поодиночке они тащили бы
jev-consult в механические промпты (guard-тест
`test_overbroad_constituents_stay_quiet`). Из 100 записанных
empty-shortlist записей 26 резолвятся в jev-consult; остаток —
преимущественно механические промпты (корректно) плюс хвост ~13
формулировок без алиаса — кандидаты на следующий раунд; словарь растёт
только по наблюдаемым промахам.

## A/B-метрика — методика

Каждый кейс прогоняется дважды — состояние после пика Jev и то же
состояние с baseline-пиком «кодер решил сам» — судья сравнивает итоговый
noul. Baseline-арм строится без Jev-evidence (строки `inspected` с
упоминаниями Jev/`.jev-*` вырезаются — кодер-один их не видел бы); кейс
может задать отдельное состояние полем `ab.state`. `--ab` включён в
weekly `live-eval.yml`; артефакты прогона: `eval-live.json/md`,
`eval-decisions.jsonl` (каждый eval-вызов пишется как routing-запись
`harness=live-eval`), `eval-report.html` и `dashboard.html`
(одностраничные отчёты: status mix, acceptance, latency, недельный тренд).
