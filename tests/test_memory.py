"""Ядро памяти мира: хранилище фактов, отбор под вопрос, описание, HTTP-слой.

Повторное наблюдение обновляет запись, а не плодит новую; пропавший блок делает запись
устаревшей; забытое не возвращается; лимит соблюдается вытеснением; всё переживает
перезапуск — он имитируется повторным чтением файла.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import memory
from api import campaign, challenges, logs, worlds
from api import memory as memory_api
from api.challenges import reset_challenge_state
from api.player_state import apply_log_to_player_state, reset_player_state
from memory.config import DEFAULTS, settings
from memory.model import (ACTIVE, CONTAINER, DEATH, DIALOGUE, MOD, NOTE, OVERWORLD, PLACE,
                          PLAYER, REMOVED, RESOURCE, SCREEN, STALE, Fact, Position)
from memory.recall import age, describe, recall
from memory.store import MemoryStore
from models.log_entry import LogEntry
from orchestrator import session as sessions

DIAMOND_ORE = "minecraft:diamond_ore"
IRON_ORE = "minecraft:iron_ore"
LABELS = {DIAMOND_ORE: "Алмазная руда", IRON_ORE: "Железная руда",
          "minecraft:iron_ingot": "Железный слиток", "minecraft:chest": "Сундук"}


def at(x, y, z, dimension=OVERWORLD):
    return Position(dimension, x, y, z)


def stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "memory.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def store(self, **kwargs):
        return MemoryStore(self.path, world="sp:Тест", **kwargs)


class FactTests(unittest.TestCase):
    def test_round_trip(self):
        fact = Fact(id="a1", game="Minecraft", world="w", kind=CONTAINER, subject="minecraft:chest",
                    label="Сундук", position=at(1, 2, 3), contents={"minecraft:iron_ingot": 5},
                    first_seen="t", last_seen="t", blocks=[(1, 2, 3)])
        again = Fact.from_dict(json.loads(json.dumps(fact.to_dict())))
        self.assertEqual(again, fact)

    def test_damaged_records_are_skipped(self):
        self.assertIsNone(Fact.from_dict({"id": "x", "kind": "spell"}))
        self.assertIsNone(Fact.from_dict({"kind": NOTE}))
        self.assertIsNone(Fact.from_dict("строка"))
        fact = Fact.from_dict({"id": "x", "kind": NOTE, "source": "hacker", "status": "??",
                               "contents": {"a": "много", "b": -1, "c": 2}})
        self.assertEqual((fact.source, fact.status, fact.contents), (MOD, ACTIVE, {"c": 2}))


class StoreTests(TempDirCase):
    def test_ores_are_grouped_by_area(self):
        store = self.store()
        result = store.observe_blocks([(DIAMOND_ORE, at(-120, -58, 340)),
                                       (DIAMOND_ORE, at(-121, -58, 340)),
                                       (DIAMOND_ORE, at(-121, -59, 341)),
                                       (IRON_ORE, at(-120, 20, 340))], labels=LABELS)
        self.assertEqual(result["added"], 2)
        diamonds = [fact for fact in store.facts(RESOURCE) if fact.subject == DIAMOND_ORE]
        self.assertEqual(len(diamonds), 1)
        self.assertEqual(diamonds[0].amount, 3)
        self.assertEqual(diamonds[0].label, "Алмазная руда")

    def test_other_area_is_another_record(self):
        store = self.store()
        store.observe_blocks([(DIAMOND_ORE, at(0, -58, 0)), (DIAMOND_ORE, at(16, -58, 0)),
                              (DIAMOND_ORE, at(-1, -58, 0))])
        self.assertEqual(len(store.facts(RESOURCE)), 3)

    def test_repeated_observation_updates_the_record(self):
        store = self.store()
        store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1))], now=stamp(10))
        result = store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1))], now=stamp(0))
        self.assertEqual((result["added"], result["updated"]), (0, 1))
        fact = store.facts(RESOURCE)[0]
        self.assertEqual((fact.amount, fact.seen_count), (1, 2))

    def test_mined_blocks_make_the_record_stale(self):
        store = self.store()
        store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1)), (DIAMOND_ORE, at(2, -58, 1))])
        store.observe_blocks([], removed=[at(1, -58, 1)])
        fact = store.facts(RESOURCE)[0]
        self.assertEqual((fact.amount, fact.position), (1, at(2, -58, 1)))
        result = store.observe_blocks([], removed=[at(2, -58, 1)])
        self.assertEqual(result["stale"], 1)
        self.assertEqual(store.facts(RESOURCE), [])
        self.assertEqual(store.facts(RESOURCE, statuses=(STALE,))[0].status, STALE)

    def test_seen_again_after_stale_is_active(self):
        store = self.store()
        store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1))])
        store.observe_blocks([], removed=[at(1, -58, 1)])
        store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1))])
        self.assertEqual(len(store.facts(RESOURCE)), 1)
        self.assertEqual(len(store.facts(RESOURCE, statuses=(ACTIVE, STALE))), 1)

    def test_container_contents_are_replaced(self):
        store = self.store()
        store.record_container("minecraft:chest", at(12, 64, -40), {"minecraft:iron_ingot": 5})
        fact = store.record_container("minecraft:chest", at(12, 64, -40),
                                      {"minecraft:iron_ingot": 2, "minecraft:coal": 0,
                                       "minecraft:torch": "x"})
        self.assertEqual(len(store.facts(CONTAINER)), 1)
        self.assertEqual(fact.contents, {"minecraft:iron_ingot": 2})
        store.record_container("minecraft:chest", at(12, 64, -40), {})
        self.assertEqual(store.facts(CONTAINER)[0].contents, {})

    def test_storage_lists_where_items_are(self):
        store = self.store()
        store.record_container("minecraft:chest", at(0, 64, 0), {"minecraft:iron_ingot": 2})
        store.record_container("minecraft:barrel", at(5, 64, 0), {"minecraft:iron_ingot": 7})
        store.record_container("minecraft:chest", at(0, 64, 0, "minecraft:the_nether"),
                               {"minecraft:iron_ingot": 9})
        where = store.storage(OVERWORLD)["minecraft:iron_ingot"]
        self.assertEqual([count for _, count in where], [7, 2])

    def test_note_with_the_same_label_moves(self):
        store = self.store()
        store.note("база", position=at(0, 64, 0))
        fact = store.note("База", position=at(100, 70, 100))
        self.assertEqual(len(store.facts(PLACE)), 1)
        self.assertEqual(fact.position, at(100, 70, 100))
        note = store.note("ключ-меч", "у благодати Церковь Элле")
        self.assertEqual((note.kind, note.source, note.confidence), (NOTE, PLAYER, 0.9))

    def test_dialogue_is_not_repeated(self):
        store = self.store()
        first = store.record_dialogue("Кейра", "Лекарство варится из корня")
        again = store.record_dialogue("Кейра", "Лекарство  варится из корня")
        self.assertIs(first, again)
        self.assertEqual((first.source, first.confidence, first.seen_count), (SCREEN, 0.6, 2))
        self.assertIsNone(store.record_dialogue("Кейра", "   "))

    def test_deaths_are_kept_with_a_limit(self):
        store = self.store()
        for minute in range(25):
            store.record_death(at(minute, 10, 0), "lava", {"minecraft:iron_ingot": 9},
                               now=stamp(100 - minute))
        self.assertEqual(len(store.facts(DEATH)), 20)
        self.assertEqual(store.last_death().position, at(24, 10, 0))

    def test_forgotten_fact_stays_out(self):
        store = self.store()
        fact = store.note("база", position=at(0, 64, 0))
        self.assertIs(store.forget(fact.id), fact)
        self.assertIsNone(store.forget(fact.id))
        self.assertIsNone(store.forget("нет такого"))
        self.assertEqual(store.facts(), [])
        self.assertEqual(store.facts(statuses=(REMOVED,)), [fact])

    def test_limit_evicts_observations_before_notes(self):
        store = self.store(max_facts=5)
        removed = store.note("старое", "забыть")
        store.forget(removed.id)
        store.note("база", position=at(0, 64, 0), now=stamp(500))
        for index in range(6):
            store.observe_blocks([(DIAMOND_ORE, at(index * 16, -58, 0))], now=stamp(100 - index))
        self.assertEqual(len(store), 5)
        self.assertEqual([fact.label for fact in store.facts(PLACE)], ["база"])
        self.assertEqual(store.facts(statuses=(REMOVED,)), [])
        # Ушли самые давние наблюдения.
        xs = sorted(fact.position.x for fact in store.facts(RESOURCE))
        self.assertEqual(xs, [32, 48, 64, 80])

    def test_facts_survive_a_restart(self):
        store = self.store()
        store.observe_blocks([(DIAMOND_ORE, at(1, -58, 1))], labels=LABELS)
        store.record_container("minecraft:chest", at(12, 64, -40), {"minecraft:iron_ingot": 5})
        store.note("портал в Незер", position=at(88, 64, -210))
        store.save()
        again = self.store()
        self.assertEqual(len(again), 3)
        self.assertEqual(again.facts(CONTAINER)[0].contents, {"minecraft:iron_ingot": 5})
        self.assertEqual(again.facts(RESOURCE)[0].blocks, [(1, -58, 1)])

    def test_broken_lines_do_not_break_loading(self):
        self.path.write_text('{"id": "a", "kind": "note", "label": "x"}\nне json\n{}\n',
                             encoding="utf-8")
        self.assertEqual(len(self.store()), 1)

    def test_store_without_a_path_writes_nothing(self):
        store = MemoryStore(None)
        store.note("база", position=at(0, 0, 0))
        store.save()
        self.assertFalse(self.path.exists())


class RecallTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.memory = self.store()
        self.memory.observe_blocks([(DIAMOND_ORE, at(-120, -58, 340))], labels=LABELS, now=stamp(60))
        self.memory.observe_blocks([(DIAMOND_ORE, at(10, -58, 10))], labels=LABELS, now=stamp(600))
        self.memory.record_container("minecraft:chest", at(12, 64, -40), {"minecraft:iron_ingot": 5},
                                     label="Сундук")
        self.memory.note("портал в Незер", position=at(88, 64, -210))
        self.memory.note("ключ-меч", "у благодати Церковь Элле")
        self.memory.record_dialogue("Кейра", "Лекарство варится из корня мандрагоры")

    def test_entity_match_wins_and_nearest_comes_first(self):
        found = recall(self.memory, subjects=[DIAMOND_ORE], position=at(0, 64, 0))
        self.assertEqual([match.fact.position.x for match in found], [10, -120])

    def test_items_in_containers_are_found_by_entity(self):
        found = recall(self.memory, subjects=["minecraft:iron_ingot"])
        self.assertEqual(found[0].fact.kind, CONTAINER)

    def test_words_find_notes_and_dialogue(self):
        self.assertEqual(recall(self.memory, "портал")[0].fact.label, "портал в Незер")
        self.assertEqual(recall(self.memory, "где ключ-меч")[0].fact.label, "ключ-меч")
        self.assertEqual(recall(self.memory, "Кейра лекарство")[0].fact.kind, DIALOGUE)

    def test_nothing_unrelated_is_returned(self):
        self.assertEqual(recall(self.memory, "дракон"), [])

    def test_kind_filter_and_limit(self):
        self.assertEqual(len(recall(self.memory, kinds=[RESOURCE])), 2)
        self.assertEqual(len(recall(self.memory, kinds=[RESOURCE], limit=1)), 1)

    def test_more_recent_wins_without_position(self):
        found = recall(self.memory, subjects=[DIAMOND_ORE])
        self.assertEqual(found[0].fact.position.x, -120)


class DescribeTests(unittest.TestCase):
    def test_resource_with_distance_and_age(self):
        fact = Fact(id="a", game="Minecraft", world="w", kind=RESOURCE, subject=DIAMOND_ORE,
                    label="Алмазная руда", position=at(-120, -58, 340),
                    last_seen=stamp(60 * 30), blocks=[(-120, -58, 340), (-121, -58, 340),
                                                       (-121, -59, 340)])
        text = describe(fact, origin=at(0, 64, 200))
        self.assertEqual(text, "Алмазная руда, 3 блока: x −120, y −58, z 340, Верхний мир, "
                               "около 184 блоков на юго-запад, вчера (по сведениям мода)")

    def test_container_lists_its_contents(self):
        fact = Fact(id="a", game="Minecraft", world="w", kind=CONTAINER, label="Сундук",
                    position=at(12, 64, -40), contents={"minecraft:iron_ingot": 5},
                    last_seen=stamp(0))
        text = describe(fact, label_of=LABELS.get, show_source=False)
        self.assertEqual(text, "Сундук: x 12, y 64, z −40, Верхний мир, внутри: "
                               "Железный слиток ×5, только что")

    def test_screen_fact_is_marked_as_recognised(self):
        fact = Fact(id="a", game="Ведьмак 3", world="w", kind=DIALOGUE, label="Кейра",
                    text="Лекарство варится из корня", source=SCREEN, last_seen=stamp(20))
        self.assertEqual(describe(fact), "Кейра: «Лекарство варится из корня»: 20 минут назад "
                                         "(распознано по снимку экрана, может быть неточным)")

    def test_age_words(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(age(now.isoformat(), now), "только что")
        self.assertEqual(age((now - timedelta(minutes=21)).isoformat(), now), "21 минуту назад")
        self.assertEqual(age((now - timedelta(hours=3)).isoformat(), now), "3 часа назад")
        self.assertEqual(age((now - timedelta(days=5)).isoformat(), now), "5 дней назад")
        self.assertIsNone(age("вчера", now))


class ConfigTests(TempDirCase):
    def test_defaults_without_a_section(self):
        self.path.write_text('{"analysis_interval": 5000}', encoding="utf-8")
        self.assertEqual(settings(self.path), DEFAULTS)
        self.assertEqual(settings(Path(self.temp_dir.name) / "нет.json"), DEFAULTS)

    def test_values_are_bounded(self):
        self.path.write_text(json.dumps({"memory": {"scan_radius": 500, "scan_interval_s": 0,
                                                    "enabled": "да", "max_facts": "x"}}),
                             encoding="utf-8")
        values = settings(self.path)
        self.assertEqual((values["scan_radius"], values["scan_interval_s"]), (48, 1))
        self.assertEqual((values["enabled"], values["max_facts"]), (True, 5000))

    def test_project_settings_match_the_spec(self):
        self.assertEqual(settings(), DEFAULTS)


class WorldMemoryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(worlds, "WORLDS_DIR", root / "worlds"),
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
            patch.object(memory_api, "label_of", return_value=LABELS.get),
        ]
        for item in self.patches:
            item.start()
        self.forget()

    def tearDown(self):
        self.forget()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    def forget(self):
        worlds._current = None
        worlds._dialogue = []
        worlds._last_plan = {}
        memory.reset()
        sessions.replace(sessions.Session())
        reset_player_state()
        reset_challenge_state()
        campaign.reset_campaign_state()


class RegistryTests(WorldMemoryCase):
    async def test_each_world_has_its_own_memory(self):
        worlds.enter("sp:Первый", "Первый")
        memory.shared().note("база", position=at(0, 64, 0))
        memory.shared().save()
        worlds.enter("sp:Второй", "Второй")
        self.assertEqual(len(memory.shared()), 0)
        worlds.enter("sp:Первый", "Первый")
        self.assertEqual(len(memory.shared()), 1)

    async def test_memory_survives_a_restart(self):
        worlds.enter("sp:Тест", "Тест")
        store = memory.shared()
        store.note("база", position=at(0, 64, 0))
        store.save()
        self.forget()
        worlds.enter("sp:Тест", "Тест")
        self.assertEqual(memory.shared().facts(PLACE)[0].label, "база")

    async def test_other_game_has_its_own_memory(self):
        worlds.enter("sp:Тест", "Тест")
        memory.shared("Ведьмак 3").note("Кейра", "живёт в Велене")
        self.assertEqual(len(memory.shared()), 0)
        self.assertEqual(memory.shared("Ведьмак 3").world, "game:ведьмак 3")
        self.assertEqual(memory.shared("Ведьмак 3").game, "Ведьмак 3")

    async def test_without_a_world_memory_is_kept_in_the_process(self):
        memory.shared().note("база", position=at(0, 64, 0))
        self.assertEqual(len(memory.shared()), 1)
        self.assertFalse((Path(self.temp_dir.name) / "worlds").exists())


class MemoryApiTests(WorldMemoryCase):
    async def test_note_list_and_forget(self):
        worlds.enter("sp:Тест", "Тест")
        apply_log_to_player_state(LogEntry(level="INFO", event_type="state", event_data={
            "dimension": "minecraft:overworld", "x": 88, "y": 64, "z": -210}))
        added = await memory_api.add_note({"label": "портал в Незер", "here": True})
        self.assertEqual(added["fact"]["position"], {"dimension": OVERWORLD, "x": 88, "y": 64,
                                                     "z": -210})
        await memory_api.add_note({"label": "ключ-меч", "text": "у благодати"})

        listed = await memory_api.list_memory()
        self.assertEqual(listed["total"], 2)
        self.assertEqual(len((await memory_api.list_memory(kind=PLACE))["facts"]), 1)
        found = await memory_api.list_memory(q="портал")
        self.assertEqual(found["facts"][0]["label"], "портал в Незер")
        self.assertIn("x 88, y 64, z −210", found["facts"][0]["description"])

        await memory_api.forget_fact(added["fact"]["id"])
        self.assertEqual((await memory_api.list_memory())["total"], 1)
        with self.assertRaises(Exception):
            await memory_api.forget_fact(added["fact"]["id"])

    async def test_place_needs_a_known_position(self):
        worlds.enter("sp:Тест", "Тест")
        with self.assertRaises(Exception):
            await memory_api.add_note({"label": "база", "here": True})
        with self.assertRaises(Exception):
            await memory_api.add_note({})
        with self.assertRaises(Exception):
            await memory_api.list_memory(kind="spell")

    async def test_death_goes_to_memory(self):
        worlds.enter("sp:Тест", "Тест")
        await logs.add_log(LogEntry(level="INFO", event_type="inventory_snapshot", event_data={
            "counts": {"minecraft:iron_ingot": 9}, "hotbar": [], "armor": [], "offhand": []}))
        await logs.add_log(LogEntry(level="ERROR", event_type="death", event_data={
            "cause": "lava", "dimension": "minecraft:overworld", "x": -30, "y": -50, "z": 115}))
        death = memory.shared().last_death()
        self.assertEqual(death.position, at(-30, -50, 115))
        self.assertEqual(death.label, "огонь или лава")
        self.assertEqual(death.contents, {"minecraft:iron_ingot": 9})
        self.assertIn("с собой было: Железный слиток ×9", describe(death, label_of=LABELS.get))
        logs.logs_db.clear()


if __name__ == "__main__":
    unittest.main()
