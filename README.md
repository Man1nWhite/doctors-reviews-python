## Doctors Reviews API

Сервис собирает отзывы о врачах с медицинских платформ СберЗдоровье (https://sberhealth.ru, https://docdoc.ru) и ПроДокторов (https://prodoctorov.ru) и определяет их тональность (положительная, отрицательная) с помощью ИИ.

Документация OpenAPI: https://doc-reviews.ak-vps.ru/docs

### Роуты

    GET  /api/v1/getReviews?url=...&platform=...&all_reviews=... — получить отзывы
    POST /api/v1/checkSentiment — определить тональность отзыва, тело запроса: {"review": "текст"}
    GET  /health — проверка работоспособности
    GET  /metrics — счётчики запросов, ошибок, попаданий в кэш и запусков браузера

Параметры `getReviews`:

    platform — sberzdorovie (СберЗдоровье) или prodoctorov (ПроДокторов)
    url — ссылка на страницу врача на выбранной платформе, например:
        https://docdoc.ru/doctor/ivanov-ivan
        https://prodoctorov.ru/moskva/vrach/ivanov_ivan/
    all_reviews — true, чтобы собрать все отзывы, а не только показанные на странице врача
        (по умолчанию false; в ответе не больше MAX_RESULT_REVIEWS)

Если включена авторизация (`API_AUTH_ENABLED=true`), передавайте ключ в заголовке `X-API-Key`.

### Как собираются отзывы

Оба источника защищены антибот-системой ServicePipe: на запросы с серверных IP она отвечает страницей JS-проверки вместо страницы врача. API собирает отзывы через браузер [Camoufox](https://camoufox.com) — Firefox с защитой от обнаружения автоматизации. Он проходит JS-проверку и сохраняет cookies в `BROWSER_PROFILE_DIR`.

Браузер переиспользуется между запросами и открывает страницы последовательно. Сбор может занимать несколько секунд или десятки секунд; при нагрузке добавляется ожидание очереди. `BROWSER_TIMEOUT_SECONDS` ограничивает работу с одной страницей после получения браузерной блокировки, но не всё время ожидания запроса. При `STARTUP_WARMUP=true` браузер и источники прогреваются перед запуском API.

Если с одного IP идёт много запросов подряд, ServicePipe вместо JS-проверки показывает капчу с картинкой, которую сервис пройти не может. Тогда API вернёт `503 source_blocked`. Снизьте частоту запросов (успешные ответы кэшируются на `CACHE_TTL_SECONDS`) или настройте прокси с другим IP (`HTTPS_PROXY`).

Проверить доступ к источникам с сервера:

```bash
python check_access.py                                        # без Docker
docker compose exec web /app/venv/bin/python check_access.py  # в Docker
```

Скрипт проверяет тестовые страницы `TEST_URL` и `PRODOCTOROV_TEST_URL`: сначала обычным HTTP-запросом, затем через браузер. С сервера HTTP-запрос обычно блокируется (`source_blocked`) — это нормально, главное, чтобы браузер вернул отзывы.

Основные библиотеки: FastAPI, httpx, Camoufox (Playwright), lxml, OpenAI SDK.

### Быстрая выдача и фоновое обновление

Успешный ответ считается свежим в течение `CACHE_TTL_SECONDS` (по умолчанию 15 минут). После этого он хранится ещё до `CACHE_STALE_TTL_SECONDS` (по умолчанию 24 часа): API сразу возвращает сохранённые отзывы и запускает обновление в фоне. Следующие запросы получают обновлённые данные после завершения сбора.

- Одновременные запросы с одинаковыми `url`, `platform` и `all_reviews` используют один сбор. Если клиент перестал ждать, сбор продолжится и наполнит кэш.
- При ошибке обновления сохраняется последний успешный результат до конца его срока хранения. Повторная попытка возможна через `BLOCKED_CACHE_TTL_SECONDS`, чтобы не обращаться к заблокированному источнику при каждом запросе.
- Если сохранённых данных ещё нет или они окончательно истекли, запрос ожидает сбор. Ошибка возвращается с соответствующим HTTP-статусом.
- Кэш и объединение запросов действуют в пределах одного процесса. Кэш хранится в памяти, ограничен `CACHE_MAX_ENTRIES` и очищается при перезапуске.
- `CACHE_STALE_TTL_SECONDS=0` отключает выдачу устаревших данных и фоновое обновление; `CACHE_TTL_SECONDS=0` отключает кэш успешных ответов полностью.

В успешных ответах есть заголовок `X-Reviews-Cache`: `HIT` — свежий кэш, `STALE` — сохранённые данные с истёкшим сроком свежести, `MISS` — ожидание сбора. Формат JSON с полями `title` и `reviews` сохраняется. В `/metrics` доступны `stale_cache_hits` и `coalesced_requests` — число выдач устаревшего кэша и обращений к уже запущенному сбору.

### Подключение к Laravel

Чтобы загрузка страницы не зависела от скорости источника, получайте отзывы в фоновой Job и сохраняйте успешный ответ в БД или кэше Laravel. Контроллер страницы должен читать сохранённые данные. Запускайте обновление по расписанию и при первом обращении к врачу; для одного врача и режима достаточно одной задачи (например, с `ShouldBeUnique`).

Пример фрагмента `handle()` задачи, реализующей `ShouldQueue`. `$url` и `$platform` — параметры задачи; адрес API и ключ задайте в `config/services.php` как `doctor_reviews.url` и `doctor_reviews.key`:

```php
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;

$allReviews = false; // true, только если нужны дополнительные отзывы
$cacheKey = 'doctor-reviews:' . hash('sha256', json_encode([$platform, $url, $allReviews]));

$response = Http::withHeaders(['X-API-Key' => config('services.doctor_reviews.key')])
    ->connectTimeout(5)
    ->timeout(120)
    ->get(rtrim(config('services.doctor_reviews.url'), '/') . '/api/v1/getReviews', [
        'url' => $url,
        'platform' => $platform,
        'all_reviews' => $allReviews,
    ])
    ->throw();

Cache::put($cacheKey, $response->json(), now()->addDays(7));
```

Используйте асинхронную очередь (`QUEUE_CONNECTION=database` или `redis`) с работающим worker. Для HTTP-таймаута 120 секунд таймаут задачи/worker должен быть больше, например 150 секунд, а `retry_after` — ещё больше, например 180 секунд. При большой очереди сборов может потребоваться больше времени; увеличение таймаута само по себе не ускоряет источник.

При таймауте, HTTP 429 или 5xx сохраняйте прежние отзывы и повторяйте задачу с задержкой; для 429 учитывайте `Retry-After`. Не записывайте пустой список вместо данных при сетевой ошибке. В приведённом примере `throw()` и исключение таймаута прерывают выполнение до записи в кэш. Если отзывы ещё не загружались, страница может показывать состояние загрузки и получить результат следующим запросом.

### Настройки

Все настройки задаются в `.env` (образец — `.env.example`).

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AI_API_URL`, `AI_API_KEY`, `AI_MODEL` | — | OpenAI-совместимый провайдер для анализа тональности |
| `API_AUTH_ENABLED`, `API_KEY` | `false`, пусто | Проверка заголовка `X-API-Key` на всех роутах, кроме `/`, `/favicon.ico` и `/health` |
| `CORS_ORIGINS` | пусто | Разрешённые origin через запятую |
| `CACHE_TTL_SECONDS` | `900` | Сколько секунд успешный ответ считается свежим; `0` отключает кэш |
| `CACHE_STALE_TTL_SECONDS` | `86400` | Дополнительное время выдачи сохранённых отзывов с обновлением в фоне; `0` отключает этот режим |
| `BLOCKED_CACHE_TTL_SECONDS` | `30` | Срок кэша `source_blocked` и пауза перед повтором неудачного фонового обновления |
| `CACHE_MAX_ENTRIES` | `256` | Размер каждого из кэшей |
| `MAX_RESULT_REVIEWS` | `200` | Максимум отзывов в ответе |
| `RATE_LIMIT`, `RATE_WINDOW_SECONDS` | `30`, `60` | Сколько запросов разрешено с одного IP и ключа за окно |
| `MAX_CONCURRENT_COLLECTIONS` | `10` | Сколько сборов выполняется одновременно |
| `SENTIMENT_MAX_BODY_BYTES` | `1048576` | Максимальный размер тела запроса `checkSentiment` |
| `BROWSER_HEADLESS` | `false` | Режим браузера: `false` — обычное окно (нужен дисплей), `virtual` — Camoufox сам запускает виртуальный дисплей Xvfb (только Linux, рекомендуется для сервера без Docker), `true` — headless |
| `BROWSER_OS` | пусто | ОС в отпечатке браузера: `linux`, `windows`, `macos`; пусто — автоматический выбор. В компактном Docker-образе зафиксирован `linux` |
| `BROWSER_PROFILE_DIR` | `data/browser` | Каталог профиля браузера с cookies |
| `BROWSER_TIMEOUT_SECONDS` | `60` | Таймаут сбора через браузер |
| `STARTUP_WARMUP` | `true` | Проверить источники и прогреть браузер перед запуском API |
| `HTTP_PROXY`, `HTTPS_PROXY` | пусто | Прокси для запросов к источникам и для браузера |
| `TEST_URL`, `PRODOCTOROV_TEST_URL` | — | Тестовые страницы для `check_access.py` |

В Docker `BROWSER_HEADLESS`, `BROWSER_OS` и `BROWSER_PROFILE_DIR` задаются в `docker-compose.yml`. По умолчанию используются headless и Linux-профиль; Xvfb также установлен для режима `virtual` или обычного окна.

Для анализа тональности используется OpenAI-compatible API. Можно указать любой провайдер с endpoint `chat/completions`: OpenRouter, OpenAI, Groq, Together, Ollama и другие.
`AI_API_URL` должен быть base URL API, обычно с `/v1`; если указать полный URL `/chat/completions`, приложение автоматически удалит этот суффикс.


## Установка на Ubuntu-сервер

Рекомендуемый способ — Docker Compose. Команды выполняются от пользователя с `sudo`.

### 1. Установить Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
```

Проверить установку:

```bash
sudo docker --version
sudo docker compose version
```

### 2. Скачать проект

```bash
sudo mkdir -p /opt/doctors-reviews
sudo chown "$USER":"$USER" /opt/doctors-reviews
git clone https://github.com/ak-flash/doctors-reviews-python /opt/doctors-reviews
cd /opt/doctors-reviews
```

Если каталог уже содержит проект:

```bash
cd /opt/doctors-reviews
git pull
```

### 3. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Минимальные настройки:

```dotenv
AI_API_URL=https://openrouter.ai/api/v1
AI_API_KEY=your-provider-api-key
AI_MODEL=provider/model-name
API_AUTH_ENABLED=true
API_KEY=change-this-api-key
CORS_ORIGINS=https://your-domain.example
```

`AI_API_URL`, `AI_API_KEY` и `AI_MODEL` должны относиться к одному OpenAI-compatible провайдеру. Не добавляйте `.env` в публичный репозиторий.

Если источник блокирует IP сервера (API возвращает `source_blocked`, а `check_access.py` не получает отзывы и через браузер), задайте прокси:

```dotenv
HTTP_PROXY=http://user:password@proxy-host:proxy-port
HTTPS_PROXY=http://user:password@proxy-host:proxy-port
```

Страницы источников открываются по HTTPS, поэтому HTTP-запросы идут через `HTTPS_PROXY`; браузер использует `HTTPS_PROXY`, а если он не задан — `HTTP_PROXY`. Поддерживаются HTTP/HTTPS proxy URL и прокси с авторизацией. Не вставляйте пароль от прокси в публичные файлы.

### 4. Запустить API

```bash
docker compose up -d --build --force-recreate --wait
docker compose ps
curl http://127.0.0.1:9000/health
```

Логи:

```bash
docker compose logs -f web
```

API будет доступен локально на `http://127.0.0.1:9000`. Документация: `/docs`.

Docker-образ уже содержит Camoufox и Xvfb, дополнительно ничего устанавливать не нужно. Проверить доступ к источникам:

```bash
docker compose exec web /app/venv/bin/python check_access.py
```

### Компактный Docker-образ

При локальной проверке Linux x86_64 размер по `docker image ls` уменьшился с **3,11 ГБ до 1,58 ГБ** (примерно на 49%) по сравнению с исходным образом. Размер конкретной пересборки зависит также от базового образа и версий зависимостей.

Образ использует Python 3.13 slim и содержит только рабочие зависимости из `requirements.txt`. Пакеты для тестов вынесены в `requirements-dev.txt`. `.dockerignore` ограничивает контекст сборки исходниками сервиса: локальные окружения, данные, логи и кэши не отправляются в Docker.

Camoufox запускается с **Linux-профилем** (`BROWSER_OS=linux`). В образ включены соответствующие шрифты, а наборы Windows/macOS удаляются на стадии сборки — это экономит около 890 МиБ распакованных данных. Выбранная ОС относится к отпечатку браузера и не зависит от ОС посетителя сайта. Для другой ОС потребуются её шрифты и пересборка образа.

Браузер, дополнения и GeoIP-база скачиваются при сборке и переносятся в рабочий образ. Владельцы файлов задаются при `COPY`, без дополнительного слоя с копией окружения после `chown -R`.

Версия браузера зафиксирована отдельно от Python-пакета: `152.0.4-beta.30`, как в исходном рабочем образе. Версия и SHA-256 архивов для Linux x86_64/arm64 находятся в `docker/fetch_camoufox.py`. При обновлении браузера обновите их вместе и запустите проверку образа: новые версии могут менять структуру и объём шрифтов.

Проверить размер и самые большие слои:

```bash
docker image ls doctors-reviews-web
docker image history doctors-reviews-web:latest
```

После перехода на компактный образ проверьте доступ к источникам командой `check_access.py` выше. Проверка самого образа без интернета (запуск браузера в headless и virtual, разбор тестовых страниц и старт API):

```bash
docker run --rm --init --network none --shm-size 256m \
  --mount "type=bind,src=$(pwd)/tests/docker_smoke.py,dst=/tmp/docker_smoke.py,readonly" \
  --entrypoint /app/venv/bin/python \
  doctors-reviews-web:latest /tmp/docker_smoke.py
```

### Обновление после изменений

```bash
cd /opt/doctors-reviews
git pull
docker compose up -d --build --force-recreate --wait
```

### Остановка

```bash
docker compose down
```

## Установка без Docker

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
cd /opt
git clone https://github.com/ak-flash/doctors-reviews-python doctors-reviews
cd doctors-reviews
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m camoufox fetch
sudo venv/bin/python -m playwright install-deps firefox
cp .env.example .env
nano .env
python main.py
```

`python -m camoufox fetch` скачивает браузер в `~/.cache/camoufox` текущего пользователя, а `playwright install-deps firefox` устанавливает системные библиотеки браузера и Xvfb. На сервере без графического окружения укажите в `.env`:

```dotenv
BROWSER_HEADLESS=virtual
```

Сервис слушает `0.0.0.0:9000`. Проверить доступ к источникам: `python check_access.py`. Для постоянной работы используйте systemd.

## Автозапуск через systemd

Создать `/etc/systemd/system/doctors-reviews.service`:

```ini
[Unit]
Description=Doctors Reviews API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/doctors-reviews
ExecStart=/opt/doctors-reviews/venv/bin/python /opt/doctors-reviews/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Замените `User=ubuntu`, если проект запускается от другого пользователя. Это должен быть тот же пользователь, от которого выполнялся `python -m camoufox fetch`: браузер ищется в его `~/.cache/camoufox`. Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now doctors-reviews
sudo systemctl status doctors-reviews
journalctl -u doctors-reviews -f
```

## Nginx reverse proxy

```bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/doctors-reviews
```

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/doctors-reviews /etc/nginx/sites-enabled/doctors-reviews
sudo nginx -t
sudo systemctl reload nginx
```

Для HTTPS установите Certbot и выпустите сертификат для своего домена:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

## Проверка sentiment API

```bash
curl -X POST http://127.0.0.1:9000/api/v1/checkSentiment \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-this-api-key' \
  -d '{"review":"Врач помог, лечение дало отличный результат"}'
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

Нужен `AI_API_KEY` в `.env` или в окружении — для unit-тестов подойдёт любое значение. Тест с реальным запросом к ИИ-провайдеру: `pytest --live`.

## Пакетный сбор отзывов по списку врачей

`GET /api/v1/getReviews` принимает **прямую ссылку на страницу врача**, а не ФИО и не название клиники. Поиска врача по имени в сервисе нет, поэтому для пакетного сбора используется манифест [`doctors.json`](doctors.json): в нём перечислены врачи, клиники и URL на поддерживаемых платформах.

```bash
# Локальный API
python collect_reviews.py --api-url http://127.0.0.1:9000 --all-reviews

# Удалённый API с авторизацией
REVIEWS_API_KEY=... python collect_reviews.py \
  --api-url https://doc-reviews.ak-vps.ru \
  --all-reviews --delay 5 --max-retries 4

# Пересобрать CSV/отчёт без обращения к API
python collect_reviews.py --offline

# Обновить дополнительные слои (Яндекс.Медицина и ДОКТУ)
python yandex_export.py --append-summary
python doctu_export.py --append-summary
python rate_doctors.py --append-summary
```

Скрипт только на стандартной библиотеке Python 3.10+. Он:

- обходит `doctors.json` и пропускает записи без URL, занося их в `output/errors.json`;
- повторяет запросы при `429 rate_limited`, `5xx`, `source_blocked` и сетевых ошибках (с учётом `Retry-After`), делает паузу `--delay` между врачами;
- сверяет заголовок страницы с ожидаемым ФИО — это защита от однофамильцев (например, `docdoc.ru/doctor/Gordeeva_Alla` ведёт к другому врачу);
- пишет `output/reviews.json` (структура ответа API), `output/reviews.csv` (плоская таблица), `output/summary.md` (сводка) и `output/errors.json`.

Результаты работы от 2026-11-02 лежат в [`output/`](output/): **77 отзывов с 16 страниц** на поддерживаемых платформах для **12 врачей** (Казаков, Джатиева, Сеферян, Огнева, Евстигнеева, Быстрова, Ермакова, Тарасова, Дарханова, Севостьяненко, Вяткина, Скорик). Первые 24 отзыва получены через публичный `/api/v1/getReviews`; когда hosted API перестал отвечать, остальные 53 собраны напрямую со страниц `/otzivi/` на `prodoctorov.ru` и приведены к тому же формату (`rating = оценка страницы × 20`). Часть URL найдена без участия пользователя — через блок «Контакты и ссылки» Яндекс.Медицины и списки врачей клиник на ПроДокторов; ещё три точные ссылки (Вяткина, Гордеева, Скорик) переданы пользователем и проверены по ФИО, отчеству и месту приёма.

Для 3 врачей (Нежведилова, Сидорова, Гордеева) найдены подтверждённые профили на ПроДокторов, но на них **0 отзывов**; для Гордеевой 5 отзывов о враче и 3 о клинике собраны с `doctu.ru` в отдельный слой (см. ниже). У Скорик ФИО и специальность подтверждены, но страница указывает текущее место приёма «Поликлиника.ру» на Красных Воротах, а не MedSwiss из запроса — это несовпадение явно записано в `warnings` в `output/reviews.json`. Оставшиеся пропуски — в основном неподтверждённые ссылки `docdoc.ru`/`sberhealth.ru` для `platform=sberzdorovie`, один несобранный URL `uud.docdoc.ru` и частичная пагинация Яндекса; они перечислены в `output/errors.json`. Записи со статусом `needs_url` не выдумываются. При запуске на своём сервере с настроенным Camoufox скрипт повторит те же запросы без общего лимита публичного инстанса.

Формат ответа `getReviews`:

```json
{
  "title": "Казаков Юрий Львович, эндокринолог - 8 отзывов | Москва - ПроДокторов",
  "reviews": [
    {"id": "...", "name": "...", "date": "2025-08-27", "date_beauty": "...",
     "message": "...", "rating": 100, "source": "prodoctorov"}
  ]
}
```

`rating` нормализован к шкале 0–100 (100 = 5 звёзд, 0 — валидная оценка «Ужасно»); в CSV есть колонка `rating_5`. Количество отзывов ограничено `MAX_RESULT_REVIEWS` (по умолчанию 200). Часть текстов на ПроДокторов может содержать `[...]` — так источник обрезает длинные отзывы в превью.

## Дополнительный источник: Яндекс.Медицина

Яндекс — агрегатор: на странице врача видны собственные отзывы, отзывы «Из отзывов на клинику» (Яндекс.Карты) и блок «Контакты и ссылки» с прямыми URL на СберЗдоровье / ПроДокторов / НаПоправку / ДОКТУ. Именно этот блок и использовался для поиска недостающих ссылок выше.

Сам репозиторий Яндекс **не собирает**: в `Platform` только `sberzdorovie` и `prodoctorov`, а `SOURCE_CONFIGS` разрешает лишь `docdoc.ru`, `sberhealth.ru`, `prodoctorov.ru`. Публичного API у Яндекса для выгрузки отзывов нет, поэтому 24 отзыва из встроенного JSON (`ugc.initState.reviews.items`) публичных страниц сохранены отдельно:

```bash
python yandex_export.py --append-summary
```

Результат: `output/yandex_reviews.json`, `output/yandex_reviews.csv`, `output/yandex_summary.md` и одноимённая секция в `output/summary.md`. Страница отдаёт первые 5 отзывов (`fetchedReviewsCount=5`), остальные подгружаются виджетом `reviews.yandex.ru` — для Огневой (15), Сеферяна (9) и Тарасовой (24) выгрузка частичная. Отзывы о клинике помечены `source_kind=yandex_clinic` и не смешиваются с отзывами о враче. Оценка Яндекса 1–5 переведена в `rating_100 = оценка × 20`.

Чтобы собирать Яндекс штатно, нужно добавить `Platform.YANDEX_MEDICINE`, домен `yandex.ru` в `SOURCE_CONFIGS`, новый коллектор под встроенный JSON/виджет и обработку пагинации; текущие парсеры для этого не приспособлены.

## Дополнительный источник: ДОКТУ

Ссылка `https://doctu.ru/msk/doctor/gordeeva-a-v` передана пользователем для Гордеевой Аллы Владимировны. Профиль подтверждён по ПроДокторов (`64578-gordeeva`): совпадают стаж 18 лет, образование (Первый МГМУ им. И.М. Сеченова, 2008), опыт работы (ООО «Лечебный центр» 2011—2021, сеть MedSwiss с 2021) и клиника «МедСвисс» на Гашека, 12.

Домен `doctu.ru` не входит в `SOURCE_CONFIGS`, поэтому `/api/v1/getReviews?url=https://doctu.ru/...` возвращает `invalid_url` (`URL domain is not allowed`). Отзывы собраны с публичной страницы и хранятся отдельно:

```bash
python doctu_export.py --append-summary
```

`output/doctu_reviews.json`, `output/doctu_reviews.csv`, `output/doctu_summary.md` и секция `<!-- DOCTU-START -->` в `output/summary.md`: **5 отзывов о враче** (средний балл 4.76, рейтинг сайта 4.8) и **3 отзыва о клинике** с `source_kind=doctu_clinic`. Последние в оценке врача не участвуют. Шкала 0–5 переведена в `rating_100 = оценка × 20`.

`collect_reviews.py --offline` пересобирает основной отчёт, но переносит секции Яндекса и ДОКТУ по маркерам `<!-- *-START --> … <!-- *-END -->`, поэтому дополнительные слои не теряются.

## Оценка врачей по 10-балльной шкале (предиабет, ожирение)

Помимо сбора отзывов, репозиторий содержит воспроизводимую оценку соответствия каждого из 15 врачей заданной теме. **Это не медицинская рекомендация и не клинический рейтинг**: балл отражает только полноту открытых публичных данных.

```bash
python rate_doctors.py --append-summary
```

Входные слои:

- `output/doctor_profiles.json` — специальность, стаж, базовый вуз, ординатура/аспирантура, учёная степень, курсы повышения квалификации, ассоциации, профиль лечения (собрано с публичных страниц ПроДокторов и ДОКТУ, ссылки на источники внутри файла);
- `output/reviews.json`, `output/doctu_reviews.json`, `output/yandex_reviews.json` — отзывы о враче; отзывы о клиниках исключены;
- `output/extra_doctor_reviews.json` — отзывы трёх врачей, добавленных в сравнение позже (сайт «Поликлиника.ру», НаПоправку). На сайте клиники числовая оценка не публикуется, поэтому положительным текстам условно присвоено 5.0 с флагом `rating_assumed=true`; их средний балл условный.

Методика: 6 критериев по 0–2 балла (максимум 12), итог нормируется в 0–10.

| Код | Критерий |
| --- | --- |
| A | Профильное обучение по ожирению / преддиабету / СД2 / диетологии / метаболизму |
| B | Профильная ординатура и аспирантура |
| C | Где учились: базовый вуз + академические сигналы (отличие, к.м.н., ассоциации, проверенные документы) |
| D | Тема предиабета/ожирения звучит в **положительных** отзывах (оценка ≥4) |
| E | Средний балл и объём отзывов по всем слоям |
| F | Стаж и актуальность страницы |

Негативные отзывы по теме (оценка <3) балл D не повышают; при двух и более из суммы вычитается 1 штрафной балл до нормирования. В текущих данных штраф применён только к Тарасовой.

Результат: `output/doctor_ratings.json`, `output/doctor_ratings.csv`, `output/doctor_ratings.md`, `output/top3_detail.md`, `output/comparison_extra.md` и секция `<!-- RATINGS-START -->` в `output/summary.md`. В сравнении **18 врачей**: 15 исходных из `doctors.json` плюс 3 добавленных по просьбе пользователя — **Захарян Елизавета Андреевна** (4.2), **Анохина Лилия Ивановна** (2.5), **Авраменко Марина Владимировна** (5.0). Их страницы находятся на `polyclinika.ru` и НаПоправку: домены не входят в `SOURCE_CONFIGS`, поэтому они **не добавлялись в `doctors.json`** и не проходили через `/api/v1/getReviews`; отзывы лежат отдельным слоем. У Анохиной образование на найденных страницах не опубликовано — это отражено нулевым баллом C, а не выводом о квалификации.

Диапазон **2.5–8.3**, топ-3: **Ермакова → Джатиева → Тарасова**. У Тарасовой сильнейшая формальная база (11/12 до штрафа: РУДН с отличием, ординатура + аспирантура в НМИЦ эндокринологии, к.м.н., диетолог, 15 лет), но два отзыва Яндекса с оценкой 1.0 предметно жалуются на прибавку веса и диабетические осложнения — после штрафа −1 она третья. Подробный разбор лидеров — `output/top3_detail.md`, разбор трёх новых врачей — `output/comparison_extra.md`. Оценка не учитывает реальные клинические исходы, неопубликованные сертификаты и стоимость приёма.

