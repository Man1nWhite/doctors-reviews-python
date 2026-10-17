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
