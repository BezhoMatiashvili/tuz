# Garage 1886 site

The `automotive` application now also serves the garage's public website, a staff login
and a dashboard. One process serves everything; what a request may reach is decided by
`automotive/site_guard.py` before the legacy middleware runs.

| Path | Who | What |
|---|---|---|
| `/`, `/en` | public | landing page (Georgian, English), rendered from editable content |
| `/parts`, `/parts/{make}`, `/parts/{make}/{model}` (and under `/en`) | public | parts in stock by make and model, rendered on the server for search engines ([garage-seo.md](garage-seo.md)) |
| `/api/site/parts/*`, `/api/site/vin` | public | part search and VIN decode |
| `/api/site/chat/*` | public | the parts assistant through a capped facade |
| `/login` | public, never linked | staff sign-in |
| `/admin`, `/api/admin/*` | employee, admin | dashboard |
| `/admin/live`, `/api/admin/live/*` | employee, admin | Live chats: customer messages of Messenger and the website chat, alarms, one customer's bot switch (below) |
| `/admin/facebook`, `/api/admin/facebook/*` | admin | Facebook: the Messenger bot's switches, the page connection, chats, comments, page stats and ads ([garage-messenger.md](garage-messenger.md)) |
| `/staff`, `/api/v1/*` | employee, admin | the staff workspace and its API (was `/`) |
| `/api/v1/operator/*` | direct loopback callers | unchanged bearer-token routes |

Any other `/api/...` address, and any address with `//`, `/./` or `/../` in it, is
answered with 404 by the guard itself.

Meta's webhooks reach a second, separate process, never this one. It is
`automotive/meta_receiver.py` on `127.0.0.1:8771`, public only through the ngrok tunnel
([garage-messenger.md](garage-messenger.md)). Any other method or path gets 404 there too.

| Path | Who | What |
|---|---|---|
| `GET /meta/webhook` | Meta | webhook handshake; a wrong verify token gets 403 |
| `POST /meta/webhook` | Meta, signed | webhook deliveries, appended to `garage.meta_inbox`; a bad or missing signature gets 401 |
| `/privacy`, `/privacy/en`, `/data-deletion`, `/data-deletion/en` | public | the static pages Meta needs to publish the app (`GET`, `HEAD`) |
| `/healthz` | public | `ok` |

## Local setup

```bash
uv sync --extra dev --locked
uv run python -m automotive.site_setup --setup-site          # migration 0012, site login, default content
uv run python -m automotive.site_setup --create-user NAME --role admin
uv run python -m automotive                                   # http://127.0.0.1:8767
```

`--setup-site` accepts only a loopback `DATABASE_URL`. It applies migration
`0012_garage_site` alone (never another pending migration), creates one login that is a
member of `garage_site` only, writes `AUTOMOTIVE_SITE_DATABASE_URL` to the private `.env`
without printing it, and stores the packaged texts, services and gallery photos into
tables that are still empty. Rerunning it keeps the existing login and content.

A photo packaged after a site was set up does not reach its gallery that way, because the
gallery is no longer empty. `uv run python -m automotive.site_setup --add-photos` stores
the packaged photos the gallery does not hold yet, after the ones already there, each
shown or hidden as its packaged entry says. Stored photos are never changed. A packaged
photo that was deleted in the dashboard is stored again, so run it when photos were added
to the package, not as a routine. The site shows the result within a minute, no restart.

Accounts: `--create-user NAME --role admin|employee` asks for a password (or `--generate`
prints one once), `--reset-password NAME` sets a new one and ends that account's sessions,
`--list-users` lists them. Accounts are disabled from the dashboard, never deleted.

Without `AUTOMOTIVE_SITE_DATABASE_URL` the public page still renders from packaged
defaults; sign-in answers `SITE_AUTH_NOT_CONFIGURED`.

## Roles

| Area | employee | admin |
|---|---|---|
| Overview: item counts, stock by location, brands, categories, makes, prices, sync health | yes | yes |
| Stock value, inferred sales and purchases, best sellers, chat volume | no | yes |
| Inventory lookup (code, price, store and warehouse quantities) | yes | yes |
| Live chats: customer messages, alarms, one customer's bot switch | yes | yes |
| Facebook section: the connection, the switches for everyone, the admin-account flag, deleting a customer's data, costs | no | yes |
| Staff workspace `/staff` and `/api/v1/*` | yes | yes |
| Texts, services, gallery | no | yes |
| Accounts, audit log | no | yes |
| Own password | yes | yes |

The split is enforced in `automotive/site_admin.py`; the dashboard only hides what the
API would refuse anyway.

An employee does see each item's price and quantities, because counter work needs them,
so they could add up a stock value by hand. What the role withholds is the ready-made
money reports, not the price list.

## Live chats and alarms

Dashboard > **Live chats** (`/admin/live`) is for whoever is at the counter, employee or admin.
Code: `site_live.py` (routes), `live_store.py` (SQL), `live_alerts.py` (what a turn means),
`live_web.py` (the website chat's recorder), migrations `0015_live_chats` and
`0016_live_waiting_dismiss`.

- **Messages in real time.** What Messenger customers and website visitors wrote, with what the
  bot answered. The page asks `GET /api/admin/live/pulse` every 3 seconds (10 while its tab is
  hidden), so a message shows within about 3 seconds. Messenger text is read from the Messenger
  tables (`garage.messenger_*`); the website chat is written by `LiveRecorder` into
  `garage.live_web_chats` / `live_web_messages` and kept for good (owner's decision,
  2026-11-20; a lost database connection is retried a few times on the recorder's own thread).
  A website visitor is named by `sha256(conversation id)[:32]`, the key the chat log already
  uses; the conversation id itself is a bearer secret and is never stored.
- **Alarms** (`garage.live_alerts`: one open row per chat and kind; a repeat merges into it, up to
  ten earlier notes are kept). Anyone may mark one done (who and when are stored) or reopen it.

  | Alarm | Raised when |
  |---|---|
  | Part not found | the turn's outcome is `nothing_found`, or `sold_out` with no card that is not out of stock, or the model lists parts it could not offer in an answer that offered others (`finish.staff.missing`: an oil change with the oil found and the filter not) |
  | Wants to visit / wants to book service | the model sets `finish.staff.intent` to `visit` or `service`; it asks once for a phone number, and the number the customer types is added to the same alarm and sounds it again |
  | Bot needs a human | the model chose outcome `handoff`; a website turn ended failed or timed out; a phone number arrived with nothing open |
  | Stuck (Messenger) | the chat's existing `needs_attention` flag, with its reason; an employee can clear it |
  | Waiting (Messenger) | the newest customer message is later than the last bot or shop reply, younger than 24 hours (Meta's window for the page's reply; the bot's own `max_reply_age_minutes` only limits what the bot answers), it is not the owner's own chat, and nothing will answer it by itself: the bot is off for that chat, off or paused for everyone, or it has said nothing for 3 minutes (a failed or cut-off reply, a message too old for the bot, one the catch-up job found after the laptop slept) |

  The model decides what a customer means (`finish.outcome`); this code only reads what the
  engine already validated and checks it against the cards that were actually offered. While the
  global Messenger switch is off (owner's decision, 2026-11-20) every unanswered chat counts as
  waiting, sounds once and is reminded every minute until someone replies in the Page inbox; a
  new message in a chat that already waits sounds again. A closing "thanks" needs no answer:
  the chat's "No reply needed" button (`POST /api/admin/live/chats/{key}/waiting/dismiss`,
  audited `live.waiting_dismissed`, migration 0016) stops it waiting until the customer writes
  again. The mark is the newest stored message the screen showed (`seen_id`, the chat's
  `last_id`): storage order, not send time, so any customer message stored after it keeps the
  chat waiting, whether it came in before the screen caught up (the page says so) or was sent
  earlier and stored late by the catch-up job or Meta's retries. A merge never turns a
  handoff or a
  phone alarm into a "failed" one, so the system's closing of failed website alarms (when the
  visitor's bot answers again, or after 24 quiet hours) never closes what a person asked for.
- **Bot off for one customer**, employee or admin (`PUT /api/admin/live/chats/{key}/bot`). A
  Messenger chat uses its existing per-chat switch, which the worker checks before every send
  (audited as `messenger.chat_off`). A website visitor gets a new flag, which the recorder's
  writer thread reads every 3 seconds into memory (a switch takes effect within about 3 seconds,
  and a visitor's message never waits for the database): while it is off the visitor reads a
  fixed "the automatic assistant is switched off, please contact the shop" message with the
  shop's phone, no model is called, the day's model budget is given back (the address keeps the
  message on its own daily count) and the message is audited as `live.web_chat_off`. Staff
  answer Messenger customers in the Facebook Page inbox as before; there is no way to answer a
  website visitor from the dashboard. Marking an alarm done or reopening it is audited too
  (`live.alert_done`, `live.alert_open`); the owner's own chat is never listed, switched or
  cleared here.
- **On every dashboard page.** The nav badge, the tab title `(N)` and a chime (WebAudio, no sound
  files) work wherever staff are; a browser lets a page play sound only after one click, so a
  notice says so until then, and each person can mute the sound in their own browser. Anything
  that needs a person (an open alarm, a waiting or a stuck chat) repeats quietly every minute.
  If the poll cannot reach the server, or does not answer within 10 seconds, every page says the
  alarms are offline, and an ended session is shown on every page too (a form with unsaved
  changes can keep the person on the page): a silent failure would silence the alarms.
- **Setup.** Migrations 0015 and 0016 come with `uv run python -m warehouse_sync --migrate`
  (`messenger_setup --setup` applies only the Messenger migrations, on purpose). Restart the
  Messenger worker and the site afterwards. Until 0015 is applied every route answers
  `LIVE_NOT_SET_UP`, the page says so, and the worker and the website chat record nothing and
  work as before; "Delete data" and the daily purge check that the table exists. With 0015 but
  not 0016, alarms and website chats are recorded and only the pages say "not set up"
  (`LiveStore.ready` / `pages_ready`).
- **What it shows and keeps.** An alarm holds at most the customer's own words (200 characters),
  a short note the model wrote for the staff and a phone number the customer typed. A Messenger
  customer's alarms go with "Delete data" (which fails, rather than skip them, when the login may
  not delete from `garage.live_alerts`) and after "Keep bot records for"; the website's after
  30 days. The website chats and messages themselves (`garage.live_web_*`) are kept for good. Both are counted from when the alarm was raised: closing or merging into one never
  extends it. The routes return no cost, token, conversation id or admin flag; the rest of the
  Facebook section stays admin only, and the feed never reads further back than 400 messages.
  A new website alarm needs room: an address may cause 5 new "part not found" / failed-turn alarms
  an hour (20 visit, service, handoff or phone alarms), and the board holds 100 of the first
  group and 300 of the second open. Only creating an alarm counts: a repeat of an open alarm and
  a phone number added to one are never refused, and a visitor whose bot answers again has their
  open "failed" alarm closed by the system.
- **Limits.** Comment replies on the Facebook page raise no alarm. The website recorder writes on
  a background thread and drops a record rather than slow a visitor down; when the database
  cannot be read the last set of muted visitors stays in place. A muted visitor who starts a new
  conversation is answered again (the switch belongs to the conversation, which a visitor's
  browser forgets after 10 minutes idle).

## What a visitor can and cannot see

- Part search returns exactly `name`, `fits`, `brand`, `price` for items available now:
  what is in the store first, then the warehouse, and within each, items with a price
  before "price on request". A zero or unreadable price is `null`. The code, quantity,
  product id and warehouse fields are never sent. A name is shown as the shop wrote it,
  and several thousand names in the catalogue contain a part number themselves.
- Price is source column `column_06`. The source records no currency; the site shows the
  symbol stored in the dashboard under Contact and brand (default `₾`), after the amount
  in Georgian (`25 ₾`) and before it in English (`₾25`), as the dashboard does.
- Search and the dropdowns read an in-memory snapshot of `warehouse.warehouse_products`
  loaded with the read-only runtime credential and rebuilt when the sync changes it, so
  visitor traffic does not query PostgreSQL. Makes and models come from the catalogue's
  own vehicle text (`MMC` is Mitsubishi, `VAG`, `MB`, chassis codes such as `E70`).
- Stock older than `stale_hours` (`automotive/rules.toml`) adds a "call to confirm" note;
  older than `hide_after_hours` it is labelled as the last known state.
- The public chat is the same assistant with its own conversation store, its own search
  coordinator (no paid catalogue, no Motorium), 500-character messages, per-address and
  whole-site daily caps, and a separate chat log
  (`AUTOMOTIVE_SITE_CHAT_LOG_PATH`, 30-day retention). Cards are rebuilt as name, fits,
  brand, price and an availability label, and say why the part is offered: the `group`
  it is listed under, a `reason` in one line and a `confidence` level (`confirmed`,
  `listed`, `check` or `spec`, shown as "Matches by part number", "Listed for this model
  and year", "Needs checking" or "Chosen by specification"; said once under the group's
  heading when it is the same for every part of the group, and left out for a level the
  page has no label for). The words of the catalogue row the choice rests on, stock
  counts, codes and sources stay in `/staff`.
  The assistant's sentences name a listed product by the shop's own stock number
  (`product_code`), which means nothing to a visitor, so those numbers are taken out of
  the text and of the choices offered, unless the visitor typed the number first. A
  manufacturer's reference number may still appear in a sentence, as it does in item
  names: the fields that hold those also hold product descriptions and oil approvals
  (`MOBIL-1 0W40 (LL01; 229.5)`), and removing such words would damage ordinary answers.
- Besides `answer`, `note` and the cards, a public chat turn carries what the widget
  draws around them, built key by key in `automotive/site_chat.py` so that a new engine
  field never leaks: `car`, a short label of the car (make, model and year, nothing else
  of the vehicle record); `question`, the answers to tap as `{"options": [{"label",
  "value"}]}`, or `null` when the reply asks nothing (the place of an answer in the list
  is the number its tap sends back as `choice`); `suggestions`, up to four next steps;
  `groups`, the headings the cards are listed under, with ids made by the facade and a
  count each; `quick_replies`, which repeats the answers or the next steps for a page
  loaded before those existed; and `checked` and `checking`, how many of the turn's
  sources or searches have come back, counted and never named. When the engine warns
  that the stock figures are getting old and writes no note of its own, `note` is the
  sentence the part search shows in that state.
- The widget: answer buttons sit under the question and stay after a tap (the chosen one
  filled, the rest switched off); next steps follow the parts as a quieter row; the car
  is named in a line under the title with "Change"; an empty chat offers four starters;
  the line under the conversation says that the assistant is looking and then that it
  is going through what it found. The conversation is opened when the visitor starts to
  write, so the assistant has made itself ready by the time the message is sent.
- Chat limits: 3 open conversations and 2 running turns per address, 4 running turns in
  all, and one address may use an eighth of the day's turns; a message the assistant did
  not take is not counted. Nobody is refused for lack of room: a conversation nobody wrote
  in, then the one idle longest, gives way to a new one. A VIN decode asks an outside
  service, so at most 4 run at once and the rest are told to try again.
- The answers are the assistant's own, the same ones the staff workspace gets. Since
  2026-11-10 they come from the assistant engine, which searches the catalogue itself and
  asks with buttons ([automotive.md](automotive.md), "Assistant engine (model-driven)");
  a visitor's conversation is never given the shop's own stock numbers. When an
  answer lists no parts and offers no choices to tap, the widget adds a shortcut to the
  part search, so the visitor is not left at a dead end. The chat can be switched off in
  the dashboard (Texts, Chat) without touching the rest of the site.

## Content

`garage.site_sections` holds one validated JSON document per section (every text is a
`{"ka": ..., "en": ...}` pair), `garage.services` the service list, `garage.media` the
gallery images (JPEG, PNG or WebP up to 1.5 MB, addressed by SHA-256, re-encoded in the
browser before upload). The schema, length limits and defaults are in
`automotive/site_content.py`; the dashboard renders its forms from that schema. Pages read
a cached snapshot that is refreshed on every dashboard save.

Stored text is checked again when it is read. A value the editor would refuse (a row
written straight into the table, or saved under older limits) is not shown: a text field
falls back to its packaged wording, a bad cell in a list is left empty, a switch that
cannot be read stays off, and the log says `site_content_invalid` once. When the cached
copy is merely a minute old, one request reloads it while the others keep the copy they
have, so a slow database does not hold the pages up; after a dashboard save every reader
waits for the new copy.

Not editable from the dashboard: interface labels (`automotive/site_strings.py`), the
design, and the engine section's part names. The design includes three pictures cut from
the owner's wall photos by `scripts/build_site_assets.py`: the Service mural in the
services grid, the "More than a Garage" lettering, and the 1886 wall beside the story
text. They are not gallery photos and do not change when the gallery does.

## Security model

- **Database:** private schema `garage` (migration 0012) with row-level security. The
  capability role `garage_site` may read and write it, may not update or delete audit rows
  or delete accounts, and may read `warehouse.warehouse_sync_runs` and, when the trade
  module is installed, `trade.sales` and `trade.purchases` for admin-only reports.
  `automotive_runtime`, the credential the chat and search use, has no access to `garage`.
- **Passwords:** `hashlib.scrypt` (N=2^15, r=8, p=3), at most two hashes at a time, one
  hash on every failed sign-in so unknown, disabled and wrong-password look alike. At most
  six sign-ins are in progress at once; more are answered 503 straight away, so a flood of
  attempts cannot hold the worker threads the public pages run on.
- **Sessions:** a random token in an `HttpOnly; SameSite=Strict` cookie (`__Host-` prefixed
  and `Secure` when a public https origin is configured); only its SHA-256 is stored;
  12 hours idle, 7 days absolute; rotated on sign-in and password change. An answer is
  cached in memory for 5 seconds, so a session ended from the command line
  (`--reset-password`) stops working within 5 seconds; ended from the dashboard it stops
  at once. A session that was valid stays known for 10 minutes: it is re-checked freely,
  and if the database cannot be reached it keeps working for up to those 10 minutes
  instead of sending staff to the sign-in form (the one case where an ended session can
  outlive the 5 seconds). A cookie never seen valid costs a database query: at most 4 such
  look-ups run at once, and wrong cookies are limited to 30 at once and 2 a second per
  address and 10 a second for the whole site. A cookie that turns out to be real is never
  charged, and one address's wrong cookies never hold back a session that is signed in.
  The Live chats poll counts as activity, so a dashboard tab left open keeps its session
  until the 7-day absolute limit; the next poll after that gets 401, the browser goes to the
  sign-in form and the alarms stop until someone signs in. That is accepted: a screen meant
  to raise alarms should stay signed in, and the absolute limit still ends it.
- **CSRF:** unsafe requests to `/api/admin/*`, `/api/auth/*` and, with the staff login on,
  `/api/v1/*` must carry an allow-listed `Origin`; admin and auth requests also need the
  `X-CSRF-Token` header, an HMAC of the session token.
- **Sign-in throttling:** per account-and-address pair: one attempt per 30 seconds after
  five failures. How many attempts an address may make at all is the rate limit's job (5
  at once, then 5 a minute). There is deliberately no lock on the account alone, because
  anyone who knows a username could then keep its owner out from anywhere, and none on the
  address alone, because one person on a shared address (an office, a phone network) could
  then keep everyone there out with two wrong tries a minute. What remains: someone at the
  same address who knows a username can slow that account's sign-in from that address.
  Guessing spread over many addresses is bounded by the two hashing slots (about ten
  checks a second for the whole site, measured 188 ms each on 2026-11-19: some 900,000 a
  day), so staff passwords should be long. An IPv6 client's /64s share their /48's
  allowance (see Rate limits), so one /48 no longer counts as 65,536 addresses. On
  2026-11-19 the owner, asked, declined further sign-in hardening (a lock for unknown
  browsers, a second factor, a common-password list); a site-wide sign-in limit was
  tried and dropped, because six addresses could empty it and keep staff out. Failed sign-ins
  are written to the audit log at most once per address a minute and ten times a minute in
  all; the rest are counted and written as one row a minute, so the log shows how much
  guessing there was without growing freely. The audit log is append-only for the site;
  pruning old rows is a task for the database owner. Recovery is `--reset-password`.
- **Rate limits:** bounded token buckets per client address (IPv6 by /64) for pages,
  assets, search, VIN decode, chat and sign-in. In the categories that cost something
  (`site_guard.WIDE_CATEGORIES`: sign-in, new chats, chat messages, VIN decode) an IPv6
  client also draws on its /48, at four times one address's allowance, so changing /64
  inside it buys nothing. There is deliberately no limit shared by the whole site: a few
  addresses could use it up and keep everyone out. A full table forgets refilled buckets
  first; more distinct addresses than it holds, taking turns, are still not limited per
  address, and what they can spend is bounded by the day budgets and the hashing and turn
  slots. The public chat's per-address day is shared by a /48 as well (twice one
  address's), and the chat and VIN day budgets are paced: no clock hour spends more than a
  quarter (at least ten), so one burst costs that hour's visitors, not the whole day's.
  That slows a drain; it does not stop one: nine addresses at the chat's per-address day
  still use up the day if they keep at it. The address comes from
  `X-Forwarded-For` only when the peer is listed in `AUTOMOTIVE_TRUSTED_PROXIES`, and the
  application refuses to start with a public origin and no trusted proxy, because every
  visitor would then share the proxy's address and one limit. The older limiter inside
  `/api/v1` refuses everyone once its table is full, so with the staff login on only
  signed-in staff reach it.
- **Headers:** the strict CSP is unchanged (no inline script or style, no `data:` or
  `blob:` images, no third-party hosts). Versioned assets under `/assets/<build>/` are
  cached as `immutable`, gallery images for an hour (so a photo taken off the site stops
  being shown), public HTML is `no-cache`, everything else stays `no-store`.
- **Files:** `/assets/<build>/...` serves a file only under the exact name the asset
  folder lists, never under a second spelling, and never anything from `static/site/dev/`,
  the build scripts' records, or a packaged photo its manifest does not mark as
  publishable (those reach the site only through the gallery, once published there).
  `/media/<sha256>` answers only for a hash the content snapshot lists as published; a
  guessed address does not reach the database. The dashboard's own scripts and styles are
  served like any other asset: they hold no data, and every request they make is checked
  by the server.
- **Operator routes** answer only a direct loopback caller, never a request that came
  through a trusted proxy. If the proxy runs on the same host and is trusted as
  `127.0.0.1`, a local `curl` looks the same as the proxy and gets 404 too. Give the proxy
  its own source address instead (`proxy_bind 127.0.0.2;` with
  `AUTOMOTIVE_TRUSTED_PROXIES=127.0.0.2`, as in the example configuration).
- **Backups:** the backup operator role can read `garage.staff_users`, which holds the
  scrypt password hashes (never passwords). Treat a backup of this database as holding
  credentials: keep it encrypted and off the web host, as the runbook already requires.
- **Facebook receiver** ([garage-messenger.md](garage-messenger.md), "Security model").
  This is the only part of the Facebook integration that faces the internet.
  - **Where it runs:** its own process, `automotive/meta_receiver.py`, bound to
    `127.0.0.1:8771` and reached through ngrok.
  - **What it checks:** each delivery's `X-Hub-Signature-256` against the app secret, in
    constant time. It then appends the items to `garage.meta_inbox`.
  - **Its login** belongs to the role `garage_webhook` (migration 0013). That role may
    insert four columns of that one table and read nothing, not even its own rows.
  - **What its code uses:** the app secret, the verify token and that login. The page tokens
    sit encrypted in the database, where that login cannot read them.
  - **The limit:** on the laptop it runs as the same user as everything else and loads the
    whole private `.env`. A takeover of the process could read the other secrets in that
    file. The separation is by database role, not by process.
  - **The rest:** the worker that answers customers has no port. The Facebook dashboard is
    admin only, behind this application's own guard; the Live chats page is open to both roles
    and shows staff customers' messages and phone numbers, as the privacy page says staff may
    read the conversations.
  - **Pen-test waiver:** on 2026-11-11 the owner accepted a waiver of the release gate's
    external penetration test for this receiver, running on the laptop behind ngrok, and
    for nothing else. A production deployment of it adds `/meta/webhook`, `/privacy` and
    `/data-deletion` to the test's scope ([production runbook](production-runbook.md),
    item 10).

## Settings

| Variable | Meaning |
|---|---|
| `AUTOMOTIVE_SITE_DATABASE_URL` | the `garage_site` login; written by `--setup-site` |
| `AUTOMOTIVE_STAFF_AUTH` | staff login for `/staff` and `/api/v1/*`; default `true`. `false` is accepted only while every allowed host is loopback |
| `AUTOMOTIVE_PUBLIC_ORIGINS` | comma-separated https origins the site is served on; also become allowed hosts |
| `AUTOMOTIVE_TRUSTED_PROXIES` | addresses or networks of the reverse proxy; required once a public origin is set, and no wider than /8 (IPv4) or /32 (IPv6) |
| `AUTOMOTIVE_SITE_CHAT_DAILY_TURNS` | public chat turns per UTC day, default 300 |
| `AUTOMOTIVE_SITE_VIN_DAILY_DECODES` | public VIN decodes per UTC day, default 200 |
| `AUTOMOTIVE_SITE_CHAT_LOG_PATH`, `AUTOMOTIVE_SITE_CHAT_LOG_RETENTION_DAYS` | public chat log file and retention (30) |

## Before it is reachable from the internet

This change makes the application deployable; it does not deploy it.

1. The [production runbook](production-runbook.md) still applies: the release gate asks
   for an authenticated external penetration test and the
   [ASVS evidence matrix](asvs-l2-evidence.md), whose rows V1 and V3 must be re-assessed
   because a public UI and browser sessions now exist.
2. Run it behind an HTTPS reverse proxy with HTTP/2 (`ops/nginx/garage-site.conf.example`,
   `ops/systemd/garage-site.service`), bound to loopback, on a host other than the sync
   worker, which holds the warehouse writer credential.
3. Set `AUTOMOTIVE_PUBLIC_ORIGINS` and `AUTOMOTIVE_TRUSTED_PROXIES` (the application
   will not start with the first and without the second); the proxy must overwrite
   `X-Forwarded-For` and `X-Forwarded-Proto` on every request.
4. One process only: conversations, rate limits and the sign-in throttle live in memory.
5. Apply migration 0012 with the migration owner, run
   `ops/sql/provision_automotive_roles.sql`, and attach `garage_site` to a dedicated
   login that has no other role.
6. Fill in the contact details, opening hours and history in the dashboard, and have a
   native speaker review the Georgian interface strings.
7. Search engines: the domain redirects, Search Console, the Google Business Profile and
   the TuzMotors overlap are in [garage-seo.md](garage-seo.md).

## Verification

`uv run python -m unittest discover -s tests` (`tests/test_site_*.py`; the database tests
need `WAREHOUSE_TEST_ADMIN_DATABASE_URL`), `node tests/site_engine_timeline_test.mjs`,
`node tests/site_chat_widget_test.mjs` (the chat widget against scripted replies),
`node tests/admin_live_model_test.mjs` (the Live chats page's pure part; the Python side is
`tests/test_live_*.py`, `test_site_live.py` and `test_messenger_live.py`),
`node tests/browser/admin_responsive_walk.mjs` (every dashboard page in Chrome at nine
screen sizes, both roles, both languages, plus saves, uploads, dialogs and chat scrolling,
against an in-memory fixture on port 8792; it needs Playwright, see the file's header, and
its review is [`dashboard-fullscreen-qa.md`](automotive-review/dashboard-fullscreen-qa.md);
it still exits 1 on three older accessibility findings listed there),
`uv run ruff check automotive tests/test_site_*.py`, `uv run bandit -q -r automotive`.
The visual review and its screenshots are in
[`docs/automotive-review/garage-site-qa.md`](automotive-review/garage-site-qa.md); the
chat widget's walk against the live assistant engine is in
[`docs/automotive-review/assistant-chat-qa.md`](automotive-review/assistant-chat-qa.md).
