# openai-userbot2

A Telegram **userbot** (a real account, not a bot) whose replies are written by a
local [`headless-harness`](../headless-harness) agent.

In a group it stays quiet unless it is addressed:

- **`@name` mention** — opens a new conversation: a fresh block is forked from the
  chat's root, the message is handed to the agent, and the answer is sent back.
- **A reply to one of its messages** — continues *that* conversation, by forking
  from the very block that produced the message being replied to. If the replied-to
  message was never mapped to a block, nothing happens at all.

While the agent works, one status message follows the turn (edited in place, so the
chat is not spammed with progress). When the answer goes out, that message is
deleted. If the turn fails, the failure is reported back to the sender instead.

## How a turn looks

```
you  ▶ @mybot 帮我把这段话翻译成英文：今天天气不错

bot  ▶ 🧠 thinking · 1,234 chars                   ← edited as the turn runs
bot  ▶ 今天天气不错 → The weather is nice today.   ← the summary, a reply in itself
       ┃ The weather is nice today.                ← details, collapsed blockquote
```

The status message is a trace of the turn rather than a sentence: the phase, how
much thinking there has been on the thinking line itself, and which tool is being
called — never its arguments or its result. Everything else becomes a line of its
own below:

```
🧠 thinking · 900 chars
🔧 calling web_search
✅ web_search
✍️ drafting · 456 chars
```

`🔧 calling <name>` is the tool being called right now; once it returns the line
turns into `✅ <name>` or `❌ <name> failed (error)`. The turn ends on one of
`✅ done`, `❌ failed`, `⏹️ cancelled`, and a failed one adds the error below.

It appears only when the harness has actually produced something — a reasoning
chunk, a tool call, the start of a draft — so a turn that is accepted and then
fails without saying anything leaves no status message behind at all, just the
failure notice. It ends as `✅ done`, `❌ failed` or `⏹️ cancelled`.

The userbot's own messages (the status line, a failure notice) are written in
English; the agent's reply follows the language of the message it answers.

The agent answers **only** by calling the local tool `tg_draft_response`:

| parameter | what it is |
|---|---|
| `summary` | the short reply itself — two or three sentences addressed to the user, standing on its own |
| `details` | the same answer carried further, Markdown, `**bold**` only (no headings, lists, code, italics) |

The summary is what the user reads first, so it is written *to* them rather than
about the answer; both halves are written in the language of the user's message,
and a message whose language cannot be told (a bare link, an emoji, a number) is
answered in English. The agent is told to assume the user reads the details as
well: the two are one reply, not a teaser and an appendix. A link is written as
the bare URL with a space on each side of it — never `[text](url)` — so it is not
glued to the words around it.

The details are wrapped in a **collapsed blockquote**. Telegram caps a message at
4096 characters, so a long `details` becomes several messages — each keeping its
own collapsed blockquote, the first one replying to the user and the rest following
in order. The tool must be the agent's last action; a draft that never arrives is
reported in the status message rather than silently dropped.

The agent is told whose voice it writes in — the userbot's own name, `@username`
and id — and who wrote (`nickname`, `@username`, user id), plus, in a group, the
group's name, `@username` and id. When the message replies to somebody else, that
message comes with it:

```
[telegram]
you are replying on behalf of the userbot 小助手 (@mybot, user id 4242)
from: 小明 (@ming, user id 7)
group: 测试群 (@testgroup, group id -1001234567890)
replying to 小红 (@hong, user id 9):
the sender highlighted: 这一段
--- quoted message ---
看这个
[file: report.pdf (application/pdf, 1.2 MB)]
--- end of quoted message ---
message:
这是什么？
[/telegram]
```

Only a reply to *somebody else* is quoted: a reply to one of the userbot's own
messages continues that very conversation instead, so the agent already has it.

**Nothing non-text is dropped.** Anything the message (or the quoted message)
carries besides text becomes a bracketed placeholder, one per line, so the model
can tell a description from the sender's words:

| what arrived | what the agent reads |
|---|---|
| photo | `[photo (1280×720)]` |
| file | `[file: report.pdf (application/pdf, 1.2 MB)]` |
| video / video message | `[video: clip.mp4 (video/mp4, 5.0 MB, 1:32, 1920×1080)]`, `[video message (0:05, 320×320)]` |
| voice / audio | `[voice message (0:12)]`, `[audio: 歌手 — 标题 (audio/mpeg, 2.9 MB, 3:45)]` |
| sticker / GIF | `[sticker: 🐱]`, `[animation: cat.gif (video/mp4, 1.0 MB)]` |
| poll / to-do / dice | `[poll: 晚饭吃什么？ (2 options: 火锅 / 烧烤)]`, `[dice: 🎲 (5)]` |
| location / venue / contact | `[location (39.9042, 116.4074)]`, `[contact: 张 三 (+86…, user id 9)]` |
| anything Telethon does not know | `[unsupported media: messageMediaToDo]` |

A link preview keeps its title, site and URL; when Telegram has cached an **Instant
View** for it, that comes too — the `t.me/iv` link, and the article's text itself
under `[instant view content]`. A message with no text at all still says
`(no text)` rather than arriving empty.

The userbot's own messages are sent with `link_preview=False`, so a reply that
contains a link never grows a preview — and never an Instant View button.

## Requirements

- Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/)
- a `headless-harness` checkout (defaults to `~/headless-harness`), whose own
  `.venv` is used when the userbot starts its server
- Telegram API credentials from <https://my.telegram.org> (`api_id`, `api_hash`)

## Setup

```bash
uv sync

export TG_API_ID=1234567
export TG_API_HASH=0123456789abcdef0123456789abcdef

uv run python -m userbot
```

The first run asks for the phone number and the login code (and the 2FA password,
if any) and writes a `userbot.session` file next to the working directory. After
that it starts headless.

```
uv run python -m userbot --help          # every flag
uv run python -m userbot --no-spawn      # require a harness you started yourself
uv run python -m userbot --harness-port 8765 --db userbot.db
```

| flag | env | default | meaning |
|---|---|---|---|
| `--api-id` / `--api-hash` | `TG_API_ID` / `TG_API_HASH` | — | Telegram credentials |
| `--session` | `TG_SESSION` | `userbot` | Telethon session file |
| `--harness-host` / `--harness-port` | `HH_HOST` / `HH_PORT` | `127.0.0.1:8765` | where the harness listens |
| `--harness-project` | `HH_PROJECT` | `~/headless-harness` | the checkout to run |
| `--harness-python` | `HH_PYTHON` | its `.venv/bin/python` | interpreter for that server |
| `--harness-db` | `HH_DB` | the harness's own | SQLite file for the harness |
| `--no-spawn` | `HH_SPAWN=0` | spawn | start the harness when nothing is listening |
| `--model` | `HH_MODEL` | the harness's | model for new conversations |
| `--db` | `USERBOT_DB` | `userbot.db` | the mapping store |
| `--max-db-bytes` | `USERBOT_MAX_DB_BYTES` | 8 MiB | budget for that store |
| `--status-interval` | `USERBOT_STATUS_INTERVAL` | `2.0` | minimum seconds between status edits |
| `--turn-timeout` | `USERBOT_TURN_TIMEOUT` | `3600` | give up on a turn after this long |
| `--log-level` | `USERBOT_LOG_LEVEL` | `INFO` | logging |

## The mapping store

Answers have to be continuable, so every message the userbot sends is recorded
against the block that produced it — `(chat_id, message_id) → agent_id`, plus one
root block per chat. Two kinds of message are recorded:

1. every message of a delivered `tg_draft_response`;
2. the failure notice, when a turn fails after the block existed.

Nothing else is: a reply to the status message, or to anything sent before the
mapping was kept, is ignored on purpose.

The store has a size budget (`--max-db-bytes`, 8 MiB by default). When it is full
the **oldest** mappings are dropped first, because the newest are the ones a reply
can still land on. SQLite reuses the pages a delete frees, and incremental
auto-vacuum hands them back, so the file stays near the budget.

## Notes

- Private chats work the same way (a mention in a one-to-one chat is just a
  normal message); there the prompt simply has no `group:` line.
- A message from another bot is ignored outright, mention or not: bots do not get
  answers, and their messages are not logged.
- The agent sees the message text as written, mention included.
- Messages that arrived while the userbot was offline are ignored.
- Replying to somebody else's message is quoted into the prompt; replying to the
  userbot's own message continues that conversation instead of quoting it back.
- A `tg_draft_response` call is declared with `rollback`, so a turn that fails
  after the reply was delivered takes the messages back down and drops their
  mappings — the failure notice then explains what happened.
- If the harness has forgotten the block a reply points at (eviction, or another
  database), the conversation restarts from a fresh root instead of staying silent.

## Tests

```bash
uv run pytest                      # everything, including the end-to-end test
uv run pytest -m "not integration" # unit tests only
```

`tests/test_integration.py` runs a **real** `HHServer` from the harness checkout
against a scripted provider (`HH_TEST_PROJECT` overrides the path): the local tool
really parks a turn, a failed turn really rolls back, and the follow-up fork really
lands on the block that answered. It is skipped when no checkout is present.

## Layout

```
userbot/store.py     message -> agent block mappings, with the size budget
userbot/render.py    the draft: bold-only markdown, collapsed quote, splitting
userbot/content.py   what a message carries: text, media placeholders, Instant Views
userbot/harness.py   the harness wire protocol, one connection, many conversations
userbot/status.py    the single status message, and its rate limit
userbot/bridge.py    the rules: what is answered, where the turn forks, what is sent
userbot/telegram.py  the bit of Telegram the bridge needs, behind one interface
userbot/main.py      the Telethon client, login, handlers, spawning the harness
```
