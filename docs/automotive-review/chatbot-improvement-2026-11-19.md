# Customer chatbot: third measured round (2026-11-18/19)

Continues [the second round](chatbot-improvement-2026-11-17.md). The owner's goal (2026-11-18):
at least 95% accuracy on real conversations, in the replies and in finding parts and cars, with
no questions asked. Nothing was committed, deployed or sent to a customer. All replays ran on
the subscription bench ($0); no paid API call was made.

## More real conversations

The 2026-11-16 export had read only the default inbox folder and the newest 30 messages of each
thread. A full read-only re-export (2026-11-18 22:52) holds 193 threads: 13 new since the 16th,
2 in the page's "other" folder, every message of every thread, 580 customer messages. Instagram
is not linked to the page. The website chat logs hold test traffic only.

The bigger gain was in what counts as a case. Until now only a thread's first segment was graded
(the customer's messages before the shop's first real reply), which left 75 gradable cases. Now
every segment is a case: a later segment is replayed with the thread before it, the staff's own
replies included, seeded exactly as the Messenger worker records a staff reply
(`messenger_engine.record_shop_reply`) and answered through `MessengerConversations`, the path the
bot takes when it answers a chat the staff started. Messages sent within 15 s of each other are
one turn, as the worker hands a burst to the engine (45% of the gaps between a customer's
messages are that short). That gives 368 segments; 272 are graded after labeling.

## How it is measured (protocol v3, frozen before any run)

- **Four numbers, each at least 95%, never blended:** right outcome on parts requests, row
  precision (the share of shown rows that are right), the car label, and the right act on
  messages that are no parts request (greeting, thanks, shop facts, visit and service requests,
  staff matters; graded from the reply's structured fields: the staff signal, no cards, the
  shop facts stated).
- **A clarifying question must ask for the fact that decides the part** (engine, axle, year...):
  one yes/no check per question by a model on the bench. Before, any question passed.
- **Pools.** MEASURE is sealed: 58 cases nobody tuned on (the 13 new threads, and later segments
  of the dev and held-out customers): 24 parts requests, 34 acts. Two independent labelers
  agreed on every one of its include and kind decisions; a tuner sees only counts. It is
  replayed three times in all: baseline, one mid-check, final. TUNE (192 cases: 102 parts, 90
  acts) is where changes are made. SPENT (22) is the held-out first segments used twice before.
- Labels are AI-made with catalogue searches, supplier fitment, part numbers and web lookups of
  car and part, without human review, as the owner chose.

## What changed in the bot

Prompt (`automotive/assistant_prompt.py`), all found on TUNE failures:

- The shop staff's replies in the history are context: the car, the part and the brand they
  settle are known; a car stated earlier stays the car label (or the make, when only the make is
  known).
- A question about a product the conversation already named (price, stock, brand, origin,
  original or not, warranty, a comparison, a total for oil and filter) is answered with that
  product's rows first. "Finish at once" for a question about shown rows now applies only to
  rows actually shown as cards: the staff's replies carry none. Comments, thanks and complaints
  get words only.
- For brake, suspension and steering parts the first step looks up the part numbers the model
  knows for the car (maker numbers, FMSI), which ties rows filed under a sibling model to the
  car; a sibling's row may be offered as check when the platform shares that very part.
- A grade, approval or version counts only when the row's own name states it; a row without it
  is not shown at all; a diesel with a particle filter takes a low-ash oil. Rows of another
  engine's version are not offered as check. Sold-out answers show only rows listed for the car.
- A search cut at 30 rows is narrowed before the bot says the shop does not have the part. Every
  model is also searched by its bare model word (RX for an RX350).
- Asking what work costs or how the shop does it is a service lead (staff signal and a phone
  request); the intent is judged on the newest messages, also after staff replies.

Search (`automotive/assistant_catalog.py`): among rows that match as many terms, a row that has
the Georgian part word itself ranks before rows of a longer word (oil before oil filters), so the
part asked for is no longer cut off by its relatives.

## Results

TUNE, three runs each, pooled (parts / rows / car / acts):

| Candidate | Parts | Rows | Car | Acts |
|---|---:|---:|---:|---:|
| Frozen 11-17 prompt | 81.0% | 91.2% | 95.9% | 95.2% |
| P4 (brake number look-ups) — **left in the tree** | 90.8% | 96.1% | 96.7% | 97.0% |
| P5b (follow-up rule, "finish at once" narrowed) | 86.9% | 94.3% | 96.3% | 96.7% |
| P5b at effort medium (+1 s per part turn) | 88.6% | 94.9% | 95.9% | 96.3% |
| P5c (P5b + "coming in for work is service"), the sealed final | 85.6% | 93.8% | 96.7% | 96.7% |
| P5b with Opus 5.5 as the bot (one run, information only; part turns p50 13.4 s) | 91.2% | 92.2% | 94.4% | 97.8% |

Run-to-run spread for one prompt is about 2-4 points on parts, so P4, P5b, P5c and effort
medium are within noise of each other on TUNE; P4 is the best by clean case-runs (538 of 576).

MEASURE (sealed), three runs each:

| Run | Parts | Rows | Car | Acts |
|---|---:|---:|---:|---:|
| Baseline, frozen 11-17 prompt | 58.3% | 87.3% | 93.9% | 89.2% |
| Mid-check, P4 | 59.7% | 93.2% | 100% | 93.1% |
| **Final, P5c** (41/72, 59/66, 66/66, 93/105) | **56.9%** | **89.4%** | **100%** | **88.6%** |

The final ran on the 58 sealed cases plus the one case of the never-looked stratum (the night's
re-export added two threads: a greeting and spam). 95% Wilson intervals with the runs pooled:
parts 45-68%, car 94-100%, acts 81-93%. Every final run used one frozen candidate (prompt
cf031fb3, effort low) on the same catalogue snapshot as the baseline. First segments were right
in every run (6/6 parts, 30/30 acts); the misses are all later segments, 14 conversations,
per-conversation parts rate 0.67. SPENT (held-out first segments): parts 84.8%, rows 100%, car
100%, acts 90.9%. Bench latency of part turns: p50 9.9 s, p90 13.3 s.

**Verdict: not met.** The car label meets the bar (100%, 66/66); parts, rows and acts do not.
The tuning pool improved from 81% to 86-91% on parts, but that did not carry over to the sealed
later segments, which stayed at 53-56% through every candidate. Two changes made for the sealed
failure counts did not help there: the follow-up rule fixed its invented reproductions (16/16)
but not the sealed cases, and the visit-versus-service clarification raised service to 28/30
while visits fell from 12/12 to 2/12. Disclosure: those last two changes were shaped by the
mid-check's aggregated failure counts, so the final is not blind to them.

## Why the sealed cases fail (read after the final, now spent data)

18 of the 59 cases were not clean in all three final runs; 15 failed in every run, and 11 of
those come from two conversations. Sorted by cause:

- **Catalogue filing, about 7 cases.** The right row exists in stock but under another car's
  text or without the number the customer typed (front pads asked by their VW number, filed
  under a generic VAG row; Camaro discs filed under the Equinox). The labelers found them with
  cross numbers and web lookups. The bot's web research is switched off in the customers' chat
  (`AUTOMOTIVE_RESEARCH=staff`, pending the owner's one-cent check), so it cannot.
- **The bot's policy differs from the labels', about 9 cases.** A customer coming in for a
  fitting is `visit` in the labels; the final's new rule called it service (4 cases, all passed
  under P4). A DCT fluid's approval was accepted by the labeler from the maker's data; the bot
  shows only rows whose name states it. "Send me a photo" and "which oil is good for my car"
  were labeled as "show the products", where the bot explains or asks for the model; two oil
  cases have spec-narrower labels than the bot's rows.
- **A rule not followed, about 2 cases.** Pads shown for "pads too, and the total with
  fitting?", the discs of the same request not.

13 of the 18 have information in their history that the replay cannot give the bot: a VIN
(masked in the export; production would decode it) in 7, a photo the staff saw (the bot reads
no photos) in 11; 5 have a part number the customer typed in an earlier message. So on this data
95% is not reachable by prompt changes alone: part of the gap is the replay hiding VINs, part
needs web research in the customers' chat or catalogue fixes, and part is policy the labels
settled differently (visit vs service, approval evidence, ask vs show).

After the final, the P5c line was reverted (it cost visits); P5b on the now-spent 59 scored
parts 44/72, rows 91.4%, acts 98/105 (development data, not a headline), level with P4's
mid-check (43/72, 93.2%, 95/102), so the tree is left on **P4** (prompt `e614a808`) with the search
ranking fix: the best on TUNE and the sealed set alike.

## Is 95% reachable on this data? A ceiling check

After the final, the 59 cases are development data, so every lever the system has was tried on
them (one run each where marked; three otherwise):

| Configuration on the 59 | Parts | Rows | Car | Acts |
|---|---:|---:|---:|---:|
| Frozen 11-17 prompt (baseline) | 58.3% | 87.3% | 93.9% | 89.2% |
| P4 prompt | 59.7% | 93.2% | 100% | 93.1% |
| P5b prompt | 61.1% | 91.4% | 98.5% | 93.3% |
| P5c prompt (the final) | 56.9% | 89.4% | 100% | 88.6% |
| P4 + web research in the customers' chat, 25 s wait (1 run) | 54.2% | 96.8% | 100% | 91.4% |
| P4 + Opus 5.5 + web research (1 run) | 58.3% | 90.3% | 100% | 85.7% |
| P4 + research, prompted to search numbers early, 25 s wait (1 run) | 50.0% | 88.2% | 86.4% | 94.3% |

Prompted to search part numbers on the web, the bot still did so once in 59 cases, and the
waits pushed part turns to a p90 of 45 s, so that three cases timed out: research is not a
working lever with this model.

Prompt, reasoning effort, model and research all leave parts between 54% and 61%; with research
switched on the bot called it once in 59 cases. The first segments of conversations were right in
every run (parts 6/6, acts 30/30); every miss is a later segment of a long conversation, and 13
of the 18 failing cases carry a VIN or a photo earlier in the thread. So the parts and acts
numbers are bounded by the cases' content (photos the bot cannot see, VINs the replay masks,
parts filed under other cars, policy the labels settled differently), not by the bot's settings.
With 24 sealed parts cases, the bar would need 23 of them right in every run.

**A fresh stratum: post comments.** The page's 6 posts carry 19 customer comments, which the bot
answers in a private message (the comment plus an excerpt of its post, as `meta_comments` builds
it). Two sealed labelers agreed on all 18 with text (8 parts requests, 9 acts, 1 VIN-only
excluded). Measured once with the P4 prompt, 3 runs: parts 18/24 (75%; the same 2 cases fail in
every run: a question about another fact than the one that decides the oil, and an oil not
shown), rows 96.2%, car 18/18, acts 27/27. Three of the four numbers meet the bar here; parts does
not.

## Correction: the "web only" cases are reachable (P8b)

The ceiling below was wrong. Re-checked on the rows themselves: the in-stock SATO pad carries the
VW number in another revision (`1EA698151B` for the typed `1EA698151D`; VW's last letters are a
revision index of the same part). Two data changes, with the model still deciding fit:

- `CatalogIndex.revisions` and `typed_numbers.other_revisions`: rows that carry a typed maker
  number in another revision (the last one or two letters differ), reported apart from exact
  hits; the prompt says to offer them as check with that reason.
- `earlier_typed_numbers`: part numbers the customer typed in earlier messages, looked up
  again each turn. In a chat the staff answered by hand no turn ever looked them up; the prompt
  says their rows are the ones to show when the new message is about that part.

P8b (prompt `aeaaafb3`) on the 59, two runs: parts 35/48 (72.9%, from about 60%), rows 88.5%,
car 100%, acts 95.7%; on TUNE, two runs: parts 88.2%, rows 95.6%, car 97.8%, acts 97.8% (P4:
90.8 / 96.1 / 96.7 / 97.0, within noise). These are development numbers on spent data; the next
fresh stratum measures it. The tree is left on P8b. Still failing on the 59: oils of a spec the
labels exclude or approvals they take from makers' data (5 cases), brake parts that differ by
engine without the question asked (3), and a photo request (1).

## Afternoon of 11-19: what moved, what did not, and why

- **The bench drifts.** The bench's `claude` CLI updates itself (one update landed at 11:51).
  The Shell discount follow-up that P4 passed 3 of 3 at 02:00 failed 0 of 4 with P4's own prompt
  text at 11:50. Arms are now compared back to back, alternating run by run, and every run
  header records `bench_cli_version`.
- **Small ablations instead of whole pools.** Before a full run, each change is tried on the
  cases it targets plus neighbours, 4 runs per arm, back to back. P8b's prompt text is needed
  for its gains: without it the ID.4 follow-up drops from 4/4 to 0/4. P9 (ask the engine
  before showing one brake size) moved none of its three targets and was reverted.
- **P10, two judging lines.** (1) A petrol engine of the last fifteen years needs API SN or SP,
  so an oil that states only SL, SJ or SM is left out. The Lexus GX460 case went from 0/4 to
  4/4. (2) A number reaches a row through its own number or its cross numbers. A row whose own
  number is another number of the same brand (MANN HU816ZKIT reached by HU816X in the shop's
  cross text) is that other part. This was the most frequent wrong row of all runs (11 times,
  three BMW cases); MANN lists the KIT for N20 engines only.
- **Labels checked against their evidence.**
  - The MB 236.21 DCT golds come from the makers' sheets (Valvoline, Pakelo), so the labels are
    principled. The bot cannot see makers' sheets, and the shop's staff answered "no" in that
    chat.
  - Five price complaints are labeled staff acts, one ("the discount is the normal price")
    parts. The prompt's "a complaint about the price is answered in words" rule matches the
    five, so it stays.
  - In the BMW 328i oil case the earlier request included the filter, but the gold has no
    filter group, so the bot's filter counts as wrong. That label is debatable.
- **The VIN gap in the replay is real but small.** In production a VIN typed to the staff
  becomes the chat's car and is decoded (NHTSA). The export masks VINs, so the replay cannot
  decode them, and the decoder is an outside service, so it is not called on customer VINs.
  Cases with a masked VIN do no worse than the others (spent: 12/16 parts against 23/32).
- **The open-fact branch was misfiring.** In two clarify cases the bot wrote "year unknown
  (customer didn't answer)" and showed every variant as check, although nobody had asked. P11
  narrows the branch to a customer's own "I don't know". It also tells the bot a sent photo is
  unseen, and that a request for a photo or label data shows the product's row. P11 is
  tested next.
- **P11 kept.** On the 31 tuning clarify cases P11 ties P10 (90/93 each). On the 13 spent
  ones it gains 2 (29/39 against 27/39): the photo request now shows the oil's row, 3/3. The
  W205, Camaro and ID.4 clarify cases did not move. Their notes show misreading ("all four";
  "the rear stays") or missing knowledge (355 mm is the Camaro SS's disc; the ID.4 has two
  disc sizes), not the branch.
- **FMSI numbers were unreachable (fixed).** 1,270 pad rows write the FMSI number with its
  plate ("D1107-8212"). A lookup of "D1107" found none of them, so even a bot that knew the
  number could not reach the Jetta GLI's in-stock pads. `_cell_codes` now indexes the D-number
  of such a cell as a stem (`_FMSI_PLATE`), ranked after exact hits, with a test.
- **Numbers on the search tool did not help (reverted).** The model made a number lookup in
  only 7 of 56 tuning brake case-runs. A required `numbers` field on `search_catalog` (P12) made
  it give numbers even less (10 of 195 brake case-runs against 24 of 195 with P11's
  `lookup_codes` sentence), at equal totals (133 against 134 tuning, 35 against 36 spent). When
  numbers were given, those case-runs were 92-100% clean, so the gap is the model not recalling
  numbers, not the tools.
- **P11b is the candidate: P11's prompt, the FMSI stems, plain tools.** It was run back to back
  against medium effort, 2 runs each:

  | arm | pool | parts | rows | car | acts |
  |---|---|---|---|---|---|
  | P11b, effort low | TUNE | 90.7% | 97.4% | 95.6% | 96.7% |
  | P11b, effort medium | TUNE | 88.7% | 96.9% | 97.2% | 97.2% |
  | P11b, effort low | spent 59 | 72.9% | 96.7% | 100% | 92.9% |
  | P11b, effort medium | spent 59 | 70.8% | 93.7% | 97.7% | 95.7% |
  | P10, effort low (same CLI, 3 h earlier) | TUNE | 89.7% | 94.7% | 96.7% | 97.2% |
  | P10, effort low | spent 59 | 68.8% | 92.9% | 97.7% | 92.9% |

  Medium effort is no better and adds 0.9 s at the median and 2.6 s at p90, so effort stays
  low. On TUNE's first segments P11b reaches parts 97.3%, rows 98.8%; the gap is the later
  segments (parts 82.6% on TUNE, 70.5% on the spent 59). On the spent comment cases it scores
  parts 13/16 and 100% on the other three numbers. One of its misses is erratum 2: the comment
  labeler read "აპორნები" as a strut mount, while 14 of 15 labels and the shop's staff use it
  for brake discs.
- **Opus as the bot, on the weak spot.** P11b with Opus 5.5 (effort low), 2 runs, was compared
  with the Sonnet runs of the same evening:

  | subset | model | parts | rows | car | acts | p50 / p90 |
  |---|---|---|---|---|---|---|
  | TUNE later segments (46 parts cases) | Sonnet | 82.6% | 95.3% | 97.7% | - | 10.7 / 19.1 s |
  | TUNE later segments (46 parts cases) | Opus | 87.0% | 96.1% | 93.0% | - | 12.5 / 24.2 s |
  | spent 59 | Sonnet | 72.9% | 96.7% | 100% | 92.9% | 7.4 / 11.7 s |
  | spent 59 | Opus | 79.2% | 93.0% | 97.7% | 85.7% | 9.0 / 17.2 s |

  Opus answers more later segments right but loses acts and car labels and is slower. Neither
  reaches 95% on later segments, so Sonnet stays. The largest single class still failing in
  TUNE's later segments is brakes filed under platform siblings (Jetta GLI under Golf V and
  Passat, Xterra under Pathfinder). Their FMSI numbers tie them, and the crawled fitment has no
  brake data for these cars.
- **A reviewer pass does not separate good from bad replies.** Sonnet at medium effort read
  finished replies without labels. It said "revise" on 19 of 43 failing case-runs and 13 of 40
  passing ones. Some false alarms came from the stale-stock flag in its input, but it also
  misread the shop's slang. Not worth a second call on every turn; shelved.
- **Privacy fix.** The greeting card's first name was masked only after "გამარჯობა,", with
  the comma. 41 private case records with "გამარჯობა NAME!" were scrubbed in place and the
  builder fixed. Runs made before the scrub cannot be re-scored on those cases (the case digest
  guard), and their saved pooled reports stand.
- **Sources of real conversations, all checked read-only.**
  - Messenger: the inbox and "other" folders hold 198 + 3 threads back to 22 Oct; page_done,
    pending and spam are empty; Instagram is not linked.
  - Post comments: 6 posts, 20 comments, 19 already used.
  - Ad posts: not readable (403), and none of the 70 ads in the 7 reachable ad accounts
    promotes the Garage page.
  - The v6 export (19:3x) adds 11 fresh cases since the morning. A stratum now waits for at
    least 20 gradable parts cases (protocol amendment, before it is read).

## Why 95% cannot be reached on this set (superseded, see above)

The bar, pooled over three runs, allows at most 3 failed case-runs of the 72 parts case-runs:
23 of the 24 parts cases must be right in every run. The labels' own evidence shows that four of
the failing cases (the front pads of a VW ID.4 asked by the VW part number) can only be answered
through web cross-references: that number is on no catalogue row, and the labeler tied the
fitting rows to it through a Brembo and a SATO number found on the web. With web research off in
the customers' chat, and unused by the model when it is on, no behaviour of the bot can answer
them, so the most it can score on this set is 20 of 24 (83%). The other nine failing parts cases
are reachable in principle (a grade named earlier in the conversation, a request for a photo or
a specification of an offered product, an engine question not asked, oils of the wrong spec,
approvals the labels took from the makers' data). A prompt arm for two of those kinds (a grade
named earlier carries over; a photo or specification request is answered with the product's
row) left parts unchanged on the 59 (60.4%, 2 runs) and on the comments (68.8%), so it was
reverted; the tree stays on P4.

## State and decisions for the owner

**Committed (the owner said "choose the best and git it", 2026-11-19 23:50): P11b.** That is
Sonnet 5.5 at effort low with prompt `2b9ab2ad`, the tools with typed and earlier numbers and
other revisions (`a21e3fbe`), the catalogue with the whole-word ranking, revisions and FMSI
plate stems (`48fa34bd`), the evaluator (`customer_eval.py`) and the reports.
- **Its dev numbers**, back to back on 11-19 evening (two runs each):
  - TUNE: parts 90.7%, rows 97.4%, car 95.6%, acts 96.7% (first segments: parts 97.3%, rows
    98.8%);
  - spent 59: parts 72.9%, rows 96.7%, car 100%, acts 92.9%.
- **The 95% bar is not met.** It is not met on any fresh stratum, and not by parts on later
  segments.
- **Checks on the committed set:** it was tested alone over HEAD (1,964 tests pass, 136 database
  tests skipped). gitleaks found nothing in any committed file.
- **Not committed:**
  - the number recall, parked privately with its notes (one ablation round, unproven, off by
    default when re-applied);
  - the Live chats page and its migration 0015, from another session and still awaiting the
    owner's checks; `assistant.py` needs only its self-contained `live_alerts.py`, which is
    committed;
  - other sessions' site and security edits;
  - `graphify-out`.
- **Measurement still open.** The fresh stratum `fresh2` (24 cases, 8 agreed parts cases, 7
  label mismatches waiting for a third labeler) is below its pre-registered 20-parts bar and
  was not measured.
- **Services.** None was restarted; the Messenger worker and the site still run the code they
  loaded on 2026-11-15.

Decisions only the owner can make, each of which moves the numbers:
- **Web research in the customers' chat** (`AUTOMOTIVE_RESEARCH=all` after the one-cent check):
  the route the labelers used for parts filed under other cars.
- **The webhook gap** (27 customer messages since 2026-11-16, 1 received): needed before the bot
  is switched on at all.
- **The stock sync** (last run 2026-11-13).
- **Visit vs service** for "I'll come for the fitting": both lead to the same phone request.
- **Approval evidence**: the row's name only (safest, the current rule) or the maker's data too.
- **Two catalogue rows**: the BMW 5W-30 LL-01 row whose code says 0W20, and pads that carry no
  OE number of the car they fit.
- **Opus as the bot**: a few points more parts on TUNE, more wrong rows, about 1.7x the latency.

The next honest measurement is a re-export this evening, labeled by sealed labelers and measured
once; the night added only one gradable case (a greeting).

## Found on the way (production)

- **The webhook misses most messages.** The page inbox holds over 580 customer messages; the
  bot's database stored 16 from 12 senders in a week. Read-only checks: only the GARAGE 1886 app
  is subscribed to the page, with `messages`, `message_echoes`, `messaging_postbacks`,
  `messaging_referrals` and `feed`; no app owns the newest threads. The likely causes are the
  app's access level for `pages_messaging` (App Review) or delivery failures, both visible only
  in the Meta developer console. With the bot switched on, it would not see most customers.
- **Stock is stale.** The catalogue's stock was last read on 2026-11-13; the site and Messenger
  say "ask the shop" after 25 hours. Replays used that snapshot unchanged.
- The BMW 5W-30 LL-01 row whose code field says 0W20 is still on the catalogue fix list.
