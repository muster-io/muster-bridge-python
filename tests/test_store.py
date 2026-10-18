# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import asyncio
from datetime import datetime, timedelta

import pytest
from conftest import START

from muster_bridge import KeyClaim, KeyState, MemoryStore, StoredAnswer, StoredEvent, UnknownCursorError
from muster_bridge.store import _Queue

pytestmark = pytest.mark.anyio

CLAIM_TTL = timedelta(minutes=2)
RETENTION = timedelta(hours=24)
NOW = START


def stored_event(event_id: str, until: datetime) -> StoredEvent:
    return StoredEvent(
        id=event_id, type="press", body=f'{{"id":"{event_id}"}}'.encode(), chat=f"c-{event_id}", until=until
    )


async def claim(store: MemoryStore, bot: str, key: str, now: datetime, want: KeyState) -> KeyClaim:
    result = await store.claim_key(bot, key, now, now + CLAIM_TTL)
    assert result.state is want, f"claim_key({bot}, {key}) = {result.state}, want {want}"
    return result


def ids(events: list[StoredEvent]) -> str:
    return " ".join(event.id for event in events)


async def test_claim_and_save() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    answer = StoredAnswer(201, b'{"message_id":"m1","replayed":false}')
    await store.save_key("main", "k1", first.claim_id, answer, NOW + RETENTION)
    again = await claim(store, "main", "k1", NOW + timedelta(hours=1), KeyState.ANSWERED)
    assert again.answer == answer


async def test_running_claim() -> None:
    store = MemoryStore()
    await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await claim(store, "main", "k1", NOW + timedelta(seconds=1), KeyState.RUNNING)


async def test_claim_lease_expires() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await claim(store, "main", "k1", NOW + CLAIM_TTL - timedelta(microseconds=1), KeyState.RUNNING)
    again = await claim(store, "main", "k1", NOW + CLAIM_TTL, KeyState.CLAIMED)
    assert again.claim_id != first.claim_id


async def test_release_keeps_answer() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await store.release_key("main", "k1", first.claim_id)
    second = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await store.save_key("main", "k1", second.claim_id, StoredAnswer(200, b"{}"), NOW + timedelta(hours=1))
    await store.release_key("main", "k1", second.claim_id)
    await claim(store, "main", "k1", NOW, KeyState.ANSWERED)
    await store.release_key("main", "never-claimed", "1")


async def test_stale_claim_leaves_the_new_one() -> None:
    # A request that outlives its claim neither frees nor overwrites the claim of the request that took the key over.
    store = MemoryStore()
    stale = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    later = NOW + CLAIM_TTL
    fresh = await claim(store, "main", "k1", later, KeyState.CLAIMED)
    await store.release_key("main", "k1", stale.claim_id)
    await claim(store, "main", "k1", later, KeyState.RUNNING)
    await store.save_key("main", "k1", stale.claim_id, StoredAnswer(201, b'{"stale":true}'), later + RETENTION)
    await claim(store, "main", "k1", later, KeyState.RUNNING)
    await store.save_key("main", "k1", fresh.claim_id, StoredAnswer(201, b'{"fresh":true}'), later + RETENTION)
    answered = await claim(store, "main", "k1", later, KeyState.ANSWERED)
    assert answered.answer == StoredAnswer(201, b'{"fresh":true}')


async def test_first_answer_stays() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    for body in (b'{"n":1}', b'{"n":2}'):
        await store.save_key("main", "k1", first.claim_id, StoredAnswer(201, body), NOW + timedelta(hours=1))
    answered = await claim(store, "main", "k1", NOW, KeyState.ANSWERED)
    assert answered.answer == StoredAnswer(201, b'{"n":1}')


async def test_save_without_claim() -> None:
    store = MemoryStore()
    await store.save_key("main", "k1", "", StoredAnswer(200, b"{}"), NOW + timedelta(hours=1))
    await claim(store, "main", "k1", NOW, KeyState.ANSWERED)


async def test_keys_per_bot() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await store.save_key("main", "k1", first.claim_id, StoredAnswer(201, b"{}"), NOW + timedelta(hours=1))
    await claim(store, "other", "k1", NOW, KeyState.CLAIMED)


async def test_keys_expire_after_24_hours() -> None:
    store = MemoryStore()
    first = await claim(store, "main", "k1", NOW, KeyState.CLAIMED)
    await store.save_key("main", "k1", first.claim_id, StoredAnswer(201, b"{}"), NOW + RETENTION)
    await claim(store, "main", "k1", NOW + RETENTION - timedelta(microseconds=1), KeyState.ANSWERED)
    await claim(store, "main", "k1", NOW + RETENTION, KeyState.CLAIMED)
    assert len(store._keys) == 1


async def test_purges_keys() -> None:
    store = MemoryStore()
    for i in range(50):
        await claim(store, "main", f"k{i}", NOW, KeyState.CLAIMED)
    await claim(store, "main", "last", NOW + CLAIM_TTL, KeyState.CLAIMED)
    assert len(store._keys) == 1
    assert len(store._key_expiry) == 1


async def test_events_dedupe() -> None:
    store = MemoryStore()
    assert await store.append_event("main", stored_event("e1", NOW + RETENTION), NOW) is True
    assert await store.append_event("main", stored_event("e1", NOW + RETENTION), NOW) is False
    assert await store.append_event("other", stored_event("e1", NOW + RETENTION), NOW) is True, (
        "an id of one bot hid the event of another"
    )
    events, _ = await store.events_after("main", "", 10, NOW)
    assert len(events) == 1


async def test_events_after() -> None:
    store = MemoryStore()
    events, head = await store.events_after("main", "", 10, NOW)
    assert events == []
    assert head
    for i in range(5):
        await store.append_event("main", stored_event(f"e{i}", NOW + RETENTION), NOW)
    first, c1 = await store.events_after("main", "", 2, NOW)
    assert ids(first) == "e0 e1"
    assert first[0].chat == "c-e0"
    assert first[0].type == "press"
    second, c2 = await store.events_after("main", c1, 10, NOW)
    assert ids(second) == "e2 e3 e4"
    none, c3 = await store.events_after("main", c2, 10, NOW)
    assert none == []
    assert c3 == c2
    from_head, _ = await store.events_after("main", head, 0, NOW)
    assert ids(from_head) == "e0 e1 e2 e3 e4"
    every, _ = await store.events_after("main", "", 0, NOW)
    assert len(every) == 5


async def test_unknown_cursor() -> None:
    store = MemoryStore()
    await store.append_event("main", stored_event("e1", NOW + RETENTION), NOW)
    _, main_cursor = await store.events_after("main", "", 10, NOW)
    _, other_cursor = await store.events_after("other", "", 10, NOW)
    epoch = main_cursor.partition(".")[0]
    for cursor in ("garbage", other_cursor, f"{epoch}.2", f"{epoch}.x", f"{epoch}.+1", f"{epoch}.²", epoch, ".1"):
        with pytest.raises(UnknownCursorError):
            await store.events_after("main", cursor, 10, NOW)
    with pytest.raises(UnknownCursorError):
        await MemoryStore().events_after("main", main_cursor, 10, NOW)


async def test_events_expire() -> None:
    store = MemoryStore()
    await store.append_event("main", stored_event("old", NOW + timedelta(hours=1)), NOW)
    await store.append_event("main", stored_event("short", NOW + timedelta(minutes=1)), NOW)
    await store.append_event("main", stored_event("new", NOW + timedelta(hours=2)), NOW)
    later = NOW + timedelta(minutes=30)
    events, _ = await store.events_after("main", "", 10, later)
    assert ids(events) == "old new"
    assert await store.find_event("main", "short", later) is None, "an expired event in the middle was found"
    assert await store.append_event("main", stored_event("short", later + timedelta(hours=2)), later), (
        "the id of an expired event was refused"
    )
    events, _ = await store.events_after("main", "", 10, NOW + timedelta(minutes=90))
    assert ids(events) == "new short"
    assert await store.find_event("main", "short", NOW + timedelta(minutes=90)) is not None


async def test_find_event() -> None:
    store = MemoryStore()
    assert await store.find_event("main", "e1", NOW) is None
    await store.append_event("main", stored_event("e1", NOW + RETENTION), NOW)
    found = await store.find_event("main", "e1", NOW)
    assert found is not None
    assert found.id == "e1"
    assert found.chat == "c-e1"
    assert await store.find_event("other", "e1", NOW) is None
    assert await store.find_event("main", "e2", NOW) is None
    assert await store.find_event("main", "e1", NOW + RETENTION) is None


async def test_concurrent() -> None:
    store = MemoryStore()
    claimed: list[str] = []

    async def worker(i: int) -> None:
        for j in range(50):
            result = await store.claim_key("main", f"k{j}", NOW, NOW + timedelta(minutes=1))
            if result.state is KeyState.CLAIMED:
                claimed.append(f"k{j}")
            await store.append_event("main", stored_event(f"e{i}-{j}", NOW + timedelta(hours=1)), NOW)
            await store.events_after("main", "", 10, NOW)
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(i) for i in range(8)))
    assert sorted(claimed) == sorted(f"k{j}" for j in range(50)), "a key was claimed twice, or not at all"
    events, _ = await store.events_after("main", "", 0, NOW)
    assert len(events) == 400


def test_queue_find_of_a_number_not_queued() -> None:
    queue = _Queue(epoch="e")
    assert queue.find(1) is None
    queue.events.append((2, stored_event("e2", NOW)))
    assert queue.find(1) is None
    assert queue.find(3) is None
