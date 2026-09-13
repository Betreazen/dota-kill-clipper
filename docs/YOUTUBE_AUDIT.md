# Лист ответов: Google Auth Platform и форма аудита YouTube API

Сайт проекта (GitHub Pages): https://betreazen.github.io/dota-kill-clipper/
Политика конфиденциальности: https://betreazen.github.io/dota-kill-clipper/privacy.html
Условия использования: https://betreazen.github.io/dota-kill-clipper/terms.html

Заполнять только перечисленное. Поля, которых здесь нет, оставить пустыми.
Личные данные (ФИО, адрес, почта) в этот файл не пишутся: они вводятся только в форму.

## 1. Google Auth Platform → Branding

| Поле | Значение |
|---|---|
| App name | `Dota Kill Clipper` |
| User support email | почта аккаунта-владельца из выпадающего списка |
| App logo | не загружать: логотип требует проверки бренда |
| Application home page | `https://betreazen.github.io/dota-kill-clipper/` |
| Application privacy policy link | `https://betreazen.github.io/dota-kill-clipper/privacy.html` |
| Authorized domains | `betreazen.github.io` |
| Developer contact information | та же почта |

Затем Save → Audience → **Publish app** → Confirm.
После публикации в OBS ещё раз нажать «Log in to YouTube»: токен из режима Testing живёт 7 дней.
Google покажет «Google hasn't verified this app» → Advanced → Go to Dota Kill Clipper (unsafe) → разрешить.

## 2. Форма аудита YouTube API

### Раздел 1
- Причина запроса: «Я хочу пройти проверку на соответствие требованиям, чтобы запросить увеличение квоты».

### Раздел 2
- В каком качестве: **Как физическое лицо**.
- Ваше полное имя: ФИО латиницей, как в паспорте.
- Официальное название организации: `от своего имени`.
- Основной сайт: `https://betreazen.github.io/dota-kill-clipper/`
- Страна, адрес, город, регион, индекс: свои.
- Категория: **Игры и киберспорт** (Gaming and Esports).
- Размер/тип организации: **Независимый разработчик или индивидуальный предприниматель**.
- Основное контактное лицо: имя и почта.
- Контактное лицо по техническим вопросам: галочка «Совпадает с основным контактным лицом».
- Контактное лицо по бизнес-вопросам, если есть: та же галочка.

### Раздел 3
- Описание работы, связанной с YouTube (вставить целиком):

```
I am an individual Dota 2 player and the owner of the YouTube channel "Betreazen highlights Dota 2" (@Betreazen_highlights). Dota Kill Clipper is my free, open-source OBS Studio script (https://github.com/Betreazen/dota-kill-clipper) that runs only on my own PC. When I get a kill in my own Dota 2 match, it saves a short clip from the OBS replay buffer and renders a vertical Short with background music (Kevin MacLeod, CC BY 4.0, credited in the description). It then uses the YouTube Data API to upload the video to my own channel as private with a scheduled publish time (one slot every two hours from 08:00 to 22:00) and adds it to my own playlists ("Dota 2", "1 kill video", "2 kill video" and so on). Once a month it reads the view counts of my own public videos older than 30 days and changes clearly underperforming videos to unlisted; it never deletes videos. The tool is used only by me for my own channel. There are no other users, and no data leaves my computer except the API calls to YouTube. I am requesting the audit so that videos uploaded from this API project are no longer locked to private and scheduled publishing works. Expected usage is up to 10 uploads per day and under 1,000 other quota units per day, within the default quota.
```

- Целевая аудитория: **Internal Users** (внутренние пользователи).
- Есть ли представитель Google: **Нет**.

### Раздел 4
- Название API-клиента: `Dota Kill Clipper`.
- Содержит ли слово YouTube: **Нет**.
- Primary Access URL: `https://betreazen.github.io/dota-kill-clipper/`
- Privacy Policy URL: `https://betreazen.github.io/dota-kill-clipper/privacy.html`
- Общедоступен ли клиент: **Да** (открытый исходный код).
- Демо-аккаунт: не заполнять. Галочку-подтверждение под ним поставить (она обязательная).

### Раздел 5
- Сколько номеров проектов: **1**.
- Номер проекта Google Cloud: Cloud Console → Dashboard → карточка Project info → Project number.
- Категория использования: **Video Uploading & Account Management**.
- Вход через аккаунт Google (OAuth 2.0): **Да**.
- Производные метрики и хранение данных: поставить галочку согласия (скрипт хранит отчёты с просмотрами до 36 месяцев).
- Ожидаемый объём: **Fewer than 1,000 requests per day**.
- Файлы:
  - Privacy Policy Screenshots → `C:\Highlights\youtube-audit\privacy-policy.pdf`
  - Homepage Screenshot → `C:\Highlights\youtube-audit\homepage.png`
  - Terms of Service Documentation → `C:\Highlights\youtube-audit\terms-of-service.pdf`
  - Conditional Evidence: свои скриншоты экрана согласия Google при нажатии «Log in to YouTube»
    и панели скрипта в OBS с блоком YouTube (если поле принимает один файл, собрать их в один PDF).
- Эндпоинты: `youtube.channels.list`, `youtube.playlistItems.insert`, `youtube.playlistItems.list`,
  `youtube.playlists.insert`, `youtube.playlists.list`, `youtube.videos.insert`, `youtube.videos.list`, `youtube.videos.update`.
- Общая квота: **No change / Default quota (10k quota points)**.
- youtube.videos.insert, квота в день: `10`; обоснование:

```
Up to 8 scheduled Shorts per day, one per publishing slot from 08:00 to 22:00, plus occasional retries after network errors.
```
