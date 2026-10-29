"""Comment lines labelled by what they are, not by what the detector says.

The shapes are taken from live code -- jira-team-analytics and the three
repositories `references/metrics.md` names -- with keys and names replaced,
because those repositories are private. Each row is labelled by a human reading
the line, before the detector ran on it, so a row the detector gets wrong stays
in the sample as a known miss. That is what lets the precision assertions in
`tests/test_comments.py` fail: with every row labelled to match the detector, a
precision bar can never be crossed (the reason `CorpusPrecisionTests` gave up
asserting one).

This is a regression net, not the published precision. The live figures are in
`references/metrics.md`, "Where `comment_noise`'s precision figures come from".
"""

# (comment line, is it a tracker or plan reference)
TICKET_REF_SAMPLE = [
    ("ПОЧЕМУ СЕКЦИЯ, А НЕ ПОПОВЕР (ABC-331036). Раньше это был поповер", True),
    ("Регрессия ABC-329765: раньше её вычитали, а раздел отвечал по всем", True),
    ("The thresholds this payload was computed with (ABC-329052).", True),
    ("ABC-333711 задача 11).", True),
    ("Data hygiene (ABC-327119): matching moved out of the allocation", True),
    ("BFS round 2: frontier {PORTFOLIO-10, PORTFOLIO-11}; the GOAL-1 edge", True),
    ("see issue #10072 for the failing example", True),
    ("fixes #412: the retry loop never ended", True),
    ("PR #88 split this out of the settings endpoint", True),
    ("--- GC-1: overlay also refreshes flow services ---", True),
    ("Branch-coverage net for the refactor (audit part A1-5)", True),
    ("since the LOW-3 fix, which replaced its hand-written query", True),
    ("ABC-329816 IMPORTANT-2: the end date is frozen for", True),
    ("раздел переехал из «Метрик» в «Пульс» целиком (ABC-333523)", True),
    ("ABC-1234: hide the banner for employers", True),
    ("see JIRA-77", True),
    ("Renumbered after CORE-9 landed in develop", True),
    ("ESTIM-9's blocker. Not itself a counted row", True),
    ("`CHANGE-2366` — живой случай практики сразу с двумя мотивами.", True),
    ("тикет #5: пустой ответ ломал выгрузку", True),
    ("Отдельно от ABC-330112: там фильтр по команде, здесь по роли.", True),
    ("Так потерялся SUP-66532 (команда K8S): у заказчика стояло 12", True),
    ("The exact MOB-56765 shape: Feature includes dev task.", True),
    ("link type (EXP-140256 -> EXP-140248). Without this filter those", True),
    ("Перенос PORTFOLIO-59287 25.09 -> 07.10, как он", True),
    ("(Ruling S5-4), and weak the same way its sibling above is", True),
    ("archive/segments/SEG-07-chart-exclusions.md", True),
    ("asserted on (ABC-326267 task 5, NEW-2). Purely additive", True),
    ("ABC-332409 (it now composes ANY step's JQL text)", True),
    ("ISDL-1536: status 'Approved' yet carries a resolution", True),
    # Known misses: the key shape is right, the meaning is not a reference.
    ("True iff value is a single bare Jira key (PROJECT-123), not a JQL.", False),
    ("Stable finding id (FE-1, LR-1, ...) used in anchors.", False),
    ("All three are read off the TIER-2 UNIT, never off an ancestor", False),
    ("RA-2.0-scoped: include employees whose team did not match", False),
    # Clean, and must stay clean.
    ("UTF-8 BOM for Excel", False),
    ("the server returns ISO-8601 in UTC, so string order is time order", False),
    ("SHA-256 hex of the selection signature", False),
    ("The single Q1-2025 entry is the current quarter", False),
    ("see https://jira.example.com/browse/ABC-12 for the upstream bug", False),
    ("TODO(ABC-12): drop after the migration", False),
    ("FIXME ABC-90 the fallback still runs twice", False),
    ("hover color #333 matches the design token", False),
    ("step #2 of the wizard owns the draft", False),
    ("CVE-2021-44228: log4j lookups must stay disabled", False),
    ("encodes as base64 per RFC-4648", False),
]

# (comment line, does it tell the code's history instead of its meaning)
HISTORY_NOTE_SAMPLE = [
    ("Раньше это был `Popover` с якорем на кнопке", True),
    ("Раньше отсюда ехали ДВЕ строки: «вошли как X» и «смотрите как Y».", True),
    ("Набор теперь независим от периода", True),
    ("диалог события теперь читает хронологию", True),
    ("Словарь разрезов и форматирование их чисел переехали в format.ts", True),
    ("Перенесена для взгляда «Я сам»: показатель", True),
    ("не рукописный объект с одним ключом, как было до правки.", True),
    ("Панель переехала из правого столбца вниз на всю ширину", True),
    ("отклик профилем, который раньше был с другим источником", True),
    ("в контроле — как раньше", True),
    ("A `bucket_presets` domain used to live here", True),
    ("builder used to bucket a dev task's parents into tiers", True),
    ("Moved out of the web lifespan when the web/worker split landed", True),
    ("after the same generator was rewritten from scratch", True),
    ("Characterized before the S3776 refactor so the counters", True),
    ("the old code path, no exclusion", True),
    ("was previously a property of the summer window", True),
    ("The previously unknown parent is typed here", True),
    ("Formerly a module-level cache", True),
    ("Since the migration fix the mart is append-only", True),
    # Known misses: a history word used for layout or a present-tense fact.
    ("строка перенесена на вторую, если не влезает", False),
    ("Base URL used to build a link to a pull request", False),
    # Real history the patterns leave out, kept so recall can fall short.
    ("после рефакторинга сюда ходит только воркер", True),
    ("ранее покрашенных колонок больше нет", True),
    # Clean, and must stay clean.
    ("всякий раз, когда окно кончается раньше даты синхронизации", False),
    ("сдвигать было нечего", False),
    ("itself rather than only used to filter", False),
    ("task hits only if it took no longer than X", False),
    ("first night after deploy reads the whole history", False),
    ("tables both answering what is happening now drift apart", False),
    ("the previously stored facts are still intact", False),
    ("Статусы, после которых сборка больше не движется сама.", False),
    ("если запрос был отменён, ответ не пишем", False),
    ("remove after migration", False),
    ("проверяем доступность ранее выбранной услуги", False),
    ("Запрашиваем подсветку чуть раньше попадания во вьюпорт", False),
    ("эта проверка стоит раньше, чем «нет плановой даты»", False),
    ("время достаётся тому, кто начался раньше; второй получает метку", False),
    ("TODO: во время финализации экспа переехать на импорт", False),
    ("Удалить костыль после исправления работы в safari", False),
]
