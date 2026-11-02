import csv
import json
from pathlib import Path

import pytest

import collect_reviews
import doctu_export
import rate_doctors
import yandex_export


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def manifest_path():
    return ROOT / "doctors.json"


def test_manifest_has_all_doctors_and_valid_platforms(manifest_path):
    doctors = collect_reviews.load_manifest(manifest_path)
    assert len(doctors) == 15
    ids = {doctor["id"] for doctor in doctors}
    assert len(ids) == 15
    targets = list(collect_reviews.iter_targets(doctors))
    assert len(targets) == 30
    assert all(target["platform"] in collect_reviews.PLATFORMS for target in targets)
    assert all("expected_name" in doctor for doctor in doctors)
    # Профили Яндекса лежат отдельным полем и не попадают в запросы к API.
    assert sum(1 for doctor in doctors if "yandex_profile" in doctor) == 6
    # ДОКТУ тоже лежит отдельным полем и не должен попадать в запросы к API.
    assert sum(1 for doctor in doctors if "doctu_profile" in doctor) == 1
    assert all(
        "yandex.ru" not in str(target["url"]) and "doctu.ru" not in str(target["url"])
        for target in targets
        if target["url"]
    )


def test_verify_title_detects_namesake():
    assert collect_reviews.verify_title(
        "Гордеева Алла Владимировна, эндокринолог - отзывы", "Гордеева Алла Владимировна"
    ) == []
    missing = collect_reviews.verify_title(
        "Гордеева Алла Юрьевна - акушер: отзывы", "Гордеева Алла Владимировна"
    )
    assert "Владимировна" in missing


def test_rating_conversion():
    assert collect_reviews.rating_to_5(100) == 5.0
    assert collect_reviews.rating_to_5(80) == 4.0
    assert collect_reviews.rating_to_5(10) == 0.5
    # Нуль — реальная оценка «Ужасно» на ПроДокторов, а не отсутствие данных.
    assert collect_reviews.rating_to_5(0) == 0.0
    assert collect_reviews.rating_to_5(-1) == ""
    assert collect_reviews.rating_to_5("bad") == ""


def test_flatten_and_outputs(tmp_path):
    records = [
        {
            "doctor_id": "test",
            "doctor": "Тестов Врач",
            "clinic": "Клиника",
            "platform": "sberzdorovie",
            "url": "https://docdoc.ru/doctor/Test",
            "title": "Тестов Врач - эндокринолог",
            "reviews": [
                {"id": "1", "name": "Анна", "date": "2024-01-01", "date_beauty": "1 января 2024",
                 "message": "Хорошо", "rating": 100, "source": "sberzdorovie"},
                {"id": "2", "name": "Борис", "date": "2024-01-02", "date_beauty": "2 января 2024",
                 "message": "Плохо", "rating": 20, "source": "sberzdorovie"},
            ],
            "collected_at": "2026-01-01T00:00:00+00:00",
            "warnings": [],
        }
    ]
    rows = collect_reviews.flatten_records(records)
    assert len(rows) == 2
    assert rows[0]["doctor"] == "Тестов Врач"
    assert rows[0]["rating_5"] == 5.0
    assert rows[1]["rating_5"] == 1.0

    issues = [{"doctor": "Другой Врач", "doctor_id": "other", "platform": "prodoctorov", "reason": "URL не указан"}]
    collect_reviews.save_outputs(records, issues, 15, tmp_path)
    assert (tmp_path / "reviews.json").exists()
    assert (tmp_path / "errors.json").exists()
    assert (tmp_path / "reviews.csv").exists()
    assert (tmp_path / "summary.md").exists()
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Всего отзывов: **2**" in summary
    assert "Другой Врач" in summary
    with (tmp_path / "reviews.csv").open(encoding="utf-8-sig", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert len(csv_rows) == 2


def test_offline_regeneration_from_saved_data(tmp_path):
    records = json.loads((ROOT / "output" / "reviews.json").read_text(encoding="utf-8"))
    issues = json.loads((ROOT / "output" / "errors.json").read_text(encoding="utf-8"))
    collect_reviews.save_outputs(records, issues, 15, tmp_path)
    assert (tmp_path / "summary.md").read_text(encoding="utf-8").count("|") > 10
    assert len(collect_reviews.flatten_records(records)) == 77
    assert len(records) == 16
    # Нулевая оценка теперь участвует в сводке как отрицательный отзыв.
    assert "Всего отзывов: **77**" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_yandex_export(tmp_path):
    data = json.loads((ROOT / "output" / "yandex_reviews.json").read_text(encoding="utf-8"))
    rows = yandex_export.flatten(data)
    assert len(data["profiles"]) == 6
    assert len(rows) == 24
    assert sum(1 for row in rows if row["source_kind"] == "yandex_clinic") == 8
    assert all(row["rating_100"] == row["rating_5"] * 20 for row in rows)
    section = yandex_export.render(data, rows)
    assert "Собрано отзывов: **24**" in section
    assert "Яндекс.Медицина" in section

    (tmp_path / "summary.md").write_text("# Отзывы\n", encoding="utf-8")
    yandex_export.append_summary(tmp_path / "summary.md", section)
    yandex_export.append_summary(tmp_path / "summary.md", section)
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert text.count(yandex_export.START) == 1
    assert text.count(yandex_export.END) == 1


def test_yandex_csv_rows(tmp_path):
    assert yandex_export.main(["--output-dir", str(tmp_path)]) == 0
    with (tmp_path / "yandex_reviews.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 24
    assert rows[0]["platform"] == "yandex_medicine"
    assert "rating_100" in rows[0]


def test_links_from_user_are_verified_and_added():
    records = json.loads((ROOT / "output" / "reviews.json").read_text(encoding="utf-8"))
    by_key = {(record["doctor_id"], record["platform"]): record for record in records}
    expectations = {
        ("vyatkina_irina", "prodoctorov"): 10,
        ("gordeeva_alla", "prodoctorov"): 0,
        ("skorik_elena", "prodoctorov"): 6,
    }
    for key, count in expectations.items():
        record = by_key[key]
        assert len(record["reviews"]) == count
        assert collect_reviews.verify_title(record["title"], record["expected_name"]) == []
        assert record["url"].startswith("https://prodoctorov.ru/moskva/vrach/")
        assert all(0 <= review["rating"] <= 100 for review in record["reviews"])
    assert "Поликлиника.ру" in " ".join(by_key[("skorik_elena", "prodoctorov")]["warnings"])
    assert any("0 отзывов" in warning for warning in by_key[("gordeeva_alla", "prodoctorov")]["warnings"])


def test_doctu_export(tmp_path):
    data = json.loads((ROOT / "output" / "doctu_reviews.json").read_text(encoding="utf-8"))
    rows = doctu_export.flatten(data)
    assert len(data["profiles"]) == 1
    assert len(rows) == 8
    assert sum(1 for row in rows if row["source_kind"] == "doctu_doctor") == 5
    assert sum(1 for row in rows if row["source_kind"] == "doctu_clinic") == 3
    assert all(row["rating_100"] == row["rating_5"] * 20 for row in rows)
    section = doctu_export.render(data, rows)
    assert "Отзывов о враче: **5**" in section
    assert "doctu_clinic" in section
    (tmp_path / "summary.md").write_text("# Отзывы\n", encoding="utf-8")
    doctu_export.append_summary(tmp_path / "summary.md", section)
    doctu_export.append_summary(tmp_path / "summary.md", section)
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert text.count(doctu_export.START) == 1
    assert text.count(doctu_export.END) == 1


def test_doctu_csv_rows(tmp_path):
    assert doctu_export.main(["--output-dir", str(tmp_path)]) == 0
    with (tmp_path / "doctu_reviews.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 8
    assert rows[0]["platform"] == "doctu"
    assert rows[0]["source_kind"] == "doctu_doctor"


def test_offline_preserves_third_party_sections(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Старый отчёт\n\n<!-- YANDEX-START -->\nЯндекс\n<!-- YANDEX-END -->\n\n"
        "<!-- DOCTU-START -->\nДОКТУ\n<!-- DOCTU-END -->\n\n"
        "<!-- RATINGS-START -->\nОценки\n<!-- RATINGS-END -->\n",
        encoding="utf-8",
    )
    records = [{"doctor_id": "x", "doctor": "X", "clinic": "", "platform": "prodoctorov",
                "url": "", "title": "", "reviews": [], "warnings": []}]
    collect_reviews.save_outputs(records, [], 15, tmp_path)
    text = summary.read_text(encoding="utf-8")
    assert "Всего отзывов: **0**" in text
    assert text.count("<!-- YANDEX-START -->") == 1
    assert text.count("<!-- DOCTU-START -->") == 1
    assert text.count("<!-- RATINGS-START -->") == 1
    assert "Яндекс" in text and "ДОКТУ" in text and "Оценки" in text


def test_ratings_cover_all_doctors():
    data = json.loads((ROOT / "output" / "doctor_ratings.json").read_text(encoding="utf-8"))
    ratings = data["ratings"]
    assert len(ratings) == 18
    assert len({row["doctor_id"] for row in ratings}) == 18
    assert [row["rank"] for row in ratings] == list(range(1, 19))
    assert sum(row["cohort"] == "original" for row in ratings) == 15
    assert sum(row["cohort"] == "added" for row in ratings) == 3
    assert {"zakharyan_elizaveta", "anohina_liliya", "avramenko_marina"} <= {
        row["doctor_id"] for row in ratings
    }
    for row in ratings:
        assert 0 <= row["score_10"] <= 10
        assert sum(row["components"].values()) == row["raw_score"]
        assert row["penalty"] in (0, 1)
        assert row["adjusted_raw"] == max(row["raw_score"] - row["penalty"], 0)
        assert row["score_10"] == round(row["adjusted_raw"] / 12 * 10, 1)
    assert ratings[0]["score_10"] >= ratings[-1]["score_10"]
    assert [row["doctor_id"] for row in ratings[:3]] == [
        "ermakova_evgeniya", "dzhatieva_diana", "tarasova_tatyana"
    ]
    by_id = {row["doctor_id"]: row for row in ratings}
    assert by_id["tarasova_tatyana"]["penalty"] == 1
    assert by_id["tarasova_tatyana"]["raw_score"] == 11
    assert by_id["ermakova_evgeniya"]["penalty"] == 0
    assert by_id["dzhatieva_diana"]["negative_topic_reviews"] == 0


def test_rating_rules_are_explicit():
    profile = {
        "specialty": "эндокринолог",
        "courses": ["2021 «Метаболические нарушения. Ожирение. Преддиабет», РАЭ"],
        "practice_focus": [],
        "education": [
            {"institution": "РУДН", "year": 2020, "kind": "Базовое образование", "program": "Лечебное дело"},
            {"institution": "МОНИКИ", "year": 2022, "kind": "Ординатура", "program": "Эндокринология"},
        ],
        "degree": None, "honors": False, "documents_verified": True,
        "associations": [], "experience_years": 6, "page_updated": "2026-10-29",
    }
    assert rate_doctors.score_topic(profile) == 2
    assert rate_doctors.score_training(profile) == 1
    assert rate_doctors.score_experience(profile) == 1
    profile["education"].append(
        {"institution": "НМИЦ эндокринологии", "year": 2024, "kind": "Аспирантура", "program": "Эндокринология"}
    )
    assert rate_doctors.score_training(profile) == 2

    gynecologist = {**profile, "specialty": "гинеколог", "courses": [], "degree": "к.м.н."}
    assert rate_doctors.score_topic(gynecologist) == 0
    assert rate_doctors.score_university(gynecologist) <= 1

    reviews = [
        {"message": "вес, метаболизм", "rating_5": 5.0, "layer": "doctu", "usable": True},
        {"message": "набор веса", "rating_5": 5.0, "layer": "doctu", "usable": True},
        {"message": "контроль сахара", "rating_5": 4.8, "layer": "yandex", "usable": True},
        {"message": "отзыв о клинике", "rating_5": 1.0, "layer": "doctu", "usable": False},
    ]
    e, avg, total, main, external, hits = rate_doctors.score_review_layers(reviews)
    # Три внешних отзыва дали бы 2 балла, но без основного слоя максимум E = 1.
    assert (e, avg, total, main, external, hits) == (1, 4.93, 3, 0, 3, 3)
    assert rate_doctors.score_topic_reviews(reviews) == 2
    negative = [
        {"message": "резкая прибавка веса, врач ничего не назначил", "rating_5": 1.0, "layer": "yandex", "usable": True},
        {"message": "перепутала сахарный диабет с предынфарктом", "rating_5": 1.0, "layer": "yandex", "usable": True},
    ]
    assert rate_doctors.score_topic_reviews(negative) == 0
    assert rate_doctors.count_topic_reviews(negative) == (0, 2)


def test_ratings_csv_and_summary_section(tmp_path):
    assert rate_doctors.main([
        "--output-dir", str(tmp_path), "--reviews-dir", str(ROOT / "output")
    ]) == 0
    with (tmp_path / "doctor_ratings.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 18
    assert rows[0]["score_10"]
    assert "penalty" in rows[0] and "negative_topic_reviews" in rows[0]
    assert "cohort" in rows[0]
    text = (tmp_path / "doctor_ratings.md").read_text(encoding="utf-8")
    assert "Не медицинская рекомендация" in text
    assert "Как читать результат" in text
    assert "Новые участники сравнения" in text
    assert "не повышают D" in text
    detail = (tmp_path / "top3_detail.md").read_text(encoding="utf-8")
    for name in ("Ермакова", "Джатиева", "Тарасова"):
        assert name in detail
    assert "11 кг" in detail
    assert "Метаболические нарушения. Ожирение. Преддиабет" in detail
    assert "Что уточнить на приёме" in detail
    extra = (tmp_path / "comparison_extra.md").read_text(encoding="utf-8")
    for name in ("Захарян", "Анохина", "Авраменко"):
        assert name in extra
    assert "extra_doctor_reviews.json" in extra
    summary = tmp_path / "summary.md"
    assert rate_doctors.main([
        "--output-dir", str(tmp_path), "--reviews-dir", str(ROOT / "output"), "--append-summary"
    ]) == 0
    rate_doctors.append_summary(summary, "кратко")
    content = summary.read_text(encoding="utf-8")
    assert content.count(rate_doctors.START) == 1
    assert content.count(rate_doctors.END) == 1


def test_extra_review_layer_is_separate():
    data = json.loads((ROOT / "output" / "extra_doctor_reviews.json").read_text(encoding="utf-8"))
    assert len(data["profiles"]) == 3
    by_id = {profile["doctor_id"]: profile for profile in data["profiles"]}
    assert by_id["avramenko_marina"]["total_reviews"] == 8
    assert by_id["anohina_liliya"]["total_reviews"] == 3
    assert all(review.get("rating_assumed") for profile in data["profiles"] for review in profile["reviews"]
               if review["source_kind"] == "clinic_doctor")
    layers = rate_doctors.load_review_layers(ROOT / "output")
    assert sum(1 for item in layers["avramenko_marina"] if item["usable"]) == 8
    assert all(item["layer"] != "main" for item in layers["avramenko_marina"])
    # Манифест сбора через API не расширялся: новые врачи добавлены только в сравнение.
    manifest = collect_reviews.load_manifest(ROOT / "doctors.json")
    assert len(manifest) == 15
