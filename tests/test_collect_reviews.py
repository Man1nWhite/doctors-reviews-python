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
    # Неоценённый отзыв явно помечается unrated=True и не превращается в 0.0.
    assert collect_reviews.rating_to_5(0, unrated=True) == ""
    assert collect_reviews.rating_to_5(-1) == ""
    assert collect_reviews.rating_to_5("bad") == ""


def test_unrated_and_duplicate_reviews_excluded_from_averages(tmp_path):
    records = json.loads((ROOT / "output" / "reviews.json").read_text(encoding="utf-8"))
    ogneva = [record for record in records if record["doctor_id"] == "ogneva_natalya"]
    all_reviews = [review for record in ogneva for review in record["reviews"]]
    assert len(all_reviews) == 10
    assert sum(1 for review in all_reviews if review.get("unrated")) == 1
    assert sum(1 for review in all_reviews if review.get("duplicate_of")) == 1
    summary = collect_reviews.render_summary(records, [], 15)
    assert "| Огнева Наталья Анатольевна | Медицинский центр \"MEDSWISS на ул. Гашека д.9\" | prodoctorov, sberzdorovie | 10 | 4.00 | 6 | 2 |" in summary
    rows = collect_reviews.flatten_records(ogneva)
    unrated_rows = [row for row in rows if row["review_id"] == "12599222"]
    assert len(unrated_rows) == 1
    assert unrated_rows[0]["rating_100"] == ""
    assert unrated_rows[0]["rating_5"] == ""


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
    assert [row["doctor_id"] for row in ratings[:5]] == [
        "ermakova_evgeniya",
        "dzhatieva_diana",
        "ogneva_natalya",
        "sevostyanenko_elizaveta",
        "tarasova_tatyana",
    ]
    by_id = {row["doctor_id"]: row for row in ratings}
    assert by_id["tarasova_tatyana"]["penalty"] == 1
    assert by_id["tarasova_tatyana"]["raw_score"] == 10
    assert by_id["tarasova_tatyana"]["positive_topic_reviews"] == 1
    assert by_id["tarasova_tatyana"]["negative_topic_reviews"] == 3
    assert by_id["ermakova_evgeniya"]["penalty"] == 0
    assert by_id["dzhatieva_diana"]["negative_topic_reviews"] == 0
    # Условные оценки Авраменко не дают баллов E; в отличие от Захарян, её отзывы без числовой оценки.
    assert by_id["zakharyan_elizaveta"]["score_10"] == 5.0
    assert by_id["avramenko_marina"]["score_10"] == 4.2
    assert by_id["zakharyan_elizaveta"]["rank"] < by_id["avramenko_marina"]["rank"]
    assert by_id["avramenko_marina"]["assumed_reviews"] == 8


def test_assumed_ratings_do_not_affect_scoring_or_tie_breaks(tmp_path):
    reviews = [
        {"message": "приятный прием", "rating_5": 4.0, "layer": "main", "usable": True},
        {"message": "спасибо врачу", "rating_5": 4.0, "layer": "main", "usable": True},
        {"message": "снижение веса", "rating_5": 5.0, "layer": "extra", "usable": True,
         "rating_assumed": True},
        {"message": "контроль сахара", "rating_5": 5.0, "layer": "extra", "usable": True,
         "rating_assumed": True},
    ]
    e, average, total, main, external, hits = rate_doctors.score_review_layers(reviews)
    assert (e, average, total, main, external, hits) == (0, 4.5, 4, 2, 2, 2)
    assert rate_doctors.score_topic_reviews(reviews) == 0
    assert rate_doctors.count_topic_reviews(reviews) == (0, 0)

    (tmp_path / "extra_doctor_reviews.json").write_text(json.dumps({"profiles": [
        {"doctor_id": "z_assumed", "reviews": [
            {"message": "положительно", "rating_5": 5.0, "rating_assumed": True}
        ]}
    ]}), encoding="utf-8")
    profiles = {"profiles": [
        {"doctor_id": "z_assumed", "name": "Assumed", "experience_years": 0},
        {"doctor_id": "a_unrated", "name": "No rating", "experience_years": 0},
    ]}
    ratings, _ = rate_doctors.build_ratings(profiles, tmp_path)
    assert [row["doctor_id"] for row in ratings] == ["a_unrated", "z_assumed"]
    assert ratings[1]["score_10"] == 0.0
    assert ratings[1]["avg_rating_5"] == 5.0
    assert ratings[1]["verified_avg_rating_5"] == 0.0


def test_duplicate_flags_are_honored_in_every_review_layer(tmp_path):
    (tmp_path / "reviews.json").write_text(json.dumps([
        {"doctor_id": "test", "reviews": [{"message": "main", "rating": 100,
         "duplicate_of": "original"}]}
    ]), encoding="utf-8")
    (tmp_path / "doctu_reviews.json").write_text(json.dumps({"profiles": [
        {"doctor_id": "test", "reviews": [{"message": "doctu", "rating_5": 5,
         "source_kind": "doctu_doctor", "duplicate_of": "original"}]}
    ]}), encoding="utf-8")
    (tmp_path / "yandex_reviews.json").write_text(json.dumps({"profiles": [
        {"doctor_id": "test", "reviews": [{"message": "yandex", "rating_5": 5,
         "source_kind": "yandex_doctor", "duplicate_of": "original"}]}
    ]}), encoding="utf-8")
    (tmp_path / "extra_doctor_reviews.json").write_text(json.dumps({"profiles": [
        {"doctor_id": "test", "reviews": [{"message": "extra", "rating_5": 5,
         "duplicate_of": "original"}]}
    ]}), encoding="utf-8")
    layers = rate_doctors.load_review_layers(tmp_path)["test"]
    assert len(layers) == 4
    assert all(not item["usable"] for item in layers)
    assert rate_doctors.score_review_layers(layers)[:3] == (0, 0.0, 0)


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


def test_review_focus_word_boundaries_and_university_rule():
    false_positives = [
        "очень известный эндокринолог",
        "помогла провести обследование и повесить холтер",
        "восстановила душевное равновесие, всё проверено временем",
    ]
    for sample in false_positives:
        assert rate_doctors.REVIEW_FOCUS.search(sample) is None

    true_positives = [
        "обратилась из-за набора веса и метаболизма",
        "помогла снизить вес без давления, направила по РПП",
        "объяснила как справиться с колебаниями сахара",
        "наблюдаюсь с преддиабетом и инсулинорезистентностью",
    ]
    for sample in true_positives:
        assert rate_doctors.REVIEW_FOCUS.search(sample) is not None

    regional_with_signals = {
        "specialty": "эндокринолог",
        "education": [
            {"institution": "Иркутский ГМУ", "year": 2013, "kind": "Базовое образование", "program": "Лечебное дело"},
            {"institution": "Бурятский ГУ", "year": 2014, "kind": "Интернатура", "program": "Эндокринология"},
        ],
        "courses": ["c1", "c2", "c3", "c4"],
        "documents_verified": True,
        "honors": False,
        "degree": None,
        "associations": [],
    }
    assert rate_doctors.score_university(regional_with_signals) == 1


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
    for name in ("Ермакова", "Джатиева", "Огнева"):
        assert name in detail
    assert "Севостьяненко" in detail
    assert "Метаболические нарушения. Ожирение. Преддиабет" in detail
    assert "Что уточнить на приёме" in detail
    extra = (tmp_path / "comparison_extra.md").read_text(encoding="utf-8")
    for name in ("Захарян", "Анохина", "Авраменко"):
        assert name in extra
    assert "не опубликованы" in extra
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
