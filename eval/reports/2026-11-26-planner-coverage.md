# Покрытие и скорость планировщика

Дата прогона: 2026-11-26T17:29:34

| Показатель | Значение |
|---|---|
| Целей проверено | 1423 |
| План построен | 1045 (73.4%) |
| Время всего прогона | 17.2 с |
| Время на цель, медиана | 0.12 мс |
| Время на цель, 95-й перцентиль | 20.7 мс |
| Время на цель, максимум | 153.2 мс |

## Почему план не строится

| Причина | Целей |
|---|---|
| нет ни рецепта, ни источника добычи | 360 |
| циклическая зависимость | 18 |

## Недостижимые цели по классам

| Класс | Целей | Примеры |
|---|---|---|
| техническая форма блока, а не предмет | 134 | acacia_wall_hanging_sign, acacia_wall_sign, air, attached_melon_stem, attached_pumpkin_stem |
| выдаётся только в творческом режиме | 103 | allay_spawn_egg, axolotl_spawn_egg, barrier, bat_spawn_egg, bedrock |
| находка в постройках и раскопках | 76 | angler_pottery_shard, angler_pottery_sherd, archer_pottery_shard, archer_pottery_sherd, arms_up_pottery_shard |
| способ получения задан кодом игры (земледелие, торговля, зелья, обмен) | 29 | chainmail_boots, chainmail_chestplate, chainmail_helmet, chainmail_leggings, dragon_breath |
| циклическая зависимость | 18 | coast_armor_trim_smithing_template, dune_armor_trim_smithing_template, eye_armor_trim_smithing_template, honey_block, honey_bottle |
| набирается ведром или бутылкой | 11 | axolotl_bucket, cod_bucket, experience_bottle, lava_bucket, milk_bucket |
| зависит от предмета без способа получения | 7 | beacon, cake, conduit, mojang_banner_pattern, music_disc_5 |

Предметы, на которые упираются чужие планы: nether_star — 1, milk_bucket — 1, heart_of_the_sea — 1, nautilus_shell — 1, enchanted_golden_apple — 1, disc_fragment_5 — 1, echo_shard — 1, scute — 1.

Технические формы блоков и предметы творческого режима целями игрока не бывают.
Остальное — способы получения, которые игра задаёт кодом, а не файлами данных:
их закрывает рукописный слой `kb/gathering.json`, запись за записью.
