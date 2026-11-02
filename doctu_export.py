#!/usr/bin/env python3
"""Экспорт дополнительных отзывов ДОКТУ в CSV и Markdown.

Источник: ``output/doctu_reviews.json`` — публичная страница врача на
``doctu.ru``. Домен не входит в ``SOURCE_CONFIGS`` репозитория, поэтому
``/api/v1/getReviews`` возвращает ``invalid_url``; данные лежат отдельно от
``output/reviews.json`` и не попадают в основной API-слой.

Отзывы о клинике помечены ``source_kind=doctu_clinic``: они привязаны к месту
приёма, а не к врачу, и не участвуют в расчёте среднего балла врача.

Запуск:
    python doctu_export.py                    # CSV + doctu_summary.md
    python doctu_export.py --append-summary   # ещё и секция в output/summary.md
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from yandex_export import append_summary as _append_summary

CSV_FIELDS = [
    "doctor_id", "doctor", "clinic", "platform", "profile_url",
    "total_reviews", "fetched_reviews", "review_id", "author", "date",
    "date_beauty", "rating_5", "rating_100", "source_kind", "source_label",
    "clinic_at_review", "message",
]

START = "<!-- DOCTU-START -->"
END = "<!-- DOCTU-END -->"


def flatten(data: dict) -> list[dict]:
    rows = []
    for profile in data.get("profiles", []):
        for review in profile.get("reviews", []):
            rows.append({
                "doctor_id": profile.get("doctor_id", ""),
                "doctor": profile.get("doctor", ""),
                "clinic": profile.get("clinic", ""),
                "platform": profile.get("platform", ""),
                "profile_url": profile.get("url", ""),
                "total_reviews": profile.get("total_reviews", 0),
                "fetched_reviews": profile.get("fetched_reviews", 0),
                "review_id": review.get("id", ""),
                "author": review.get("name", ""),
                "date": review.get("date", ""),
                "date_beauty": review.get("date_beauty", ""),
                "rating_5": review.get("rating_5", ""),
                "rating_100": review.get("rating_100", 0),
                "source_kind": review.get("source_kind", ""),
                "source_label": review.get("source_label", ""),
                "clinic_at_review": review.get("clinic_at_review", ""),
                "message": review.get("message", ""),
            })
    return rows


def render(data: dict, rows: list[dict]) -> str:
    profiles = data.get("profiles", [])
    doctor_rows = [row for row in rows if row["source_kind"] == "doctu_doctor"]
    clinic_rows = [row for row in rows if row["source_kind"] == "doctu_clinic"]
    lines = [
        "# Дополнительно: ДОКТУ",
        "",
        "Отзывы взяты с публичной страницы врача на `doctu.ru`, а не из",
        "`/api/v1/getReviews`: домен не разрешён в `SOURCE_CONFIGS`, и прямой вызов",
        "API вернул `invalid_url` (`URL domain is not allowed`). Отзывы о клинике",
        "(`source_kind=doctu_clinic`) в оценке врача не участвуют.",
        "",
        f"- Профилей врачей: **{len(profiles)}**",
        f"- Отзывов о враче: **{len(doctor_rows)}**",
        f"- Отзывов о клинике (не о враче): **{len(clinic_rows)}**",
        "- Шкала ДОКТУ 0–5 переведена в `rating_100 = оценка × 20` для совместимости с основной выгрузкой.",
        "",
        "| Врач | Профиль | Рейтинг на сайте | Отзывов о враче | Средний балл | Положительных | Отрицательных |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile in profiles:
        own = [row for row in doctor_rows if row["doctor_id"] == profile.get("doctor_id", "")]
        ratings = [float(row["rating_5"]) for row in own if isinstance(row["rating_5"], (int, float))]
        average = f"{sum(ratings) / len(ratings):.2f}" if ratings else "—"
        positive = sum(1 for value in ratings if value >= 4)
        negative = sum(1 for value in ratings if value < 3)
        lines.append(
            f"| {profile.get('doctor', '')} | [{profile.get('doctor_id', '')}]({profile.get('url', '')}) "
            f"| {profile.get('site_rating', '—')} | {len(own)} / {profile.get('total_reviews', 0)} "
            f"| {average} | {positive} | {negative} |"
        )
    lines += [
        "",
        "Файлы: `output/doctu_reviews.json`, `output/doctu_reviews.csv`.",
        "",
    ]
    return "\n".join(lines)


def append_summary(summary_path: Path, section: str) -> None:
    _append_summary(summary_path, section, start=START, end=END)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="output/doctu_reviews.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--append-summary", action="store_true")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = flatten(data)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "doctu_reviews.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    section = render(data, rows)
    (out / "doctu_summary.md").write_text(section, encoding="utf-8")
    if args.append_summary:
        append_summary(out / "summary.md", section)
    print(f"ДОКТУ: {len(data.get('profiles', []))} профилей, {len(rows)} записей → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
