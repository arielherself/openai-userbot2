"""The inline tools on their own: the menu they fetch, and the pick they send."""

from __future__ import annotations

import asyncio

from support import FakeTelegram

from userbot import inline

CATS = [
    {"title": "Grumpy Cat", "description": "a grumpy cat", "type": "gif"},
    {"title": "Angry Cat", "type": "article"},
]


def menu_id_of(answer) -> str:
    """The id the listing printed, the way the model reads it back."""
    return answer.result.splitlines()[0].split()[1]


def test_the_menu_comes_back_numbered_and_kept():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus()

        answer = await inline.open_menu(delivery, menus, "like", "cat")
        assert answer.error == ""
        assert delivery.inline_queries == [{"bot": "like", "query": "cat"}]
        first = answer.result.splitlines()
        assert first[0].startswith('menu menu-') and '(@like, asking for "cat"), 2 entries:' in first[0]
        assert first[1] == "1. Grumpy Cat [gif] — a grumpy cat"
        assert first[2] == "2. Angry Cat [article]"
        # the menu is held under the id it printed, so the pick can find it
        assert menus.get(menu_id_of(answer)) is not None

    asyncio.run(scenario())


def test_a_menu_can_be_opened_with_nothing_typed():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "")] = CATS
        answer = await inline.open_menu(delivery, inline.Menus(), "like")
        assert delivery.inline_queries == [{"bot": "like", "query": ""}]
        assert "with nothing typed" in answer.result

    asyncio.run(scenario())


def test_an_entry_with_no_title_or_description_still_prints():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = [{}]
        answer = await inline.open_menu(delivery, inline.Menus(), "like", "cat")
        assert answer.result.splitlines()[1] == "1. (untitled)"

    asyncio.run(scenario())


def test_an_empty_menu_is_an_answer_not_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = []
        answer = await inline.open_menu(delivery, inline.Menus(), "like", "cat")
        assert answer.error == ""
        assert "offers no entries" in answer.result

    asyncio.run(scenario())


def test_a_pick_sends_that_entry_into_the_chat():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus()
        opened = await inline.open_menu(delivery, menus, "like", "cat")

        answer = await inline.send_result(delivery, menus, menu_id_of(opened), 2, -100)
        assert answer.error == ""
        assert '"Angry Cat"' in answer.result
        assert answer.forwarded == delivery.inline_sent[-1]["id"]
        assert delivery.inline_sent == [
            {
                "id": answer.forwarded,
                "chat_id": -100,
                "bot": "like",
                "query": "cat",
                "index": 1,
                "title": "Angry Cat",
            }
        ]
        # the menu stays pickable: the same entry can be sent again
        again = await inline.send_result(delivery, menus, menu_id_of(opened), 2, -100)
        assert again.error == "" and len(delivery.inline_sent) == 2

    asyncio.run(scenario())


def test_a_menu_that_was_never_opened_cannot_be_picked_from():
    async def scenario():
        answer = await inline.send_result(FakeTelegram(), inline.Menus(), "menu-nope", 1, -100)
        assert answer.result == ""
        assert "there is no menu menu-nope" in answer.error
        assert "tg_open_inline_menu" in answer.error

    asyncio.run(scenario())


def test_an_entry_the_menu_does_not_hold_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus()
        opened = await inline.open_menu(delivery, menus, "like", "cat")

        answer = await inline.send_result(delivery, menus, menu_id_of(opened), 9, -100)
        assert "holds 2 entries" in answer.error and "no entry 9" in answer.error
        assert delivery.inline_sent == []

    asyncio.run(scenario())


def test_a_menu_too_old_to_pick_from_is_gone():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus(ttl=0.02)
        opened = await inline.open_menu(delivery, menus, "like", "cat")

        await asyncio.sleep(0.03)
        answer = await inline.send_result(delivery, menus, menu_id_of(opened), 1, -100)
        assert "too old to pick from" in answer.error
        assert delivery.inline_sent == []

    asyncio.run(scenario())


def test_the_cap_lets_go_of_the_oldest_menus():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus(limit=2)
        first = menu_id_of(await inline.open_menu(delivery, menus, "like", "cat"))
        second = menu_id_of(await inline.open_menu(delivery, menus, "like", "cat"))
        third = menu_id_of(await inline.open_menu(delivery, menus, "like", "cat"))

        assert menus.get(first) is None  # the oldest made room
        assert menus.get(second) is not None and menus.get(third) is not None

    asyncio.run(scenario())


def test_a_bot_that_cannot_be_reached_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.fail_inline = True
        answer = await inline.open_menu(delivery, inline.Menus(), "like", "cat")
        assert answer.result == ""
        assert "could not ask @like for its menu" in answer.error

    asyncio.run(scenario())


def test_a_pick_telegram_refuses_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.inline[("like", "cat")] = CATS
        menus = inline.Menus()
        opened = await inline.open_menu(delivery, menus, "like", "cat")

        delivery.fail_inline_send = True
        answer = await inline.send_result(delivery, menus, menu_id_of(opened), 1, -100)
        assert answer.forwarded is None
        assert "could not send that entry: telegram said no" in answer.error

    asyncio.run(scenario())


def test_the_arguments_are_read_the_way_the_model_writes_them():
    bot, query, complaint = inline.open_arguments({"bot": " @like "})
    assert (bot, query, complaint) == ("like", "", None)
    assert "`bot`" in inline.open_arguments({})[2]
    assert "`bot`" in inline.open_arguments({"bot": "@"})[2]
    assert "`query`" in inline.open_arguments({"bot": "@like", "query": 7})[2]

    menu, index, complaint = inline.pick_arguments({"menu": " menu-x ", "index": "2"})
    assert (menu, index, complaint) == ("menu-x", 2, None)
    assert "must be the id" in inline.pick_arguments({"menu": " ", "index": 1})[2]
    assert "counts from 1" in inline.pick_arguments({"menu": "menu-x", "index": 0})[2]
    assert "counting from 1" in inline.pick_arguments({"menu": "menu-x", "index": True})[2]
    assert "counting from 1" in inline.pick_arguments({"menu": "menu-x", "index": "x"})[2]


def test_the_tools_declare_what_the_model_must_know():
    opened, pick = inline.TOOLS
    assert opened["name"] == "tg_open_inline_menu"
    assert [param["name"] for param in opened["params"]] == ["bot", "query"]
    assert "tg_send_inline_result" in opened["description"]
    assert "@pteebot" in opened["description"]  # the film and TV menu is named

    assert pick["name"] == "tg_send_inline_result"
    assert [param["name"] for param in pick["params"]] == ["menu", "index"]
    assert pick["rollback"] is True and pick["external_effects"] is True
    assert "tg_open_inline_menu" in pick["description"]
