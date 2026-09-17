"""The mapping store: lookups, roots, and the size budget."""

from __future__ import annotations

from userbot.store import MappingStore


def store_at(tmp_path, max_bytes=8 * 1024 * 1024) -> MappingStore:
    return MappingStore(str(tmp_path / "mappings.db"), max_bytes)


def test_a_message_points_at_the_block_that_answered_it(tmp_path):
    store = store_at(tmp_path)
    store.record(-100123, 42, "tg-aaaa")
    assert store.lookup(-100123, 42) == "tg-aaaa"
    assert store.lookup(-100123, 43) is None
    assert store.lookup(-100999, 42) is None  # message ids are per chat
    store.close()


def test_a_rewritten_message_takes_the_new_block(tmp_path):
    store = store_at(tmp_path)
    store.record(1, 7, "tg-old")
    store.record(1, 7, "tg-new")
    assert store.lookup(1, 7) == "tg-new"
    assert store.count() == 1
    store.close()


def test_several_messages_of_one_reply_share_a_block(tmp_path):
    store = store_at(tmp_path)
    store.record_many([(1, 10, "tg-a"), (1, 11, "tg-a"), (1, 12, "tg-a")])
    assert [store.lookup(1, message_id) for message_id in (10, 11, 12)] == ["tg-a"] * 3
    store.close()


def test_forgetting_a_message_removes_it(tmp_path):
    store = store_at(tmp_path)
    store.record_many([(1, 10, "tg-a"), (1, 11, "tg-a")])
    store.forget(1, [10])
    assert store.lookup(1, 10) is None
    assert store.lookup(1, 11) == "tg-a"
    store.close()


def test_a_chat_remembers_where_its_conversations_start(tmp_path):
    store = store_at(tmp_path)
    assert store.get_root(-1001) is None
    store.set_root(-1001, "root-1")
    assert store.get_root(-1001) == "root-1"
    store.forget_root(-1001)
    assert store.get_root(-1001) is None
    store.close()


def test_mappings_survive_a_restart(tmp_path):
    path = str(tmp_path / "mappings.db")
    first = MappingStore(path)
    first.record(5, 6, "tg-x")
    first.set_root(5, "root-x")
    first.close()
    second = MappingStore(path)
    assert second.lookup(5, 6) == "tg-x"
    assert second.get_root(5) == "root-x"
    second.close()


def test_the_budget_drops_the_oldest_mappings_first(tmp_path):
    store = store_at(tmp_path, max_bytes=64 * 1024)
    for message_id in range(3000):
        store.record(1, message_id, f"tg-{message_id}")
    assert store.live_bytes() <= 64 * 1024
    assert store.count() < 3000
    assert store.lookup(1, 2999) == "tg-2999"  # the newest are the ones still reachable
    assert store.lookup(1, 0) is None
    assert store.evicted > 0
    store.close()


def test_a_budget_of_zero_keeps_everything(tmp_path):
    store = store_at(tmp_path, max_bytes=0)
    for message_id in range(500):
        store.record(1, message_id, "tg-x")
    assert store.count() == 500
    store.close()
