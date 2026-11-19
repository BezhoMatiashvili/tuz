# Customer chatbot improvement: first measured round

The 98–99% goal remains active and unproven. Astra directed and reviewed the work;
Sol implemented retrieval and evaluation changes, and Luna audited and exported customer
history. The workers reached their usage limit after leaving their changes in the workspace.
Astra completed review, corrected a regression, and ran the final checks. The repeatable
workflow is in [the improvement runbook](../chatbot-improvement-loop.md).

## Evidence and changes

Facebook history was read using GET requests through the existing connection. The final
private export contains 180 conversations and 537 customer messages, including conversations
without automotive keywords. One of the 181 returned threads had no visible customer message.
The 180 stable anonymous customer groups split into 128 training, 20 development and 32
held-out groups. Holdout wording was not inspected. Three histories are truncated; 45 threads
contain nontext messages. Those gaps remain explicit, rather than silently disappearing from
evaluation. The earlier keyword-filtered export is superseded and is not a representative
benchmark. Private artifacts are under ignored `data/customer_eval/`.

The active assistant could miss an available equivalent when stocked rows filled the search
limit before an out-of-stock listing carrying its cross-reference numbers was examined. The
new bounded retrieval selects those source listings separately. It adds candidates without
promoting them to verified fitment. Existing available candidates retain priority, and extra
candidates and their supporting listings each remain capped at ten.

Astra compared the original and changed retrieval on 200 frozen synthetic search calls
against the current 65,814-row catalogue. An initial patch displaced one stocked candidate;
that regression was reproduced and corrected before acceptance. Final paired results:

| Measure | Result |
|---|---:|
| Search result sets changed | 36 / 200 |
| Searches losing a stocked candidate | 0 |
| Expected synthetic truth gained / lost | 0 / 0 |
| New search errors | 0 |
| Invalid historical queries, unchanged | 2 |
| Median search time, before / after | 8.99 / 12.05 ms |

These are candidate-retrieval measurements, not evidence of improved end-to-end customer
accuracy. Redundant unavailable explanations were reduced in six searches. Model calls were
not added to the production search path.

`automotive.customer_eval` now prepares private cases, assigns stable group splits, runs
bounded replays through the actual assistant with chat logging disabled, and scores independent
labels. It separates vehicle, requested-parts, clarification and joint results. Missing or
failed runs cannot count as successes. Unknown labels, nontext inputs and counterfactual
follow-ups stay visible in coverage. Results record catalogue, code, prompt, readings and
input hashes. A changing candidate cannot pass the point-rate gate.

## Verification and next iteration

The final suite ran 1,938 tests successfully, with 136 database-dependent tests skipped.
Ruff passed on every changed Python file; Bandit passed on the sync package and changed
production modules; `git diff --check` passed. Repository-wide Ruff still reports 39 existing
issues outside these changes, primarily in the database explorer and its tests.

Four initial live probes handled the customer wording and three live evaluator smoke cases
completed. The latter correctly report zero grading coverage and no accuracy percentage:
their independent labels do not exist yet. The cheap triage model produced 16 training-only
drafts. Astra rejected them as gold after finding concrete errors in Georgian transliteration,
vehicle identity and suspension terminology. Cheaper workers remain useful for bounded code
changes; domain labels need stronger reasoning and independent evidence.

The remaining work includes independent intent/fitment annotation, ambiguous trade terms,
attachment and VIN context, and generation/engine/position checks. One smoke response offered
an eighth-generation-labelled row for a 2014 Accord Hybrid without establishing the
cross-generation fitment. Honda identifies that Hybrid as ninth generation in its
[2014 press kit](https://hondanews.com/en-US/honda-automobiles/releases/release-357366bbc5124bdbbe8e8783404c1190-2014-honda-accord-hybrid-sedan).
That is an unresolved evidence gap, not proof that the physical part is incompatible.

Next, label the development cases independently, distinguish recognition from catalogue
coverage and physical fitment, implement the demonstrated failures, and evaluate the frozen
real-customer holdout only after the candidate is fixed. No deployment or customer messages
were performed. The private `astra-round-001-judgment.json` records the continue decision and
artifact references; `meta_history_v2.jsonl` is the full, unfiltered corpus to use next.

## Continuation (afternoon, Claude Code)

The Codex session stopped at its usage limit at 12:20 UTC with two edits half-applied. This
continuation finished, measured and partly reversed them. Nothing was committed or deployed.

**Code.** The rate-limit retry in `AssistantTurn._send` is now a loop, so a failure on the
retried request is recorded and never retried again (two failing tests fixed). Codex's regex
term table (`assistant_terms.py`) was removed from the engine and parked privately: it
classified customer words before the model, which the owner ruled out on 2026-11-10. Its
knowledge moved into the prompt as worked guidance instead (the disc slang, an ambiguous
reservoir word, models written in Georgian letters, a stated grade or approval being part of
the request, a brand first with alternatives after it, keeping a row found by the customer's
own number, showing both axles rather than asking first). `customer_eval run` gained
`--runtime subscription` (the default, the $0 `claude -p` bench) and `--parallel`; the
scorer gained the owner's two numbers (right outcome, wrong-row share) with stock read at
run time, token-based car matching, and clarification cases that may carry candidate rows.

**Labels.** One case per conversation: the customer messages before the shop's first real
reply. AI labelers searched supplier fitment, part numbers and the catalogue's own vehicle
texts with their readings, never the bot's search or answers; there was no human review, as
the owner chose. Two labelers independently labeled the same 64 training conversations by
accident: they agreed on inclusion for 58 of 64 and on the row sets with mean Jaccard 0.96.
Gradable cases: train 30 (even half, consensus) + 25 (odd half), dev 7, held out 13 (labeled
by a separate agent that reported counts only). Most conversations are ad clicks, greetings,
photos or a masked VIN and cannot be graded.

**Results.** Prompt changes were made on the even training half and dev, so those numbers
(30/33 right) are optimistic. The frozen candidate (prompt hash in
`data/customer_eval/frozen-candidate-v2.prompt.sha`) was then run once on the unseen odd half
and once on the held-out cases:

| Set | Right outcome | 95% Wilson | Wrong rows |
|---|---:|---:|---:|
| Train, odd half (unseen) | 13/19 (68%) | 46–85% | 21/61 |
| Held out | 9/11 (82%) | 52–95% | 13/27 |

Cars were recognised in every graded case (16/16, 9/9). Photos made 8 selected cases
ungradable. By what each unseen case required (both sets together): show a stocked row 7/10
(odd 4/7, held out 3/3), say nothing fitting is in stock 3/7, ask 12/13. Seven of the 11 held-out
cases needed a question, and any question passes them, so the 82% mostly measures asking;
7/10 is the figure for finding the part. All 13 held-out wrong rows are oil rows in oil
requests, the same pattern as below (counted by script; the cases were not opened). The 30/33
on the tuned sets was measured before the last prompt edit. Of the odd half's 21 wrong rows, 20 are other brands of the same oil grade shown
after the named brand was out of stock: the labels count them wrong, the prompt allows them
as alternatives (the looser rule was chosen during this session; a stricter wording tried
before it showed no rows in such a case). That is a sales-policy decision for the owner.
Without that disagreement the odd half would score about 16/19 with 1/61 wrong rows: an
estimate of either policy once prompt and labels agree, not a measurement. The real misses were an oil grade written as
`0.40`, and two cars whose disc rows the catalogue files under a sister model's name.

**Verdict.** The 98–99% target is not met and cannot be shown on this data: even a perfect
score on 11 held-out cases has a 95% lower bound near 74%. The held-out set is now spent;
further tuning needs a fresh batch of conversations. One catalogue data error found
(a BMW oil whose code field contradicts its name) is in `data/assistant_eval/catalogue_fix_list.md`.
Checks: 1,952 tests passed (136 database tests skipped), Ruff and Bandit clean on the changed
files. All replays ran on the subscription bench (no paid fallback in any run log); labeling
used about 2.1M subagent tokens of the same subscription. The Messenger worker and webhook
services run from this checkout with the code they loaded earlier: restarting them would put
the new prompt and the untested-against-the-provider retry change in front of customers. One
private conversation export kept an unmasked Japanese frame number that the VIN mask missed.
