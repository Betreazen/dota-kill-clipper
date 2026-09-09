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

## Открытые вопросы (ждут ответа пользователя)
См. `SCENARIOS.md` §3. Ключевые: (1) скрипт внутри OBS + установка Python 3.12 vs внешняя программа через obs-websocket;
(2) replay buffer vs пост-нарезка из записи; (3) точные тайминги серии/хвоста; (4) что считать убийством (только мои);
(5) схема папок/имён; (6) `-c copy` vs перекодирование; (7) папка матча при старте или при первом убийстве; (8) GitHub.

## Факты об окружении (проверены 2026-09-09)
- OBS 32.2.2; obs-websocket 5.7.4 встроен, сервер выключен, порт 4455, auth required.
- Профиль «Безымянный»: Advanced, `RecFilePath=D:/Material/Obs записи`, `RecFormat2=mp4`, NVENC h264 tex, 2560×1440,
  `RecSplitFileType=Time`, `RecSplitFileTime=15`, replay buffer выкл (`RecRBTime=20`, `RecRBSize=512`), `FilenameFormatting=%CCYY-%MM-%DD %hh-%mm-%ss`.
- OBS-скриптинг: `data/obs-scripting/64bit/_obspython.pyd` линкуется с `python3.dll` (stable ABI); в документации 32.2.2 заявлено 3.6–3.12;
  на машине только Python 3.14 (`C:\Users\ampro\AppData\Local\Python\pythoncore-3.14-64`). Настройка пути Python в OBS не задана.
- ffmpeg `C:\ffmpeg\bin\ffmpeg.exe` в PATH. Git, gh (Betreazen, логин есть).
- Dota 2: `C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta`; GSI-конфиги: helper (3210, throttle 0.5, только provider+map),
  d2pt (53000, throttle 0.1, player/hero/events и т.д., с токеном). Launch options уже `-console -condebug -conclearlog`.
- Установленные скиллы Claude Code (2026-09-09): superpowers (14), caveman (22, без hooks — плагин-система не задействована),
  humanizer, karpathy-guidelines, tldraw, cybersecurity-skills (индекс). Уже были: ECC 2.2.1, seo, ponytail, spec-pilot, graphify, grill-me.
  В `~/.claude/skills/generated` осталась не-скилл папка из caveman (удаление заблокировано политикой) — безвредна.
- `claude` CLI не в PATH — установка плагинов через marketplace недоступна из сессии, скиллы скопированы папками.

## Журнал
- 2026-09-09 23:13 — постановка задачи (8 пунктов). Разведка, установка скиллов, создание репо и документов, сценарии, вопросы пользователю.
