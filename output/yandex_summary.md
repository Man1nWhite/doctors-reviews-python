# Дополнительно: Яндекс.Медицина

Отзывы взяты из встроенного JSON публичных страниц Яндекс.Медицины, а не из
`/api/v1/getReviews`: в репозитории нет коллектора для Яндекса и домен не разрешён
в `SOURCE_CONFIGS`. Отзывы с `source_kind=yandex_clinic` изначально написаны о клинике
и привязаны к врачу Яндексом — они явно помечены.

- Профилей врачей: **6**
- Собрано отзывов: **24** (из **57** заявленных на страницах)
- Из отзывов о клинике: **8**
- Шкала Яндекса 1–5 переведена в `rating_100 = оценка × 20` для совместимости с основной выгрузкой.

> Страница отдаёт первые 5 отзывов (`fetchedReviewsCount=5`); остальные подгружаются
> виджетом `reviews.yandex.ru`, публичного API у Яндекса для этого нет.

| Врач | Профиль | Отзывов собрано / заявлено | Средний балл | Положительных | Отрицательных |
| --- | --- | ---: | ---: | ---: | ---: |
| Казаков Юрий Львович | [kazakov_yuriy](https://yandex.ru/medicine/doctor/kazakov_yuriy_9d6zxvkx7YSbZ) | 3 / 3 | 3.00 | 1 | 1 |
| Огнева Наталья Анатольевна | [ogneva_natalya](https://yandex.ru/medicine/doctor/ogneva_natalya_jibq3imocZHEw) | 5 / 15 | 2.40 | 1 | 3 |
| Сеферян Сергей Левонович | [seferyan_sergey](https://yandex.ru/medicine/doctor/seferyan_sergey_MIdF43g5qP0E8) | 5 / 9 | 5.00 | 5 | 0 |
| Ермакова Евгения Михайловна | [ermakova_evgeniya](https://yandex.ru/medicine/doctor/yermakova_yevgeniya_65x0gEh2P369a) | 5 / 5 | 5.00 | 5 | 0 |
| Тарасова Татьяна Сергеевна | [tarasova_tatyana](https://yandex.ru/medicine/doctor/tarasova_tatyana_XvSIos6cyNHVw) | 5 / 24 | 2.60 | 2 | 3 |
| Дарханова Евгения Владимировна | [darkhanova_evgeniya](https://yandex.ru/medicine/doctor/balzhinimaeva_erzhena_uUP4LV5CHV6EA) | 1 / 1 | 5.00 | 1 | 0 |

Файлы: `output/yandex_reviews.json`, `output/yandex_reviews.csv`.
