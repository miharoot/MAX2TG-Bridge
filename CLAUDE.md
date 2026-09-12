# MAX2TG-Bridge — контекст для AI-ассистентов

## Назначение

Двусторонний мост MAX ↔ Telegram через форум-топики супергруппы. Каждый MAX-чат соответствует отдельному Telegram topic. Репозиторий полностью переведён на PyMax; собственного WebSocket-клиента больше нет.

Проект основан на [ircitdev/MAX2TG-Bridge](https://github.com/ircitdev/MAX2TG-Bridge), который развивает [Aist/max2tg](https://github.com/Aist/max2tg). Лицензия MIT.

Разные MAX-чаты можно направлять в разные Telegram-супергруппы (`MAX_CHAT_ROUTES`, `/bind`, `/add` в нужной группе) — не только в разные топики одной группы.

## Структура

- `app/main.py` — запуск PyMax и Telegram polling.
- `app/config.py` — конфигурация окружения; QR является auth-flow по умолчанию.
- `app/pymax_auth.py` — фабрика `pymax.Client`/`WebClient`.
- `app/pymax_client.py` — единый клиент MAX: события, чаты, контакты, медиа и безопасное скачивание.
- `app/max_listener.py` — MAX → Telegram, альбомы, fallback вложений, read/reaction форвардинг, уведомления reconnect.
- `app/tg_handler.py` — Telegram → MAX, команды, буферизация альбомов и нативные PyMax attachments.
- `app/tg_sender.py` — Telegram Bot API, топики, мультигрупповой роутинг, media groups, retry и увеличенные timeout.
- `app/resolver.py` — кеш чатов и контактов.
- `app/topics.py` — постоянная карта MAX chat ID ↔ (Telegram chat ID, thread ID).

## PyMax

Переменные: `MAX_PYMAX_AUTH=qr|sms`, `MAX_PHONE` для SMS, опциональные `MAX_2FA_PASSWORD`, `MAX_PYMAX_WORK_DIR`, `MAX_PYMAX_SESSION_NAME`. Сессия находится в `state/pymax` и должна сохраняться между рестартами.

Используются нативные `Photo`, `Video`, `Voice` и `File`. Голосовые TG → MAX перед загрузкой перекодируются через ffmpeg (`imageio-ffmpeg` в зависимостях) — см. патч `upload_voice` в «Известных ограничениях». Telegram-альбом собирается по `media_group_id` и отправляется одним сообщением MAX. MAX-вложения группируются в Telegram media group до 10 элементов. Чаты из `MAX_CHAT_IDS`, отсутствующие в incremental sync, догружаются через PyMax.

Read-события (`on_message_read`) и реакции (`on_reaction_update`) из PyMax смэплены на `MaxReadEvent`/`MaxReactionEvent` в `app/pymax_client.py` — отметки «прочитано» ставятся ✅-реакцией на последнее пересланное сообщение, реакции идут отдельной строкой в топике.

Текст Telegram → MAX пока plain text: публичный `pymax.send_message()` не принимает старые entities напрямую.

## Runtime

- `state/topics.json` — карта топиков, не удалять.
- `state/pymax/*.db` — авторизованная PyMax-сессия, не удалять.
- `logs/max2tg.log` — rotating log.
- Контейнер запускается через `docker compose up -d --build`.

## Тесты

`pytest -q`. Покрываются config, topics, resolver, PyMax auth/client, маршрутизация (включая мульти-группу), медиа, альбомы и reconnect.

## Известные ограничения

- Некоторые входящие voice attachments MAX не содержат доступного URL; мост отправляет fallback-текст.
- `/u/<token>` — личная ссылка человека; pymax понимает только `join/<токен>`, поэтому такая ссылка уходит в `LINK_INFO` (опкод 89), и если MAX отвечает пользователем (`contact`/`user`/`profile`/`contacts`/`users` — какой именно ключ, апстрим не документирует, поэтому принимаются все и ключи ответа логируются), привязывается диалог с ним. Надёжнее — `/add +7…` (поиск по номеру, `search_by_phone`) или `/add <user_id>`. Ссылки вида `max.ru/id<цифры>_gos` — это **публичные хэндлы групп и каналов**, а не профили людей (проверено на живом канале); цифры в них не user_id, поэтому `/add` сначала ищет ссылку среди уже известных чатов (`chat["link"]`) и привязывает найденный чат без запросов. Личный чат привязывается по id человека (`/add <user_id>` или ссылка `max.ru/id<цифры>` как последняя попытка): id чата — это XOR двух user_id (`Client.get_chat_id`), подтверждено на живых DM, но привязка происходит только если MAX подтвердил существование пользователя — на цифры от хэндла канала он отвечает пустым списком.
- Phone/about могут отсутствовать в ответах MAX.
- PyMax использует неофициальный внутренний API MAX и может ломаться при изменениях протокола.
- Реакции MAX → TG не привязаны к конкретному сообщению (нет карты MAX message_id ↔ TG message_id) — идут отдельной строкой в топике.
- `app/pymax_client.py` при создании клиента патчит `upload_voice` целиком (`_patch_voice_upload_user_agent`) — апстримная версия отправляет голосовые так, что MAX их молча отвергает (см. [PyMax#103](https://github.com/MaxApiTeam/PyMax/issues/103)). Установлено перебором против живого сервера, все три части обязательны:
  - **multipart-форма**, а не сырое тело с `Content-Range` (скопировано апстримом с video-загрузки) — на любое сырое тело MAX отвечает `BAD_REQUEST`;
  - **`audioId`**, а не token из video-пайплайна: `VoiceAttachPayload` с пустым token сериализуется в `{_type: AUDIO, audioId: ...}` (в апстриме эта ветка — мёртвый код, т.к. token проставляется всегда), иначе MAX резолвит токен как видео и отвечает `errors.process.attachment.video.not.ready` навсегда;
  - **перекодирование через ffmpeg** в Opus 48 кГц моно (`_VOICE_UPLOAD_FORMATS`): телеграмовский файл — уже Opus в OGG, но MAX бракует его с `AUDIO_VALIDATION_FAILED` (и WebM-ремукс тоже); дело не в контейнере, а в параметрах записи. ffmpeg берётся системный, иначе из пакета `imageio-ffmpeg`; без него отправляется оригинал.
  Ошибки загрузки MAX отдаёт **с HTTP 200** в теле ответа — апстрим его не читает, из-за чего отказ годами выглядел как таймаут обработки. Тело логируется.
- Ещё два патча связаны с тем, что голосовые грузятся через video-пайплайн:
  - `pymax.exceptions.ApiError.__init__` (`_patch_api_error_not_ready_matching`) — обходит баг, из-за которого встроенный retry на `attachment.not.ready` не срабатывал для реальных кодов вида `errors.process.attachment.video.not.ready`.
  - `pymax.dispatch.mapping.EVENT_MAP[Opcode.NOTIF_ATTACH]` (`_patch_voice_ready_resolution`) — обходит баг классификации: уведомление о готовности голосового содержит и `videoId`, и `audioId`, но резолвер проверяет video-сигнал первым и всегда ошибочно принимает голосовое за видео, из-за чего wait в `_process_attachment_error` никогда не резолвится и падает по таймауту (60с).
  Оба патча идемпотентны и безвредны, если апстрим это когда-нибудь починит — можно оставить или убрать.

## Git

## Rules

- Do not modify unrelated files.
- Prefer small, focused changes.
- Preserve existing architecture unless explicitly asked.
- Add tests for bug fixes.
- Do not remove existing tests.
- Do not change `.env` or secrets.
- Never commit secrets.

Before making changes:
- inspect git status
- inspect relevant code
- understand existing implementation

After changes:
- run relevant tests
- show git diff
- do not commit unless explicitly requested.

## Investigation

Do not speculate about code that has not been inspected.
Read the relevant implementation before proposing changes.

## Rules 2

Правила ниже действуют на **каждый** будущий `git push`, без отдельного напоминания от пользователя.

1. Коммитить локально можно свободно и когда угодно.
2. **Перед `git push`** (в любую ветку, включая `main`) — обязательно спросить разрешения у пользователя и дождаться явного подтверждения. Без него не пушить.
3. **После получения разрешения, но перед самим пушем** — прогнать `pytest -q`, если менялся код (`app/`, `tests/`); для чисто документационных правок (README.md, CLAUDE.md и т.п. без изменений в коде) тесты не нужны.
4. **На каждый push, где меняется код `app/`** (не документация) — обязательно, автоматически, без напоминания:
   - бампнуть `VERSION`: баг-фикс → `+0.0.1` (patch), новая фича → `+0.1.0` (minor); при сомнении, фикс это или фича, считать фиксом (patch);
   - если пуш содержит новую фичу — добавить её описание в README (в подходящий раздел `## Возможности`/т.п.) и коротко — в сводку отличий этого форка вверху README;
   - закоммитить бамп версии (и правки README, если были) отдельным коммитом `chore: version X.Y.Z` с кратким описанием, что вошло (как в истории `af7c86a`/`9715e68`/`0b233d7`/`0eca8f2` — ориентироваться на их стиль);
   - запушить ветку;
   - создать git-тег `vX.Y.Z` на этом коммите и запушить тег;
   - создать GitHub Release для тега `vX.Y.Z` с описанием изменений.
5. Если push — только документация (без изменений `app/`/`tests/`), пункт 4 пропускается: версию не бампать, релиз не делать, просто пушить коммит.
6. Если несколько локальных коммитов копятся между пушами — версию бампать один раз при итоговом пуше, отражая в тексте релиза все вошедшие изменения, а не на каждый отдельный коммит.


