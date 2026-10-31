# Ретранслятор отчётов

Программа на машинах открытая, поэтому ключ от GitHub в ней хранить нельзя. Отчёты об
обновлениях (только с машин, где пользователь разрешил их отправку) идут сюда — на маленький
Worker в Cloudflare. Он проверяет каждый отчёт и записывает его в закрытый репозиторий
`Purple-Palm/arch-update-cactus-reports`:

- лог обновления — файлом `logs/ГГГГ/ММ/<ID>-<дата-время>.log`;
- issue с итогом: модель машины, что обновилось, проблемы и предупреждения, конец лога.
  Issue чистого обновления сразу закрывается, поэтому **открытые issues — это те, что стоит посмотреть**.
  Метки: `ок`, `предупреждения`, `проблемы`, `не начато`, `chromebook`.

Всё делается в браузере, на Windows. Командная строка нужна один раз — для проверки в PowerShell (шаг 5).
Бесплатно: хватает бесплатных тарифов GitHub и Cloudflare, банковская карта не нужна.

## 1. Закрытый репозиторий для отчётов

1. Открой https://github.com/new.
2. **Repository name**: `arch-update-cactus-reports`.
3. Выбери **Private**.
4. Включи **Add a README file** (в пустой репозиторий ретранслятору писать неудобно).
5. **Create repository**.

## 2. Токен GitHub только для этого репозитория

1. Открой ссылку — в ней уже заполнены название, срок и права:
   https://github.com/settings/personal-access-tokens/new?name=arch-update-reports&description=Relay+for+Arch+Update+by+Cactus+reports&expires_in=366&contents=write&issues=write
2. **Resource owner**: `Purple-Palm`.
3. **Repository access**: **Only select repositories** → выбери `arch-update-cactus-reports`.
   Только его — не «All repositories».
4. **Permissions** → **Repository permissions** (по ссылке уже стоят, проверь):
   - **Contents**: **Read and write** — записывать логи;
   - **Issues**: **Read and write** — заводить и закрывать issues, создавать метки;
   - **Metadata**: **Read-only** — GitHub добавляет сам, без неё нельзя.

   Больше ничего не добавляй.
5. **Expiration**: 366 дней (столько стоит по ссылке, это самый долгий срок с датой).
   Можно выбрать и **No expiration**, но безопаснее со сроком: что делать, когда он кончится, — в разделе «Замена токена».
6. **Generate token** и сразу скопируй токен (`github_pat_…`): GitHub показывает его один раз.
   Никуда, кроме шага 4, его не вставляй и никому не отправляй.

## 3. Аккаунт Cloudflare

1. https://dash.cloudflare.com/sign-up — почта и пароль, потом подтверди почту по письму.
2. Тариф не выбирай — Workers бесплатны (100 000 запросов в день; отчётов будет несколько в день).

## 4. Worker

1. В панели Cloudflare слева: **Workers & Pages** (может быть внутри раздела **Compute**).
2. **Create** (или **Create application**) → **Start with Hello World!**
   (или шаблон **Hello World** / **Create Worker**; если предлагают подключить Git — не нужно).
3. Имя: `arch-update-reports` → **Deploy**. Если Cloudflare попросит выбрать поддомен
   `workers.dev` для аккаунта — придумай любой, например `purple-palm`.
4. На странице Worker'а — **Edit code**. Слева открыт файл `worker.js`:
   выдели в нём всё (Ctrl+A) и удали.
5. Открой код ретранслятора (версия, прошедшая тесты): https://raw.githubusercontent.com/Purple-Palm/arch-update-cactus/stable/relay/worker.js —
   Ctrl+A, Ctrl+C, вернись в редактор, Ctrl+V. Справа вверху — **Deploy**.
6. Вернись на страницу Worker'а → **Settings** → **Variables and Secrets** → **Add**:
   - **Type**: **Text**, **Variable name**: `REPO`, **Value**: `Purple-Palm/arch-update-cactus-reports`;
   - **Add variable** → **Type**: **Secret**, **Variable name**: `GITHUB_TOKEN`, **Value**: токен из шага 2.

   Нажми **Deploy**. Секрет Cloudflare хранит зашифрованным и больше никому не показывает — даже тебе.
7. Адрес Worker'а — на его странице (раздел **Domains & Routes** или ссылка **Visit**), вида
   `https://arch-update-reports.ПОДДОМЕН.workers.dev`. Если открыть его в браузере, Worker ответит
   `{"ok":false,"error":"сюда отчёты отправляют методом POST"}` — так и должно быть.

## 5. Проверка из PowerShell

Открой PowerShell (Пуск → «PowerShell»), вставь целиком, заменив `ПОДДОМЕН` на свой:

```powershell
$url = "https://arch-update-reports.ПОДДОМЕН.workers.dev/report"
$report = @{
    schema  = 1
    id      = "00000000"
    time    = (Get-Date).ToUniversalTime().ToString("s") + "Z"
    updater = @{ commit = "test"; date = "" }
    device  = @{ vendor = "Test"; model = "PowerShell"; chromebook = $false; cpu = "Test CPU"; cores = 4; ram_gib = 8 }
    update  = @{ mode = "auto"; verdict = 1; started = $true; seconds = 42
                 changed = @("gnupg"); added = @(); removed = @()
                 problems = @(); warnings = @("test warning"); ok = @("test ok") }
    log     = "test line 1`ntest line 2`n"
}
$json = $report | ConvertTo-Json -Depth 5
try {
    Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json; charset=utf-8" `
        -Body ([Text.Encoding]::UTF8.GetBytes($json))
} catch {
    $_.Exception.Message
    $_.ErrorDetails.Message
}
```

Должна напечататься табличка: `True` под `ok` и номер issue под `issue`. В репозитории отчётов
появятся issue «⚠ … · Test PowerShell · 1 пакет · 00000000» с меткой `предупреждения` и файл в
папке `logs`. Тестовый issue можно удалить: на его странице справа внизу — **Delete issue**.

Если вместо этого ошибка:

| код | что значит | что сделать |
|---|---|---|
| 400 | отчёт не прошёл проверку (в ответе написано, какое поле) | вставь пример ещё раз целиком |
| 404 | не тот адрес | в конце адреса должно быть `/report` |
| 429 | слишком много отчётов за час | подожди час |
| 502 | GitHub не принял запись | проверь токен: репозиторий `arch-update-cactus-reports`, права Contents и Issues — Read and write, срок не истёк |
| 503 | не заданы `REPO` или `GITHUB_TOKEN` (или GitHub попросил подождать — это написано в ответе) | шаг 4.6, и не забудь **Deploy**; если «подождать» — повтори через несколько минут |
| 1101, 1102 | ошибка в коде Worker'а | код вставлен не целиком — повтори шаг 4.4–4.5 |

## 6. Адрес — разработчику

Пришли разработчику адрес из шага 4.7 с `/report` в конце:
`https://arch-update-reports.ПОДДОМЕН.workers.dev/report`. Он впишет его в программу
(`ENDPOINT` в `lib/report.py`); после самообновления машины начнут отправлять отчёты — только те,
где пользователь разрешил. Отчёты, накопленные до этого (до 20 на машину), уйдут постепенно.

## Замена токена

За несколько дней до конца срока GitHub пришлёт письмо. Тогда (или если токен попал не туда):

1. GitHub → аватар → **Settings** → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → `arch-update-reports` → **Regenerate token**: права и репозиторий
   останутся те же, будет новый срок и новый токен. Скопируй его.
   (Если такой кнопки нет — создай новый токен по ссылке из шага 2, а старый удали.)
2. Cloudflare → Worker → **Settings** → **Variables and Secrets** → у `GITHUB_TOKEN` значок
   редактирования → вставь новый токен → **Deploy**.

Пока токена нет или он истёк, ретранслятор отвечает ошибкой, и отчёты ждут на машинах
(до 20 на каждой) — после замены они придут сами.

## Выключить

- **На время**: удали секрет `GITHUB_TOKEN` (Settings → Variables and Secrets) или отзови токен на GitHub.
  Отчёты будут ждать на машинах и придут, когда вернёшь токен.
- **Совсем**: удали Worker (**Settings** → внизу **Delete**), удали токен на GitHub и скажи
  разработчику, чтобы убрал адрес из программы. Репозиторий отчётов можно удалить
  (его **Settings** → внизу **Delete this repository**).

## Что достаётся Cloudflare

Cloudflare хранит токен и пропускает отчёты через себя. Токен же даёт доступ только к одному
закрытому репозиторию `arch-update-cactus-reports` и только к двум вещам: файлам (Contents) и
issues (Issues), плюс обязательный Metadata (только чтение). Ни к программе `arch-update-cactus`,
ни к другим репозиториям, ни к настройкам аккаунта он не даёт ничего. Так что даже тот, кто
получил бы токен из Cloudflare, смог бы только читать и дописывать этот репозиторий отчётов —
и это прекращается, как только токен отозван.

## Защита от мусора

Адрес ретранслятора есть в открытой программе, поэтому прислать что-то может кто угодно.
Ретранслятор принимает только отчёты правильного вида (не больше 1,5 МиБ, лог не больше 1 МиБ),
не больше 20 запросов в час с одного адреса и 6 отчётов в час от одной машины — эти счётчики
живут в памяти Worker'а, — и не больше 20 отчётов в час всего (это он проверяет по самому
репозиторию). IP-адрес отправителя он не хранит и никуда не передаёт: для счётчика берётся
только хэш от него.

Если в репозиторий всё-таки пошёл мусор — выключи ретранслятор на время (раздел «Выключить»)
и напиши разработчику.

Свой журнал запросов Cloudflare ведёт сам и хранит 3 дня (на странице Worker'а — **Observability**);
его можно выключить: **Settings** → **Observability** → **Workers Logs**.
