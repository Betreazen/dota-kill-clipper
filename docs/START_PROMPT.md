# START_PROMPT — точка входа для сессии реализации

Как использовать: открой Claude Code в `C:\Claude code projects\dota-kill-clipper` и набери `/implement`
(команда лежит в `.claude/commands/implement.md` и подгружает этот файл). Либо скопируй текст ниже целиком в первое сообщение.

---

## Промпт

Ты реализуешь проект **dota-kill-clipper** — Python-скрипт для OBS Studio, который по событиям Dota 2 GSI
автоматически сохраняет из буфера повтора клипы с моими убийствами и ассистами и раскладывает их по папкам матчей.

### 1. Сначала прочитай, в этом порядке
1. `CLAUDE.md` — правила репозитория.
2. `docs/MEMORY.md` — решения, факты об окружении, журнал. Это память задачи.
3. `docs/PROJECT.md` — **утверждённая спека** (статус в шапке файла). Реализуй ровно её, ничего не расширяй.
4. `docs/PLAN.md` — фазы и чекбоксы; работай по ним и отмечай выполненное.
5. `docs/SCENARIOS.md` — сценарии и краевые случаи; каждый из них должен быть покрыт тестом или явно отмечен как «вне рамок».

Интервью и спека уже пройдены. Заново вопросы по спеке не задавай. Если по ходу найдёшь реальное противоречие в спеке —
сформулируй его одним абзацем, предложи решение по умолчанию и продолжай с ним, зафиксировав в `docs/MEMORY.md`.

### 2. Скиллы и агенты, которые использовать
- `spec-pilot` шаги 3–4 (спека уже есть): делегирование только на реально независимые куски, потом самопроверка.
- `test-driven-development` / `tdd-guide`: сначала тест (RED), потом код (GREEN), потом упрощение.
- `karpathy-guidelines` и `ponytail`: минимальный код, стандартная библиотека, без спекулятивных абстракций, без лишних зависимостей.
- `systematic-debugging` при любом падении; `verification-before-completion` перед словом «готово».
- Агенты `code-reviewer` и `python-reviewer` после каждой фазы; CRITICAL/HIGH исправлять до перехода дальше.
- Коммиты: conventional commits, коммит после каждой зелёной фазы. `git push` — только если я явно попросил.

### 3. Что переиспользовать
- `C:\Claude code projects\dota-helper-app\d2pt\events.py` — проверенный парсинг GSI (`_parse`, логика «первый пакет матча базовый»).
- `C:\Claude code projects\dota-helper-app\d2pt\gsi_config.py` — поиск Dota через реестр Steam + `libraryfolders.vdf`, запись cfg.
Копируй только нужные функции в свои модули, без импорта из чужого репозитория.

### 4. Технические ограничения OBS-скриптинга (обязательно)
- Точка входа `obs_dota_kill_clipper.py` рядом с пакетом `killclipper/`; в начале скрипта добавь `os.path.dirname(__file__)` в `sys.path`.
- API OBS (`obspython`) вызывать **только из главного потока OBS**. HTTP-приёмник GSI крутится в отдельном потоке
  (`http.server.ThreadingHTTPServer`) и кладёт пакеты в `queue.Queue`; главный поток забирает их в `obs.timer_add(callback, 100)`.
  Таймеры серий тоже реализуй через этот же тик, а не через `threading.Timer`.
- Буфер: `obs.obs_frontend_replay_buffer_active()`, `obs_frontend_replay_buffer_start()`, `obs_frontend_replay_buffer_save()`,
  путь сохранённого файла — `obs.obs_frontend_get_last_replay()` по событию `OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED`
  (`obs.obs_frontend_add_event_callback`). Пока буфер пишет файл, новые сохранения не запускать — очередь серий.
- Длина/лимит буфера: `obs.obs_frontend_get_profile_config()` → секция `AdvOut`/`SimpleOutput`, ключи `RecRBTime`, `RecRBSize`;
  менять только если текущее значение меньше нужного, и писать об этом в лог. После изменения буфер нужно перезапустить.
- Настройки: `script_properties()` с `obs_properties_add_path` (папка), `obs_properties_add_int`, `obs_properties_add_bool`,
  `obs_properties_add_button` («Установить GSI-конфиг»), `script_defaults()`, `script_update()`, `script_unload()` (остановить HTTP-сервер и таймеры).
- Логи: `obs.script_log(obs.LOG_INFO, …)` плюс файл `%LOCALAPPDATA%\dota-kill-clipper\clipper.log` (`logging.handlers.RotatingFileHandler`).
- Python для OBS: 3.12 в `%LOCALAPPDATA%\Python\pythoncore-3.12-64` (уже установлен). Код должен работать и на 3.14 (тесты гоняются на нём).
- ffmpeg: `C:\ffmpeg\bin\ffmpeg.exe` (искать также в PATH). Резка: `ffmpeg -y -ss <start> -to <end> -i <replay> -c copy <clip>`
  (`-ss` до `-i` для быстрого поиска; границы снаппятся к ключевым кадрам — это принято спекой). Запускать через `subprocess.Popen`
  без ожидания в главном потоке; результат забирать на тике. Исходный файл буфера после успешной резки удалять; при ошибке —
  переименовать в `<имя>_raw.mp4` в папке матча.

### 5. Порядок работы (фазы из `docs/PLAN.md`)
1. **Фаза 1, чистая логика, TDD:** `killclipper/gsi.py` (парсинг пакета → состояние), `series.py` (детектор серий с инъекцией времени),
   `naming.py` (папка, имя клипа, `match.json`), `cut.py` (расчёт границ по `saved_at`, `duration`, `first`, `last`, хвост),
   `dota_paths.py`. Тесты в `tests/`, запуск `python -m pytest -q`. Не трогай OBS, пока эта фаза не зелёная.
2. **Фаза 2, интеграция с OBS:** `obs_dota_kill_clipper.py`. Проверь загрузку: OBS → Tools → Scripts → Python Settings → путь к 3.12,
   добавь скрипт, в Script Log не должно быть traceback. Если OBS сейчас запущен, скажи мне, что его нужно перезапустить, сам не убивай процесс.
3. **Фаза 3, проверка:** прогон в Dota (демо-режим / боты): я сделаю убийства, ты проверишь клипы в `C:\Highlights` по критерию готовности
   `PROJECT.md` §8 (длины через `ffprobe`, имена, `match.json`). Ревью агентами, исправления.
4. **Фаза 4:** `install.ps1`, README с установкой и устранением неполадок, финальный коммит.

### 6. Зоны человека (не делать самому)
Удаление чужих файлов в `C:\Highlights` и записей OBS, завершение процесса OBS или Dota, правка чужих GSI-конфигов
(`gamestate_integration_helper.cfg`, `gamestate_integration_d2pt.cfg`), `git push`, изменения настроек OBS кроме буфера повтора.

### 7. Перед сдачей каждой фазы
Опиши, как проверишь результат, затем реально выполни проверку и покажи вывод (pytest, Script Log OBS, `ffprobe`).
Обнови `docs/PLAN.md` (чекбоксы) и `docs/MEMORY.md` (решения, грабли, журнал с датой). Закончи коротким отчётом: что сделано,
что проверено и как, что осталось.

Начинай с фазы 1.
