#!/usr/bin/env python3
"""Оценка врачей по 10-балльной шкале для запроса «предиабет, ожирение».

Это НЕ клинический рейтинг и НЕ медицинская рекомендация. Балл — это
«индекс соответствия открытого профиля заданной теме»: он показывает, насколько
публичные данные (образование, курсы, практика, отзывы) подтверждают работу с
предиабетом/ожирением. Отсутствие данных в открытых источниках не означает
отсутствие квалификации.

Входные данные:
    output/doctor_profiles.json      — образование/курсы/стаж (собрано со страниц)
    output/reviews.json              — основной слой отзывов
    output/doctu_reviews.json        — отзывы ДОКТУ о враче
    output/yandex_reviews.json       — отзывы Яндекса (кроме отзывов о клинике)
    output/extra_doctor_reviews.json — отзывы врачей с неподдерживаемых доменов

Методика: 6 критериев, каждый 0..2, максимум 12 → нормируется в 0..10.
За ≥2 негативных отзыва именно по теме вычитается 1 штрафной балл.

    A Профильное обучение   — курсы/программы по ожирению, преддиабету,
                              СД2, диетологии, метаболизму
    B Ординатура/аспирантура — профильная последипломная подготовка
    C Где учились           — профильный вуз/центр + академические сигналы
                              (с отличием, к.м.н./аспирантура, ассоциации,
                              проверенные документы, ≥4 курсов, топ-вуз + топ-ординатура)
    D Практика по теме      — тема звучит в ПОЛОЖИТЕЛЬНЫХ отзывах (по границам слов)
    E Отзывы               — средний балл и объём по всем пригодным оценённым слоям
    F Стаж/актуальность    — опыт и свежесть страницы

Запуск:
    python rate_doctors.py                     # JSON + CSV + Markdown + разбор топ-3
    python rate_doctors.py --append-summary    # ещё секция в output/summary.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from yandex_export import append_summary as _append_summary

START = "<!-- RATINGS-START -->"
END = "<!-- RATINGS-END -->"

CRITERIA = {
    "A": "Профильное обучение (ожирение/преддиабет/СД2/диетология)",
    "B": "Ординатура и аспирантура",
    "C": "Где учились: вуз и академические сигналы",
    "D": "Тема в положительных отзывах",
    "E": "Отзывы: средний балл и объём",
    "F": "Стаж и актуальность профиля",
}

STRONG_FOCUS = re.compile(
    r"ожирен|преддиабет|предиабет|сахарн\w+ диабет|диетолог|метаболическ|"
    r"инсулинорезистент|нглт|\bflash\b|бариатри|\bрпп\b|школ\w+ (?:обучения )?больн\w+|"
    r"снижени\w+ веса|лишн\w+ вес|повышенн\w+ сахар",
    re.IGNORECASE,
)
GENERAL_FOCUS = re.compile(r"эндокринолог", re.IGNORECASE)
REVIEW_FOCUS = re.compile(
    r"\b(?:"
    r"предиабет\w*|преддиабет\w*|диабет\w*|инсулинорезистент\w*|"
    r"глюкоз\w*|гликеми\w*|гликир\w*|сахар(?:а|у|ом|е)?|"
    r"ожирен\w*|похуде\w*|вес(?:а|у|ом|е)?|масс\w+\s+тела|"
    r"метабол\w*|диет\w*|рпп|"
    r"инсулин\w*|метформин\w*|оземпик\w*|маунжаро|семаглутид\w*|тирзепатид\w*|"
    r"кортизол\w*"
    r")\b",
    re.IGNORECASE,
)
TOP_INSTITUTIONS = (
    "Сеченова", "Пирогова", "РНИМУ", "Дружбы народов", "РУДН",
    "НМИЦ", "Эндокринологический научный центр", "ЭНЦ",
    "МОНИКИ", "РМАНПО", "РМАПО", "Дедова",
)

CSV_FIELDS = [
    "rank", "doctor_id", "doctor", "cohort", "score_10", "A", "B", "C", "D", "E", "F",
    "raw_score", "penalty", "adjusted_raw",
    "avg_rating_5", "verified_avg_rating_5", "review_count", "main_reviews",
    "external_reviews", "assumed_reviews",
    "positive_topic_reviews", "negative_topic_reviews",
    "base_university", "residency", "focus_courses", "specialty",
    "experience_years", "page_updated", "rationale",
]

# Содержательные выводы по лидерам. Цифры подтягиваются из расчёта автоматически.
TOP3_NOTES = {
    "ermakova_evgeniya": {
        "headline": "Самое прямое попадание в запрос при идеальных, но пока коротких отзывах",
        "strengths": [
            "Единственный из 18 врачей с курсом, который называется буквально «Метаболические нарушения. Ожирение. Преддиабет» (Российская ассоциация эндокринологов, 2021).",
            "Вокруг него выстроен целый кластер: СД 2 типа (МОНИКИ 2022, ЦГМА 2023), ингибиторы НГЛТ-2, Flash-мониторинг гликемии Abbott (дважды), междисциплинарный подход к СД2.",
            "Переподготовка по УЗД позволяет самой смотреть щитовидную железу и органы брюшной полости — полезно при оценке ожирения и стеатоза печени.",
            "Документы (диплом, свидетельство об ординатуре) проверены ПроДокторов; страница обновлена 29.10.2026.",
            "Оба отзыва на ПроДокторов — 5.0 и оба именно про «вес, метаболизм» / «набор веса, усталость»; все 5 отзывов Яндекса также 5.0.",
        ],
        "risks": [
            "Стаж 6 лет (выпуск 2020, ординатура 2022) — самый короткий среди лидеров топ-3; нет к.м.н., аспирантуры и ассоциаций.",
            "Всего 2 отзыва в основном слое и 3 пригодных на Яндексе: выборка маленькая и очень свежая (октябрь 2026, Яндекс — 2023–2026).",
            "Ординатура в МОНИКИ, а не в НМИЦ эндокринологии; базовый вуз — РУДН без отличия.",
            "В Яндексе 2 из 5 отзывов помечены как отзывы о клинике — в оценку врача они не вошли.",
        ],
        "questions": [
            "Какой алгоритм обследования при преддиабете: НbA1c, глюкоза натощак, инсулин/HOMA-IR, липидный профиль?",
            "Работает ли с инсулинорезистентностью без сразу назначаемых препаратов, и когда считает нужной терапию?",
            "Есть ли опыт ведения пациентов на аналогах ГПП-1 и как организован мониторинг (в т.ч. Flash)?",
        ],
        "verdict": "Лучший выбор, если приоритет — именно современная доказательная работа с предиабетом/весом и важны свежие профильные курсы; с поправкой на небольшой стаж и короткую историю отзывов.",
    },
    "dzhatieva_diana": {
        "headline": "Лучший баланс профильного образования, диетологии и реальных отзывов о преддиабете",
        "strengths": [
            "Ординатура в НМИЦ эндокринологии (2019) плюс отдельная переподготовка по диетологии в том же 2019 году — редкое сочетание для запроса «ожирение».",
            "Диплом СОГМА с отличием (2017), документы проверены, членство в European Society of Endocrinology и Российской ассоциации эндокринологов.",
            "10 отзывов в основном слое, средний 4.90, без единого отрицательного; страница обновлена 05.06.2026.",
            "Прямые тематические отзывы: пациентка с преддиабетом пишет «благодаря ей, ко мне ещё не пришёл сахарный диабет. Довольствуюсь преддиабетом!»; две пациентки отдельно отмечают РПП-информированность и работу со снижением веса без давления.",
            "Видна культура «не назначать лишнего»: пересмотр Бетесда-4 → Бетесда-2 и отмена ненужной операции, отказ сразу выписывать препараты для похудения с направлением к психологу при РПП.",
        ],
        "risks": [
            "Стаж 9 лет — меньше, чем у Огневой/Тарасовой/Быстровой; нет к.м.н. и аспирантуры (только ординатура).",
            "Значительная часть отзывов — щитовидная железа, гипофиз, ТТГ, а не метаболизм: тематических положительных отзывов 3 из 10.",
            "Нет отдельного курса по СД2/НГЛТ-2/мониторингу гликемии, который есть у Ермаковой.",
            "Профиль Яндекс.Медицины для этого врача в выгрузке отсутствует — нет второго независимого слоя отзывов.",
        ],
        "questions": [
            "Как выстроен план при преддиабете: питание/физическая активность, контроль НbA1c, метформин — и при каких значениях?",
            "Работает ли в связке с психологом/диетологом при РПП и эмоциональном переедании?",
            "Какой опыт ведения пациентов с ожирением и инсулинорезистентностью вне беременности?",
        ],
        "verdict": "Наиболее устойчивый выбор «по совокупности»: сильная школа НМИЦ + диетология + 10 стабильно высоких отзывов, включая прямой преддиабет. Уступает Ермаковой только по адресности курсов и чуть более низкому среднему (4.90 против 5.00).",
    },
    "ogneva_natalya": {
        "headline": "Сильная академическая база Сеченовского + ЭНЦ, но резкий конфликт свежих отзывов",
        "strengths": [
            "Полная академическая траектория в Первом МГМУ им. И.М. Сеченова: базовое образование (2007), ординатура (2009), аспирантура по эндокринологии (2013), к.м.н.",
            "Профильный курс ФГБУ ЭНЦ «Ведение школ для больных ожирением, сахарным диабетом 1 и 2 типов, остеопорозом» (2009) и повышение квалификации в РМАПО (2014) и НМИЦ эндокринологии (2019).",
            "Стаж 19 лет, страница обновлена в 2025 году.",
            "В старых отзывах СберЗдоровья (2017–2023) устойчивые 5.0, включая прямой положительный отзыв о коррекции колебаний сахара; на ПроДокторов также есть положительный отзыв без числовой оценки (`unrated=true`).",
        ],
        "risks": [
            "Ключевой риск: средний балл 3.50 из 12 уникальных оценённых отзывов проходит границу E=1 буквально на пороге, а на самом ПроДокторов рейтинг врача — 2.6 (из 11 заявленных отзывов в выдачу попали только 2).",
            "Свежие отзывы 2024–2025 годов резко контрастируют со старыми: два отзыва 1.0 на СберЗдоровье (включая жалобу пациента с диабетом на 2,5-часовое ожидание и категоричную коммуникацию по инсулину) и среднее 2.50 по 4 отзывам о враче на Яндексе.",
            "В дополнительном отзыве на клинику на Яндексе (`yandex_clinic`, 2.0, в балл врача не включён) пациентка прямо жалуется, что на приёме у Огневой не обсудили текущий вес, не назначили анализы и отправили к диетологу.",
            "Единственный профильный курс по школам ожирения и диабета датирован 2009 годом; более свежих адресных программ по ожирению/ГПП-1 на странице не указано.",
        ],
        "questions": [
            "Ведёт ли врач пациентов с ожирением и преддиабетом самостоятельно или направляет за коррекцией веса к отдельному диетологу?",
            "Какой стандартный план первичного обследования при наборе веса и подозрении на инсулинорезистентность?",
            "Сколько длится первичный приём и как организован разбор дневника питания и гликемии?",
        ],
        "verdict": "Входит в топ-3 за счёт максимальных баллов за академическую школу и 19-летний стаж (A2 B2 C2 F2), но по отзывам пациентов заметно уступает Ермаковой, Джатиевой и идущей четвёртой Севостьяненко (7.5/10 при среднем 5.00). Требует осторожной очной проверки стиля коммуникации.",
    },
    "sevostyanenko_elizaveta": {
        "headline": "Современный доказательный фокус на снижении веса и 7 отзывов 5.0 при малом стаже",
        "strengths": [
            "РостГМУ с отличием (2023) → ординатура в НМИЦ эндокринологии им. академика И.И. Дедова (2025), член РАЭ, проверенные документы.",
            "Свежий курс «Доказательная диетология» (клиника Фомина, 2026) и насыщенный портфель из 14 курсов/конференций 2023–2026 гг.",
            "7 отзывов 5.0 на ПроДокторов, из них 3 предметно про снижение веса и избыточную массу тела (включая результат −7 кг за два месяца).",
        ],
        "risks": [
            "Стаж 3 года (критерий F=0) — единственный фактор, отделяющий её от 8.3/10.",
            "Все 7 отзывов датированы 2026 годом и собраны с одной площадки.",
        ],
        "questions": [
            "Какой опыт длительного удержания веса после отмены или снижения дозы препаратов?",
            "Какие обследования назначаются до старта медикаментозной терапии ожирения?",
        ],
        "verdict": "Главный практический кандидат сразу за лидерами, если важнее современная доказательная диетология и отзывы по снижению веса, чем многолетний стаж.",
    },
    "tarasova_tatyana": {
        "headline": "Максимум академической школы НМИЦ, но штраф за предметный негатив Яндекса",
        "strengths": [
            "Сильнейшая формальная база среди всех 18 врачей: РУДН с отличием (2011) → интернатура по терапии → ординатура (2014) и аспирантура (2017) в НМИЦ эндокринологии, к.м.н.",
            "С 2018 года работает в ФГБУ «НМИЦ Эндокринологии» Минздрава РФ и параллельно в MedSwiss; стаж 15 лет, вторая специальность «диетолог».",
            "На ПроДокторов 10 отзывов со средним 4.63; на Яндексе есть прямой отзыв 5.0 по уменьшению веса.",
        ],
        "risks": [
            "Три из пяти выгруженных отзывов Яндекса (1.0) предметно затрагивают тему: резкая прибавка +11 кг за 9 месяцев без назначенных анализов, жалоба на повышенный сахар и пропуск острого диабетического состояния — за это применён штраф −1.",
            "Положительных отзывов прямо по весу/глюкозе после исключения ложных совпадений по подстроке — 1 (D=1).",
        ],
        "questions": [
            "При +11 кг за 9 месяцев какие обследования назначаются обязательно (кортизол, инсулин, НbA1c, ТТГ, липиды, УЗИ)?",
            "Как разграничиваются коррекция образа жизни и медикаментозная терапия при ожирении/преддиабете?",
        ],
        "verdict": "Академически врач №1 (НМИЦ + к.м.н. + диетолог), но для запроса «похудение и преддиабет» требует очной проверки из-за резкого расхождения ПроДокторов (4.63) и Яндекса (2.60).",
    },
}


NEW_DOCTOR_IDS = [
    "zakharyan_elizaveta", "anohina_liliya", "avramenko_marina",
]

EXTRA_NOTES = {
    "zakharyan_elizaveta": {
        "headline": "Сильная профильная база Сеченовский + МОНИКИ и редкий курс по инсулинорезистентности, но почти нет отзывов",
        "strengths": [
            "Первый МГМУ им. И.М. Сеченова (2018) → ординатура по эндокринологии в МОНИКИ им. Владимирского (2020).",
            "Четыре курса 2021 года, включая «Роль инсулинорезистентности в развитии сахарного диабета и его осложнений» и «Метаболические нарушения при синдроме поликистозных яичников» — прямое попадание в фокус и C=2 по совокупности школы и курсов.",
            "Специализация на сайте клиники называет СД 1 и 2 типа, ожирение, скрининг поздних осложнений диабета, остеопороз и гиперпролактинемию.",
            "Стаж 8 лет по сайту клиники, приём в «Поликлиника.ру» на Таганской.",
        ],
        "risks": [
            "Всего 1 отзыв (НаПоправку, 5.0), и он про гипофиз/гормоны, а не про вес или преддиабет — критерии D и E равны 0.",
            "Стаж расходится по источникам: 8 лет на сайте клиники, 10 на НаПоправку, 4–6 на агрегаторах.",
            "Нет к.м.н., аспирантуры, ассоциаций, профиля на ПроДокторов/DocDoc в выгрузке; отзывы не проходят через API репозитория.",
            "В запросе пользователя опечатка «Елизвета»; корректное написание — Елизавета.",
        ],
        "questions": [
            "Как обследуется инсулинорезистентность и преддиабет (HOMA-IR, HbA1c, глюкозотолерантный тест)?",
            "Есть ли отдельный протокол ведения пациентов с ожирением и СПКЯ?",
            "Как организован скрининг поздних осложнений СД?",
        ],
        "verdict": "Делит 9–10 место (5.0/10) с Авраменко и стоит выше по подтверждённой числовой оценке: по образованию и адресным курсам заметно сильнее середины рейтинга, но подтвердить практику по предиабету/ожирению пока нечем — один отзыв не по теме.",
    },
    "anohina_liliya": {
        "headline": "Свежие положительные отзывы, но образование не опубликовано",
        "strengths": [
            "Врач-эндокринолог сети «Поликлиника.ру», приём на Таганской / Пролетарской, стаж 5 лет по сайту клиники.",
            "Три отзыва сентября 2026 года: благодарят за внимание, новый курс лечения и дополнительное обследование, называют «лучшим эндокринологом», отмечают понятные объяснения.",
            "Упоминается среди врачей, к которым направляют при синдроме Иценко — Кушинга.",
        ],
        "risks": [
            "Образование и повышение квалификации на найденных публичных страницах не опубликованы — это единственный профиль в сравнении с пустым блоком «Где учились».",
            "Нет профильных курсов по ожирению/преддиабету/СД2, нет ординатуры в профильном центре и академических сигналов.",
            "Отзывы общего характера, без упоминаний веса или гликемии; числовая оценка на сайте клиники отсутствует (условно 5.0, `rating_assumed=true`).",
            "Все отзывы приходят из внешнего слоя, поэтому критерий E ограничен 1 баллом, а при равенстве итогового балла приоритет отдаётся врачам с реальными числовыми оценками.",
        ],
        "questions": [
            "Где и когда получено базовое образование и ординатура по эндокринологии?",
            "Есть ли дополнительная подготовка по ожирению, преддиабету или СД 2 типа?",
            "Какой алгоритм обследования при наборе веса?",
        ],
        "verdict": "Нижняя группа сравнения (2.5/10) именно из-за отсутствия опубликованного образования: оценка отражает нехватку данных, а не уровень врача. Для осознанного выбора нужны документы об образовании или профиль на площадке с образовательным блоком.",
    },
    "avramenko_marina": {
        "headline": "Хорошая база Сеченовский + МОНИКИ и заявленная работа с СД/ожирением при 8 отзывах",
        "strengths": [
            "Первый МГМУ им. И.М. Сеченова (2019) → ординатура по эндокринологии в МОНИКИ им. Владимирского (2021).",
            "Специализация на сайте клиники прямо включает диагностику и лечение сахарного диабета 1 и 2 типа; на агрегаторе дополнительно указаны метаболический синдром и ожирение.",
            "8 положительных отзывов на сайте клиники за 2025–2026: «четко, коротко, по делу», «дает четкий план лечения», «направила на анализы, исключающие заболевание», «никаких лишних процедур не назначала».",
            "Стаж 7 лет, приём в «Поликлиника.ру» на Фрунзенской.",
        ],
        "risks": [
            "Отзывы не имеют числовой оценки (`rating_assumed=true`) и не упоминают вес/преддиабет — D=0, а E ограничен 1 баллом как у внешнего слоя.",
            "Нет к.м.н., аспирантуры и отдельных опубликованных курсов по ожирению/диабету; вторая специальность «диетолог» не заявлена.",
            "На ПроДокторов в блоках «Популярные эндокринологи» упоминается 1 отзыв, но отдельная страница врача не найдена — основной слой отзывов пуст.",
        ],
        "questions": [
            "Какой протокол при преддиабете и метаболическом синдроме: какие анализы, когда терапия?",
            "Есть ли дополнительная подготовка по диетологии/ожирению?",
            "Можно ли увидеть отзыв на ПроДокторов и результаты лечения в динамике?",
        ],
        "verdict": "Крепкая середина рейтинга (5.0/10): уровень образования и заявленная специализация по СД2/метаболическому синдрому подтверждают профиль, но лидерам она уступает из-за отсутствия адресных курсов и тематических отзывов с числовыми оценками.",
    },
}

DEFAULT_TOP3_NOTES = {
    "headline": "Врач вошёл в топ-3 по формальной сумме баллов",
    "strengths": ["См. разбивку критериев A–F и таблицу «Где учились» в основном отчёте."],
    "risks": ["Отдельный текстовый разбор для этого профиля не подготовлен."],
    "questions": ["Уточните на приёме перечень обследований при преддиабете и ожирении."],
    "verdict": "Оценка получена по общей методике без ручного содержательного разбора.",
}


def _institution_is_top(name: str) -> bool:
    return any(token.lower() in name.lower() for token in TOP_INSTITUTIONS)


def _education_kinds(profile: dict) -> list[str]:
    return [str(item.get("kind", "")).lower() for item in profile.get("education", [])]


def score_topic(profile: dict) -> int:
    text = " ".join(
        profile.get("courses", [])
        + profile.get("practice_focus", [])
        + [str(item.get("program", "")) for item in profile.get("education", [])]
        + [profile.get("specialty", "")]
    )
    specialty = profile.get("specialty", "").lower()
    if "гинеколог" in specialty and not STRONG_FOCUS.search(text):
        return 0
    if STRONG_FOCUS.search(text):
        return 2
    if GENERAL_FOCUS.search(text):
        return 1
    return 0


def score_training(profile: dict) -> int:
    kinds = _education_kinds(profile)
    has_ordinatura = any("ординатура" in kind for kind in kinds)
    has_aspirantura = any("аспирантура" in kind for kind in kinds)
    if has_ordinatura and (has_aspirantura or profile.get("degree")):
        return 2
    if has_ordinatura:
        return 1
    return 0


def score_university(profile: dict) -> int:
    education = profile.get("education", [])
    base = str(education[0].get("institution", "")) if education else ""
    kinds = _education_kinds(profile)
    has_ordinatura = any("ординатура" in kind for kind in kinds)
    has_aspirantura = any("аспирантура" in kind for kind in kinds)
    top_base = _institution_is_top(base)
    top_postgrad = any(
        _institution_is_top(str(item.get("institution", "")))
        for item in education[1:]
    )
    has_top_school = top_base or top_postgrad
    signals = sum([
        bool(profile.get("honors")),
        bool(profile.get("degree")) or has_aspirantura,
        bool(profile.get("documents_verified")),
        bool(profile.get("associations")),
        len(profile.get("courses", [])) >= 4,
        top_base and top_postgrad and has_ordinatura,
    ])
    result = 0
    if has_top_school and signals >= 2:
        result = 2
    elif has_top_school or signals >= 1:
        result = 1
    specialty = profile.get("specialty", "").lower()
    if "гинеколог" in specialty and "эндокринолог" not in specialty:
        result = min(result, 1)
    return result


def score_experience(profile: dict) -> int:
    years = int(profile.get("experience_years", 0))
    updated_year = int(str(profile.get("page_updated", "0000"))[:4] or 0)
    if years >= 15 and updated_year >= 2025:
        return 2
    if years >= 5:
        return 1
    return 0


def load_review_layers(output_dir: Path) -> dict[str, list[dict]]:
    """Возвращает все отзывы в разрезе doctor_id с пометкой пригодности."""
    by_doctor: dict[str, list[dict]] = {}

    def add(
        doctor_id: str,
        message: str,
        rating_5: float,
        layer: str,
        usable: bool,
        date: str = "",
        *,
        rating_assumed: bool = False,
    ) -> None:
        by_doctor.setdefault(doctor_id, []).append({
            "message": message,
            "rating_5": rating_5,
            "layer": layer,
            "usable": usable,
            "date": date,
            "rating_assumed": rating_assumed,
        })

    main_path = output_dir / "reviews.json"
    if main_path.exists():
        for record in json.loads(main_path.read_text(encoding="utf-8")):
            for review in record.get("reviews", []):
                usable = not bool(review.get("unrated")) and not bool(review.get("duplicate_of"))
                add(
                    record["doctor_id"], review.get("message", ""),
                    float(review.get("rating", 0)) / 20, "main", usable,
                    review.get("date", ""),
                )
    doctu_path = output_dir / "doctu_reviews.json"
    if doctu_path.exists():
        for profile in json.loads(doctu_path.read_text(encoding="utf-8")).get("profiles", []):
            for review in profile.get("reviews", []):
                usable = (
                    review.get("source_kind") == "doctu_doctor"
                    and not bool(review.get("unrated"))
                )
                add(
                    profile["doctor_id"], review.get("message", ""),
                    float(review.get("rating_5", 0)), "doctu", usable,
                    review.get("date", ""),
                )
    yandex_path = output_dir / "yandex_reviews.json"
    if yandex_path.exists():
        for profile in json.loads(yandex_path.read_text(encoding="utf-8")).get("profiles", []):
            for review in profile.get("reviews", []):
                usable = (
                    review.get("source_kind") != "yandex_clinic"
                    and not bool(review.get("unrated"))
                )
                add(
                    profile["doctor_id"], review.get("message", ""),
                    float(review.get("rating_5", 0)), "yandex", usable,
                    review.get("date_beauty", ""),
                )
    extra_path = output_dir / "extra_doctor_reviews.json"
    if extra_path.exists():
        for profile in json.loads(extra_path.read_text(encoding="utf-8")).get("profiles", []):
            for review in profile.get("reviews", []):
                usable = not bool(review.get("unrated"))
                add(
                    profile["doctor_id"], review.get("message", ""),
                    float(review.get("rating_5", 0)), "extra", usable,
                    review.get("date", ""),
                    rating_assumed=bool(review.get("rating_assumed")),
                )
    return by_doctor


def score_review_layers(reviews: list[dict]) -> tuple[int, float, int, int, int, int]:
    """Возвращает (балл E, средний, всего, основных, внешних, тематических)."""
    usable = [item for item in reviews if item["usable"]]
    main_count = sum(1 for item in usable if item["layer"] == "main")
    external_count = len(usable) - main_count
    values = [item["rating_5"] for item in usable]
    average = round(sum(values) / len(values), 2) if values else 0.0
    topic_hits = sum(1 for item in usable if REVIEW_FOCUS.search(item["message"]))
    if len(values) >= 3 and average >= 4.5:
        base = 2
    elif len(values) >= 3 and average >= 3.5:
        base = 1
    else:
        base = 0
    if main_count == 0 and external_count > 0:
        base = min(base, 1)
    return base, average, len(values), main_count, external_count, topic_hits


def score_topic_reviews(reviews: list[dict]) -> int:
    """Тема в ПОЛОЖИТЕЛЬНЫХ отзывах: негативные упоминания балл не повышают."""
    hits = sum(
        1 for item in reviews
        if item["usable"] and item["rating_5"] >= 4 and REVIEW_FOCUS.search(item["message"])
    )
    return 2 if hits >= 2 else 1 if hits == 1 else 0


def count_topic_reviews(reviews: list[dict]) -> tuple[int, int]:
    usable = [item for item in reviews if item["usable"] and REVIEW_FOCUS.search(item["message"])]
    positive = sum(1 for item in usable if item["rating_5"] >= 4)
    negative = sum(1 for item in usable if item["rating_5"] < 3)
    return positive, negative


def summarize_education(profile: dict) -> tuple[str, str, str]:
    education = profile.get("education", [])
    base = str(education[0].get("institution", "")) if education else "—"
    postgraduate = [
        f"{item.get('institution', '')} ({item.get('year', '')}, {item.get('kind', '')})"
        for item in education[1:]
        if any(word in str(item.get("kind", "")).lower() for word in ("ординатура", "аспирантура", "интернатура"))
    ]
    focus = [
        item for item in profile.get("courses", []) + profile.get("practice_focus", [])
        if STRONG_FOCUS.search(str(item))
    ]
    return base, "; ".join(postgraduate) or "—", "; ".join(focus[:2]) or "—"


def rate(profile: dict, reviews: list[dict]) -> dict[str, Any]:
    components = {
        "A": score_topic(profile),
        "B": score_training(profile),
        "C": score_university(profile),
        "D": score_topic_reviews(reviews),
        "E": 0,
        "F": score_experience(profile),
    }
    e_score, average, total, main_count, external_count, topic_hits = score_review_layers(reviews)
    components["E"] = e_score
    usable = [item for item in reviews if item["usable"]]
    verified_values = [item["rating_5"] for item in usable if not item.get("rating_assumed")]
    verified_avg = round(sum(verified_values) / len(verified_values), 2) if verified_values else 0.0
    assumed_reviews = sum(1 for item in usable if item.get("rating_assumed"))
    positive_topic, negative_topic = count_topic_reviews(reviews)
    penalty = 1 if negative_topic >= 2 else 0
    raw = sum(components.values())
    adjusted = max(raw - penalty, 0)
    base, residency, focus_courses = summarize_education(profile)
    return {
        "doctor_id": profile["doctor_id"],
        "doctor": profile["name"],
        "specialty": profile.get("specialty", ""),
        "score_10": round(adjusted / 12 * 10, 1),
        "raw_score": raw,
        "penalty": penalty,
        "adjusted_raw": adjusted,
        "components": components,
        "avg_rating_5": average,
        "verified_avg_rating_5": verified_avg,
        "review_count": total,
        "main_reviews": main_count,
        "external_reviews": external_count,
        "assumed_reviews": assumed_reviews,
        "topic_review_hits": topic_hits,
        "positive_topic_reviews": positive_topic,
        "negative_topic_reviews": negative_topic,
        "base_university": base,
        "residency": residency,
        "focus_courses": focus_courses,
        "experience_years": profile.get("experience_years", 0),
        "page_updated": profile.get("page_updated", ""),
        "rationale": profile.get("notes", ""),
        "source_urls": profile.get("source_urls", []),
    }


def build_ratings(profiles: dict, reviews_dir: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    by_doctor = load_review_layers(reviews_dir)
    ratings = [rate(profile, by_doctor.get(profile["doctor_id"], [])) for profile in profiles["profiles"]]
    ratings.sort(key=lambda row: (
        -row["score_10"],
        -row["verified_avg_rating_5"],
        -row["avg_rating_5"],
        -row["experience_years"],
        row["doctor_id"],
    ))
    for index, row in enumerate(ratings, 1):
        row["rank"] = index
        row["cohort"] = "added" if row["doctor_id"] in NEW_DOCTOR_IDS else "original"
    return ratings, by_doctor


def render_markdown(profiles: dict, ratings: list[dict]) -> str:
    top_names = " → ".join(row["doctor"].split()[0] for row in ratings[:3])
    lines = [
        "# Оценка врачей по 10-балльной шкале: предиабет и ожирение",
        "",
        "> **Не медицинская рекомендация.** Это индекс соответствия *открытых публичных данных* ",
        "> заданной теме, а не оценка клинической квалификации. Нет данных в открытых источниках — ",
        "> не значит, что врач не работает с предиабетом. Решение о выборе врача принимается на приёме.",
        "",
        f"Фокус: **{profiles.get('focus', '')}**. Врачей в оценке: **{len(ratings)}** "
        f"(из них **{len(NEW_DOCTOR_IDS)}** добавлены в сравнение отдельным слоем).",
        "",
        "## Методика",
        "",
        "Шесть критериев, каждый 0–2 балла; сумма 0–12 нормируется в 0–10 (`балл = сумма / 12 × 10`).",
        "За ≥2 негативных отзыва именно по теме вычитается 1 штрафной балл до нормирования.",
        "",
        "| Код | Критерий | Что даёт 2 балла |",
        "| --- | --- | --- |",
        "| A | Профильное обучение | Курс/программа прямо по ожирению, преддиабету, СД2, диетологии, метаболизму |",
        "| B | Ординатура/аспирантура | Ординатура + аспирантура или учёная степень |",
        "| C | Где учились | Профильный вуз/центр плюс ≥2 академических сигнала (отличие, к.м.н./аспирантура, ассоциации, проверенные документы, ≥4 курсов, топ-вуз + топ-ординатура) |",
        "| D | Тема в положительных отзывах | Тема звучит по границам слов минимум в 2 отзывах с оценкой ≥4 |",
        "| E | Отзывы | Средний ≥4.5 и ≥3 уникальных оценённых отзывов по всем слоям |",
        "| F | Стаж/актуальность | Стаж ≥15 лет и страница обновлена в 2025–2026 |",
        "",
        "Источники отзывов: основной слой `reviews.json`, отзывы о враче с ДОКТУ и Яндекс.Медицины ",
        "(отзывы о клиниках, отзывы без числовой оценки `unrated=true` и межплатформенные дубли `duplicate_of` исключены из среднего), ",
        "а также `extra_doctor_reviews.json` для трёх новых врачей. ",
        "На сайте клиники числовая оценка не публикуется: положительным текстам условно присвоено 5.0 ",
        "с пометкой `rating_assumed=true`, поэтому их средний балл условный и не даёт преимущества на тай-брейке перед реальными оценками. ",
        "Если отзывы есть только во внешних слоях, критерий E ограничен 1 баллом. ",
        "**Негативные отзывы по теме (оценка <3) не повышают D**; при двух и более из суммы вычитается 1 балл. ",
        "В текущих данных штраф применён только к Тарасовой: три отзыва Яндекса с оценкой 1.0 предметно ",
        "жалуются на резкую прибавку веса (+11 кг), повышенный сахар и пропуск диабетических осложнений. Яндекс отдаёт лишь первые 5 отзывов, ",
        "поэтому его средний балл может смещать оценку — конфликтующие сигналы перечислены в обоснованиях.",
        "",
        "### Как читать результат",
        "",
        f"Топ-3 по сумме баллов: **{top_names}** (все по 8.3/10), сразу за ними по 7.5/10 — **Севостьяненко** и **Тарасова**. ",
        "У Ермаковой самое адресное обучение («Метаболические нарушения. Ожирение. Преддиабет») и идеальные 5.00; ",
        "у Джатиевой лучший баланс школы НМИЦ, диетологии и 10 стабильных отзывов (4.90) с прямым преддиабетом. ",
        "Огнева набирает 8.3 за счёт академической базы Сеченовского + ЭНЦ и 19-летнего стажа (A2 B2 C2 F2), но её отзывы пограничны (3.50, рейтинг ПроДокторов 2.6, свежий негатив 2024–2025). ",
        "Если приоритет — живые отзывы именно о снижении веса, практическим №3 выступает Севостьяненко (7.5/10, 7 отзывов 5.00, минус только за 3-летний стаж); ",
        "у Тарасовой сильнейшая формальная школа НМИЦ + к.м.н., но 7.5/10 после штрафа −1 за три тематические жалобы на Яндексе. ",
        "Подробный разбор с цитатами, таймлайном образования и вопросами к врачу — в `output/top3_detail.md`.",
        "",
        "## Рейтинг",
        "",
        "| № | Врач | Балл /10 | A | B | C | D | E | F | Штраф | Ср. балл | Отзывов |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in ratings:
        c = row["components"]
        lines.append(
            f"| {row['rank']} | {row['doctor']} | **{row['score_10']:.1f}** | "
            f"{c['A']} | {c['B']} | {c['C']} | {c['D']} | {c['E']} | {c['F']} | "
            f"−{row['penalty']} | {row['avg_rating_5']:.2f} | {row['review_count']} |"
        )
    lines += ["", "## Где учились", "", "| Врач | Базовый вуз | Ординатура / аспирантура | Профильные курсы / практика |", "| --- | --- | --- | --- |"]
    for row in ratings:
        lines.append(
            f"| {row['doctor']} | {row['base_university']} | {row['residency']} | {row['focus_courses']} |"
        )
    lines += [
        "", "## Новые участники сравнения", "",
        "| Врач | Балл /10 | Где учились | Профиль по теме | Отзывов |",
        "| --- | ---: | --- | --- | ---: |",
    ]
    rating_by_id = {row["doctor_id"]: row for row in ratings}
    for key in NEW_DOCTOR_IDS:
        row = rating_by_id[key]
        lines.append(
            f"| {row['doctor']} | **{row['score_10']:.1f}** | {row['base_university']} "
            f"| {row['focus_courses']} | {row['review_count']} |"
        )
    lines += [
        "", "Подробный разбор с образованием, отзывами и вопросами к врачу — `output/comparison_extra.md`.", "",
        "## Обоснования и оговорки", "",
    ]
    for row in ratings:
        c = row["components"]
        years = int(row["experience_years"])
        lines.append(
            f"**{row['rank']}. {row['doctor']} — {row['score_10']:.1f}/10** "
            f"(A{c['A']} B{c['B']} C{c['C']} D{c['D']} E{c['E']} F{c['F']}, штраф −{row['penalty']}, "
            f"стаж {years} {_plural_years(years)}, стр. обновлена {row['page_updated']}). "
            f"{row['rationale']}"
        )
        lines.append("")
    lines += [
        "## Что оценка не учитывает",
        "",
        "- Реальные клинические исходы, % пациентов, достигших снижения веса или нормализации гликемии;",
        "- лицензии, сертификаты и аккредитации, не опубликованные на страницах врачей;",
        "- стоимость приёма, доступность записи и личные предпочтения пациента;",
        "- полноту отзывов Яндекс.Медицины (только первые 5) и возможную предвзятость отзывов.",
        "",
        "Файлы: `output/doctor_profiles.json`, `output/doctor_ratings.json`, `output/doctor_ratings.csv`, ",
        "`output/extra_doctor_reviews.json`, `output/top3_detail.md` (лидеры), `output/comparison_extra.md` (новые врачи).",
        "",
    ]
    return "\n".join(lines)


def _plural_reviews(count: int) -> str:
    """Родительный падеж после предлога «из»: из 1 отзыва, из 3 отзывов."""
    if count % 10 == 1 and count % 100 != 11:
        return "отзыва"
    return "отзывов"


def _plural_years(years: int) -> str:
    if years % 10 == 1 and years % 100 != 11:
        return "год"
    if years % 10 in (2, 3, 4) and years % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def _quote(message: str, limit: int = 320) -> str:
    clean = " ".join(message.split())
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def render_detail(
    profiles: dict,
    ratings: list[dict],
    by_doctor: dict[str, list[dict]],
    doctor_ids: list[str],
    notes_map: dict,
    *,
    title: str,
    intro: str,
    closing: list[str],
) -> str:
    pmap = {item["doctor_id"]: item for item in profiles["profiles"]}
    by_id = {row["doctor_id"]: row for row in ratings}
    top = [by_id[key] for key in doctor_ids if key in by_id]
    lines = [
        f"# {title}",
        "",
        "> **Не медицинская рекомендация.** Ниже — разбор открытых публичных данных и отзывов, ",
        "> а не оценка клинической компетентности. Отзывы субъективны: часть площадок отдаёт ",
        "> только первые 5 отзывов или не публикует числовую оценку, а отсутствие упоминаний темы ",
        "> не означает, что врач с ней не работает.",
        "",
        intro,
        "",
        "---",
        "",
    ]
    for index, row in enumerate(top, 1):
        doctor_id = row["doctor_id"]
        profile = pmap[doctor_id]
        notes = notes_map.get(doctor_id, DEFAULT_TOP3_NOTES)
        c = row["components"]
        assumed_note = (
            f", из них условных без числовой оценки: {row['assumed_reviews']}"
            if row.get("assumed_reviews")
            else ""
        )
        years = int(row["experience_years"])
        lines += [
            f"## {index}. {row['doctor']} — {row['score_10']:.1f}/10",
            "",
            f"*{notes['headline']}*",
            "",
            f"Специальность: **{row['specialty']}** · стаж **{years} {_plural_years(years)}** · "
            f"страница обновлена **{row['page_updated']}** · средний балл отзывов **{row['avg_rating_5']:.2f}** "
            f"из **{row['review_count']}** {_plural_reviews(row['review_count'])} "
            f"(основной слой {row['main_reviews']}, внешние {row['external_reviews']}{assumed_note}).",
            "",
            f"Разбивка: A={c['A']} (курсы), B={c['B']} (ординатура), C={c['C']} (вуз), "
            f"D={c['D']} (тема в положительных отзывах), E={c['E']} (отзывы), F={c['F']} (стаж). "
            f"Сумма {row['raw_score']}/12, штраф −{row['penalty']}, итог {row['adjusted_raw']}/12 → "
            f"**{row['score_10']:.1f}/10**.",
            "",
            "### Где учились",
            "",
        ]
        if profile["education"]:
            for item in profile["education"]:
                lines.append(f"- **{item['year']}** — {item['institution']}: {item['program']} ({item['kind']}).")
        else:
            lines.append("- Данные о базовом образовании и ординатуре на найденных публичных страницах не опубликованы.")
        if profile.get("degree"):
            lines.append(f"- Учёная степень: **{profile['degree']}**.")
        if profile.get("honors"):
            lines.append("- Базовое образование — **с отличием**.")
        if profile.get("documents_verified"):
            lines.append("- Документы об образовании **проверены ПроДокторов**.")
        if profile.get("associations"):
            lines.append(f"- Ассоциации: {', '.join(profile['associations'])}.")
        lines += ["", "### Профильное обучение и практика по теме", ""]
        for course in profile.get("courses", []):
            lines.append(f"- {course}")
        for focus in profile.get("practice_focus", []):
            lines.append(f"- Практика/профиль: {focus}")
        if not profile.get("courses") and not profile.get("practice_focus"):
            lines.append("- Отдельных тематических курсов на странице не указано.")
        lines += ["", "### Что говорят отзывы по теме", ""]
        reviews = [item for item in by_doctor.get(doctor_id, []) if item["usable"]]
        positives = [item for item in reviews if REVIEW_FOCUS.search(item["message"]) and item["rating_5"] >= 4]
        negatives = [item for item in reviews if REVIEW_FOCUS.search(item["message"]) and item["rating_5"] < 3]
        if positives:
            for item in positives[:4]:
                lines.append(
                    f"- ✅ **{item['rating_5']:.1f}** ({item['layer']}, {item.get('date', 'без даты')}): "
                    f"«{_quote(item['message'])}»"
                )
        else:
            lines.append("- Положительных отзывов с прямым упоминанием темы в выгрузке нет.")
        if negatives:
            for item in negatives[:4]:
                lines.append(
                    f"- ⚠️ **{item['rating_5']:.1f}** ({item['layer']}, {item.get('date', 'без даты')}): "
                    f"«{_quote(item['message'])}»"
                )
        else:
            lines.append("- Негативных отзывов именно по теме в выгрузке нет.")
        lines += ["", "### Сильные стороны", ""]
        lines += [f"- {item}" for item in notes["strengths"]]
        lines += ["", "### Риски и оговорки", ""]
        lines += [f"- {item}" for item in notes["risks"]]
        lines += ["", "### Что уточнить на приёме", ""]
        lines += [f"- {item}" for item in notes["questions"]]
        lines += ["", "### Итог", "", notes["verdict"], "", "Источники:"]
        lines += [f"- {url}" for url in profile.get("source_urls", [])]
        lines += ["", "---", ""]
    lines += closing + [""]
    return "\n".join(lines)


def write_csv(ratings: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in ratings:
            flat = {**row, **{key: row["components"][key] for key in CRITERIA}}
            writer.writerow(flat)


def append_summary(summary_path: Path, section: str) -> None:
    _append_summary(summary_path, section, start=START, end=END)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", default="output/doctor_profiles.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--reviews-dir", default=None,
                        help="Где лежат reviews.json/doctu_reviews.json/yandex_reviews.json "
                             "(по умолчанию --output-dir)")
    parser.add_argument("--append-summary", action="store_true")
    args = parser.parse_args(argv)

    profiles = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    reviews_dir = Path(args.reviews_dir) if args.reviews_dir else out
    ratings, by_doctor = build_ratings(profiles, reviews_dir)
    (out / "doctor_ratings.json").write_text(
        json.dumps({"focus": profiles.get("focus"), "criteria": CRITERIA, "ratings": ratings}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(ratings, out / "doctor_ratings.csv")
    section = render_markdown(profiles, ratings)
    (out / "doctor_ratings.md").write_text(section, encoding="utf-8")
    (out / "top3_detail.md").write_text(
        render_detail(
            profiles, ratings, by_doctor,
            [row["doctor_id"] for row in ratings[:3]], TOP3_NOTES,
            title="Топ-3: подробный разбор (предиабет, ожирение)",
            intro=(
                "Порядок по уточнённой методике (поиск темы по границам слов, исключение "
                "неоценённых/дублирующихся записей, соответствие критерия C правилу топ-школы): **"
                + " → ".join(row["doctor"].split()[0] for row in ratings[:3])
                + "** (все по 8.3/10; следом с 7.5/10 идут **Севостьяненко** со средним 5.00 и **Тарасова** после штрафа −1)."
            ),
            closing=[
                "Связанные файлы: `output/doctor_ratings.md`, `output/doctor_ratings.csv`, ",
                "`output/doctor_profiles.json`, `output/reviews.json`, `output/yandex_reviews.json`.",
            ],
        ),
        encoding="utf-8",
    )
    (out / "comparison_extra.md").write_text(
        render_detail(
            profiles, ratings, by_doctor, NEW_DOCTOR_IDS, EXTRA_NOTES,
            title="Три новых врача в сравнении (предиабет, ожирение)",
            intro="Добавлены по просьбе пользователя. Их страницы и отзывы находятся на `polyclinika.ru` "
                  "и НаПоправку — домены не входят в `SOURCE_CONFIGS`, поэтому данные лежат в отдельном слое "
                  "`output/extra_doctor_reviews.json`, а не в `output/reviews.json`.",
            closing=[
                "Связанные файлы: `output/extra_doctor_reviews.json`, `output/doctor_ratings.md`, ",
                "`output/doctor_profiles.json`, `output/top3_detail.md`.",
            ],
        ),
        encoding="utf-8",
    )
    if args.append_summary:
        append_summary(out / "summary.md", section)
    print(
        f"Оценено врачей: {len(ratings)} (новых: {len(NEW_DOCTOR_IDS)}); топ-3: "
        f"{', '.join(row['doctor'].split()[0] for row in ratings[:3])} → {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
