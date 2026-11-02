#!/usr/bin/env python3
"""Экспорт дополнительных отзывов Яндекс.Медицины в CSV и Markdown.

Источник: ``output/yandex_reviews.json`` — вручную сохранённый встроенный JSON
(``ugc.initState.reviews.items``) публичных страниц врачей. Этот источник НЕ
проходит через ``/api/v1/getReviews`` репозитория: Яндекс не входит в
``SOURCE_CONFIGS`` и для него нет коллектора, поэтому данные лежат отдельно.

Запуск:
    python yandex_export.py                 # CSV + yandex_summary.md
    python yandex_export.py --append-summary  # ещё и секция в output/summary.md
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

CSV_FIELDS = [
    "doctor_id", "doctor", "clinic", "platform", "profile_url",
    "total_reviews", "fetched_reviews", "review_id", "author", "date_beauty",
    "rating_5", "rating_100", "source_kind", "source_label",
    "clinic_at_review", "message",
]

START = "<!-- YANDEX-START -->"
END = "<!-- YANDEX-END -->"


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
    total = sum(p.get("total_reviews", 0) for p in profiles)
    clinic_kind = sum(1 for row in rows if row["source_kind"] == "yandex_clinic")
    lines = [
        "# Дополнительно: Яндекс.Медицина",
        "",
        "Отзывы взяты из встроенного JSON публичных страниц Яндекс.Медицины, а не из",
        "`/api/v1/getReviews`: в репозитории нет коллектора для Яндекса и домен не разрешён",
        "в `SOURCE_CONFIGS`. Отзывы с `source_kind=yandex_clinic` изначально написаны о клинике",
        "и привязаны к врачу Яндексом — они явно помечены.",
        "",
        f"- Профилей врачей: **{len(profiles)}**",
        f"- Собрано отзывов: **{len(rows)}** (из **{total}** заявленных на страницах)",
        f"- Из отзывов о клинике: **{clinic_kind}**",
        "- Шкала Яндекса 1–5 переведена в `rating_100 = оценка × 20` для совместимости с основной выгрузкой.",
        "",
        "> Страница отдаёт первые 5 отзывов (`fetchedReviewsCount=5`); остальные подгружаются",
        "> виджетом `reviews.yandex.ru`, публичного API у Яндекса для этого нет.",
        "",
        "| Врач | Профиль | Отзывов собрано / заявлено | Средний балл | Положительных | Отрицательных |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for profile in profiles:
        own = [row for row in rows if row["doctor_id"] == profile.get("doctor_id", "")]
        ratings = [float(row["rating_5"]) for row in own if isinstance(row["rating_5"], (int, float))]
        average = f"{sum(ratings) / len(ratings):.2f}" if ratings else "—"
        positive = sum(1 for value in ratings if value >= 4)
        negative = sum(1 for value in ratings if value < 3)
        lines.append(
            f"| {profile.get('doctor', '')} | [{profile.get('doctor_id', '')}]({profile.get('url', '')}) "
            f"| {len(own)} / {profile.get('total_reviews', 0)} | {average} | {positive} | {negative} |"
        )
    lines += ["", "Файлы: `output/yandex_reviews.json`, `output/yandex_reviews.csv`.", ""]
    return "\n".join(lines)


def append_summary(
    summary_path: Path,
    section: str,
    start: str = START,
    end: str = END,
) -> None:
    """Идемпотентно вставляет секцию, не затирая секции других источников."""
    text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    if start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = before.rstrip() + "\n"
    else:
        after = ""
    summary_path.write_text(
        text.rstrip() + "\n\n" + start + "\n" + section.strip() + "\n" + end + "\n" + after.lstrip("\n"),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="output/yandex_reviews.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--append-summary", action="store_true")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = flatten(data)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "yandex_reviews.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    section = render(data, rows)
    (out / "yandex_summary.md").write_text(section, encoding="utf-8")
    if args.append_summary:
        append_summary(out / "summary.md", section)
    print(f"Яндекс: {len(data.get('profiles', []))} профилей, {len(rows)} отзывов → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
