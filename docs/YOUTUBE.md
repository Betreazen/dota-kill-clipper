# YouTube: настройка и работа

Восстановление после сбоев описано в [RELIABILITY.md](RELIABILITY.md).
Статус `upload_unknown` (в панели `checking`) означает неизвестный результат загрузки: слот сохраняется, видео ищется на канале
и загружается заново, только если его там нет. Квоты и лимит канала ставят загрузку на паузу, видео ждут в очереди.

Загрузчик привязан к каналу **Betreazen highlights Dota 2** (`@Betreazen_highlights`). После входа скрипт сверяет
ID канала из токена с ID этого handle; токен другого канала (личного или другого бренд-аккаунта) отклоняется и удаляется.

## 1. Проект Google (один раз)
1. [console.cloud.google.com](https://console.cloud.google.com) → создать проект → APIs & Services → Library → включить **YouTube Data API v3**.
2. Google Auth Platform → Branding и Audience: заполнить поля по листу [YOUTUBE_AUDIT.md](YOUTUBE_AUDIT.md), затем **Publish app** (статус In production).
   В статусе Testing Google выдаёт refresh token только на 7 дней. После публикации один раз заново нажать «Log in to YouTube».
   Экран «Google hasn't verified this app» для личного использования пропускается: Advanced → Go to Dota Kill Clipper.
   Сайт и политика конфиденциальности проекта: https://betreazen.github.io/dota-kill-clipper/ (GitHub Pages из папки `docs`).
3. Credentials → Create credentials → OAuth client ID → тип **Desktop app** → скачать JSON.
   Хранить вне репозитория (например `E:\Highlights\json-login\`). Путь указать в панели скрипта: «Google OAuth client JSON».
4. Библиотеки для Python OBS:
   `%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe -m pip install -r requirements-youtube.txt`.

## 2. Аудит
По документации Google видео из проекта без аудита могут оставаться приватными. Проверено 2026-09-13: на этом проекте
запланированное видео стало публичным в назначенное время ещё до одобрения аудита. Аудит подан на всякий случай. Нужно подать форму *YouTube API Services — Audit and Quota Extension Form*
(support.google.com/youtube/contact/yt_api_form). Готовые ответы и файлы для загрузки — в [YOUTUBE_AUDIT.md](YOUTUBE_AUDIT.md).
Ответ Google придёт на контактную почту из формы.

## 3. Вход
1. В панели скрипта включить «Upload Shorts to YouTube», выбрать JSON клиента.
2. Нажать **Log in to YouTube** → откроется браузер → выбрать аккаунт и канал **Betreazen highlights Dota 2** → разрешить доступ.
3. В Script Log: `YouTube: logged in to Betreazen highlights Dota 2`; в статусе панели `YouTube: logged in`.
   Токен: `%LOCALAPPDATA%\dota-kill-clipper\youtube-token.json` (не в репозитории).

Проверка без OBS (из папки проекта, Python 3.12):
```
python -m killclipper.youtube login  "E:\Highlights\json-login\client_secret_….json"
python -m killclipper.youtube review "E:\Highlights\json-login\client_secret_….json"   # только печатает отчёт среза
```

## 4. Как публикуется видео
1. Готов горизонтальный клип → сразу готовится текст (язык ru/en случайно) и задание попадает в `jobs.json`.
2. Рабочий поток монтирует Short в `…\shorts\`, затем загружает его как `private` с `publishAt`
   в ближайший свободный слот 08:00, 10:00 … 22:00 (Europe/Minsk, не ближе 15 минут). Занятые слоты — в `schedule.json`.
3. Видео добавляется в плейлист «Dota 2» и, если есть убийства, в «N kill video». Плейлисты создаются сами, публичные, с описанием.
Описания плейлистов SEO-шные (RU + EN, ключевые слова, хештеги). Обновить их на канале после правки словаря:
   `python -m killclipper.youtube playlists "E:\Highlights\json-login\client_secret_….json"`.
4. Ошибка загрузки (сеть, квота) → повтор с паузой до 1 часа, до 10 попыток; слот освобождается. Без входа задания ждут.

## 5. Ежемесячный срез
При первом запуске OBS в новом месяце (если есть вход): берутся публичные видео старше 30 дней, считаются максимум, минимум
и медиана просмотров. Видео с просмотрами меньше половины медианы переводятся в «Доступ по ссылке» (unlisted), если в срезе
не меньше 8 видео. Ничего не удаляется. Отчёт: `%LOCALAPPDATA%\dota-kill-clipper\reports\review-YYYY-MM.json`.

## 6. Квоты API (по документации Google на 2026-09)
- `videos.insert` — отдельный лимит 100 загрузок в сутки.
- Остальное из 10 000 единиц в сутки: добавление в плейлист 50, создание плейлиста 50, `videos.update` 50, чтение списков 1.
  Восемь видео в день с двумя плейлистами — около 850 единиц.

## 7. Музыка
`python tools/fetch_music.py` скачивает подборку треков Kevin MacLeod (incompetech.com, CC BY 4.0) и пишет `credits.json`.
Атрибуция выбранного трека автоматически добавляется в конец описания — без неё лицензия нарушается.
