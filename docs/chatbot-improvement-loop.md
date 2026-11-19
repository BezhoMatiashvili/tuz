# Measured customer-chat improvement loop

Use ECC `eval-harness` and `continuous-agent-loop`: define outcomes first, make one bounded
change, replay, compare, and stop on regression or an exhausted budget. Astra owns the plan,
review and failure triage. Luna handles routine, tightly scoped implementation and test work;
Sol handles hard failures after Astra narrows the cause. The agent runtime performs actual
orchestration. This document and the evaluator never execute agent-generated shell commands,
commit, deploy, or change the chatbot themselves.

The target is the owner's two numbers (decided 2026-11-11), never blended: a **right outcome in
at least 98% of verified real-customer held-out cases** and **at most 1% wrong rows among the rows
shown**, with coverage and uncertainty reported. 99% is aspirational. A point estimate on a small
sample cannot establish either reliability: 18 of 18 right still has a 95% lower bound near 82%.
Synthetic exams, model judgments and successful unit tests are development evidence only.

## Private case contract

Keep customer wording, labels, transcripts and detailed reports under ignored `data/`. Never
copy raw customer text, customer IDs, VINs, phone numbers, names, credentials or chat-log IDs
into tests, commits, screenshots or shared summaries. Sanitization must preserve model years,
engine sizes, part numbers and customer spelling; review the export before sending it to a
model. Public-site traffic may contain test sessions and is `public_site_unverified` until
independently audited; never silently relabel it as real customer traffic.

`automotive.customer_eval` reads one JSON object per JSONL line:

```json
{
  "schema_version": 1,
  "case_id": "opaque-case-001",
  "group_id": "opaque-customer-family-001",
  "conversation_id": "opaque-thread-001",
  "source": "real_customer",
  "label_status": "verified",
  "replay_eligible": true,
  "messages": [
    {"role": "user", "content": "invented example: Camry 2015 front pads and oil filter"}
  ],
  "gold": {
    "vehicle": {"accepted_labels": ["Toyota Camry 2015"]},
    "parts": {"groups": [
      {"acceptable_product_ids": ["EXAMPLE-PAD-A", "EXAMPLE-PAD-B"]},
      {"acceptable_product_ids": ["EXAMPLE-FILTER-A"]}
    ]},
    "ambiguity": "resolved"
  }
}
```

The example above is invented; actual examples belong only in private artifacts. Sources are
`real_customer`, `synthetic`, or `public_site_unverified`. `label_status` is `unreviewed` until an
independent reviewer verifies the gold against customer intent and fitment evidence. Astra
owns this review; unresolved physical-fitment questions go to a parts specialist. Gold must
precede candidate responses. Shop replies can suggest labels but are weak supervision;
the current chatbot, an unsupported model judgment, and product-name similarity cannot establish gold.

`gold.vehicle=null` means unknown/ungraded; `{}` requires no vehicle label. Otherwise supply
all accepted exact car labels, compared after case/whitespace normalization. Include the
correct year, generation and engine details when needed to distinguish fitment. The active
assistant's typed-car path exposes `car_label`, so this grader does not infer structured vehicle
facts from arbitrary prose or ask a model to judge its own answer.

`gold.vehicle` may instead give `required_tokens`: the few words a correct car label must
contain, compared without spaces or punctuation (`["camry", "2015"]`). Car recognition is a
diagnostic beside the headline numbers.

`gold.parts=null` means ungraded. Otherwise each requested group (`part` names it) lists every
catalogue `source_product_id` that fits, whatever its stock; an empty list says the catalogue
holds none. **Right outcome**: every group with a fitting row in stock *at the run* (the run
records `gold_stock`) has one shown on the final turn; a group with none in stock is right when
no wrong row is shown; a `clarify` case is also right when the bot asks a question. **Wrong rows**:
distinct shown rows outside every fitting set, as a share of all shown rows. A group for a
product the customer named by brand may also list `alternative_product_ids`: other brands of the
same grade (and, for a car maker's own oil, stating that maker's approval). They are what the
shop's own staff offer when the named brand is out of stock, so they are permitted only while
the group has no row in stock, never required, and wrong rows while it has one. The older joint
and parts dimensions stay in the report for comparison. Every final offered product must belong
to the allowed union for the older parts dimension. For multi-turn cases, independently label `gold.parts.allowed_earlier_product_ids` with
the union of all acceptable offers on earlier turns (an empty list forbids earlier offers).
This permits a legitimate shopping-intent switch without applying the final intent backward.
Earlier offers without this explicit safety annotation are safety-ungraded and cannot earn
part/joint success. Group headings alone never prove a correct part. Keep IDs tied to the frozen
catalog snapshot: do not delete an unavailable expected product from gold after seeing a miss.

`gold.ambiguity` is `clarify`, `resolved`, or `unknown`. A `clarify` case lists the candidate
rows of every reading, so check rows shown beside the question are not wrong (prompt examples
10 and 15 ask for that). The older ambiguity dimension still requires a question and no cards.
The deterministic check only tests the question/card structure, not whether the question asks
for the missing distinguishing fact.

Since 2026-11-16 the graded unit is the **first segment**: the customer messages sent before the
shop's first real reply (its greeting card and holding replies do not count). Replaying them
does not depend on a shop answer, so these cases are replay-eligible. Labels come from
`data/customer_eval/tools/` (`LABELING.md`, `labeltool.py`, `merge_labels.py`): supplier fitment,
part numbers and the catalogue's own vehicle texts with their readings, never the bot's search
or answers. They are AI-made without human review, as the owner chose; the held-out split is
labeled by a separate agent that reports counts only.

Customer messages use `role=user`; historical shop messages use `role=assistant`. Message
`kind` is `text` (the default) or `attachment`. The adapter preserves attachment-only input as
`kind=attachment` and `[NON_TEXT_ATTACHMENT]`; it is unsupported, never sent as fabricated
customer wording to the model, and remains ungraded with the unsupported count surfaced. Replay sends
only the recorded customer sequence through fresh `ConversationStore`/assistant sessions.
Historical shop replies are retained for private annotation, never injected as candidate
answers. Recorded follow-ups that depended on a different shop reply need independent review:
this is explicitly `counterfactual_user_only` replay. `replay_eligible` defaults to false and
must be independently verified before a case enters accuracy denominators, even when gold is
otherwise verified. A reviewer must confirm that the recorded customer sequence remains
meaningful without the historical shop answers; otherwise the case stays ungraded.

## Freeze before improving

Use stable opaque family IDs, keeping every thread from one customer or near-duplicate request
in the same group. The frozen `customer-groups-v1` SHA256 split is 70% train, 15% development and
15% held out. Do not regenerate IDs, tune the salt, move groups, or rewrite held-out labels after
observing candidate answers. Deidentified exports with fresh ordinal IDs must be frozen as one
batch; they do not establish cross-export customer identity. Link repeated customers privately
before merging a later batch. Preserve the original export and annotation audit independently.

The `prepare` adapter accepts the audit export shape (`case_id`, `group_id`, `source`, and
`messages` with `customer`/`shop` roles). It preserves source and produces only unreviewed,
unknown-gold cases. It does not extract labels from shop replies or candidate responses.

```sh
.venv/bin/python -m automotive.customer_eval prepare \
  --cases data/customer_eval/meta_sample.jsonl \
  --output data/customer_eval/meta-prepared-v1.jsonl
.venv/bin/python -m automotive.customer_eval validate \
  --cases data/customer_eval/meta-prepared-v1.jsonl
```

Independently annotate a new private copy, freeze its hash, then validate. No verified labels
means zero grading coverage and no measured accuracy, even if every replay finishes.

## Bounded replay and deterministic scoring

```sh
.venv/bin/python -m automotive.customer_eval run \
  --cases data/customer_eval/cases-v2-dev.jsonl --split dev \
  --results data/customer_eval/candidate-001-dev.jsonl \
  --report data/customer_eval/candidate-001-dev-summary.json \
  --runtime subscription --parallel 3 \
  --limit 40 --max-turns 4 --max-requests 400 --max-seconds 3600
.venv/bin/python -m automotive.customer_eval score \
  --cases data/customer_eval/labeled-v1.jsonl --split dev \
  --results data/customer_eval/candidate-001-dev.jsonl
```

`run` uses `assistant_eval.live`, the actual assistant and catalog, and disables both chat
logs. `--runtime subscription` (the default) serves the model through the owner's Claude
subscription bench (`claude -p`) and bills nothing, so the paid ledger neither limits nor counts
it; a subscription refusal stops further turns. `--runtime api` uses the paid provider and the
ledger below. Pass `--max-requests` and `--max-seconds` explicitly: the defaults stop a run
after about ten turns. Public audience is default; `--audience staff` and `--model` are explicit alternatives.
Each run writes a fresh private result file; it cannot overwrite or silently resume old results.
The total deadline starts before application initialization and admission stops once it is
expired; the existing application initializer itself has no evaluator cancellation hook.
Turn bounds apply while polling actual conversation turns. Request admission reserves
the configured maximum assistant requests for each next turn, plus one reserved opening
request. Preparation may perform additional internal requests, and separate research calls
are outside this counter; it is not a strict provider-wide request cap. The shared `assistant_eval` ledger records estimated
opening/turn cost, including an estimated failed request charge. Its reused prices may be stale,
and research charges may be missing. `--budget` stops admission at the estimated shared ledger
threshold; it is **not a hard billing cap**. Use provider-side spending limits for a strict cap.
No provider calls occur in `validate`, `prepare`, or `score`.

Result rows contain `case_id`, `case_sha256` (the canonical sorted-key JSON hash), `status`,
and `turns` with actual `ConversationStore.get()` payloads. `replay_case()` produces this contract.
Changed input or gold invalidates old results. The result artifact records run-time model/config, prompt hash, catalog snapshot hash, Git
revision, evaluator hash, and automotive Python source digests before and after replay.
Scoring preserves that run metadata and separately labels the current grader revision/hash;
it does not attribute old responses to today's code. Reports also hash the cases/results.
Review source digests before/after and retain the diff if any run overlaps code edits.

Scoring includes **all selected cases**; missing, failed, partial, stale, malformed, unrun and
turn-truncated cases fail every known criterion. A paginated offer list is not a complete
safety observation. Unknown and unreviewed gold stays ungraded and appears in coverage. Metrics
are separate for each source and for vehicle, parts, clarification routing and joint success;
they include graded counts, coverage, wrong offers and 95% Wilson intervals. Cases within a
family are correlated, so case-level Wilson intervals are descriptive; review group counts
and use an independent group sample for a production confidence claim.

`point_target_met` is a narrow held-out real-case point-rate check (right outcome in at least
98%, wrong rows at most 1% of shown rows, stable recorded source identity, full selected-case
outcome coverage). It does not imply coverage suffices, that labels are verified in reality,
or that prose fitment claims are safe. `production_proof` deliberately remains false: independent
provenance, representative coverage and Astra's evidence-backed audit of fitment claims are required.
A product ID check cannot inspect assertions hidden in prose. Report the actual sample size,
coverage and interval; never replace these with “98–99% achieved.”

## Improvement rounds

1. Astra freezes acceptance criteria, label/catalog versions, budget and maximum rounds. Run a
   baseline on train/dev. Classify failures by vehicle identity, part intent, missing catalog
   evidence, wrong offers, clarification, context, provider failure, or unavailable gold.
2. Astra assigns one narrow fix to Luna with examples from train/dev, an allowed file scope,
   required tests and a stop condition. Luna returns a reviewable diff plus evidence. Escalate
   difficult reasoning or repeated failures to Sol; Astra reviews the result and owns decisions.
3. Run focused tests and the unchanged development replay. Count every admitted case; preserve
   failures and spending records. Compare joint success, safety, coverage and latency/cost.
   Freeze on a safety regression or no measurable progress; narrow the failure rather than
   repeating the same change. Add synthetic regressions as development checks only.
4. After the candidate is fixed, run the frozen real-customer holdout once. Keep case-level
   failures away from implementers until the release decision. Repeated holdout-driven tuning
   makes that holdout development data; acquire a fresh independent group holdout afterward.
5. Astra reports the observed counts/rates/intervals, missing labels, unresolved safety issues
   and private artifact hashes. Promotion requires independent fitment review and the
   repository's production checks. This loop does not deploy, change production logs or create
   a success claim automatically. Stop when the bounded experiment ends; preserve its state.

## Protocol v3: every segment, with the shop's history (2026-11-18)

The owner's goal of 2026-11-18 is at least 95% on real conversations, for each of four
numbers on its own: right outcome on parts requests, row precision (1 - wrong rows / shown
rows), the car label, and the right act on messages that are no parts request. The private,
frozen protocol is `data/customer_eval/protocol-v3.md`; the commands are in this section.

- **Cases.** Every customer segment of every Messenger thread, not only the first
  (`tools/build_segments_v3.py`). A later segment carries the thread before it as `history`;
  `customer_eval` seeds the chat with it through `messenger_engine.record_shop_reply`, restores
  it into `MessengerConversations` and answers the segment's messages: the path the Messenger
  worker takes when the bot answers a chat the staff started. Messages sent within 15 s of
  each other are one turn, joined one per line, as the worker hands a burst to the engine.
- **Pools.** MEASURE is sealed: segments nobody tuned on (threads new since the last export,
  later segments of dev and held-out groups, anything a re-export adds). Its labels come from
  two independent labelers who report counts only; it is replayed three times in all
  (baseline, one mid-check, final). TUNE holds the rest of the train split and is where
  changes are made; SPENT is the twice-used held-out first segments.
- **Grading.** Parts cases as before, plus: a clarify case's question must ask for the labeled
  fact (`customer_eval judge-questions`: one yes/no check per question on the subscription
  bench). Act cases are graded from structured fields: `staff.intent` for visit and service,
  no cards for greetings, thanks and staff matters, the shop facts stated for `shop_fact`.
- **Commands.** `customer_eval run --pool tune ...`, `judge-questions`, and `pool-report --runs
  R1 R2 R3 --pool measure` (the four numbers pooled over the runs of one candidate, and per
  run). `data/customer_eval/tools/run_v3.sh TAG POOL RUNS` does all three, logged.
  `tools/failure_types.py` counts failure types without printing a case, which is all a tuner
  may see of MEASURE.
