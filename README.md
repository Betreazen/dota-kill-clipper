# dota-kill-clipper

Скрипт для OBS Studio: автоматически сохраняет клипы с твоими убийствами и ассистами в Dota 2.
Источник событий — Dota 2 Game State Integration (GSI), источник видео — буфер повтора OBS.
Полная запись не нужна: достаточно открытого OBS с работающим буфером.

Каждое убийство (или серия убийств в одном бою) превращается в клип «10 с до первого события … 10 с после
последнего» (для серии хвост 15 с), минимум 20 с. Клипы раскладываются по папкам матчей:

```
C:\Highlights\2026-09-09 21-35 pudge (match 8954164528)\
    [00.23.27-00.23.47] 1 kill (KDA 3-1-2).mp4
    [00.30.52-00.31.43] 2 kills + 1 assist (KDA 6-1-4).mp4
    match.json
```

## Требования
- Windows, OBS Studio 30+ (проверено на 32.2), режим вывода Advanced или Simple.
- Python 3.12 x64 для OBS (OBS 32 поддерживает 3.6–3.12): `py install 3.12` через Python install manager
  ставит его в `%LOCALAPPDATA%\Python\pythoncore-3.12-64`, не трогая другие версии.
- ffmpeg (`C:\ffmpeg\bin\ffmpeg.exe` или в PATH), рядом должен лежать `ffprobe.exe`.
- Dota 2 через Steam (путь ищется по реестру Steam и `libraryfolders.vdf`).

## Установка
1. Склонируй репозиторий в любую папку без кириллицы в пути (например `C:\tools\dota-kill-clipper`).
2. `powershell -ExecutionPolicy Bypass -File install.ps1` — создаст `C:\Highlights`, поставит GSI-конфиг
   `gamestate_integration_killclipper.cfg` в Dota (порт 3220, токен в `%LOCALAPPDATA%\dota-kill-clipper\token.txt`).
   Тот же конфиг ставит кнопка «Install GSI config into Dota 2» в панели скрипта.
3. OBS → Tools → Scripts → вкладка Python Settings → путь `%LOCALAPPDATA%\Python\pythoncore-3.12-64`.
4. Tools → Scripts → «+» → `obs_dota_kill_clipper.py`. В Script Log должно появиться
   `[killclipper] Loaded (python 3.12.x)` и `GSI listening on 127.0.0.1:3220`.
5. Скрипт сам включает буфер повтора и поднимает его длину до 120 с / 2048 МБ в профиле OBS.
   Если он написал `Replay buffer settings changed … Restart OBS` — перезапусти OBS один раз.
6. Перезапусти Dota 2, если она была открыта: GSI-конфиги читаются при старте игры.

## Настройки (панель скрипта)
Папка клипов · порт GSI · секунды до первого убийства (10) · хвост одиночного (10) · хвост серии (15) ·
окно серии (15) · путь к ffmpeg · учитывать ассисты · кнопка установки GSI · строка статуса
(«GSI: last packet N s ago | Replay buffer: active | Last clip: …», кнопка Refresh status обновляет).

## Как это работает
1. Dota шлёт GSI-пакеты (каждые 0.1 с) на `127.0.0.1:3220`; HTTP-сервер в фоновом потоке кладёт их в очередь.
2. Таймер OBS (100 мс) забирает пакеты: рост `player.kills` / `player.assists` в активной игре = событие.
   Первый пакет матча — базовый, реконнект и поздний старт OBS не дают ложных событий.
3. События ближе 15 с друг к другу — одна серия. Через max(окно, хвост) после последнего события серия закрывается
   → `obs_frontend_replay_buffer_save()`.
4. По событию OBS «буфер сохранён» скрипт узнаёт путь файла, считает границы по реальному времени
   (`ffprobe` даёт длину файла) и в рабочем потоке режет `ffmpeg -ss … -to … -c copy`.
   Границы снаппятся к ключевым кадрам (клип начинается до 2 с раньше). Исходный файл буфера удаляется;
   при ошибке резки остаётся `<имя>_raw.mp4` в папке матча.
5. `match.json` в папке матча: id, герой, steamid, время старта, все события и клипы со статусами.

## Shorts и YouTube
Для игр на Techies каждый готовый клип дополнительно монтируется в вертикальный Short 1080×1920 и публикуется на канале
«Betreazen highlights Dota 2» по расписанию. Настройка Google и входа — в [docs/YOUTUBE.md](docs/YOUTUBE.md), правила — в `docs/PROJECT.md` §10.

- Вертикальная версия кладётся в подпапку `shorts` папки матча, горизонтальный клип остаётся.
- CTA сверху и снизу: файлы 1080×420 с прозрачностью (ProRes 4444 `.mov`, WebM VP9 с альфой, GIF, PNG).
  Короче клипа — зациклятся, другой размер — впишутся в полосу по ширине.
- Музыка: `python tools/fetch_music.py` скачивает ~150 энергичных треков Kevin MacLeod (CC BY 4.0) в `C:\Highlights\music`
  и пишет `credits.json`; атрибуция трека попадает в описание видео.
- Библиотеки Google для Python OBS: `%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe -m pip install -r requirements-youtube.txt`.
- Очередь, расписание и отчёты среза: `%LOCALAPPDATA%\dota-kill-clipper\` (`jobs.json`, `schedule.json`, `reports\`).

## Устранение неполадок
| Симптом | Что проверить |
|---|---|
| В Script Log нет строки `Loaded` / traceback при добавлении | Python Settings указывает на 3.12 x64; путь к скрипту без кириллицы; `killclipper/` лежит рядом со скриптом |
| Статус `GSI: no packets` в игре | Файл `…\dota 2 beta\game\dota\cfg\gamestate_integration\gamestate_integration_killclipper.cfg` есть; Dota перезапущена; порт 3220 совпадает в cfg и панели; брандмауэр не блокирует localhost |
| `Cannot listen on port 3220` | Порт занят другим приложением — смени порт в панели и переустанови GSI-конфиг кнопкой |
| `Replay buffer is not active` | Settings → Output → Recording → Replay Buffer включён; после смены настроек перезапусти OBS; скрипт пытается включить буфер сам каждые 5 с |
| Клип `_raw.mp4` вместо нарезанного | ffmpeg/ffprobe не найдены или упали — путь в панели, подробности в логе |
| Клип короче ожидаемого, в логе `TRUNCATED` | Бой длиннее буфера — увеличь длину буфера в OBS (по умолчанию скрипт ставит 120 с) |
| В статусе `YouTube: not logged in` | Нажми «Log in to YouTube» и выбери канал Betreazen highlights Dota 2; токен другого канала отклоняется |
| Видео на YouTube остаются приватными после даты публикации | Проект Google не прошёл аудит YouTube API — см. docs/YOUTUBE.md |
| Short не появился, в логе `Render failed` | Путь к CTA или музыке, NVENC занят (скрипт сам пробует libx264); после 3 попыток задание помечается `failed` в `jobs.json` |
| Клипы уходят не в ту папку / пустые папки | Папка матча создаётся при входе в игру (так задумано); демо и боты попадают в `… (demo)` |

Лог: `%LOCALAPPDATA%\dota-kill-clipper\clipper.log` (ротация 1 МБ × 3) и Script Log в OBS.

## Разработка
```
python -m pytest -q          # Python 3.14 (код совместим с 3.12)
```
Структура: `obs_dota_kill_clipper.py` (обвязка OBS) · `killclipper/` (`gsi`, `series`, `naming`, `cut`,
`dota_paths`, `clipper`, `render`, `publishing`, `youtube`, `pipeline`, `seo_dictionary.json`) · `tools/` · `tests/` · `docs/` (спека, план, память задачи, сценарии).
Вне стандартной библиотеки — только библиотеки Google для YouTube (`requirements-youtube.txt`). Правила для Claude Code — в [CLAUDE.md](CLAUDE.md).

## Вне рамок
Клипы смертей, командные убийства, оверлеи поверх игры, публикация куда-либо кроме YouTube, Linux/macOS.
