# MEMORY — память задачи dota-kill-clipper

Читать первым при возобновлении работы. Обновлять после каждого значимого шага.

## Суть задачи (одним абзацем)
OBS-плагин для Dota 2: во время записи автоматически сохранять клипы с моими убийствами (10 с до первого убийства,
хвост после последнего, серия = один клип, минимум 20 с, ждать 5 с после последнего убийства серии), раскладывать по папкам
матчей с именами вида `[00.23.37-00.23.57] 1 kill (KDA 3-1-2).mp4`. Корневую папку задаёт пользователь в интерфейсе плагина.
События берём из Dota 2 GSI (рост `player.kills`).

## Принятые решения
| Дата | Решение | Почему |
|---|---|---|
| 2026-09-09 | Репозиторий `C:\Claude code projects\dota-kill-clipper`, ветка `main`, локальный (GitHub — по решению пользователя) | Требование R6; push — зона человека |
| 2026-09-09 | Источник убийств — GSI, не console.log | GSI даёт счётчики kills/deaths/assists, matchid, clock_time каждые 0.1–0.5 с; console.log убийств надёжно не пишет |
| 2026-09-09 | Код детектора GSI брать за основу из `dota-helper-app/d2pt/events.py` и `gsi_config.py` | Проверено в бою, там же поиск пути Dota через реестр Steam |
| 2026-09-09 | Cybersecurity-скиллы (818 шт.) установлены как один индексный скилл `cybersecurity-skills`, а не 818 папок | Иначе список скиллов в каждой сессии раздуется |

| 2026-09-09 | Скрипт внутри OBS на Python 3.12; буфер повтора вместо записи; kills + assists; хвост 10 с (одиночное) / 15 с (серия); окно серии 15 с; ffmpeg `-c copy`; папка матча при входе; корень `C:\Highlights`; публичный GitHub | Ответы пользователя на интервью, см. `SCENARIOS.md` §3 и `PROJECT.md` §2 |

## Открытые вопросы
- Ждём явное «да» на финальную спеку `PROJECT.md` — до него код не пишем.
- Проверить на практике, что OBS 32.2.2 грузит Python 3.12 из `%LOCALAPPDATA%\Python\pythoncore-3.12-64` (первый шаг фазы 2).

## Факты об окружении (проверены 2026-09-09)
- OBS 32.2.2; obs-websocket 5.7.4 встроен, сервер выключен, порт 4455, auth required.
- Профиль «Безымянный»: Advanced, `RecFilePath=D:/Material/Obs записи`, `RecFormat2=mp4`, NVENC h264 tex, 2560×1440,
  `RecSplitFileType=Time`, `RecSplitFileTime=15`, replay buffer выкл (`RecRBTime=20`, `RecRBSize=512`), `FilenameFormatting=%CCYY-%MM-%DD %hh-%mm-%ss`.
- OBS-скриптинг: `data/obs-scripting/64bit/_obspython.pyd` линкуется с `python3.dll` (stable ABI); в документации 32.2.2 заявлено 3.6–3.12;
  Python ставится через Python install manager (`py install <ver>` → `%LOCALAPPDATA%\Python\pythoncore-<ver>-64`, `python` = 3.14 по умолчанию).
  Для OBS поставлен 3.12 рядом (2026-09-09), PATH и дефолт не тронуты. Путь для OBS → Scripts → Python Settings: `%LOCALAPPDATA%\Python\pythoncore-3.12-64`.
- ffmpeg `C:\ffmpeg\bin\ffmpeg.exe` в PATH. Git, gh (Betreazen, логин есть).
- Dota 2: `C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta`; GSI-конфиги: helper (3210, throttle 0.5, только provider+map),
  d2pt (53000, throttle 0.1, player/hero/events и т.д., с токеном). Launch options уже `-console -condebug -conclearlog`.
- Установленные скиллы Claude Code (2026-09-09): superpowers (14), caveman (22, без hooks — плагин-система не задействована),
  humanizer, karpathy-guidelines, tldraw, cybersecurity-skills (индекс). Уже были: ECC 2.2.1, seo, ponytail, spec-pilot, graphify, grill-me.
  В `~/.claude/skills/generated` осталась не-скилл папка из caveman (удаление заблокировано политикой) — безвредна.
- `claude` CLI не в PATH — установка плагинов через marketplace недоступна из сессии, скиллы скопированы папками.

## Журнал
- 2026-09-09 23:13 — постановка задачи (8 пунктов). Разведка, установка скиллов, создание репо и документов, сценарии, вопросы пользователю.
- 2026-09-09 23:34 — ответы на 10 вопросов получены. Спека финализирована, Python 3.12 установлен, репозиторий опубликован. Ждём «да».
