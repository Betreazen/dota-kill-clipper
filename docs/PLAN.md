# PLAN — dota-kill-clipper

Текущая фаза: **2. Интеграция с OBS**.

## Фаза 0 — Интервью и спека
- [x] Установить скиллы из `My skill for claude code` (2026-09-09; см. MEMORY.md, что именно и как)
- [x] Разведка окружения: OBS 32.2.2, ffmpeg, Python 3.14, GSI-конфиги, код d2pt для переиспользования
- [x] Создать репозиторий и документы (README, CLAUDE.md, PROJECT, PLAN, MEMORY, SCENARIOS)
- [x] Продумать сценарии и краевые случаи → `SCENARIOS.md`
- [x] Получить ответы пользователя на вопросы из `SCENARIOS.md` (2026-09-09)
- [x] Обновить `PROJECT.md` до финальной спеки
- [x] Установить Python 3.12 для OBS (`py install 3.12`, дефолт 3.14 не тронут)
- [x] Создать публичный репозиторий `Betreazen/dota-kill-clipper` и запушить документы
- [x] Спека `PROJECT.md` утверждена; написан `docs/START_PROMPT.md` и команда `/implement` для сессии реализации
- [x] Старт фазы 1 в новой сессии (`/implement` в этом репозитории, 2026-09-09)

## Фаза 1 — Чистая логика, TDD (2026-09-09, 57 тестов зелёные на 3.14, импорт на 3.12 проверен)
- [x] Структура: пакет `killclipper/` + точка входа `obs_dota_kill_clipper.py` (фаза 2)
- [x] `gsi.py`: `GsiServer` (ThreadingHTTPServer → queue), `GsiTracker` (пакет → события match/kill/assist, базовый пакет, реконнект, демо)
- [x] `series.py`: `SeriesDetector` с инъекцией времени (окно 15, хвост 10/15, закрытие по max(окно, хвост), flush)
- [x] `naming.py`: папка матча, имя клипа, отрицательное время, `unique_path`, `MatchLog` (match.json)
- [x] `cut.py`: `clip_bounds` (усечение по буферу), `ffmpeg_cmd`, `probe_duration`, `cut_clip` (удаление/`_raw`), smoke на реальном ffmpeg
- [x] `dota_paths.py`: поиск Dota через реестр Steam + `libraryfolders.vdf`, `install_cfg` (идемпотентно)
- [x] Ревью python-reviewer + code-reviewer, исправлены HIGH (baseline при рестарте бот-игры, Content-Length, utf-8)

## Фаза 2 — Интеграция с OBS
- [ ] Включение replay buffer при старте записи, сохранение по завершении серии, получение пути к файлу
- [ ] `cut.py`: расчёт границ по времени сохранения и вызов ffmpeg (`-c copy` или NVENC — по решению)
- [ ] Панель настроек: папка клипов, окно серии, хвост, порт GSI, режим резки, вкл/выкл
- [ ] Логирование в файл + в лог OBS

## Фаза 3 — Проверка
- [ ] Юнит-тесты (pytest) на series/naming/cut-границы
- [ ] Ручной прогон в демо-режиме / игре с ботами по критерию готовности из PROJECT.md §8
- [ ] Ревью (code-reviewer + python-reviewer), исправить CRITICAL/HIGH

## Фаза 4 — Установка и документация
- [ ] `install.ps1`: копирует скрипт, ставит GSI-cfg, подсказывает шаги в OBS
- [ ] README: установка, настройка OBS (буфер, Python), запуск, устранение неполадок
- [ ] Коммит, при желании пользователя — публикация на GitHub
