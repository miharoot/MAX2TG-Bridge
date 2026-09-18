# История изменений

Собрано из git-истории: для каждой версии — коммиты, вошедшие в неё, и
описание из коммита `chore: version X.Y.Z`. До версии 0.4.11 то же самое
публиковалось как GitHub Releases; этот файл — их замена, чтобы история
версий жила в репозитории, а не только на GitHub.

Отсчёт ведётся от точки форка `0c011e1` (последний коммит апстрима
[ircitdev/MAX2TG-Bridge](https://github.com/ircitdev/MAX2TG-Bridge)).

## 0.8.3 — 2026-09-18

- Текущая стабильная.
- отметка «прочитано» в MAX после ответа в топик больше не пропадает
  после рестарта: id последнего сообщения чата, которому она
  адресуется, теперь поднимается из базы outbox при старте и
  пополняется сообщениями из истории, а не живёт только в памяти
- ✅ по чужому прочтению встаёт на ваше сообщение, а не на последнее
  чужое: якорь пишут обе стороны — и пересылка MAX → Telegram, и
  подтверждённая отправка из топика, — и хранится он в базе, так что
  переживает рестарт (раньше после перезапуска ставить ✅ было не на что)
- реакция в Telegram отправляется с тем же повтором, что и остальные
  вызовы: одного таймаута прокси хватало, чтобы ✅ потерялась навсегда,
  потому что событие чтения MAX не повторяет

## 0.8.2 — 2026-09-18

- отметки «прочитано» стали видны в логе в обе стороны: событие чтения от
  MAX печатается вместе с сырым payload, а следом — что с ним сделали
  (снято прочтение, это наш собственный маркер, нечего отметить реакцией,
  реакция поставлена или Telegram её не принял)
- со стороны Telegram → MAX в лог идёт и причина, по которой отметка не
  ушла: нет известного id последнего сообщения чата или MAX её отклонил
- отказ Telegram в реакции поднят с debug на warning — на обычном уровне
  логирования он был не виден, и «MAX не прислал отметку», «отметку
  отбросили» и «Telegram не принял реакцию» выглядели одинаково никак
- поведение не менялось, только видимость: это подготовка к разбору того,
  почему отметки «прочитано» не появляются

## 0.8.1 — 2026-09-18

- подхват пропущенного после переподключения больше не возвращает в
  Telegram сообщения, отправленные из самого Telegram: MAX отдаёт их в
  истории чата наравне с обычными, а защита от эха стояла только на живом
  событии, поэтому своё же сообщение приезжало обратно в свой топик
- идентификаторы отправленных мостом сообщений теперь хранятся в базе
  outbox (живут неделю), а не только в памяти процесса — прежняя карта
  `cid` не переживала рестарт и не совпадала с копией из истории
- пропущенное своё сообщение всё равно двигает отметку «докуда
  переслано», иначе окно подхвата росло бы бесконечно и тянуло его снова
  на каждом переподключении

## 0.8.0 — 2026-09-13

- служебные события MAX читаются словами: «➕ добавил(а) в чат: Иван
  Петров», «🚪 вышел(а) из чата», «✏️ переименовал(а) чат» — раньше всё
  это приходило как «[нетекстовое сообщение]», потому что текста в таком
  сообщении нет, есть только вложение CONTROL. Незнакомое событие
  печатается как есть, чтобы его можно было опознать по логу

## 0.7.1 — 2026-09-13
- догрузка истории (`/catchup`, `MAX_BACKFILL`, `MAX_CATCHUP`) наконец
  работает: MAX отдаёт историю без указания чата в каждом сообщении —
  чат был в запросе, — и мост отбрасывал их все, докладывая «MAX не
  вернул ни одного сообщения», хотя ответ был на восемь килобайт
- перезапрос списка в `/list` снова умеет удалять: сравнение шло со
  снимком, который строится из накопительного кеша pymax, а тот не
  забывает ни один когда-либо виденный чат
- `/del_max` на чате, в котором MAX нас не числит участником
  (`chat.exit.not.active.user`), убирает его из списка вместо показа
  сырой ошибки

## 0.7.0 — 2026-09-13

- новая команда `/catchup` — внутри топика перетаскивает из MAX последние
  сообщения этого чата (сколько — `MAX_BACKFILL_LIMIT`). Работает только
  при включённой `MAX_BACKFILL`; уже пересланное придёт повторно
- `/list` перед выводом запрашивает у MAX актуальный список чатов, и чат,
  покинутый с телефона, больше не остаётся в списке. Удалять что-либо
  разрешено только полному ответу: если MAX отдал список не целиком,
  мост лишь добавляет новое и пишет об этом в ответе
- чат, покинутый через `/del_max`, сразу исчезает из `/list`, а чат,
  добавленный через `/add`, сразу появляется — раньше и то и другое
  ждало переподключения
- в релизном workflow тег создаёт сам `gh` вместо отдельного `git push`

## 0.6.0 — 2026-09-13
- подхват пропущенного (`MAX_CATCHUP`) охватывает все привязанные чаты с
  первого же запуска: раньше чат участвовал в нём только после того, как
  в нём проходило хотя бы одно сообщение на новой версии, и тихий чат
  оставался непокрытым сколько угодно долго. Теперь при первом
  подключении с пустой таблицей отметок каждый привязанный чат
  помечается его последним сообщением — по времени из снапшота MAX, а не
  по часам контейнера. То, что пришло до этого первого запуска, остаётся
  только в MAX

## 0.5.0 — 2026-09-13

- мост больше не ждёт Telegram, прежде чем подключиться к MAX: раньше
  недоступный прокси держал запуск, и сообщения, пришедшие в MAX за это
  время, не принимались вовсе; теперь Telegram поднимается в фоне, а всё
  из MAX копится в очереди доставки
- сообщения, отправленные в Telegram, пока мост лежал, больше не
  выбрасываются при старте — Telegram хранит их около суток и отдаёт при
  подключении
- новый топик может «догонять»: при `/bind` и `/add` в него подтягиваются
  последние сообщения чата из MAX (`MAX_BACKFILL`, потолок на чат —
  `MAX_BACKFILL_LIMIT`)
- подхват пропущенного: при подключении мост забирает то, что пришло в
  привязанные чаты, пока он лежал (`MAX_CATCHUP`, `MAX_CATCHUP_LIMIT`).
  Отметка «докуда переслано» по каждому чату хранится в `state/outbox.db`
- обе опции выключены по умолчанию
- пауза между повторами доставки: 2 мин → 4 → 8, потолок 10 минут (была
  1 минута с потолком в час)

## 0.4.11 — 2026-09-13

- правки документации и тестов.

## 0.4.10 — 2026-09-13

- мост больше не падает, если Telegram (или SOCKS5-прокси) недоступен
  при старте: повтор с задержкой 10, 20, 30… до 60 секунд, и для
  первого getUpdates тоже; неверный токен по-прежнему останавливает

- fix: start the startup retry at 10s
- fix: wait for Telegram at startup instead of dying

## 0.4.9 — 2026-09-13

- «Избранное» больше не подменяет названия групп и каналов: признак —
  нулевой id (my_id ^ my_id) и тип DIALOG, а не одинокий участник в
  неполном списке от MAX
- имя чата MAX рядом с id в подтверждениях /del и /del_max и в ошибке
  отказанного вступления
- /list показывает, в какой Telegram-группе лежит топик: «группа\топик #N»

- style: /list writes the binding as "группа\топик #N"
- feat: show a MAX chat's name next to its id
- fix: id 0 identifies "Избранное" on its own
- fix: only a real dialog can be "Избранное", and /list names the TG group

## 0.4.8 — 2026-09-12

- `/del_max` — выйти из чата на стороне самого MAX: покинуть группу,
  отписаться от канала или удалить диалог. Только в основной группе, с
  подтверждением, топик Telegram при этом не трогается. Диалог удаляется
  только у себя: в pymax у этого вызова по умолчанию стоит «удалить у
  всех», что стёрло бы переписку и у собеседника, — параметр прибит и
  наружу не выведен. «Избранное» команда удалять отказывается;
- `/del <id>` — удалить топик, не заходя в него: по номеру топика или по
  id чата MAX. Нужно как раз тогда, когда топик привязан к чату, из
  которого аккаунт уже вышел, — такого чата в /list нет, и кликать
  не по чему;
- `/list` разделён: черта перед каждым разделом и пустая строка между
  чатами — после появления ссылок записи стали в три-четыре строки и
  сливались в сплошную стену.

- feat: /list separates its sections and its chats
- feat: /del can name a topic instead of being inside it
- feat: /del_max leaves a chat on the MAX side

## 0.4.7 — 2026-09-12

- «Избранное» (чат с самим собой в MAX) больше не безымянное: в /list и
  в заголовке топика оно так и называется, а топик получает карточку с
  пояснением. Раньше всё это ломалось об одно: каждое место, где
  называется личный чат, ищет собеседника, а в чате с собой его нет —
  отсюда «(без названия)», имя топика по вашему собственному имени и
  молча пропущенная карточка. Отправка туда работала и до этого;
- справка /help описывала /add как «ссылка на группу/канал» и молчала
  про остальные пять форм — теперь перечислены все;
- README: таблица форм /add, раздел про «Избранное» и две строки в
  сводке отличий форка.

- feat: bridge MAX's saved messages as a named chat, and spell /add out in help

## 0.4.6 — 2026-09-12

- `max.ru/id<цифры>_gos` оказалась публичной ссылкой канала, а не
  профилем человека: /add принимал цифры из неё за user_id, привязывал
  несуществующий диалог и называл топик числом, пока сам канал
  оставался непривязанным. Теперь ссылка сперва ищется среди уже
  известных чатов и привязывает найденный — без запросов и вступления;
- диалог с человеком никуда не делся, но открывается по слову MAX, а не
  по виду ссылки: только если запрос подтверждает такого пользователя.
  Добавлены `/add +79991234567` (поиск по номеру), `/add <id
  пользователя>` и личная ссылка `max.ru/u/<токен>` — её токен читает
  сам MAX через LINK_INFO;
- `/add <id чата>` привязывает группу или канал сразу, как /bind;
- попутно: первый аргумент считался ссылкой всегда, из-за чего ветки с
  id и телефоном были недостижимы;
- подсказка /add и докстринг больше не обещают удалённое поведение.

Версия поднята как patch по решению автора, хотя формально это новые
возможности.

- feat: /add takes a chat id too, and stops mistaking ids for links
- feat: /add reaches a person by phone number, and through their personal link
- feat: /add opens a dialog with a person again, on MAX's word rather than a guess
- docs: /add's usage text still promised the profile links it no longer has
- fix: max.ru/id<digits>_gos is a channel's own link, not a person's profile

## 0.4.5 — 2026-09-12

- карточка топика снова прикрепляется: она уходила в никуда, потому что
  запрос контактов к MAX не имел таймаута, а MAX на некоторые профили
  не отвечает вовсе — карточка не задерживалась, её просто не было, и
  никто об этом не сообщал. Запрос ограничен по времени в одном месте,
  под карточкой, резолвером имён и подбором заголовка;
- имя топика: убрана заглушка из user_id, которую я внёс в 0.4.4 —
  ниже по течению она выглядела настоящим именем и глушила и поиск
  имени в /add, и переименование топика, когда MAX наконец назовёт
  человека;
- отказ MAX вступить в чат теперь называет id найденного чата, чтобы
  после вступления из приложения можно было сразу сделать /bind.

Проверено против v0.2.2: post_topic_intro, _topic_title_for_message,
ensure_topic, _looks_numeric, chat_name, _extract_name_from_contact и
_peer_id_in_dm с тех пор не менялись ни на строку; resolver.py и
topics.py — тоже.

- fix: an unanswered contact lookup swallowed the topic's intro card

## 0.4.4 — 2026-09-12

- /add на профильную ссылку больше не зависает: MAX может вовсе не
  ответить на запрос о человеке вне контактов (в логе видно, как запрос
  ушёл и ответа не было), и команда ждала его бесконечно, не отвечая
  пользователю ничем. Запрос ограничен по времени и стал
  необязательным — id личного чата считается локально;
- имя топика берётся так, как оно в MAX: сначала уже известное имя из
  снапшота/контактов (без единого запроса), затем запрос для
  незнакомца; если MAX не дал имени вовсе, топик переименуется при
  первом сообщении;
- /add снова означает «вступить»: резолв ссылки через LINK_INFO
  подменяет отказавший join только когда MAX перечисляет нас среди
  участников чата, то есть вступать уже не нужно. Иначе докладывается
  ошибка join, а не привязывается чат, в который бот не входил;
- попутно подтверждено на живых данных, что id личного чата — это XOR
  двух user_id: оба DM из снапшота совпали точно.

- fix: /add must join before it binds, and name the topic the way MAX does
- fix: /add could hang forever on a profile link, and gave up too early on a join link

## 0.4.3 — 2026-09-12

- /add принимает ссылки на профиль (max.ru/id<цифры>): раньше любая
  max.ru-ссылка уходила в join_group/join_channel, которые ищут в ней
  join-токен, — за ссылкой на человека присоединяться не к чему, и
  команда могла только ошибиться. Теперь id личного чата вычисляется
  локально (XOR двух user_id), у сервера спрашивается только
  существование пользователя; его имя идёт в заголовок топика;
- ссылки без join-токена и без числового id уходят в MAX через
  LINK_INFO, вместо отказа на стороне pymax;
- /list печатает id, веб-ссылку и приглашение в чат (когда MAX его
  прислал) моноширинными строками — чтобы выделять и копировать, а не
  кликать по слову «открыть»; ссылки ниоткуда не перевыпускаются.

Версия поднята как patch по решению автора, хотя формально это новые
возможности.

- feat: /list shows the chat's MAX invite link when there is one
- feat: /add accepts profile links, and /list gives the URL as copyable text

## 0.4.2 — 2026-09-12

- /bind больше не падает с NameError: 7 сентября из него выпали две
  строки разбора аргументов, и с тех пор команда молча не делала
  ничего — ни топика, ни сообщения об ошибке; подсказка по
  использованию при этом оказалась недостижимым кодом;
- появились тесты на командные обработчики, которых не было ни одного,
  включая smoke-проверку, что ни одна команда не падает и не молчит;
- TG_UPLOAD_MB заработала: файл больше лимита не уходит в Bot API, а
  объявляется в топике текстом; раньше переменная читалась из окружения
  и не использовалась, единственным потолком был MAX_DOWNLOAD_MB;
- DEBUG_DUMP_JSON удалена — код дампа исчез при переходе на PyMax,
  флаг с тех пор не читал никто;
- убран код, до которого ничего не доходит: broadcast_photo (её вызов
  намеренно заменили в 829504c), дублирующий импорт в pymax_auth,
  неиспользуемые quote и voice.size(), лишний параметр _header.

- refactor: remove code nothing reaches
- fix: make TG_UPLOAD_MB real and drop DEBUG_DUMP_JSON, which was not
- fix: /bind raised NameError instead of binding anything

## 0.4.1 — 2026-09-12

- отметка «прочитано» из MAX больше не срабатывает на собственном
  ридмаркере: id сравниваются как строки (в одних payload'ах они
  приходят числом, в других — строкой, и рассогласование типов
  открывало guard наружу);
- потолок паузы между повторами доставки поднят с 10 минут до часа;
- строка очереди с нечитаемым payload удаляется, а не пропускается на
  каждом свипе;
- явный отказ MAX от содержимого (голосовое, которое он не принимает)
  один раз помечается в топике и снимается с повтора; всё остальное —
  сеть, таймауты, недоступность MAX — по-прежнему остаётся в очереди;
- очередь доставки покрыта end-to-end тестами против реального SQLite.

- fix: stop re-delivering what MAX will never accept, and throttle the rest
- test: exercise the outbox end to end against a real SQLite file
- fix: compare read-marker ids as strings so the own-read guard holds

## 0.4.0 — 2026-09-12

Unsupported Telegram attachments now get an explicit warning in the
topic instead of disappearing. An attachment type outside the media
handler's filter previously reached no handler at all — no upload, no
error, nothing in the topic — which is how missing video_note support
stayed invisible for so long.

Also adds end-to-end test coverage for files in both directions, which
the check behind this release found to be working but untested on the
TG → MAX side.

Minor rather than patch: this is new user-visible behaviour.

- feat: warn in the topic instead of dropping unsupported attachments

## 0.3.2 — 2026-09-12

Telegram round video messages (video_note) now reach MAX. They used to
vanish silently: VIDEO_NOTE was missing from the media handler's filter,
so the update never reached the handler at all — no upload, no error,
not even the unsupported-attachment warning in the topic. They are sent
through pymax's VideoNote, so they stay round video messages in MAX
rather than becoming plain videos.

- fix: forward Telegram round video messages to MAX
- docs: correct the voice-sending section in README

## 0.3.1 — 2026-09-12

Voice messages from Telegram now actually arrive in MAX — confirmed
against the live server, which accepted the upload and returned the
stored message with a playable audio URL.

Settles the upload on the re-encode MAX accepts (48 kHz mono Opus) and
drops the variants now known to fail — both WebM shapes and m4a/AAC —
which were costing two rejected round-trips and two burned upload slots
per voice message. The container turned out to be irrelevant: MAX
refuses Telegram's Opus-in-OGG and a WebM remux alike, so what it
objects to is how Telegram encodes its recordings.

- fix: settle voice uploads on the re-encode MAX accepts

## 0.3.0 — 2026-09-12

Voice messages from Telegram now actually reach MAX. Two production
sweeps established what MAX wants: a multipart form rather than the raw
body with a Content-Range that pymax copied from its video upload (every
raw spelling answers BAD_REQUEST), and then that the recording itself is
what it refuses — Telegram's Opus-in-OGG gets AUDIO_VALIDATION_FAILED
however the part is labelled. So the upload repackages the same Opus
stream into the WebM container MAX's own web client records, falling
back through lossier forms to the untouched original.

Minor rather than patch: this adds a dependency (imageio-ffmpeg, which
supplies the ffmpeg binary when the host has none) and changes how voice
uploads are performed.

- feat: get ffmpeg from the imageio-ffmpeg wheel when there is no system one
- fix: repackage Telegram voice notes into MAX's own container before upload

## 0.2.10 — 2026-09-12

Voice is now posted as a multipart form instead of the raw body with a
Content-Range that pymax copied from its video upload: MAX answers every
raw spelling with BAD_REQUEST and the multipart one with
AUDIO_VALIDATION_FAILED, i.e. only the latter is understood well enough
to reach the audio check. The sweep now varies how the part is labelled
(field name, filename, content type), each against its own upload slot.

- fix: post voice as a multipart form, and sweep how the part is labelled

## 0.2.9 — 2026-09-12

Makes the voice upload variant sweep actually compare the shapes: each
now gets its own upload slot (MAX burns the cid on a rejected POST) and
its own connection (MAX drops the socket after rejecting one), and a
dropped connection no longer abandons the remaining variants. In 0.2.8
those two flaws meant only the first shape was ever really tried.

- fix: give each voice upload variant a fresh slot and connection

## 0.2.8 — 2026-09-12

Voice uploads now try the plausible upload shapes in one run — the one
pymax's working file upload uses (Content-Range without the "bytes "
prefix), no range at all, pymax's current voice shape, and a multipart
form like its working photo upload — and log which one MAX accepts.
MAX's audio endpoint is undocumented and rejected the previous shape
with BAD_REQUEST, and settling that one guess at a time was costing a
release per attempt.

- fix: try several upload shapes for voice instead of one guess per release

## 0.2.7 — 2026-09-12

Voice uploads now send the browser User-Agent the web session already
introduced itself with at handshake time, instead of pymax's Android
shape OKMessages/{app_version} — a field that is unset on a web session,
so the header went out as literal "OKMessages/None" and MAX rejected the
upload with BAD_REQUEST. This is the step after 0.2.6's Content-Type
fix, which got past the audio validator itself.

- fix: send the session's real User-Agent when uploading voice to MAX

## 0.2.6 — 2026-09-12

Voice uploads now declare their real Content-Type (audio/ogg for the
Opus-in-OGG that Telegram voice notes are) instead of pymax's hardcoded
application/octet-stream, which left MAX's audio validator nothing to
identify the payload by and made it reject the upload outright with
AUDIO_VALIDATION_FAILED — reported in an HTTP 200 body pymax never read,
which is why this looked like a processing-timeout problem for three
releases. A rejection is also no longer retried as if it were a timeout.

- fix: declare the real audio content type when uploading voice to MAX

## 0.2.5 — 2026-09-12

Voice messages: the attach is now referenced by audioId instead of the
video-pipeline token pymax hands to MSG_SEND. That token is what made
MAX answer errors.process.attachment.video.not.ready forever for audio
attachments, which no amount of waiting or retrying could get past.

- fix: reference voice attachments by audioId instead of the video token

## 0.2.4 — 2026-09-12

Fixes voice messages never sending: pymax's upload_voice() was mangling
the User-Agent header sent with the upload HTTP request (percent-encoded
via urllib.parse.quote()), which MAX's server apparently can't parse —
the attachment then stays stuck "not ready" forever and the "ready"
notification never arrives. Root cause matched against an upstream bug
report (MaxApiTeam/PyMax#103) with identical symptoms. Patched to send
the header unquoted; kept the earlier shortened-wait + retry-loop fix
as a fallback.

- fix: stop mangling User-Agent header on voice uploads, causing them to never send
- ci: fix missing git committer identity in release workflow
- ci: replace Claude Code Action release workflow with plain shell + gh
- ci: auto-tag and publish GitHub Release on VERSION bump

## 0.2.3 — 2026-09-12

Fixes the outbox background retry sweep racing an in-flight delivery
attempt: for voice/video sends, which can legitimately take pymax's
internal "attachment not ready" retry up to a minute, the sweep's 20s
poll interval could see the item still pending and start a duplicate
upload+send while the original attempt was still waiting. Added an
in-flight claim on outbox items to prevent this.

- fix: outbox retry sweep can duplicate an in-flight voice/video send
- fix: voice sends still failing after the previous ApiError patch — wrong event classification, not just wrong error matching
- Auto update 2026-09-12 15:16:31
- Update README.md
- Update README.md
- fix: remove duplicated '## Git' header from rebase conflict resolution
- docs: make git/versioning/release workflow in CLAUDE.md unambiguous
- Update CLAUDE.md
- docs: compare this fork against ircitdev upstream and Aist/max2tg

## 0.2.2 — 2026-09-12

Огромный объём работы с момента первого тега — переход на PyMax, устойчивость к сетевым сбоям, множество исправленных багов. Ключевое из недавнего:

- **Исправлен краш диспетчера входящих сообщений** — `RuntimeError: cannot pickle 'generator' object` из-за глубокого копирования в `dataclasses.asdict()`, когда `msg.raw` иногда содержал живой генератор из внутреннего fallback-пути pymax. Сообщение просто терялось. Заменено на неглубокую сборку payload.
- **TCP keepalive на Telegram HTTP-клиентах** — снижает частоту `httpx.RemoteProtocolError` при long-polling через обязательный SOCKS5-прокси (прокси иногда тихо рвёт простаивающее соединение).
- **Полная миграция на PyMax** (`maxapi-python`) вместо самодельного WebSocket-клиента.
- Persistent SQLite outbox с retry-логикой для надёжной доставки в обе стороны.
- Read-receipts и реакции пробрасываются между MAX и Telegram.
- Множество более мелких фиксов протокола и устойчивости (voice/video not.ready, read_message валидация, пагинация полного списка чатов и другое).

309 тестов, все зелёные.

Fixes two issues found in production logs since 0.2.1:
- Inbound MAX-message dispatch could crash entirely with
  "cannot pickle 'generator' object" — dataclasses.asdict() on a
  MaxMessage deep-copies msg.raw, which can occasionally hold a live
  generator from pymax's own unresolved-schema fallback path right
  after startup. Replaced with a shallow field-by-field payload build.
- Recurring httpx.RemoteProtocolError during Telegram long-polling
  over the (mandatory) SOCKS5 proxy — enabled TCP keepalive on both
  Telegram HTTP clients so a silently-dropped proxy connection is
  noticed and recycled sooner.

- Update CLAUDE.md
- Enable TCP keepalive on Telegram HTTP clients (mitigate proxy drops)
- fix: inbound-message dispatch crashed on 'cannot pickle generator object'
- Update CLAUDE.md

## 0.2.1 — 2026-09-11

Fixes two real production bugs reported in this account's logs:
- Voice/video messages TG → MAX failing with
  errors.process.attachment.video.not.ready (patched pymax's
  attachment-not-ready error matching so its own built-in retry
  fires correctly)
- read_message tearing down the whole websocket connection with a
  MAX validation error (message_id was sent as a JSON string instead
  of a number)

- fix: read_message crashed the whole websocket connection on a valid ID
- fix: voice (and other attachment) sends TG → MAX failing on "not.ready"
- fix: single-attachment MAX→TG forwards also lost the read-receipt track
- fix: MAX→TG read-receipt could land on a stale message after an album
- docs: require explicit permission before any git push

## 0.2.0 — 2026-09-10

New in this release: reply-triggered MAX read-receipts, flat 1h
disconnect throttle with restored-only-after-lost gating, and the
persistent SQLite outbox with retry for both TG→MAX and MAX→TG.

- feat: background outbox retry loop, wired into main.py
- feat: persistent SQLite outbox for MAX↔TG message delivery, with retry-safe redelivery paths
- feat: flat 1h disconnect throttle; restored-notice only follows a real disconnect notice
- test: fix build_pymax_client stub signature after cherry-picking read-receipts
- feat: read-receipts TG → MAX
- docs: add/update /list in help text and README

## 0.1.0 — 2026-09-08

Adds a VERSION file and logs it at startup (MAX2TG-Bridge v0.1.0).
This is the first tagged release: PyMax QR transport, Telegram-side
notifications for MAX connect/disconnect/auth-failure, QR delivered
to Telegram as a scannable PNG, /list and /add fixes.

- feat: use a solid Unicode block for the ASCII-log QR when possible
- fix: correct QR module aspect ratio in ASCII log render
- fix: UnboundLocalError in /add — link was never assigned before use
- fix: send QR PNG only to the default chat, not all groups
- feat: broadcast Max listener crash (e.g. auth failure) to all groups
- feat: broadcast MAX login QR to Telegram as a scannable PNG
- fix: render QR through the app logger as pure ASCII, not raw stdout
- feat: group /list output by chat type with headers and a summary
- fix: /list includes DMs; isolate per-chat snapshot errors
- feat: use pymax's ConsoleQrHandler for QR auth
- fix: fallback for pymax models with unresolved pydantic schema (MockValSer)
- Fix /list reporting no MAX chats/channels when some exist
- ASCI QR in log
- fix: build_tg_app crashed with TG_PROXY set (RuntimeError from PTB)
- fix: app/health.py still imported the removed legacy app.max_client
- feat: log all command invocations (/bind, /add, /list, /profile, /intro, /del, /help)
- refactor: migrate MAX bridge exclusively to PyMax
- Handle binary fields in PyMax models
- Add PyMax backend auth support
- Log a warning when the outgoing-reply reaction fails to set
- Merge fixes from ircitdev forks + forward MAX read/reaction events
- docs: add README section on routing to multiple Telegram groups

