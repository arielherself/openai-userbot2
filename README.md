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
deleted. If the turn fails, the failure is reported back to the sender instead —
unless it was the network that broke it, in which case the same message is run
again first, on a fresh block, at most five times.

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

For as long as it stands, the tracking message is recorded against the block it
is tracking, so replying to it with **`/inspect`** prints what the turn is doing
— the block it runs in, the same trace with nothing clipped, and where the reply
stands. That mapping is as temporary as the message: deleting the message drops
it, and the answer to `/inspect` is a message of the userbot's like any other, so
it can be asked about in turn. `/inspect` never starts a turn of its own.

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

### Music

Two more tools talk to a third-party music bot, [@Music163DownBot](https://t.me/Music163DownBot):

| tool | what happens |
|---|---|
| `tg_search_music(keyword, platform)` | the bot is sent `/search <keyword> <code>` and its listing comes back as the tool result — a numbered song per line, each with its link, **as the bot wrote it**: Telegram turns a bot's Markdown into message entities, so the text is re-rendered with its links (`[title](link)`) rather than handed over with them stripped. An edit that adds the keyboard counts as the answer too, which is how that bot delivers it. |
| `tg_send_music(url, platform)` | the bot is sent `/music <url> <code>`, and the audio file it returns is **forwarded into the chat**; a refusal comes back as the tool result instead |

### Reading a chat

| tool | what it does |
|---|---|
| `tg_view_current_chat()` | reads this chat's last 50 messages |
| `tg_view_public_chat(username)` | reads the last 50 of a public group or channel, named like `@telegram` |
| `tg_read_message(message_id, from_chat?)` | reads one message by the id the tools above print in brackets |
| `tg_forward_message(message_id, from_chat?)` | forwards one message into this chat, so it lands at the end |

Each message comes back oldest-first with its **message id**, the time it was
sent (in the machine's own timezone, offset included), its sender — display name,
`@username` when there is one, user id — and its full content, using the same
media placeholders as the prompts. A message that answers another one shows what
it answers, on a line of its own:

```
2. [4822] 2026-09-18 12:13 +0800 小红 (@hong, user id 9):
↩ in reply to [4821] 小明 (@ming): 今晚吃什么？
火锅吧
```

The answered message is taken from the same reading when it is in the window, and
asked for by id when it is older — one batched request however many replies there
are — so a run of replies reads as a conversation. It is clipped to a line (the
id is right there to read in full), a message that is gone is named as such, and a
quote of a selection adds the part that was highlighted. A message whose author Telegram hides fits the
same shape: an anonymous admin has no id or username to print, so it shows the
signature it posts under, or simply `anonymous admin`, rather than a `user id 0`
that means nothing. Being hidden says nothing about the media: a picture an
anonymous admin posted is fetched and passed on like any other, because the file
carries its own reference and never needed a sender. The result opens with the chat it read, so it is
clear what was looked at. A chat with nothing in it is an answer, not a failure;
a username without the `@`, a chat that cannot be resolved, a message id that is
not there, and a read Telegram refuses all come back as tool errors. Both
single-message tools also take an optional `from_chat` — a public `@name` or the
chat id the view tools printed — so a message can be read or forwarded out of
another chat; leaving it out means this one, and the error for a missing message
says which chat it looked in. The two
reading tools declare no rollback and change nothing; a forwarded message is
recorded against the block like the other forwards, and deleted again if the turn
that made it fails.

### Parsed content

| tool | what happens |
|---|---|
| `tg_send_parsed_content(url)` | the link is sent to [@ParseHubot](https://t.me/ParseHubot), which answers that very message with the page or video rendered; that answer is **forwarded into the chat** |

It covers 酷安, 贴吧, 豆瓣, 知乎, 皮皮虾, 最右, 抖音, 快手, 微博, 微信公众号, 小黑盒,
小红书, Youtube, Twitter, TikTok, Threads, Snapchat, Instagram, Facebook and
Bilibili. The answer counts only when it *is* a reply to the link and carries a
link of its own (a bare URL, or one hidden in formatted link text) — a note like
"解析中…" is not the content yet. The bot has 15 seconds to acknowledge the link
at all; after that the call may take up to 5 minutes, and a bot that stays silent
returns an error. The forwarded content is recorded like the reply's messages, so
replying to it continues the conversation, and a failed turn deletes it again.

`platform` is one of `NetEase`, `AppleMusic`, `QQMusic`, `Soda`, mapped to the
bot's own names (`163`, `am`, `qq`, `qs`). The agent is told to try them in that
order, falling back to the next one when a search comes up empty — and to pass the
platform the link actually came from to `tg_send_music`.

The music bot is shared, so the whole userbot makes **one music call at a time**
and waits **5 seconds after each call ends** before the next one starts — across
all chats, not per conversation. A `tg_search_music` or `tg_send_music` call has a
budget of **5 minutes that covers everything**: waiting for its turn at that rate
limit, the bot's grace period, and the answer. A call that would spend its budget
just queueing returns `the music bot is busy: …` instead of waiting in line.

Once a call has its turn, a working bot acknowledges the command almost
immediately, so **30 seconds of complete silence** counts as unreachable; after
that it may take the rest of the budget to produce the listing or the file. A
conversation's root is created with a `local_timeout` of 15 minutes so the harness
outlives a queue plus a slow fetch.

Every failure on our side — the bot cannot be reached, it never answers, the file
cannot be forwarded — comes back to the agent as a **tool error**, so it can fall
back to the next platform or tell the user what happened. A refusal from the bot
("fail: …") is not an error: it is the tool's result, passed on verbatim.
`tg_send_music` declares a rollback, so a turn that fails after the file went out
deletes it again — and drops its mapping, like the reply's.

A draft is published with every `@` shown as `#`: Telegram reads an `@name` as a
mention, which would notify whoever the agent happened to name.

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

### Scheduled tasks

A task fires once, at a time the agent names, and its answer goes back where it was
set: the same chat, as a reply to the message that asked for it.

| tool | what it does |
|---|---|
| `tg_add_schedule(content, at)` | puts a task on the list: at `at` — machine-local time, `YYYY-MM-DD HH:MM`, seconds optional — an agent turn starts in this very conversation, with `content` as its whole prompt |
| `tg_view_schedule()` | everything waiting, soonest first, across all chats: id, local time with its offset, and the text it will run |
| `tg_remove_schedule(id)` | cancels one task, so it never fires |

`at` must be in the future and **at most 24 hours ahead**; a later time is refused,
as is one that has already gone by (minutes are accepted a little behind the clock,
so a "now" rounded down still works). The list holds **20 tasks at once** — when it
is full `tg_add_schedule` answers with an error instead, and a task has to be
cancelled or fire before another fits. The clock wakes for whatever is due next, so
a task fires within seconds of its time; a task whose time passed while the userbot
was down fires when it starts.

When a task comes due the agent gets a prompt of the same shape as a message's —
who it writes as, then the task's own text under `task:` where a message puts
`from:` and `message:` — and is told to deliver the answer with
`tg_draft_response` as the last thing it does:

```
[telegram]
you are replying on behalf of the userbot 小助手 (@mybot, user id 4242)
a scheduled task you set earlier is due now, and what you write goes to the chat it was set in, as a reply to the message it was set for
task:
提醒用户喝水
[/telegram]
Answer in the language of the task above — … deliver it by calling tg_draft_response …
```

The turn runs in the conversation that set the task, so the context that scheduled
it is still there; if the harness has forgotten that block, it starts from a fresh
root like any other turn. The answer is a message of the userbot's like any other,
so replying to it continues the conversation. `tg_add_schedule` declares a
rollback: a turn that fails after scheduling cancels the task again, so nothing
fires that the user was never told about. The tasks live in the same SQLite file as
the mappings, in a table of their own; they are few and short-lived, so the size
budget never touches them.

### Files into a sandbox

| tool | what happens |
|---|---|
| `tg_download_file_to_sandbox(chat_id, message_id, sandbox_id, path)` | the file a message carries is downloaded and written into a live harness sandbox at `path`; the bytes go straight there, never through the model |
| `git_clone_to_sandbox(url, sandbox_id, path, depth, branch)` | the repository is cloned on the userbot's side — a sandbox has no network — and arrives as one `tar.gz` holding the checkout **and its `.git`**, to unpack with `nix_exec` |
| `curl_to_sandbox(url, sandbox_id, path)` | what the URL serves — a PDF, a picture, an archive, a script — is fetched with `curl` and written into the sandbox at `path` |

All three end the same way: the userbot fetches the bytes **on this side of the
wire** and hands them to the harness's own `nix_add_file` as a **tool pipe**. The
model is shown the two tool names and what the sandbox wrote, and the file itself
stays out of the transcript however large it is. What travels is a piped argument,
one command's worth of base64, which is also what sizes the ceiling: the harness
takes **200 MiB** in one `nix_add_file` call, so nothing here may bring more than
**150 MB** down at once. A download is measured while it runs and stopped the
moment it crosses that, so a runaway clone or an endless URL costs a moment rather
than the disk.

Only `http(s)` URLs are fetched: a sandbox tool reaches the network, never this
machine's filesystem, so `file://` and `ftp://` are refused before anything runs.
`path` has to be under the sandbox's writable `/workspace`, the only place a file
outlives the `nix_exec` that made it (`/workspace/../…` is refused rather than
followed), and both fetch tools take their name from the URL when they are not
told one — `/workspace/<repository>.tar.gz` and `/workspace/<last segment>`. A
clone lands as the archive it is, so its `path` must end in `.tar.gz`; `depth`
says how much history comes with it (1, the tip commit only, unless more is asked
for — 0 is the whole history) and `branch` a branch or tag other than the default.
A fresh sandbox has no `tar` of its own — the agent installs `gnutar` and `gzip`
(and `git`, for the `.git` the archive carries) with `nix_add_dependency` before
unpacking it with `nix_exec`.

`chat_id` names the chat the way the reading tools do — a public `@name`, or the
id they print. A message that carries no file, a destination somewhere else, a URL
that cannot be reached, a reply that passes the cap and a sandbox that is no
longer live all come back as **tool errors**, so the agent can look again rather
than assume the file is there.

All three need a harness speaking protocol `4` — tool pipes arrived with it. On an
older one the call answers with an error instead of fetching a file nothing could
carry.

### Files out of a sandbox

| tool | what happens |
|---|---|
| `tg_send_file_from_sandbox(sandbox_id, path)` | a file a command left in a live sandbox is posted into this chat as a document named after the last part of its path — `/workspace/chart.png` arrives as `chart.png` |
| `tg_send_file(path, content)` | the last step of that pipe: `nix_cat_file` hands it the path and the file's bytes, base64-encoded, and it posts them here under the name the path ends with |

The file comes back out the way it went in, over the same wire.
`tg_send_file_from_sandbox` answers with a **tool pipe** into the harness's
`nix_cat_file`, which reads the file out of the sandbox and hands it straight to
`tg_send_file` — a local tool, so the bytes cross the connection base64-encoded
inside one `local_tool_called` event. Nothing of the file enters the transcript:
the model is shown the chain (`[tool pipe] tg_send_file_from_sandbox ->
nix_cat_file -> tg_send_file`) and what the send said, never the bytes. It goes
as a document, exactly as the sandbox held it and under the name its path ends
with — a picture is not re-encoded into a photo — and it is recorded against the
block like the other forwards, so replying to it continues the conversation and a
failed turn deletes it again.

`path` is the file's absolute path in the sandbox, usually under `/workspace`,
the only place a file outlives the `nix_exec` that made it; a path carrying `.` or
`..` is refused before anything is read. A sandbox that is gone, a file that is
missing or unreadable, and one past the harness's 200 MiB read limit all come
back as **tool errors**. The read is the harness's own, so this pair needs a
harness whose `nix_cat_file` can hand a file to a client-run tool, and protocol
`4` like the pipes above.

## Requirements

- Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/)
- `git` and `curl` on the machine that runs the userbot: the two fetch tools clone
  and download there, because a sandbox has no network of its own
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

## Pictures

The harness takes images next to a prompt, and next to a tool result (protocol 3),
so the agent can look at what it is being asked about instead of reading a
placeholder. The userbot fetches them from Telegram as `data:` URIs — a photo, a
picture sent as a file, or a **frame of a video** (its thumbnail) — and only for
three things:

- the message that mentioned it,
- the message the sender quoted or replied to (when that is somebody else's, so
  the prompt carries its text anyway),
- a message read with `tg_read_message`, which is why its description says the
  pictures come back with it.

A link is read the same way. The **preview**'s own picture or video is judged like
a message's media — a video giving up its thumbnail — and so is the media of a
cached **Instant View** page: its photos, the pictures sent as files, and a frame
of each video it holds. The preview comes first, and all of it travels with the
message that carries the link.

Everything else keeps the placeholder: the history the view tools print stays
text, and so does a message whose picture Telegram will not hand over. At most
four pictures ride along, each capped at 3 MiB and the lot at 5 MiB of base64,
because they travel in the same JSON line as the prompt. A harness that reports
protocol `2` is sent none — it would ignore the field.

## The mapping store

Answers have to be continuable, so every message the userbot sends is recorded
against the block that produced it — `(chat_id, message_id) → agent_id`, plus one
root block per chat. Three kinds of message are recorded:

1. every message of a delivered `tg_draft_response`;
2. every track `tg_send_music` forwards into the chat;
3. the failure notice, when a turn fails after the block existed.

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
- The agent sees the message text as written, mention included. A hidden sender
  (an anonymous admin) is named the same way everywhere: the signature it posts
  under, or `anonymous admin`, with no invented id.
- Pictures are fetched only for what the agent is looking at *now* — the new
  message, what it quotes, or one it asked to read — never for a history listing.
- Messages that arrived while the userbot was offline are ignored.
- Replying to somebody else's message is quoted into the prompt; replying to the
  userbot's own message continues that conversation instead of quoting it back.
- The local tools that post something — `tg_draft_response`, `tg_send_music`,
  `tg_send_parsed_content`, `tg_send_file` — declare a rollback, so a turn that
  fails after they did takes the messages back down (and drops their mappings)
  before the failure notice explains what happened. `tg_add_schedule` declares one
  too: the task it put on the list is cancelled again instead. A rollback reaches
  a pipe's own step like any other call: a file `tg_send_file` posted is deleted
  even though the model never made that call itself.
- **An attempt that did not get through is run again.** Three things count: the
  link to the harness going away; a provider request that never reached an answer
  at all (the harness reports the round it lost); and a provider that answered with
  a status that means "not now" — 408, 429, and 500/502/503/504. Either way the
  turn is attempted again — up to five retries after the first try, waiting 1, 2,
  4, 8 and 8 seconds so the tries outlast a blip — forked from the same parent
  block, since the attempt that failed committed nothing. The failed attempt's
  status message is followed into the retry rather than replaced, and whatever it
  managed to send is taken back first, the way a rollback would have, so the retry
  does not leave a second copy of it in the chat. Any other failure — a 400 or a
  401 from the provider, a tool's own error, a protocol refusal from the harness, a
  turn that ran out of time — is reported at once, because asking again would reach
  the same answer. The link to the harness is also re-opened on its own (five tries,
  half a second apart) before a command goes out, so a socket that died between two
  commands costs a reconnect rather than a turn.
- The tools that move a file between a chat and a sandbox declare what the move
  leaves behind. The ones going in — `tg_download_file_to_sandbox`,
  `git_clone_to_sandbox` and `curl_to_sandbox` — declare an external effect and no
  rollback: what they put into a sandbox is there to stay, and the failure note
  says so rather than letting the next model assume the sandbox is clean; the
  harness's own `nix_add_file`, which each pipe ends on, declares the same. The
  one coming out, `tg_send_file_from_sandbox`, changes nothing itself — its read
  is the harness's — and the `tg_send_file` it pipes to answers for the document
  it posted.
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
userbot/music.py     the two tools that reach the music bot
userbot/parse.py     the tool that has the parse bot render a link
userbot/history.py   the tools that read a chat, one message, or forward it
userbot/schedule.py  the scheduled tasks: the three tools, and the clock that fires them
userbot/sandbox.py   the sandbox pipes both ways: a chat's file in, a sandbox's file out
userbot/fetch.py     the tools that fetch a repository or a URL into a sandbox
userbot/relay.py     the shape both share: ask a bot, wait for its answer
userbot/tools.py     what the local tools share: their answer, their arguments
userbot/harness.py   the harness wire protocol, one connection, many conversations
userbot/status.py    the single status message, and its rate limit
userbot/bridge.py    the rules: what is answered, where the turn forks, what is sent
userbot/telegram.py  the bit of Telegram the bridge needs, behind one interface
userbot/main.py      the Telethon client, login, handlers, spawning the harness
```
