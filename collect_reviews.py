#!/usr/bin/env python3
"""Пакетный сбор отзывов о врачах через API этого репозитория.

API `/api/v1/getReviews` принимает прямую ссылку на страницу врача и платформу,
поэтому список ФИО хранится в `doctors.json` вместе с URL. Скрипт последовательно
опрашивает API, повторяет запросы при лимитах/временных ошибках источника,
проверяет, что заголовок страницы соответствует ожидаемому ФИО, и сохраняет
результат в JSON, CSV и Markdown-отчёт.

Используется только стандартная библиотека Python 3.10+.

Примеры:

    python collect_reviews.py --api-url http://127.0.0.1:9000 --all-reviews
    python collect_reviews.py --api-url https://doc-reviews.ak-vps.ru --api-key "$API_KEY"
    python collect_reviews.py --offline   # пересобрать CSV/отчёт из уже сохранённого JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PLATFORMS = {"sberzdorovie", "prodoctorov"}
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
RETRYABLE_ERRORS = {"rate_limited", "source_blocked", "collector_error", "collection_error", "source_http_error", "empty_response"}
CSV_FIELDS = [
    "doctor_id",
    "doctor",
    "clinic",
    "platform",
    "source",
    "review_id",
    "author",
    "date",
    "date_beauty",
    "rating_100",
    "rating_5",
    "message",
    "doctor_url",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    doctors = data.get("doctors") if isinstance(data, dict) else data
    if not isinstance(doctors, list):
        raise ValueError("Манифест должен содержать список doctors")
    return doctors


def iter_targets(doctors: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Разворачивает манифест в список задач (одна задача = один URL врача)."""
    for doctor in doctors:
        for link in doctor.get("urls", []):
            yield {
                "doctor": doctor,
                "platform": link.get("platform"),
                "url": link.get("url"),
                "status": link.get("status", ""),
                "note": link.get("note", ""),
            }


def verify_title(title: str, expected_name: str) -> list[str]:
    """Возвращает части ФИО, которых нет в заголовке страницы врача.

    Это защита от однофамильцев: например, `Gordeeva_Alla` на docdoc.ru
    ведёт к Алле Юрьевне, а не к Алле Владимировне из списка.
    """
    normalized = " ".join((title or "").lower().replace("ё", "е").split())
    missing = []
    for part in (expected_name or "").split():
        token = part.lower().replace("ё", "е")
        if token and token not in normalized:
            missing.append(part)
    return missing


def rating_to_5(value: Any, *, unrated: bool = False) -> float | str:
    if unrated or value is None or isinstance(value, bool):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number < 0:
        return ""
    # В ответе API рейтинг нормализован к шкале 0..100 (100 = 5 звёзд).
    # Нуль — валидная оценка «Ужасно» на ПроДокторов, её нельзя выбрасывать
    # из среднего балла и из подсчёта отрицательных отзывов; отзывы без
    # числовой оценки помечаются отдельно флагом unrated=True.
    return round(number / 20, 2)


def review_rating_5(review: dict[str, Any]) -> float | str:
    return rating_to_5(review.get("rating", 0), unrated=bool(review.get("unrated")))


def build_request_url(api_url: str, url: str, platform: str, all_reviews: bool) -> str:
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1/getReviews"):
        base += "/api/v1/getReviews"
    query = urllib.parse.urlencode({"url": url, "platform": platform, "all_reviews": str(all_reviews).lower()})
    return f"{base}?{query}"


def http_get_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "doctors-reviews-batch/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API вернул не объект JSON")
    return payload


def fetch_reviews(
    api_url: str,
    target_url: str,
    platform: str,
    *,
    all_reviews: bool,
    api_key: str,
    timeout: float,
    max_retries: int,
    base_delay: float,
    sleep=time.sleep,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Один запрос с повторами. Возвращает (payload, error)."""
    request_url = build_request_url(api_url, target_url, platform, all_reviews)
    last_error: dict[str, Any] | None = None
    for attempt in range(max_retries + 1):
        wait = 0.0
        try:
            payload = http_get_json(request_url, api_key, timeout)
            if payload.get("error"):
                code = str(payload["error"])
                last_error = {"error": code, "details": payload.get("details", ""), "http_status": 200}
                if code in RETRYABLE_ERRORS and attempt < max_retries:
                    wait = max(base_delay, 2.0 ** attempt)
                else:
                    return None, last_error
            else:
                return payload, None
        except urllib.error.HTTPError as error:
            retry_after = 0.0
            try:
                retry_after = float(error.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                pass
            body = error.read().decode("utf-8", "ignore")
            try:
                payload = json.loads(body)
                details = payload.get("details", payload.get("error", body[:200]))
            except (ValueError, AttributeError):
                details = body[:200]
            last_error = {"error": f"http_{error.code}", "details": details, "http_status": error.code}
            if error.code in RETRYABLE_HTTP and attempt < max_retries:
                wait = max(base_delay, 2.0 ** attempt, retry_after)
            else:
                return None, last_error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            last_error = {"error": type(error).__name__, "details": str(error), "http_status": None}
            if attempt < max_retries:
                wait = max(base_delay, 2.0 ** attempt)
            else:
                return None, last_error
        if wait:
            sleep(wait)
    return None, last_error


def make_record(target: dict[str, Any], payload: dict[str, Any], *, collected_at: str) -> dict[str, Any]:
    doctor = target["doctor"]
    title = str(payload.get("title", ""))
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        reviews = []
    missing = verify_title(title, doctor.get("expected_name", ""))
    warnings = []
    if missing:
        warnings.append("Заголовок страницы не совпадает с ожидаемым ФИО: " + ", ".join(missing))
    if not reviews:
        warnings.append("Отзывов на странице нет (или они не распознаны коллектором)")
    return {
        "doctor_id": doctor.get("id", ""),
        "doctor": doctor.get("name", ""),
        "expected_name": doctor.get("expected_name", ""),
        "clinic": doctor.get("clinic", ""),
        "platform": target["platform"],
        "url": target["url"],
        "title": title,
        "reviews": reviews,
        "collected_at": collected_at,
        "warnings": warnings,
    }


def flatten_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        for review in record.get("reviews", []):
            if not isinstance(review, dict):
                continue
            rows.append({
                "doctor_id": record.get("doctor_id", ""),
                "doctor": record.get("doctor", ""),
                "clinic": record.get("clinic", ""),
                "platform": record.get("platform", ""),
                "source": review.get("source", ""),
                "review_id": review.get("id", ""),
                "author": review.get("name", ""),
                "date": review.get("date", ""),
                "date_beauty": review.get("date_beauty", ""),
                "rating_100": "" if review.get("unrated") else review.get("rating", 0),
                "rating_5": review_rating_5(review),
                "message": review.get("message", ""),
                "doctor_url": record.get("url", ""),
            })
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


SECTION_RE = re.compile(
    r"<!-- ([A-Z0-9_-]+)-START -->\n?(.*?)\n?<!-- \1-END -->", re.DOTALL
)


def preserve_sections(new_text: str, old_text: str) -> str:
    """Переносит дополнительные секции (Яндекс, ДОКТУ) в пересобранный отчёт."""
    if not old_text:
        return new_text
    blocks = [match.group(0).strip() for match in SECTION_RE.finditer(old_text)]
    if not blocks:
        return new_text
    return new_text.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"


def render_summary(records: list[dict[str, Any]], issues: list[dict[str, Any]], manifest_count: int) -> str:
    total_reviews = sum(len(record.get("reviews", [])) for record in records)
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = by_id.setdefault(record.get("doctor_id", ""), {
            "name": record.get("doctor", ""),
            "clinic": record.get("clinic", ""),
            "platforms": [],
            "reviews": [],
            "warnings": [],
        })
        entry["platforms"].append(record.get("platform", ""))
        entry["reviews"].extend(record.get("reviews", []))
        entry["warnings"].extend(record.get("warnings", []))

    lines = [
        "# Отзывы о врачах",
        "",
        f"Сформировано: {utc_now()}",
        "",
        f"- Врачей в манифесте: **{manifest_count}**",
        f"- Успешно собрано страниц врачей: **{len(records)}**",
        f"- Всего отзывов: **{total_reviews}**",
        f"- Записей с ошибками/без ссылки: **{len(issues)}**",
        "",
        "Рейтинг в ответе API нормализован к шкале 0–100; в отчёте `rating_5` — перевод в 5-балльную шкалу.",
        "Положительными считаются отзывы с `rating_5 >= 4`, отрицательными — `< 3`.",
        "",
        "## Сводка по врачам",
        "",
        "| Врач | Клиника | Платформы | Отзывов | Средний балл | Положительных | Отрицательных |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for doctor_id, entry in by_id.items():
        ratings = [
            review_rating_5(review)
            for review in entry["reviews"]
            if not review.get("duplicate_of")
        ]
        ratings = [value for value in ratings if isinstance(value, float)]
        average = f"{sum(ratings) / len(ratings):.2f}" if ratings else "—"
        positive = sum(1 for value in ratings if value >= 4)
        negative = sum(1 for value in ratings if value < 3)
        platforms = ", ".join(sorted(set(entry["platforms"])))
        lines.append(
            f"| {entry['name']} | {entry['clinic']} | {platforms} | {len(entry['reviews'])} | {average} | {positive} | {negative} |"
        )

    warnings = [(doctor_id, warning) for doctor_id, entry in by_id.items() for warning in entry["warnings"]]
    if warnings:
        lines += ["", "## Предупреждения", ""]
        for doctor_id, warning in warnings:
            lines.append(f"- `{doctor_id}`: {warning}")

    if issues:
        lines += ["", "## Что осталось без данных", "", "| Врач | Платформа | Причина |", "| --- | --- | --- |"]
        for issue in issues:
            lines.append(f"| {issue.get('doctor', '')} | {issue.get('platform', '')} | {issue.get('reason', '')} |")
        lines += [
            "",
            "Пропуски закрываются добавлением точных URL страниц врачей в `doctors.json` "
            "и повторным запуском скрипта. URL должны вести на `docdoc.ru`/`sberhealth.ru` "
            "(platform=`sberzdorovie`) или `prodoctorov.ru` (platform=`prodoctorov`).",
        ]
    return "\n".join(lines) + "\n"


def collect(
    doctors: list[dict[str, Any]],
    *,
    api_url: str,
    api_key: str,
    all_reviews: bool,
    timeout: float,
    max_retries: int,
    delay: float,
    sleep=time.sleep,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    collected_at = utc_now()
    for target in iter_targets(doctors):
        doctor = target["doctor"]
        common = {"doctor": doctor.get("name", ""), "doctor_id": doctor.get("id", ""), "platform": target["platform"]}
        if not target["url"]:
            issues.append({**common, "reason": f"URL не указан ({target['status']}: {target['note']})"})
            continue
        if target["platform"] not in PLATFORMS:
            issues.append({**common, "reason": f"Неподдерживаемая платформа: {target['platform']}"})
            continue
        print(f"[{doctor.get('name')}] {target['platform']}: {target['url']}", flush=True)
        payload, error = fetch_reviews(
            api_url,
            target["url"],
            target["platform"],
            all_reviews=all_reviews,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            base_delay=delay,
            sleep=sleep,
        )
        if error:
            details = error.get("details") or error.get("error")
            issues.append({**common, "url": target["url"], "reason": f"{error['error']}: {details}"})
            print(f"  ошибка: {error['error']}: {details}", file=sys.stderr, flush=True)
        else:
            record = make_record(target, payload or {}, collected_at=collected_at)
            records.append(record)
            print(f"  отзывов: {len(record['reviews'])}, заголовок: {record['title']}", flush=True)
        sleep(delay)
    return records, issues


def save_outputs(records: list[dict[str, Any]], issues: list[dict[str, Any]], manifest_count: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "reviews.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "errors.json").write_text(json.dumps(issues, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(flatten_records(records), output_dir / "reviews.csv")
    summary_path = output_dir / "summary.md"
    old_summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    summary_path.write_text(
        preserve_sections(render_summary(records, issues, manifest_count), old_summary),
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", default="doctors.json", help="Путь к манифесту врачей")
    parser.add_argument("--api-url", default=os.getenv("REVIEWS_API_URL", "http://127.0.0.1:9000"), help="Base URL API")
    parser.add_argument("--api-key", default=os.getenv("REVIEWS_API_KEY", ""), help="Ключ для заголовка X-API-Key")
    parser.add_argument("--all-reviews", action="store_true", help="Собирать все отзывы, а не только показанные на странице")
    parser.add_argument("--output-dir", default="output", help="Каталог для reviews.json/csv/summary.md/errors.json")
    parser.add_argument("--delay", type=float, default=3.0, help="Пауза между запросами, секунды")
    parser.add_argument("--timeout", type=float, default=120.0, help="Таймаут одного HTTP-запроса")
    parser.add_argument("--max-retries", type=int, default=3, help="Число повторов при 429/5xx/ошибках источника")
    parser.add_argument("--offline", action="store_true", help="Не обращаться к API: пересобрать CSV и отчёт из reviews.json/errors.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = Path(args.manifest)
    output_dir = Path(args.output_dir)
    doctors = load_manifest(manifest_path)
    if args.offline:
        records = json.loads((output_dir / "reviews.json").read_text(encoding="utf-8"))
        issues_path = output_dir / "errors.json"
        issues = json.loads(issues_path.read_text(encoding="utf-8")) if issues_path.exists() else []
        save_outputs(records, issues, len(doctors), output_dir)
        print(f"Пересобрано из {output_dir / 'reviews.json'}: {sum(len(r.get('reviews', [])) for r in records)} отзывов")
        return 0
    records, issues = collect(
        doctors,
        api_url=args.api_url,
        api_key=args.api_key,
        all_reviews=args.all_reviews,
        timeout=args.timeout,
        max_retries=args.max_retries,
        delay=args.delay,
    )
    save_outputs(records, issues, len(doctors), output_dir)
    print(f"Готово: {len(records)} страниц, {sum(len(r['reviews']) for r in records)} отзывов, {len(issues)} пропущено → {output_dir}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
