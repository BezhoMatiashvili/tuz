# Customer chatbot: second measured round (2026-11-17)

Continues [the first round](chatbot-improvement-2026-11-16.md). Nothing was committed, deployed or
sent to a customer; the Messenger services still run the code they loaded before 2026-11-16.

**No fresh conversations exist.** The Facebook export of 2026-11-16 was the whole page history the
Graph API returned (181 threads, limit 500), and the bot's own database holds 7 threads with no
customer message after the export. Every number below therefore reuses the 2026-11-16 sets.

**The oil-brand policy, settled from the shop's own replies.** In every training chat where a named
oil brand was out of stock, the staff said so and offered Shell or Petronas of the same grade; for
BMW original oil they offered "Shell with the same approval". The labels had counted such rows as
wrong. Brand-named oil groups now carry `alternative_product_ids`: other brands of the same grade,
and for a car maker's own oil only those whose name states that maker's approval. They are
permitted only while the named group has no row in stock, never required. One fixed rule
(`data/customer_eval/tools/oil_alternatives.py`, private) was applied to every split; on the
held-out split it printed counts only (2 groups). Re-grading the saved 2026-11-16 answers under
the amended labels, before any prompt change: odd training half 15/19 (was 13/19), wrong rows 9/61
(was 21/61); held out 11/11 (was 9/11), wrong rows 0/27 (was 13/27).

**Prompt changes** (all from training cases): an oil grade written 0.40, 5/40 or 5:30 is engine
oil of that grade; brake, suspension and steering parts are also searched under the car's platform
siblings (Highlander XU40 and Lexus RX AL10; Rogue S35, X-Trail T31 and Qashqai J10), and a
sibling's row is offered only as check when a part number ties it to the car; a named version of a
part (drilled discs) is part of the request like a grade; a named brand decides while it is in
stock, and alternatives to a maker's own oil must state its approval. A second edit (approval in
the product's own name, model family words) fixed neither target case and scored lower, so it was
reverted. Frozen prompt: `data/customer_eval/frozen-candidate-v3.prompt.sha` (run hash
87e9ad95fc1685b2).

The frozen prompt was run twice on the tuned-on sets (both runs reported) and once on the held-out
set:

| Set | Right outcome, run 1 / run 2 | Wrong rows, run 1 / run 2 | Status of the set |
|---|---:|---:|---|
| Training, even half | 24/26 / 23/26 | 1/53 / 2/49 | tuned on |
| Training, odd half | 17/19 / 16/19 | 2/50 / 8/52 | tuned on (unseen on 11-16) |
| Development | 6/7 / 6/7 | 1/14 / 5/21 | tuned on |
| Tuned-on sets together | 47/52 (90%, 79–96%) / 45/52 (87%, 75–93%) | 4/117 / 15/122 | |
| Held out (one run) | 10/11 (91%, 62–98%) | 0/20 | second use, labels amended |

Yesterday's answers re-graded under the amended labels scored 45/52 with 18/133 wrong rows on the
tuned-on sets and 11/11 with 0/27 on the held-out set. So on the only set not tuned on, today's
prompt shows no gain: the held-out move from 82% to 91% is the label correction. On the tuned-on
sets the prompt gained 0 to 2 cases, and its wrong-row share (3% or 12%) depends on the run; most
wrong rows come from two oil cases (BMW alternatives without a stated approval, and a dev case
whose label may miss some Shell 5W-30 rows).

By what each case required (frozen prompt, run 1, all sets): show a stocked row 18/22, say nothing
fitting is in stock 16/17, ask 23/24. Cars were recognised in every graded case.

Remaining misses (training): a BMW 5W-30 original in stock under a catalogue row whose code field
says 0W20 (already on the fix list), alternatives to BMW oil shown without a stated approval, the
Highlander's drilled discs filed under the Lexus RX, the Xterra's pads filed under the Pathfinder
R51, and a clutch bearing found by the customer's number but dropped on the next turn. Two runs of
the same frozen prompt differ by one case per set and by 11 wrong rows in total, so a single run
on these sets cannot tell small prompt changes apart.

**Verdict.** On real conversations the bot is right in about 87–91% of gradable cases (55 or 57
of 63 across all sets, 77–96% at 95% confidence), with no wrong rows among the held-out answers
and 3–12% on the tuned-on sets depending on the run. The 98% / 1% target is not met. Today's gain
over yesterday comes from correcting the labels to the shop's own policy, not from the prompt. The
held-out set has now been used twice and the rest was tuned on; a trustworthy number needs new
conversations, and showing 98% takes about 190 gradable cases with no miss at all. The Messenger
services were last started on 2026-11-15 and still run the code loaded then. The bot's own
database has stored no customer message since 2026-11-16 06:14 UTC (not investigated): that is
where fresh test chats would come from. Fixing the catalogue row whose code field says 0W20 (BMW
5W-30 LL-01, on the fix list) would clear a miss that recurs in two cases. All replays ran on the
subscription bench; no paid call. Checks: 2,132 tests passed (193 database tests skipped); Ruff,
Bandit and `git diff --check` clean on the changed files.
