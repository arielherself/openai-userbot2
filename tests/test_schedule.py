"""Scheduled tasks: the tools, what they store, and the clock that fires them."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from userbot import schedule
from userbot.bridge import Incoming, build_prompt
from userbot.content import Content
from userbot.store import MappingStore, Schedule


def store_at(tmp_path) -> MappingStore:
    return MappingStore(str(tmp_path / "mappings.db"))


def at(seconds_from_now: float) -> str:
    """A local time the way the model would write one."""
    when = datetime.now().astimezone() + timedelta(seconds=seconds_from_now)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def task_at(due_at: float, content="提醒我喝水") -> Schedule:
    return Schedule(
        id=schedule.new_task_id(),
        content=content,
        due_at=due_at,
        chat_id=-100,
        message_id=50,
        agent_id="tg-a",
    )


class FakeBridge:
    """Just enough bridge for the clock: `fire` is recorded, not run."""

    def __init__(self) -> None:
        self.fired: list[Schedule] = []

    async def fire(self, task: Schedule) -> None:
        self.fired.append(task)


def async_run(coroutine):
    return asyncio.run(coroutine)


def test_the_tools_declare_what_they_need():
    add, view, remove = schedule.TOOLS

    assert add["name"] == "tg_add_schedule"
    assert [param["name"] for param in add["params"]] == ["content", "at"]
    assert add["params"][1]["type"] == "string"
    assert "at most 24 hours ahead" in add["description"]
    assert add["rollback"] is True and add["external_effects"] is True  # a failed turn cancels it

    assert view["name"] == "tg_view_schedule"
    assert view["params"] == []
    assert "rollback" not in view  # reading changes nothing

    assert remove["name"] == "tg_remove_schedule"
    assert [param["name"] for param in remove["params"]] == ["id"]
    assert "rollback" not in remove  # a cancelled task cannot come back

    assert schedule.MAX_TASKS == 20


def test_a_time_is_read_the_way_a_model_writes_it():
    written = schedule.parse_time("2026-09-19 14:30")
    assert schedule.format_time(written).startswith("2026-09-19 14:30")
    for shape in ("2026-09-19 14:30:00", "2026-09-19T14:30", "2026-09-19T14:30:00"):
        assert schedule.parse_time(shape) == written
    assert schedule.parse_time(" 2026-09-19 14:30 ") == written
    for nonsense in ("tomorrow 2pm", "2026/09/19 14:30", ""):
        with pytest.raises(ValueError):
            schedule.parse_time(nonsense)


def test_new_arguments_are_read_and_checked():
    content, when, complaint = schedule.schedule_arguments(
        {"content": "提醒我喝水", "at": at(1800)}
    )
    assert complaint is None
    assert content == "提醒我喝水"
    assert abs(when - (time.time() + 1800)) < 2  # the local time came back as a moment

    assert "non-empty string" in schedule.schedule_arguments({"at": at(60)})[2]
    assert "local time" in schedule.schedule_arguments({"content": "x", "at": "tomorrow"})[2]
    assert "local time" in schedule.schedule_arguments({"content": "x"})[2]
    assert (
        "24 hours"
        in schedule.schedule_arguments({"content": "x", "at": at(schedule.MAX_AHEAD + 600)})[2]
    )
    assert "already passed" in schedule.schedule_arguments({"content": "x", "at": at(-3600)})[2]


def test_a_time_a_rounding_late_is_still_taken():
    """The model writes minutes: a "now" a few seconds behind is not a mistake."""
    _, _, complaint = schedule.schedule_arguments({"content": "x", "at": at(-20)})
    assert complaint is None


def test_a_task_is_stored_with_where_its_answer_goes(tmp_path):
    store = store_at(tmp_path)
    try:
        due = time.time() + 120
        answer = schedule.add_task(store, "提醒我喝水", due, -100, 50, "tg-a")
        assert answer.error == ""
        assert answer.scheduled in answer.result
        assert "fire" in answer.result

        (stored,) = store.schedules()
        assert stored.id == answer.scheduled
        assert stored.id.startswith("sch-")
        assert stored.content == "提醒我喝水"
        assert stored.due_at == due
        assert stored.chat_id == -100 and stored.message_id == 50
        assert stored.agent_id == "tg-a"
    finally:
        store.close()


def test_the_list_holds_twenty_tasks_and_then_refuses(tmp_path):
    store = store_at(tmp_path)
    try:
        due = time.time() + 60
        for index in range(schedule.MAX_TASKS):
            assert schedule.add_task(store, f"task {index}", due, -100, 50, "tg-a").error == ""
        refused = schedule.add_task(store, "one more", due, -100, 50, "tg-a")
        assert "already 20" in refused.error and "tg_remove_schedule" in refused.error
        assert store.count_schedules() == schedule.MAX_TASKS
    finally:
        store.close()


def test_nothing_waiting_reads_as_such(tmp_path):
    store = store_at(tmp_path)
    try:
        answer = schedule.view_tasks(store)
        assert answer.error == "" and answer.result == "no scheduled tasks are waiting"
    finally:
        store.close()


def test_the_view_lists_what_is_waiting_soonest_first(tmp_path):
    store = store_at(tmp_path)
    try:
        now = time.time()
        later = schedule.add_task(store, "晚一点的", now + 3600, -100, 50, "tg-a")
        sooner = schedule.add_task(store, "早一点的", now + 600, -100, 51, "tg-a")

        lines = schedule.view_tasks(store).result.splitlines()
        assert lines[0] == "2 scheduled task(s), soonest first:"
        assert f"[{sooner.scheduled}]" in lines[1] and "chat -100" in lines[1]
        assert "message 51" in lines[1]
        assert lines[2] == "早一点的"
        assert f"[{later.scheduled}]" in lines[3] and lines[4] == "晚一点的"
    finally:
        store.close()


def test_a_task_can_be_taken_off_the_list(tmp_path):
    store = store_at(tmp_path)
    try:
        answer = schedule.add_task(store, "提醒我喝水", time.time() + 60, -100, 50, "tg-a")
        removed = schedule.remove_task(store, answer.scheduled)
        assert removed.error == "" and "cancelled" in removed.result
        assert store.schedules() == []

        missing = schedule.remove_task(store, "sch-nope")
        assert missing.result == "" and "no scheduled task" in missing.error
    finally:
        store.close()


def test_the_clock_fires_what_is_due_and_forgets_it(tmp_path):
    async def scenario():
        store = store_at(tmp_path)
        bridge = FakeBridge()
        tasks: set[asyncio.Task] = set()
        try:
            due = task_at(time.time() - 1, "到点了")
            early = task_at(time.time() + 3600, "还早")
            store.add_schedule(due)
            store.add_schedule(early)

            clock = asyncio.create_task(schedule.run_scheduler(bridge, store, tasks))
            try:
                for _ in range(300):
                    if bridge.fired:
                        break
                    await asyncio.sleep(0.01)
                assert [task.content for task in bridge.fired] == ["到点了"]
                # it fired once, and it is gone: what is left is only the task in the future
                assert [task.id for task in store.schedules()] == [early.id]
                await asyncio.gather(*tasks, return_exceptions=True)
                assert [task.content for task in bridge.fired] == ["到点了"]
            finally:
                clock.cancel()
                try:
                    await clock
                except asyncio.CancelledError:
                    pass
        finally:
            store.close()

    async_run(scenario())


def test_the_clock_sleeps_until_the_next_task_or_a_moment():
    now = 1000.0
    assert schedule._wait(None, now) == schedule.IDLE_SECONDS
    assert schedule._wait(now + 2, now) == 2
    assert schedule._wait(now + 3600, now) == schedule.IDLE_SECONDS
    assert schedule._wait(now - 5, now) == schedule.NUDGE_SECONDS  # never a spin


def test_a_fired_task_reads_as_a_task_not_a_message():
    message = Incoming(
        chat_id=-100,
        message_id=50,
        content=Content(text="提醒我喝水"),
        sender_id=None,
        sender_name="a scheduled task",
        scheduled=True,
    )
    prompt = build_prompt(message)
    assert "a scheduled task you set earlier is due now" in prompt
    assert "task:\n提醒我喝水" in prompt
    assert "from:" not in prompt and "group:" not in prompt
    assert "Answer in the language of the task above" in prompt
    assert "calling tg_draft_response" in prompt
    assert prompt.endswith("as the last thing you do.")
