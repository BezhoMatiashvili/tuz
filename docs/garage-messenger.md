# Garage 1886 on Facebook

The Facebook page **Garage • ფარეხი 1886** gets the same parts assistant that answers the
website chat. It works through Meta's Graph API and Meta's own app GARAGE 1886, the way
TNLU's Messenger bot does, but without TNLU's MCP server. Customers who write to the page on
Messenger get an answer. A new comment on a Garage post gets a private reply on Messenger.
The dashboard shows the chats, and page stats and Garage's own ad results come in read-only.
An admin switches the bot on and off from the dashboard, or from their own Messenger with
`#bot off` / `#bot on`. In between there is **test mode**: the bot answers only the chats an
admin marked as testers (`#bot test`), and everyone else is left to the shop.

Everything runs on this laptop. Only a small receiver for Meta's webhooks is reachable from
the internet, through an ngrok tunnel; the dashboard stays on loopback.

## How it works

```
             HTTPS                     loopback
  Meta ─────────────> ngrok tunnel ─────────────> receiver, 127.0.0.1:8771
  webhooks:           garage-ngrok                garage-meta-receiver
  messages, echoes,                               checks the signature, appends the item
  button taps, ads                                to garage.meta_inbox, NOTIFY, answers 200
  referrals, comments                                        │
                                                             ▼
  Graph API  <── replies, private replies ──────  worker, no port
  (Send API,     syncs: names, comments, posts,   garage-messenger
   stats, ads,   page stats, ads, tokens,         switches and limits -> parts assistant
   subscriptions) subscription self-repair        (public audience, its own files)
                                                             │
                                                  PostgreSQL, schema garage
                                                             │
  admin's browser ──> Garage app, 127.0.0.1:8767 ──> Dashboard → Facebook (admin only)
  employee's or admin's browser ──────────────────> Dashboard → Live chats (both roles)
```

| Process | Command | systemd user unit | Reachable from | Database login (role) |
|---|---|---|---|---|
| Receiver | `python -m automotive.meta_receiver` | `garage-meta-receiver` | the internet, only through ngrok | `MESSENGER_RECEIVER_DATABASE_URL` (`garage_webhook`) |
| Worker | `python -m automotive.messenger_worker` | `garage-messenger` | nothing: it has no port | `MESSENGER_DATABASE_URL` (`garage_messenger`), and the app's runtime login for the catalogue |
| Tunnel | `ngrok http 127.0.0.1:8771 --url=https://glorified-stratus-budding.ngrok-free.dev` | `garage-ngrok` | Meta, and anyone on the internet | none |
| Dashboard | the Garage app, `python -m automotive` | none (started by hand) | loopback | `AUTOMOTIVE_SITE_DATABASE_URL` (`garage_site`) |

- **The receiver** (`automotive/meta_receiver.py`) checks each delivery's
  `X-Hub-Signature-256` and stores every item in `garage.meta_inbox`. It wakes the worker
  and answers 200 at once. It answers 503 only when the inbox could not be written, so that
  Meta retries.
- **The worker** (`automotive/messenger_worker.py`) answers the customers, in this order:
  1. It reads the inbox (LISTEN plus a 2-second poll).
  2. It waits until the customer has paused for 5 seconds, or at most 45 seconds after the
     first message, so the burst is answered together.
  3. It reads the switches again.
  4. It runs one assistant turn (`automotive/messenger_engine.py`).
  5. It sends the reply through the Send API, cut into Messenger-sized pieces by
     `automotive/messenger_render.py`.

  Up to 3 chats are answered at once, one turn per chat. The worker also runs the background
  jobs (see [Jobs](#jobs)).
- **The engine** uses the website chat's public configuration: no paid catalogue, no
  Motorium, no learning, and no web research unless `AUTOMOTIVE_RESEARCH=all`. Its files
  live in `data/messenger/` (source cache, research cache), so it never shares a file with
  the app on port 8767. It keeps no chat log: the chats are in the database, where Delete
  data reaches them. The `shop_info` tool quotes the phone and hours from the dashboard's
  brand settings.
- **The dashboard** is Facebook in the admin menu, at `/admin/facebook`, with the tabs
  Status & switches, Connect, Chats, Comments, Page and Ads. Its API is
  `/api/admin/facebook/*` (`automotive/site_meta_admin.py` and
  `automotive/site_meta_activity.py`), for admins only.
- **Live chats** is a second page, for employees and admins alike, at `/admin/live`
  ([garage-site.md](garage-site.md#live-chats-and-alarms)). It shows these chats as they
  arrive (messages are stored even while the bot is off), raises an alarm when the bot could
  not find a part, asks for staff, or got stuck, and lets an employee switch the bot off for
  one customer: the same per-chat switch as Chats, through a narrower route that cannot touch
  the admin-account flag or delete anything. After each turn the worker records the alarms the
  engine reported (`EngineResult.alerts`, `live_store.LiveStore.record_turn`), before it sends
  the reply, so a reply that could not be delivered still alarms. Alarms are in
  `garage.live_alerts` (migration 0015); "Delete data" removes a customer's, and the daily
  purge removes those older than "Keep bot records for".

### Why a Meta app, when everything goes through the Graph API?

Graph is only the API. The app is who is calling, and Meta ties all of this to an app:

- **Tokens.** Graph accepts only access tokens that Meta issued to an app. Permissions such
  as `pages_messaging` are granted to that app. Several things name GARAGE 1886 and need its
  App ID and App Secret:
  - the Explorer token the owner pastes, and the page token made from it;
  - the `debug_token` check and the 60-day token exchange;
  - the `appsecret_proof` on every call made with GARAGE 1886's tokens.
- **Incoming messages.** Graph never calls us. Meta pushes new messages, echoes and comments
  only to an app's webhook subscription, signed with that app's secret. The receiver checks
  that signature.
- **One webhook address per app.** TNLU's two apps already point at TNLU's server. Garage
  therefore has its own app, and TNLU is not touched.
- **Bot or shop.** Every echo names the app that sent it. The worker counts GARAGE 1886's
  echoes as the bot and every other app's as the shop (Business Suite, the Messenger app).
- **Publishing.** Meta delivers webhooks about people who have no role on an app only once
  the app is Live. Publishing asks for a privacy policy and data-deletion address, which is
  why the receiver serves `/privacy` and `/data-deletion`.

## Owner setup, in order

Each step needs the one before it. Nothing reaches a customer before step 10, and the bot
ships switched off.

### 1. Create the Meta app GARAGE 1886

1. In Meta for Developers, create an app named **GARAGE 1886**:
   - type Business, with no business portfolio (the page is in none);
   - use cases **Messenger**, **Manage everything on your Page** and **Measure ad
     performance (Marketing API)**.
2. Put the App ID into `.env` as `META_APP_ID`. It is not a secret.

If Meta will not combine the Marketing API with the other use cases in one app, the plan's
answer is a second small app, "GARAGE 1886 Ads". The code already reads an `ads_user` token
when one is stored, but nothing stores one yet: ask before relying on it.

### 2. Put the App Secret into `.env`

The owner copies it from App settings → Basic → App secret → Show (Meta asks for the
password) into `.env`:

```bash
META_APP_SECRET=...
```

It belongs in `.env` only. Never put it in `.env.example`, in a chat, in a unit file or in
git.

### 3. Prepare the database and the keys

```bash
cd /home/bezhomatiashvili/Desktop/tuz
uv run python -m automotive.messenger_setup --setup
```

`--setup` refuses a `DATABASE_URL` that is not on loopback. It also needs the garage site
set up already (migration 0012, from `automotive.site_setup --setup-site`). It then does
the following:

1. Provisions the roles (`ops/sql/provision_automotive_roles.sql`).
2. Applies the Messenger migrations that are missing, `0013_meta_messenger` and
   `0014_meta_forget`, never another pending migration. On a database set up before 0014
   existed, a rerun applies 0014 alone. Apply it before the services run code that needs
   it; until then Delete data leaves comments alone (`comments: null`).
3. Creates two logins, each a member of one role only: the receiver's (`garage_webhook`) and
   the worker's (`garage_messenger`).
4. Generates `META_WEBHOOK_VERIFY_TOKEN` and `META_TOKEN_ENCRYPTION_KEY`.
5. Sets `MESSENGER_PUBLIC_URL` to the reserved ngrok address and `META_GRAPH_API_VERSION`
   to `v26.0`.

It writes only keys that are missing and makes `.env` private (mode 600). It prints the
names of what it added, never a value, and names `META_APP_ID` / `META_APP_SECRET` while
they are still empty. A rerun changes nothing that is already there.

The Garage app on port 8767 reads `.env` only when it starts. Restart it once the keys are
in, so that Dashboard → Facebook can use them; the app is restarted only with the owner's
OK. Until the restart, the section says a restart is needed or names the missing
variables.

### 4. Install and start the services

The units in `ops/systemd/` are **user** units: they run as the laptop user, need no sudo,
and read `.env` themselves. During setup the receiver and the tunnel may have been started
by hand as temporary units with the same names; `systemctl --user list-unit-files 'garage-*'`
lists those as `transient`. Stop them first; a stopped temporary unit disappears.

```bash
cd /home/bezhomatiashvili/Desktop/tuz
systemctl --user stop garage-ngrok.service garage-meta-receiver.service
systemctl --user reset-failed garage-ngrok.service garage-meta-receiver.service 2>/dev/null
install -m 0644 ops/systemd/garage-meta-receiver.service ops/systemd/garage-messenger.service \
    ops/systemd/garage-ngrok.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now garage-meta-receiver.service garage-ngrok.service \
    garage-messenger.service
loginctl enable-linger    # the services keep running without a login (already on here)
```

Check them:

```bash
systemctl --user status garage-meta-receiver garage-ngrok garage-messenger --no-pager
uv run python -m automotive.messenger_setup --check-public
```

`--check-public` goes through the public address, as Meta does. It must print two `ok`
lines:

- the webhook refuses a made-up verify token with 403;
- `/healthz` answers 200.

A unit that stops at once with exit status 2 is missing a setting. Its journal names the
variable (`journalctl --user -u garage-messenger -n 20`). Fill it in `.env`, then run
`systemctl --user restart garage-messenger`.

Keep the laptop awake while it is plugged in: turn off Settings → Power → Automatic
Suspend. Closing the lid suspends it, even on power, unless logind is told otherwise. While it
sleeps, Meta's deliveries fail and are dropped after about two hours (the `catchup` job reads
them back later, but nobody is alerted meanwhile).

The user unit `ops/systemd/laptop/garage-keep-awake.service` (installed and enabled
2026-11-20) holds logind's lid inhibitor while the owner is logged in, without root: check it
with `systemd-inhibit --list | grep "Garage 1886"`. Before anybody logs in after a reboot it
cannot take the lock (polkit), so for that case too, once, as root:

    sudo mkdir -p /etc/systemd/logind.conf.d
    printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
      | sudo tee /etc/systemd/logind.conf.d/50-garage-lid.conf
    sudo systemctl reload systemd-logind

Check with `busctl get-property org.freedesktop.login1 /org/freedesktop/login1
org.freedesktop.login1.Manager HandleLidSwitch` (it answers `s "ignore"`). Suspend from the
power menu still sleeps: don't use it on this laptop. (`reload` re-reads the setting without
ending anybody's session: logind supports it on this systemd, 259.)

### 5. Publish the app (Live)

1. **Confirm the shop facts on the two pages first.** The receiver cannot read the
   dashboard, so they are constants in `automotive/meta_receiver.py` (`SHOP`): name,
   address, phone, the "last updated" dates and how many days a deletion request may take.
   The pages say conversations are kept until the customer asks for deletion.
2. **Open the pages** in a browser. ngrok shows browsers a warning page first; Meta's
   servers do not get it.
   - https://glorified-stratus-budding.ngrok-free.dev/privacy (English at `/privacy/en`)
   - https://glorified-stratus-budding.ngrok-free.dev/data-deletion (English at
     `/data-deletion/en`)
3. **Fill in App settings → Basic:**
   - Privacy policy URL: the `/privacy` address;
   - User data deletion, data deletion instructions URL: the `/data-deletion` address;
   - an app icon made from the GARAGE 1886 logo;
   - a category.
4. **Publish the app** (switch it to Live).

There is no need to type the webhook address or the verify token into Meta: step 6
registers them. If Meta rejects the ngrok address for the privacy page, the two static
pages have to be hosted elsewhere; ask first.

### 6. Connect the page

1. **Get a token.** Open Graph API Explorer (https://developers.facebook.com/tools/explorer/):
   1. Select the app **GARAGE 1886**.
   2. Tick the 8 permissions:
      - `pages_show_list`
      - `pages_messaging`
      - `pages_manage_metadata`
      - `pages_read_engagement`
      - `pages_read_user_content`
      - `pages_manage_engagement`
      - `read_insights`
      - `ads_read`
   3. Press Generate Access Token and allow it for the Garage page.
2. **Paste the token** into Dashboard → Facebook → Connect, then press Connect. The form
   empties the field as it sends, and the token is never shown again. Or, from a terminal:
   `uv run python -m automotive.messenger_setup --connect` reads it with hidden input.

The receiver and the tunnel must be running at this moment, because Meta calls the webhook
address while the app is subscribed.

The server then does the following, and no token reaches the browser or a log:

1. Checks the token with `debug_token`: it must be a user token of GARAGE 1886.
2. Exchanges it for a long-lived one (about 60 days).
3. Finds the Garage page in `/me/accounts`, using `META_PAGE_ID` or `--page-id` when the
   login manages several pages.
4. Checks the page token.
5. Encrypts both tokens (AES-256-GCM, `META_TOKEN_ENCRYPTION_KEY`) and stores them.
6. Subscribes the app (`POST /{app}/subscriptions`, callback
   `MESSENGER_PUBLIC_URL/meta/webhook`).
7. Subscribes the page (`POST /{page}/subscribed_apps`), both with the fields `messages`,
   `message_echoes`, `messaging_postbacks`, `messaging_referrals` and `feed`.

If `pages_messaging`, `pages_manage_metadata` or `pages_show_list` is missing, connect
refuses. Any other missing permission only switches its feature off, and the answer lists
it. If the subscription failed, the connection is kept: start the receiver and the tunnel,
then press Repair webhooks on the Connect tab.

Then list where Garage's ads run:

```bash
uv run python -m automotive.messenger_setup --discover-ads
```

It prints, for every ad account the login can read, how many ads promote the page. If it
finds none, tell the developer where Garage's ads run.

### 7. Probe: may this app message customers? (gate, sending half)

Meta has refused new apps before. A system-user token got code 10. TNLU Assistant passed
with a page token from the owner's personal login, but that is unproven for a brand-new
app. The probe settles it without delivering anything.

How to run it:

- Dashboard → Facebook → Connect → Test sending, or
- `uv run python -m automotive.messenger_setup --probe`, or `--probe --psid ID` for one
  known customer.

What it does: it sends `typing_on` to a customer whose last message is more than 25 hours
old.

| Result | Meaning | What to do |
|---|---|---|
| `passed` | Meta answered "outside the 24-hour window" (10/2018278), or accepted the indicator. This app may message customers. | Go on to step 8. |
| `gated` | A bare code 10. Meta does not let this app message people who have no role on it. | See [If Meta's gate blocks the new app](#if-metas-gate-blocks-the-new-app). |
| `inconclusive` | No suitable quiet customer, or an unexpected answer. The reason is shown. | Try again later, or probe one known customer with `--psid`. |

A pass proves something only if the customer it used has no role on the app. The result
names that customer by the last four digits of their id (`…1234`). If that is you or a
tester, mark that chat Admin account (Chats tab) and probe again; admin chats are never
probed.

### 8. Receive check (gate, receiving half)

Leave the bot **off**. Two things must arrive:

- a message from a real customer, someone with no role on the app (not you);
- a reply typed by staff in Meta Business Suite.

Both appear in Status & switches → Latest events, or with
`uv run python -m automotive.messenger_setup --events`. They show as field `messages`, with
the outcomes `stored` and `echo_shop`. Events show the type, the time and the outcome, never
the text. This proves that the Live app receives webhooks about people with no role on it.

### 9. Switch Meta's Auto reply off

Go to Meta Business Suite → Inbox → Automations and turn **Auto reply** off. Leads can stay
on: it only tags chats.

Every echo from another app counts as the shop answering, and that includes Auto reply's
greeting. Auto reply also fires on old chats, so no timing rule can tell it from a person.
The dashboard therefore turns the bot on only after a tick that confirms Auto reply is off.

### 10. First test

This costs about $0.03 per answer on the AI provider. Check the OpenRouter balance first:
the direct Anthropic account is unfunded today, so OpenRouter pays for the engine.

1. **Turn the bot on.** In Status & switches, press Turn the bot on, tick "Meta Business
   Suite Auto reply is off", and confirm.
2. **Turn comment replies on.** Switch on "Reply privately to comments".
3. **Write from your personal profile,** not while acting as the page:
   - one message to the page;
   - one comment on a Garage post.
4. **Check the answers** on your phone, and in Chats: the chat shows the bot's reply.

While the bot is on, real customers who write are answered too. Messages written before
the switch went on are never answered.

**Or test with a few people first (test mode).** Test mode answers only the chats marked
as testers, so real customers are not touched and Meta's Auto reply may stay on:

1. Each tester writes one message to the page from their own Facebook account (Messenger
   ids are page-scoped, so a chat can be chosen only after it exists).
2. In Chats, open their chat and switch on **Tester**. The Status tab lists the testers,
   with a link to each chat and a Remove button; the Chats list has a Testers filter.
3. In Status & switches, press **Start test mode** and confirm. No Auto reply tick is
   needed. The same works from an admin account's Messenger with `#bot test`.
4. Testers write again; only messages written after test mode started are answered.
5. Afterwards, **Turn on for everyone** (asks for the Auto reply tick) or **Turn the bot
   off**.

With Meta's Auto reply on, a tester's first message in a new conversation may get Meta's
greeting. That echo counts as the shop answering, so the bot leaves that one message and
answers the tester's next one.

**Starting a test over (Clear bot memory).** The bot remembers a chat: the car, the VIN,
what was asked and what it showed. To run the next test as a new customer would, open the
tester's chat in Chats and press **Clear bot memory**. The bot then forgets that chat and
reads nothing the tester wrote before; the tester's next message starts a new conversation.
The messages themselves stay on the Chats page (a line shows where the memory was cleared)
and on Facebook. A reply the bot is writing at that moment is not sent, and a message it
had not answered yet is never answered, so it no longer rings as waiting on Live chats. The
button is there only for chats marked as testers; a customer's chat cannot be cleared.

### 11. Go live

1. **Leave the bot on,** or turn it off until the day you choose.
2. **Mark your own chat as the admin account.** In Chats, open your own chat (it exists
   after step 10) and switch Admin account on. From then on, `#bot` commands from your
   Messenger work, on the phone too.
3. **Confirm the shop facts** in the dashboard's Contact and brand: phone, address and hours.
   The bot and the fallback message quote them.

## Switches and limits

Everything below is on Dashboard → Facebook → Status & switches, except the per-chat
switches (Chats tab). The values are stored in `garage.meta_settings` and every change is
audited.

| Setting | Default | What it does |
|---|---|---|
| Messenger bot | off | The main switch: **off**, **test mode** (only the chats marked as testers) or **on** (everyone). Each start stamps the time: only messages written after the bot began answering that customer are ever answered, so people already waiting are never bulk-answered. Switching from test mode to everyone therefore never answers what other customers wrote during the test, while a tester's reply already under way still goes out. Turning it off always works, even with a stale page open, and ends test mode. |
| Auto reply confirmed off | not confirmed | The tick in the turn-on dialog. The bot cannot be turned on for everyone without it. Test mode does not need it, and starting test mode clears it (tests run with Auto reply on), so turning on for everyone afterwards asks again: `#bot on` is refused until the tick is given in the dashboard. |
| Paused: AI provider | not paused | Set by the worker itself when the AI account fails every request: no credit (`PROVIDER_BILLING`), a refused key (`AUTHENTICATION_ERROR`, `ACCESS_DENIED`) or no key (`NOT_CONFIGURED`). It then sends nothing instead of a fallback to everyone. It is cleared by Resume, or by `#bot on`. |
| Reply privately to comments | off | Its own switch. Comments made after it went on, and at most 7 days old, get one private reply each. A customer whose chat is switched off in Chats gets none, once an earlier private reply has linked their comments to that chat. |
| Oldest message to answer | 1440 min (24 h) | 5-1440. Older messages are stored but not answered. |
| Daily limit, all chats | 150 | AI turns per Tbilisi day, comment replies included. 0 allows none. |
| Daily limit per customer | 30 | AI turns per chat per day; comments do not count here. |
| Fallback reply (ka / en) | empty | Sent when the engine fails or times out. Empty uses the built-in text, and the shop phone is added at the end. Up to 500 characters. |
| Public reply to a comment | empty | Optional text posted under a comment after a successful private reply. Up to 300 characters. |
| Keep bot records for | 365 days | 30-365. Reply attempts, job runs, comments and alarms; messages are kept for good. See [Data](#data-retention-and-deleting-a-customers-data). |
| Bot in this chat (Chats) | on | Turn it off to take a chat over yourself; turn it on again afterwards. It also stops private replies to that customer's comments. |
| Tester (Chats) | off | In test mode the bot answers only these chats. Status & switches lists them and can remove one. The chat must exist (the person wrote to the page once). With no tester marked, test mode answers nobody and the Status card warns. |
| Clear bot memory (Chats, testers only) | - | A button, not a switch: the bot forgets that tester's chat and reads nothing written before. The stored messages stay. Audited as `messenger.chat_context_cleared`. |
| Admin account (Chats) | off | That Facebook account may send `#bot` commands. Mark only your own. |
| Needs attention (Chats) | off | Set by the worker when it could not answer properly. Clear it once handled; employees see it on Live chats as a stuck chat and may clear it there. |

At a daily limit, the customer gets one polite notice with the phone number per day, and
the chat is flagged `daily_cap`. Today's turns and estimated cost are on the Status card,
in the Today panel.

Four timings are `.env` settings rather than dashboard ones:

| Variable | Default | Range | What it sets |
|---|---|---|---|
| `MESSENGER_QUIET_SECONDS` | 5 | 1-60 | the quiet window |
| `MESSENGER_MAX_WAIT_SECONDS` | 45 | 5-300 | the longest a burst waits |
| `MESSENGER_CONCURRENT_TURNS` | 3 | 1-8 | chats answered at once |
| `MESSENGER_SEND_TIMEOUT_SECONDS` | 20 | 5-60 | how long one Send API call may take |

### Which messages get an answer

The worker checks every rule twice: when it takes the burst, and again immediately before
every Send API call, typing indicators included. A switch turned off mid-turn therefore
stops the reply before it leaves (`skipped_off`). A customer's newest message is answered
only when all of these hold:

- the bot is on and not paused, and the chat's own switch is on;
- in test mode, the chat is marked as a tester (otherwise `skipped_not_tester`: no
  warm-up, no engine turn, nothing counted against the limits, and Live chats shows the
  customer as waiting for staff at once);
- the message was written after the bot began answering this customer (test mode or on
  for a tester, on for everyone else), and is younger than the oldest message to answer;
- the shop has not answered since: a message typed in Business Suite or the Messenger app
  means the bot leaves it, and answers the customer's next message with the shop's words in
  view;
- no newer message arrived during the turn; if one did, the next turn reads both;
- the daily limits allow a turn;
- the page token works;
- the chat is not held by another app (Meta's "standby"): such messages are stored, never
  answered.

Older unanswered messages are read along with a qualifying one, but they are never answered
on their own.

### What a customer receives

- **The answer.** It is split at sentence or line breaks into messages of at most 2,000
  characters. It follows the customer's language.
- **Parts.** Up to 5 parts appear as lines with their names, brands, SAP codes, prices and
  availability. A "more" button shows the rest.
- **Choices.** They become quick-reply buttons when there are 13 or fewer and every label
  fits in 20 characters. Otherwise they become a numbered list, and a reply of "2" picks the
  second choice.
- **Special cases:**

| Situation | What the customer gets |
|---|---|
| A photo, voice note or sticker with no text | A short request to write the car, VIN or part. |
| Two different VINs | A request for one VIN. |
| A reply to a message more than 1 hour old (the laptop slept) | The reply starts with an apology for the delay. |
| The engine fails or times out | One short message with the shop phone, and the chat is flagged. A callback is never promised. |
| The customer says they want to come in, or to have work done | The model sets `finish.staff.intent` to `visit` or `service` and, once, asks for a phone number in the same reply, worded as a request. It may say the team will get in touch, never a time, a price or that a part is kept for them. Staff get an alarm at once (Live chats), and the number, when the customer types it, is added to that alarm. Someone who declines is not asked again. |

## `#bot` commands

Only chats marked **Admin account** can use them. They work from the owner's own Facebook
account, so also from the phone. Case does not matter, and the command must be the whole
message.

| Message | Effect |
|---|---|
| `#bot off` | Turns the Messenger bot off and confirms it, or says it was already off. |
| `#bot on` | Turns it on for everyone (also from test mode), clears an AI-provider pause, and confirms it. It is refused until Auto reply has been confirmed off in the dashboard. |
| `#bot test` | Switches to test mode (only the chats marked as testers), clears an AI-provider pause, and confirms it with the number of testers. Needs no Auto reply confirmation. |
| `#bot status` | On, test mode (with the number of testers), off or paused; today's turns against the limit; comment replies; the last change; today's estimated cost. |
| any other `#bot …` | A short help text. |

Commands never reach the engine and never count as turns. Each one is written to the audit
log: `messenger.bot_off`, `messenger.bot_on`, `messenger.bot_test`, `messenger.bot_on_refused`,
`messenger.bot_status` or `messenger.bot_help`. A customer who types `#bot` gets an
ordinary answer. When the database refuses the change, the switch stays as it was and the
answer says the command did not go through; send it again, or use the dashboard. A command
whose webhook item failed midway is applied when the worker retries the item.

## Jobs

The worker runs these on their own, records each run in `garage.meta_sync_runs`, and shows
them on the Status tab under Background jobs:

| Job | Every | What it does |
|---|---|---|
| `subscription` | 1 hour, at start and after a wake-up | Repairs the page's webhook subscription, and the app's when it is inactive, points elsewhere or lacks a field. |
| `catchup` | 10 min, at start and after a wake-up | Messages the webhook missed (`automotive/meta_catchup.py`): Meta retries a delivery for about two hours, so what customers wrote while the laptop slept longer never arrived (measured 2026-11-20: 44 of 109 customer messages of one week). Reads the conversations updated since an hour before its last good run (at most 30 days back; the very first run: since the page was connected, and a later reconnect never shortens the window) and stores each message not stored yet, except those of a chat whose data was deleted. It never answers: every customer message it stores that nobody answered is claimed as handled (`messenger_replies` status `skipped_old`, error_text `catchup`, both channels), so whichever one the worker would take as a trigger (two in one second, the owner's command) gets no late reply, and the Live page shows the chat as waiting for staff. When that customer writes again, the bot's answer reads the earlier unanswered messages too. A run that hits its page limits (500 conversations, 300 messages a chat: far above this page's volume) keeps what it read and ends as `error` `Truncated`, so the window is never marked done; the next run reads newest first and stops at the same place, so if this ever shows, raise `MAX_CONVERSATION_PAGES` / `MAX_MESSAGE_PAGES` in `automotive/meta_catchup.py`. |
| `names` | 10 min | Customers' names and conversation ids. |
| `comments` | 10 min | Comments of the last 30 days, for the list only. These are never auto-answered. |
| `posts` | 1 hour | Posts and their reactions, comments, shares and views. |
| `page_stats` | 1 day | Followers and the page's daily views. |
| `ads` | 3 hours | Garage's ads and their daily results (spend, reach, clicks, chats started), read-only. |
| `token_health` | 6 hours | Asks Meta whether the stored tokens still work. |
| `purge` | 1 day | Retention (see [Data](#data-retention-and-deleting-a-customers-data)). |

Run now (Background jobs) asks the worker to run a job at its next pass. A wake-up from sleep
(the wall clock jumped past the monotonic clock) runs the start-up jobs again 20 seconds later,
once Wi-Fi is back.

## Costs

| Item | Cost |
|---|---|
| Meta | Nothing for what this integration does: replies inside the 24-hour window, private replies to comments, and reading stats and ads. |
| ngrok | The free plan: one static address and a monthly request allowance. |
| AI provider | Every engine turn is billed by the provider. Requests go to Anthropic's API first and, when that account refuses for billing, to OpenRouter (`OPENROUTER_API_KEY`). The direct account is unfunded today, so OpenRouter pays. |

AI cost details:

- **What costs.** A turn is one answer to a burst of messages, or one comment reply. The
  plan's estimate is about $0.03 a turn.
- **The ceiling.** The default limits cap a day at 150 turns, about $4.50, and 30 turns per
  customer.
- **What is free.** Media-only replies, "more" pages, `#bot` replies and limit notices do
  not use the engine. A reply stopped before the engine ran (a switch, a dead token, Meta
  refusing the typing indicator) is not a turn either; one stopped after it ran still is.
- **The displayed figure.** The dashboard and `#bot status` show today's cost as an
  estimate: the engine reports token counts, priced with the table in
  `automotive/assistant_eval.py`.
- **When the balance runs out,** the bot pauses itself (Paused: AI provider) instead of
  sending everyone the fallback. Top up, then press Resume.

## If Meta's gate blocks the new app

If the probe says `gated`, the plan's fallback is to keep receiving on GARAGE 1886 and send
through **TNLU Assistant**, the receive/send split TNLU already runs:

1. The owner makes a Garage page token with a TNLU Assistant Explorer token.
2. For that token to last, it needs one of two things. Which one is the owner's choice:
   - TNLU Assistant's app secret goes into Garage's `.env`; or
   - the token is extended in Meta's Access Token Debugger.
3. That page token is stored for sending only, as purpose `send_page`.
4. Probe again before relying on it.

Comment private replies are never gated and work in every case.

The worker and the probe already send with a stored `send_page` token, and add
`appsecret_proof` only when the token belongs to GARAGE 1886. Nothing can store such a
token yet: Connect refuses another app's token (`WRONG_APP`). That step is built only if the
probe says `gated`.

## Troubleshooting

Logs carry event names and codes only, never a message, a token, a VIN or a phone number:

```bash
journalctl --user -u garage-meta-receiver -u garage-messenger -u garage-ngrok -f
uv run python -m automotive.messenger_setup --status      # connection, tokens, switches, today
uv run python -m automotive.messenger_setup --events 50   # the newest webhook events, no text
```

### Is the receiver reachable?

`uv run python -m automotive.messenger_setup --check-public` sends a made-up verify token
through the public address. By hand:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'ngrok-skip-browser-warning: 1' \
  'https://glorified-stratus-budding.ngrok-free.dev/meta/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=check'
```

Never test with the real verify token: it would stay in the shell history.

| Answer | Meaning |
|---|---|
| `403` | Correct: the tunnel and the receiver work, and the wrong token is refused. |
| `502`, or an ngrok error page | The tunnel works but the receiver does not: `systemctl --user status garage-meta-receiver`. |
| `404` from ngrok ("endpoint offline") | The tunnel is down: `systemctl --user status garage-ngrok`. |
| `429` | Too many failed checks from one address (30 a minute); wait a minute. |
| `200` with `check` in the body | Must never happen with a wrong token. Stop the tunnel and report it. |

### No messages arrive

1. **Last webhook.** Look at Status & switches, or `--status`. The Status card warns when
   nothing arrived for 24 hours while the page is connected. The Today panel counts
   webhooks per day.
2. **Not subscribed.** A "not subscribed" warning means no webhook subscription is on
   record for the page: connect could not make one. Meta can also drop a subscription
   after long delivery failures, and no warning shows that. The worker checks and repairs
   both cases at start and every hour (job `subscription`), and Repair webhooks on the
   Connect tab does it now. Repairing the app subscription needs the receiver and the
   tunnel up, because Meta calls the webhook address.
3. **The laptop slept or was offline.** Nothing is answered meanwhile. Meta retries failed
   deliveries for a while and may then drop the subscription; see the previous point. After
   waking:
   - messages older than the oldest message to answer (24 h by default) are stored but not
     answered;
   - replies to messages older than 1 hour start with an apology.
4. **The ngrok quota.** The free plan has a monthly request allowance. When it runs out,
   ngrok refuses deliveries until the next period, and Meta retries and may then drop the
   subscription. Usage is on dashboard.ngrok.com. The choices are to wait, upgrade, or move
   the receiver.

### A customer got no answer

Open the chat in Chats. Each reply attempt has a status, which tells the reason. The bot
did not answer because of a rule:

| Status | Reason |
|---|---|
| `skipped_off` | The bot was off, or the message was older than the switch-on time. |
| `skipped_chat_off` | This chat's switch was off. |
| `skipped_not_tester` | The bot was in test mode and this chat is not a tester (also on a comment whose author has no tester chat linked yet). |
| `skipped_old` | Older than the maximum age, or outside Meta's 24-hour window. |
| `skipped_shop_answered` | Staff answered first. |
| `skipped_superseded` | A newer message took over. |
| `skipped_cap` | A daily limit was reached. |
| `skipped_paused` | The bot is paused for the AI provider. |

The bot tried and the reply did not get through:

| Status | Reason |
|---|---|
| `fallback_sent` | The engine failed or timed out; the phone message went. |
| `paused_provider` | The AI account failed; the bot paused itself. |
| `blocked_by_meta` | Meta refused to send; see the gate. |
| `failed` | Sending failed: the token, the network or Meta. The Meta code is recorded. |
| `abandoned` | The worker stopped mid-turn. |

A flagged chat names its reason:

| Flag | Meaning |
|---|---|
| `daily_cap` | A daily limit was reached. |
| `ai_provider` | The AI account failed. |
| `engine_failed`, `engine_timeout` | The engine failed or ran out of time. |
| `send_token_dead`, `send_token_broken` | The page token no longer works. |
| `send_failed` | Sending failed. |
| `blocked_by_meta` | Meta refused to send. |
| `oversized_message` | The message was too large to store; it was kept as ids only. |
| `worker_stopped` | The worker stopped mid-turn. |
| `comment_reply_failed` | A comment got only the fallback. |

Also check whether the worker is up: `systemctl --user status garage-messenger`. At start
it waits up to 5 minutes for the catalogue to load before it answers.

### A token is broken or expiring

The Status card warns about these:

- a broken token;
- a token expiring within 14 days;
- data access ending within 14 days.

Each token's expiry date and data-access end date are on the Connect tab. The login token,
which the ads sync uses, lasts about 60 days. Once data access ends, stats and ads stop
until the page is connected again.

How to deal with them:

- Check tokens (Connect tab) asks Meta now. The `token_health` job does the same every 6
  hours.
- The fix is always the same: paste a new Graph API Explorer token in Connect.
- The page token does not expire, but it dies when the owner changes the Facebook password
  or logs out everywhere.
- A send that hits a dead token marks it broken and stops sending until the page is
  connected again.

### A service keeps restarting or will not start

| Symptom | Cause | Fix |
|---|---|---|
| Exit status 2 | A missing or malformed setting; the journal names it. | Fix `.env`, then `systemctl --user restart <unit>`. |
| Exit status 1 (worker) | The database is not reachable yet, or the engine could not be built. | It retries every 10 seconds on its own. |
| The tunnel does not come up after install, or loops after an ngrok update, and the journal shows "operation not permitted" | ngrok makes a system call the sandbox blocks. The sandbox was checked with ngrok starting, not with a live tunnel. | Delete the `SystemCallFilter=` line from `~/.config/systemd/user/garage-ngrok.service`, then `systemctl --user daemon-reload` and `systemctl --user restart garage-ngrok`. |
| The worker's journal shows "Read-only file system" | The engine writes outside `data/messenger`. The sandbox was checked with the modules importing, not with a full engine start. | Add that folder to `ReadWritePaths=` in `~/.config/systemd/user/garage-messenger.service`, run `systemctl --user daemon-reload` and `systemctl --user restart garage-messenger`, and tell the developer. |

## Data, retention and deleting a customer's data

Everything is stored in the local PostgreSQL, in schema `garage`; nothing goes to another
server of ours.

| Data | Where | Kept |
|---|---|---|
| Webhook items, as Meta sent them (they include message text) | `garage.meta_inbox` | 6 days, so at most 7 with the daily purge |
| Messages from customers, bot and shop | `messenger_messages` | for good, until "Delete data" (owner's decision, 2026-11-20) |
| Reply attempts | `messenger_replies` | Keep bot records for (365 days by default) |
| Chats: name, switches, engine state | `messenger_threads` | for good while a message is left, so in practice until "Delete data" |
| Job runs and dashboard job requests | `meta_sync_runs`, `meta_job_requests` | Keep bot records for |
| Comments | `meta_comments` | Keep bot records for (by the comment's own time) |
| Hashed ids of customers whose data was deleted (SHA-256 of the author or comment id, never the id) | `meta_forgotten` (migration 0014) | 31 days, past the comment backfill's 30 |
| Page stats, posts, pictures, ad results | `meta_page_daily`, `meta_posts`, `meta_images`, `meta_ad*` | not purged; they hold the page's own content |
| Encrypted tokens | `meta_tokens` | until Disconnect |
| Audit log (customers appear as `…1234`) | `garage.audit_log` | not pruned by the site |

The daily `purge` job applies the database periods. The engine keeps no chat log for
Messenger. Before that changed it wrote `data/messenger/chat_log.sqlite3`. The worker
deletes that file and its `-wal` and `-shm` files when it starts, and the journal shows
`messenger_old_chat_log_removed`. It never deletes a log the app on port 8767 writes.

The public pages promise that conversations are kept until the customer asks for their
deletion, 7 days for the webhook copies and an answer to a deletion request within
`SHOP['answer_days']` days. If `SHOP` in `automotive/meta_receiver.py` changes, change its
dates too.

**A deletion request** ("delete my data", by Messenger or by phone):

1. Tell the customer it is being done. A message sent afterwards would start a new stored
   chat.
2. In Chats, open the chat → Delete data, and confirm. This deletes the chat, its messages,
   its engine state, its reply attempts and the customer's stored comments: those the bot
   answered into this chat, and every other comment of the same Facebook author. Their
   hashed ids go to `meta_forgotten`, so the comment backfill and late webhooks never store
   those comments again. The chat's hashed id goes there too (`chat:<psid>`), so the
   `catchup` job never reads its earlier messages back from the Page's conversations. A
   comment or message the customer writes later is stored as usual. Nothing is touched on
   Facebook: the conversation stays in Messenger, and comments stay on Facebook.
3. Its webhook copies go with the 7-day purge. The dashboard's login may not delete them, but
   a copy the worker had not handled yet (it was stopped) or one Meta sends again changes
   nothing: a message or ad referral sent before the deletion ends as `ignored: deleted_chat`,
   and neither the chat nor its messages come back (`MetaStore.deleted_after`, checked in
   `MetaStore.add_message` too).

**Not covered yet:**

- Comments of a customer that are not linked to their chat. The link is made when the bot
  answers a comment privately. Their comments can only be found by name in the Comments
  tab, and the dashboard cannot delete a single stored comment.
- The engine state of a customer who writes again. It keeps up to 12 earlier exchanges.
  Delete data removes it, with the messages, which are otherwise kept for good.
- The privacy page names Meta, OpenRouter and Anthropic. A VIN a customer sends is also
  decoded with NHTSA's vPIC (USA), and with `JSFILTER_ENABLED`, JS Asakashi's filter
  catalogue (Japan) receives the VIN too.

## Rollback

1. **Stop answering now.** Turn the bot and comment replies off: on the dashboard, or with
   `#bot off`.
2. **Stop Meta's deliveries** while the database still has the page token. Do not use the
   dashboard's Disconnect for this: it deletes the stored tokens but leaves the page
   subscribed, and unsubscribing afterwards needs a new token or Meta's own settings. Run
   this instead, as the site login, as the dashboard does. It unsubscribes the page, then
   deletes the tokens:

   ```bash
   uv run python -c "from automotive.messenger_setup import _context; from automotive.meta_connect import disconnect; print(disconnect(_context(), 'cli', unsubscribe=True))"
   ```

   Removing or deleting the GARAGE 1886 app in Meta for Developers ends its webhooks and
   tokens for good.
3. **Stop the services:**

   ```bash
   systemctl --user disable --now garage-messenger.service garage-ngrok.service \
       garage-meta-receiver.service
   ```

   Turn Meta's Auto reply on again if it is wanted.
4. **Database: prefer leaving it.** With the services stopped and the tokens deleted, the
   tables do nothing. Migration 0013's down script is destructive: it drops every
   `garage.meta_*` and `garage.messenger_*` table, which means the tokens, chats, comments,
   stats, ad results and switches. If the data must go:
   1. Take a backup first. It holds customer messages and staff password hashes, so keep it
      private and outside the repository.
   2. As the database owner, run 0014's down script, then 0013's, and remove both rows in
      one transaction, so that a later `--setup` applies them again.

   ```bash
   cd /home/bezhomatiashvili/Desktop/tuz
   docker compose exec -T postgres sh -c \
     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=garage --format=custom' \
     > ~/garage-before-0013-down.dump
   { cat migrations/0014_meta_forget.down.sql migrations/0013_meta_messenger.down.sql; \
     echo "DELETE FROM public.warehouse_schema_migrations WHERE version IN ('0013_meta_messenger', '0014_meta_forget');"; } \
     | docker compose exec -T postgres sh -c \
       'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction'
   ```

   The logins `garage_local_webhook_*` and `garage_local_messenger_*` and their `.env` keys
   stay until they are dropped by hand.
5. **The ngrok address.** `glorified-stratus-budding.ngrok-free.dev` is TNLU's reserved
   address. While Garage uses it, TNLU's old laptop tunnel (`tnlu-ngrok.service`, disabled)
   cannot.

## Security model

**Only the receiver is public.** It is a separate process, bound to `127.0.0.1:8771`, and
reached only through the tunnel. It answers a fixed list; any other method or path gets
404 before the router sees it:

| Route | Behaviour |
|---|---|
| `GET /meta/webhook` | Meta's handshake. The verify token is compared in constant time. Anything wrong gets 403. |
| `POST /meta/webhook` | `X-Hub-Signature-256` is checked against the raw body with the app secret, in constant time. A missing or malformed signature gets 401 before the body is read. A well-formed one can only be checked once the whole body is in, so a wrong one gets 401 after it. The body may be at most 1 MB, and must arrive within 3 seconds. One address may send at most 8 bodies at once (429 beyond that), and an upload dropped midway counts as a failure. Every refusal closes the connection, so a half-sent body cannot hold one of the server's 64 slots. |
| `GET`/`HEAD` `/privacy`, `/privacy/en`, `/data-deletion`, `/data-deletion/en` | The static pages Meta needs. |
| `GET`/`HEAD` `/healthz` | `ok`. |

Its other protections:

- per-address token buckets (signed deliveries, failures, pages);
- no cookies, no CORS, and no access log (the handshake's query string carries the verify
  token);
- strict security headers;
- logs with event names and counts only.

**Database roles** (migrations 0013 and 0014, row-level security by role membership). Each process
has its own login, and each login belongs to one role:

| Role | Used by | May |
|---|---|---|
| `garage_webhook` | the receiver | Insert four columns of `garage.meta_inbox`, nothing else. It cannot read anything back, not even its own rows, and it has no privilege on the inbox's id sequence (0014). |
| `garage_messenger` | the worker | Read and write the Facebook tables. Read `garage.site_sections` and `garage.services` (the shop facts). Append to `garage.audit_log`. Raise and update alarms in `garage.live_alerts`, never change an alarm's chat or kind (0015). Read the key and text of the owner's prompt edits in `garage.llm_prompts` (0019). No other staff table. |
| `garage_site` | the dashboard | Change the switches, the connection and job requests. Read chats, comments, stats and ads. See only the inbox's bookkeeping columns, never a payload. Change only a chat's switch columns. Delete a customer's chat and comments, and record their hashed ids in `meta_forgotten` (0014). Read the Live chats tables, mark alarms done, switch a website visitor's bot, record the website chat and its alarms, and remove a deleted customer's alarms (0015). Mark a chat as a tester for test mode (0018). Edit the live bot's prompts and keep their history (`garage.llm_prompts`, `garage.llm_prompt_history`, 0019). Empty the bot's memory of a tester's chat through `garage.messenger_clear_context` (0023); it still cannot write a chat's `engine_state` itself. |

`automotive_runtime`, the catalogue login the engine also uses, has no access to `garage`.

**What each process's code uses:**

| Process | Uses |
|---|---|
| Receiver | The app secret, the verify token and its append-only login. The page tokens sit encrypted in the database, which that login cannot read. |
| Worker | The app ID and secret, the token encryption key (it decrypts the page token in memory), the worker and runtime logins, the VIN key and the AI provider keys. |
| Dashboard (port 8767) | The app ID and secret, the encryption key, the verify token and the site login. The pasted token goes straight to `meta_connect` and is dropped. |
| ngrok | Its own authtoken, in `~/.config/ngrok/ngrok.yml`. |

**The limit of that separation.** The three Python processes run as the laptop user, and
each loads the whole private `.env` into its environment (`load_dotenv` in
`automotive/meta_config.py`). The file also stays readable to them inside the sandbox. A
takeover of the receiver process could therefore read every secret in `.env`, including:

- the AI keys;
- the token encryption key;
- the other database logins, among them the local admin `DATABASE_URL`.

Its own database login would still read nothing. On this laptop the separation is by
database role and by what the code uses, not by process. Two steps would close it:

- the receiver reads only its own keys from a file of its own;
- its unit hides the shared `.env` from it.

**Tokens** are sealed with AES-256-GCM using `META_TOKEN_ENCRYPTION_KEY` before they are
stored; the key never reaches the database. Tokens never go to the browser or a log. Graph
calls made with a GARAGE 1886 token carry `appsecret_proof`; a token minted through another
app (the TNLU fallback) is sent without it.

**Sandbox.** The units run as the laptop user, with the file system read-only except the
worker's `data/messenger`. Each unit file explains its sandbox lines and why two are
absent:

- `MemoryDenyWriteExecute` breaks thread creation with the uv-managed Python;
- IP firewalling is not available to user units.

**The ngrok inspector.** ngrok's local inspector (`http://127.0.0.1:4040`, on by default)
keeps recent requests in memory, customer messages and the handshake's verify token
included, and anything running on the laptop can read them. The tunnel's unit therefore
starts ngrok with `--inspect=false`. A tunnel started by hand needs the same flag.

**Penetration test waiver.** The release gate in
[production-runbook.md](production-runbook.md) (items 9-10) asks for an external
penetration test before any part of this system is public. On 2026-11-11 the owner accepted
a waiver for **this laptop receiver only**, by approving the plan. The waiver rests on what
the receiver is: a signed-webhook intake that only writes to a queue, plus two static pages,
with no login, no admin and no data read back. Its condition is this build's own
adversarial security review and tests, done before go-live. A production deployment of the
receiver is not covered: its pen-test scope then includes `/meta/webhook`, `/privacy` and
`/data-deletion`.

## Settings (`.env`)

Read by `automotive/meta_config.py`, apart from `automotive.config.Settings`. A missing Meta
block never stops the website.

| Variable | Needed by | Meaning |
|---|---|---|
| `META_APP_ID` | worker, dashboard | GARAGE 1886's App ID |
| `META_APP_SECRET` | receiver, worker, dashboard | The App Secret; the owner puts it in `.env` |
| `META_WEBHOOK_VERIFY_TOKEN` | receiver, dashboard | 32-256 of `A-Z a-z 0-9 - _`; `--setup` generates it |
| `META_TOKEN_ENCRYPTION_KEY` | worker, dashboard | 64 hex characters; `--setup` generates it. Losing or changing it means connecting again. |
| `META_GRAPH_API_VERSION` | all | Default `v26.0` |
| `META_PAGE_ID` | optional | Pins the page when the pasted token reaches several |
| `MESSENGER_PUBLIC_URL` | dashboard (Connect) | The https address Meta calls; the webhook is `<address>/meta/webhook`. `--setup` sets the ngrok address. |
| `MESSENGER_RECEIVER_PORT` | receiver | Default 8771. The receiver always binds `127.0.0.1`; the tunnel unit names the same port. |
| `MESSENGER_RECEIVER_DATABASE_URL` | receiver | The `garage_webhook` login; `--setup` creates it |
| `MESSENGER_DATABASE_URL` | worker | The `garage_messenger` login; `--setup` creates it |
| `MESSENGER_DATA_DIR` | worker | Default `data/messenger`. If it changes, change the worker unit's two paths too. |
| `MESSENGER_QUIET_SECONDS`, `MESSENGER_MAX_WAIT_SECONDS`, `MESSENGER_CONCURRENT_TURNS`, `MESSENGER_SEND_TIMEOUT_SECONDS` | worker | See [Switches and limits](#switches-and-limits) |

The engine also reads the app's own settings: `AUTOMOTIVE_DATABASE_URL`,
`AUTOMOTIVE_VIN_HMAC_KEY`, the provider keys and the `AUTOMOTIVE_ASSISTANT_*` and
`AUTOMOTIVE_RESEARCH` values.

## Commands and routes

```bash
uv run python -m automotive.messenger_setup --setup           # migrations 0013-0014, logins, generated keys
uv run python -m automotive.messenger_setup --status          # connection, tokens, switches, today
uv run python -m automotive.messenger_setup --connect [--page-id ID]   # token read hidden
uv run python -m automotive.messenger_setup --probe [--psid ID]        # sends nothing
uv run python -m automotive.messenger_setup --discover-ads    # ads promoting the page, per account
uv run python -m automotive.messenger_setup --events [N]      # newest webhook events, no text
uv run python -m automotive.messenger_setup --check-public    # 403 for a wrong verify token, /healthz
```

Every command except `--setup` and `--check-public` acts with the site login, as the
dashboard does.

Dashboard API, admin only, behind the site's session, Origin and CSRF checks, all under
`/api/admin/facebook`:

| Route | Does |
|---|---|
| `GET /status` | Connection, tokens, switches, warnings, today, latest events, jobs |
| `PUT /settings` | Switches, limits and texts |
| `POST /resume` | Clears the AI-provider pause |
| `POST /connect` | Connects with a pasted token |
| `POST /check` | Checks the tokens |
| `POST /subscribe` | Repairs the webhooks |
| `POST /disconnect` | Disconnects |
| `POST /probe` | Runs the probe |
| `POST /jobs/{job}` | Runs a job now |
| `GET /threads` | Lists the chats |
| `GET /threads/{psid}` | One chat and its messages |
| `PUT /threads/{psid}` | The chat's switch, admin account and attention flag |
| `DELETE /threads/{psid}` | Deletes the customer's data |
| `GET /events` | The newest webhook items, bookkeeping only |
| `GET /comments` | Customers' comments, with the private-reply status |
| `POST /comments/{comment_id}/hide` | Hides or unhides a comment, on Facebook first |
| `POST /comments/{comment_id}/reply` | Posts the admin's public reply, once |
| `GET /page-stats` | Followers and page metrics per day |
| `GET /posts` | Posts, most engaging first |
| `GET /images/{key}` | A cached post picture or ad thumbnail |
| `GET /ads` | Garage's ads and the best ones, read-only |
| `GET /ads/daily` | Spend, impressions, clicks and chats started per day |

The switches, connection and chat routes are in `automotive/site_meta_admin.py`; the
comment, statistics, post and ad routes are in `automotive/site_meta_activity.py`. The Live
chats routes (`/api/admin/live/*`, both roles) are in `automotive/site_live.py`.
Migration 0015 is applied by `uv run python -m warehouse_sync --migrate`, not by
`messenger_setup --setup`; until it is, the worker records no alarms and is otherwise unaffected.

Migration 0018 (test mode) must be applied **before** the services are restarted on code
that has test mode: that code reads its columns, and the Live pages say "not set up" until
it is in. Applying it first is safe for services still on older code. Test mode is stored
apart from `bot_enabled`, so older code, or a rollback with 0018's down script, reads test
mode as off and answers nobody.

Migration 0023 (Clear bot memory) must likewise be applied **before** the services are
restarted on code that has it: that code reads `messenger_threads.context_cleared_at` with
every chat. Applying it first is safe for services still on older code. Its function,
`garage.messenger_clear_context`, runs with its owner's rights (SECURITY DEFINER) so the
dashboard login can empty a tester's bot memory without being able to write one; only the
dashboard's and the worker's logins may call it.

## Verification

```bash
uv run python -m unittest tests.test_meta_foundation tests.test_meta_graph \
    tests.test_meta_connect tests.test_meta_store_database tests.test_meta_receiver \
    tests.test_messenger_events tests.test_messenger_engine tests.test_messenger_render \
    tests.test_messenger_worker tests.test_messenger_setup tests.test_meta_comments \
    tests.test_meta_page_stats tests.test_meta_ads tests.test_site_meta_admin \
    tests.test_site_meta_activity tests.test_messenger_e2e \
    tests.test_messenger_test_mode_database -v
uv run ruff check automotive tests
uv run bandit -q -r automotive
```

The database tests need `WAREHOUSE_TEST_ADMIN_DATABASE_URL` pointing to a disposable
PostgreSQL. They skip without it, and they never use the developer database. They create
their own database and NOLOGIN roles, and act under `SET ROLE`, so every grant above is
tested as the role that has it.

`tests.test_messenger_e2e` runs the pieces together on loopback. Signed deliveries go over
HTTP to the real receiver, served by uvicorn on a spare port. The real worker runs on its own
threads with the production Graph client, whose session sends the fixed Graph prefix to a
fake Graph server. The engine is a fake. The receiver, the worker and the dashboard's actions
each connect with their own LOGIN role in one capability role only. While it runs, a socket
audit hook refuses every connection and name lookup off loopback.

Every test is $0. Meta's Graph API, OpenRouter and Anthropic are replaced by fakes: a mocked
HTTP session, a fake Graph server on loopback, and a scripted model. No customer is ever
messaged.

The live checks need the owner: steps 4-10 above.
