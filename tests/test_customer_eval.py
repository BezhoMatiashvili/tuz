"""Customer grading tests use invented cases and local fakes; never production evidence."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automotive import customer_eval as evaluator


def case(name="a", source="real_customer"):
    return {"schema_version": 1, "case_id": name, "source": source,
            "group_id": "opaque-family", "conversation_id": "opaque-thread",
            "label_status": "verified", "replay_eligible": True,
            "messages": [{"role": "user", "content": "invented"}],
            "gold": {"vehicle": {"accepted_labels": ["Toyota Camry 2015"]},
                     "parts": {"groups": [{"acceptable_product_ids": ["1", "2"]},
                                          {"acceptable_product_ids": ["3"]}]},
                     "ambiguity": "resolved"}}


def turn(ids=("1", "3"), **extra):
    return {"status": "COMPLETE", "engine_status": "OK", "answer": "Example answer",
            "car_label": "Toyota Camry 2015", "question": None,
            "cards": [{"product": {"source_product_id": i}} for i in ids], **extra}


def result(item, turns=None, **extra):
    return {"case_id": item["case_id"], "case_sha256": evaluator.case_digest(item),
            "status": "complete", "turns": turns if turns is not None else [turn()], **extra}


class CustomerScoringTests(unittest.TestCase):
    def test_joint_requires_exact_accepted_vehicle_and_all_groups(self):
        item = case()
        good = evaluator.score_case(item, result(item, [turn(("2", "3"))]))
        self.assertTrue(good["joint"])
        wrong_vehicle = evaluator.score_case(item, result(item, [turn(car_label="Camry 2016")]))
        self.assertFalse(wrong_vehicle["vehicle"])
        self.assertTrue(wrong_vehicle["parts"])
        self.assertFalse(wrong_vehicle["joint"])
        missing_group = evaluator.score_case(item, result(item, [turn(("1",))]))
        self.assertFalse(missing_group["parts"])

    def test_extra_and_earlier_wrong_offers_cannot_be_hidden_by_correction(self):
        item = case()
        item["messages"].append({"role": "user", "content": "invented correction"})
        item["gold"]["parts"]["allowed_earlier_product_ids"] = ["1", "2", "3"]
        scored = evaluator.score_case(item, result(item, [turn(("wrong",)), turn()]))
        self.assertEqual(scored["wrong_offers"], 1)
        self.assertFalse(scored["joint"])
        scored = evaluator.score_case(case(), result(case(), [turn(("1", "3", "wrong"))]))
        self.assertFalse(scored["parts"])

    def test_missing_failed_empty_truncated_and_wrong_hash_are_failures(self):
        item = case()
        records = [None, result(item, status="bounded"), result(item, []),
                   result(item, [turn(status="RUNNING")]),
                   result(item, [turn(engine_status="TIMEOUT")]),
                   result(item, [turn(answer="")]), result(item, case_sha256="stale")]
        for record in records:
            with self.subTest(record=record):
                scored = evaluator.score_case(item, record)
                self.assertFalse(scored["joint"])
                self.assertFalse(scored["vehicle"])
                self.assertFalse(scored["parts"])

    def test_unknown_and_unreviewed_gold_never_become_success(self):
        item = case()
        item["gold"].update(vehicle=None, parts=None, ambiguity="unknown")
        scored = evaluator.score_case(item, result(item))
        self.assertIsNone(scored["joint"])
        self.assertTrue(scored["offer_safety_ungraded"])
        item = case()
        item["label_status"] = "unreviewed"
        scored = evaluator.score_case(item, result(item))
        self.assertIsNone(scored["vehicle"])
        self.assertIsNone(scored["parts"])

    def test_no_car_and_safe_clarification_are_separate_from_parts_found(self):
        item = case()
        item["gold"].update(vehicle={}, parts=None, ambiguity="clarify")
        payload = turn((), car_label="", question={"text": "Which model?"})
        scored = evaluator.score_case(item, result(item, [payload]))
        self.assertTrue(scored["vehicle"])
        self.assertTrue(scored["ambiguity"])
        self.assertIsNone(scored["parts"])
        self.assertIsNone(scored["joint"])
        payload["cards"] = turn()["cards"]
        self.assertFalse(evaluator.score_case(item, result(item, [payload]))["ambiguity"])

    def test_denominators_include_unrun_and_sources_stay_separate(self):
        real, absent, synthetic = case(), case("b"), case("c", "synthetic")
        synthetic.update(group_id="synthetic-family", conversation_id="synthetic-thread")
        summary = evaluator.summarize([real, absent, synthetic],
                                      [result(real), result(synthetic)], "all")
        score = summary["sources"]["real_customer"]["joint"]
        self.assertEqual((score["passed"], score["graded"], score["rate"]), (1, 2, .5))
        self.assertEqual(summary["sources"]["synthetic"]["joint"]["rate"], 1)
        self.assertFalse(summary["production_proof"])
        self.assertFalse(summary["point_target_met"])

    def test_no_labeled_real_cases_has_no_rate_or_target(self):
        item = case()
        item["label_status"] = "unreviewed"
        summary = evaluator.summarize([item], [], "all")
        score = summary["sources"]["real_customer"]["joint"]
        self.assertIsNone(score["rate"])
        self.assertEqual(score["coverage"], 0)
        self.assertIsNone(score["wilson_95"])

    def test_result_identity_and_group_leakage_are_rejected(self):
        item = case()
        with self.assertRaises(ValueError):
            evaluator.summarize([item], [result(item), result(item)], "all")
        with self.assertRaises(ValueError):
            evaluator.summarize([item], [{"case_id": "missing"}], "all")
        other = case("b")
        other["group_id"] = "other"
        with self.assertRaises(ValueError):
            evaluator.validate_cases([item, other])

    def test_schema_groups_need_ids_and_clarification_may_carry_candidates(self):
        item = case()
        item["gold"]["ambiguity"] = "clarify"
        evaluator.validate_cases([item])  # candidates of every reading, asked about
        item = case()
        item["gold"]["parts"] = {"groups": [{"label": "brakes"}]}
        with self.assertRaises(ValueError):
            evaluator.validate_cases([item])
        item["gold"]["parts"] = {"groups": [{"acceptable_product_ids": [], "part": "pads"}]}
        evaluator.validate_cases([item])  # the catalogue has no fitting row
        item["gold"]["vehicle"] = {"required_tokens": []}
        with self.assertRaises(ValueError):
            evaluator.validate_cases([item])

    def test_split_stable_at_family_level_and_wilson_not_100_percent(self):
        self.assertEqual(evaluator.split_for("family"), evaluator.split_for("family"))
        lower, upper = evaluator.wilson(10, 10)
        self.assertLess(lower, .98)
        self.assertAlmostEqual(upper, 1)

    def test_malformed_card_cannot_earn_part_success(self):
        item = case()
        payload = turn()
        payload["cards"].append({"product": None})
        scored = evaluator.score_case(item, result(item, [payload]))
        self.assertFalse(scored["parts"])
        self.assertEqual(scored["wrong_offers"], 1)

    def test_malformed_payload_and_paginated_offer_list_fail_closed(self):
        item = case()
        for payload in [turn(cards=5), turn(total=300), "malformed"]:
            with self.subTest(payload=payload):
                self.assertFalse(evaluator.score_case(item, result(item, [payload]))["joint"])

    def test_export_adapter_preserves_unverified_source_and_never_invents_gold(self):
        raw = [{"case_id": "a", "group_id": "g", "source": "public_site_unverified",
                "messages": [{"role": "customer", "content": "invented"},
                             {"role": "shop", "content": "invented reply"}]}]
        prepared = evaluator.prepare_cases(raw)
        self.assertEqual(prepared[0]["source"], "public_site_unverified")
        self.assertEqual(prepared[0]["messages"][0]["role"], "user")
        self.assertEqual(prepared[0]["messages"][1]["role"], "assistant")
        self.assertEqual(prepared[0]["label_status"], "unreviewed")
        self.assertIsNone(prepared[0]["gold"]["parts"])
        summary = evaluator.summarize(prepared, [], "all")
        self.assertEqual(summary["sources"]["real_customer"]["selected"], 0)
        self.assertIsNone(summary["sources"]["public_site_unverified"]["joint"]["rate"])

    def test_earlier_switch_needs_explicit_safety_gold(self):
        item = case()
        item["messages"].append({"role": "user", "content": "invented switch"})
        replay = result(item, [turn(("earlier-part",)), turn()])
        scored = evaluator.score_case(item, replay)
        self.assertTrue(scored["offer_safety_ungraded"])
        self.assertFalse(scored["parts"])
        item["gold"]["parts"]["allowed_earlier_product_ids"] = ["earlier-part"]
        scored = evaluator.score_case(item, result(item, replay["turns"]))
        self.assertTrue(scored["joint"])
        self.assertEqual(scored["wrong_offers"], 0)

    def test_unverified_counterfactual_replay_cannot_earn_resolution(self):
        item = case()
        item["replay_eligible"] = False
        scored = evaluator.score_case(item, result(item))
        self.assertIsNone(scored["joint"])
        self.assertTrue(scored["counterfactual_replay_unverified"])

    def test_point_target_requires_full_joint_coverage(self):
        item = case()
        while evaluator.split_for(item["group_id"]) != "heldout":
            item["group_id"] += "x"
        absent = copy.deepcopy(item)
        absent["case_id"] = "b"
        absent["label_status"] = "unreviewed"
        summary = evaluator.summarize([item, absent], [result(item)], "heldout")
        self.assertEqual(summary["sources"]["real_customer"]["joint"]["rate"], 1)
        self.assertFalse(summary["point_target_met"])

    def test_missing_or_changed_candidate_identity_cannot_pass_the_target(self):
        item = case()
        while evaluator.split_for(item["group_id"]) != "heldout":
            item["group_id"] += "x"
        start = {"record_type": "run_metadata", "source_sha256": "before"}
        end = {"record_type": "run_end", "source_end_sha256": "before"}
        good = evaluator.summarize([item], [start, result(item), end])
        self.assertTrue(good["point_target_met"])
        end["source_end_sha256"] = "changed"
        for records in ([result(item)], [start, result(item)], [start, result(item), end]):
            summary = evaluator.summarize([item], records)
            self.assertFalse(summary["stable_candidate"])
            self.assertFalse(summary["point_target_met"])


class OutcomeTests(unittest.TestCase):
    """The owner's two numbers: right outcome per case and the wrong share of shown rows."""

    def test_a_fitting_row_in_stock_at_the_run_must_be_shown(self):
        item = case()
        stock = {"1": False, "2": True, "3": True}
        shown_sold_out = evaluator.score_case(item, result(item, [turn(("1", "3"))]), stock)
        self.assertFalse(shown_sold_out["outcome"])
        self.assertEqual(shown_sold_out["wrong_rows"], 0)
        good = evaluator.score_case(item, result(item, [turn(("2", "3"))]), stock)
        self.assertTrue(good["outcome"])

    def test_a_part_with_nothing_in_stock_is_right_only_without_a_wrong_row(self):
        item = case()
        stock = {"1": False, "2": False, "3": True}
        self.assertTrue(evaluator.score_case(item, result(item, [turn(("3",))]),
                                             stock)["outcome"])
        substitute = evaluator.score_case(item, result(item, [turn(("3", "9"))]), stock)
        self.assertFalse(substitute["outcome"])
        self.assertEqual((substitute["shown_rows"], substitute["wrong_rows"]), (2, 1))

    def test_another_brand_is_permitted_only_while_the_named_one_is_out(self):
        item = case()
        item["gold"]["parts"]["groups"][0]["alternative_product_ids"] = ["7"]
        named_out = {"1": False, "2": False, "3": True}
        offered = evaluator.score_case(item, result(item, [turn(("3", "7"))]), named_out)
        self.assertTrue(offered["outcome"])
        self.assertEqual(offered["wrong_rows"], 0)
        not_offered = evaluator.score_case(item, result(item, [turn(("3",))]), named_out)
        self.assertTrue(not_offered["outcome"])
        named_in = {"1": True, "2": False, "3": True}
        beside = evaluator.score_case(item, result(item, [turn(("1", "3", "7"))]), named_in)
        self.assertEqual(beside["wrong_rows"], 1)
        item["gold"]["parts"]["groups"][0]["alternative_product_ids"] = ["1"]
        with self.assertRaises(ValueError):
            evaluator.validate_cases([item])

    def test_earlier_turn_rows_count_only_against_an_earlier_label(self):
        item = case()
        item["messages"].append({"role": "user", "content": "invented narrower follow-up"})
        stock = {"1": False, "2": False, "3": True}
        turns = [turn(("8", "9")), turn(("3",))]  # answered the broader first message
        scored = evaluator.score_case(item, result(item, turns), stock)
        self.assertTrue(scored["outcome"])
        self.assertEqual(scored["wrong_rows"], 0)
        item["gold"]["parts"]["allowed_earlier_product_ids"] = ["8"]
        scored = evaluator.score_case(item, result(item, turns), stock)
        self.assertEqual(scored["wrong_rows"], 1)

    def test_an_asked_question_settles_an_unclear_request_with_check_rows(self):
        item = case()
        item["gold"]["ambiguity"] = "clarify"
        asked = turn(("1",), question={"text": "Which engine?"})
        scored = evaluator.score_case(item, result(item, [asked]), {"1": True, "3": True})
        self.assertTrue(scored["outcome"])
        silent = evaluator.score_case(item, result(item, [turn(("1",))]),
                                      {"1": True, "3": True})
        self.assertFalse(silent["outcome"])

    def test_vehicle_tokens_match_any_spelling_of_the_label(self):
        item = case()
        item["gold"]["vehicle"] = {"required_tokens": ["camry", "2015"]}
        scored = evaluator.score_case(item, result(item, [turn(
            car_label="Toyota Camry XV50 2015 2.5")]))
        self.assertTrue(scored["vehicle"])
        scored = evaluator.score_case(item, result(item, [turn(car_label="Toyota Camry")]))
        self.assertFalse(scored["vehicle"])

    def test_target_needs_outcome_rate_and_a_wrong_row_share_under_one_percent(self):
        item = case()
        while evaluator.split_for(item["group_id"]) != "heldout":
            item["group_id"] += "x"
        start = {"record_type": "run_metadata", "source_sha256": "same",
                 "gold_stock": {"1": True, "2": True, "3": True}}
        end = {"record_type": "run_end", "source_end_sha256": "same"}
        good = evaluator.summarize([item], [start, result(item), end])
        self.assertTrue(good["point_target_met"])
        self.assertEqual(good["sources"]["real_customer"]["rows"],
                         {"shown": 2, "wrong": 0, "wrong_rate": 0.0})
        wrong = evaluator.summarize([item], [start, result(item, [turn(("1", "3", "9"))]),
                                             end])
        self.assertEqual(wrong["sources"]["real_customer"]["outcome"]["rate"], 1)
        self.assertFalse(wrong["point_target_met"])


class CustomerReplayTests(unittest.TestCase):
    def test_only_historical_customer_messages_are_replayed_and_deleted(self):
        item = case()
        item["messages"].insert(0, {"role": "assistant", "content": "old human shop reply"})
        store = Mock()
        store.create.return_value = {"conversation_id": "new"}
        store.submit.return_value = {"turn_id": "t"}
        store.get.return_value = turn()
        record = evaluator.replay_case(store, item, max_turns=2, turn_seconds=1,
                                       deadline=float("inf"))
        store.submit.assert_called_once_with("new", "invented", language="auto")
        store.delete.assert_called_once_with("new")
        self.assertTrue(evaluator.score_case(item, record)["joint"])

    def test_bounds_and_exception_stop_without_claiming_success(self):
        item = case()
        store = Mock()
        store.create.return_value = {"conversation_id": "new"}
        record = evaluator.replay_case(store, item, max_turns=0, turn_seconds=1,
                                       deadline=float("inf"))
        store.submit.assert_not_called()
        self.assertFalse(evaluator.score_case(item, record)["joint"])
        store.submit.side_effect = RuntimeError("private text must not escape")
        record = evaluator.replay_case(store, item, max_turns=1, turn_seconds=1,
                                       deadline=float("inf"))
        self.assertEqual(record["status"], "error")
        self.assertNotIn("private", str(record))

    def test_private_paths_cannot_write_customer_content_outside_ignored_data(self):
        with self.assertRaises(ValueError):
            evaluator.private_path("tests/customer.jsonl")

    def test_gold_change_invalidates_prior_results(self):
        item = case()
        old = result(item)
        changed = copy.deepcopy(item)
        changed["messages"][0]["content"] = "different invented message"
        self.assertFalse(evaluator.score_case(changed, old)["complete"])

    def test_attachment_is_preserved_unsupported_and_ungraded_without_submit(self):
        item = evaluator.prepare_cases([{"case_id": "a", "group_id": "g",
                                        "source": "real_customer", "messages": [
                                            {"role": "customer", "content": ""}]}])[0]
        self.assertEqual(item["messages"][0]["kind"], "attachment")
        store = Mock()
        replay = evaluator.replay_case(store, item, max_turns=2, turn_seconds=1,
                                       deadline=float("inf"))
        store.create.assert_not_called()
        self.assertEqual(replay["status"], "unsupported")
        self.assertIsNone(evaluator.score_case(item, replay)["joint"])

    def test_private_output_is_exclusive_and_owner_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.jsonl"
            with evaluator.private_output(path) as handle:
                handle.write("private")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                evaluator.private_output(path)

    def bench_run(self, limited_at=None):
        """One subscription-bench run of two invented cases against local fakes."""
        import json

        items = [case("a"), case("b")]
        store = Mock()
        store.turn_seconds = 75.0
        store.create.return_value = {"conversation_id": "new"}
        store.submit.return_value = {"turn_id": "t"}
        store.get.return_value = turn(usage={"model_requests": 1})
        store.chat_service.settings = SimpleNamespace(assistant_max_requests=4)
        catalog = SimpleNamespace(rows=lambda: [], guide_version="v")
        client = Mock(limited_at=limited_at)
        ledger = Mock()
        ledger.total.return_value = 999.0  # spent: an api run would refuse to start
        with tempfile.TemporaryDirectory() as folder:
            cases_path = Path(folder) / "cases.jsonl"
            cases_path.write_text("\n".join(json.dumps(i) for i in items))
            output = Path(folder) / "results.jsonl"
            args = SimpleNamespace(
                split=evaluator.split_for(items[0]["group_id"]), report=None, budget=1,
                runtime="subscription", parallel=2, model=None, audience="public",
                cases=str(cases_path), limit=10, max_turns=1, max_requests=50,
                max_seconds=60, turn_seconds=1)
            with patch("automotive.assistant_bench.SubscriptionClient",
                       return_value=client), \
                    patch("automotive.assistant_eval.live",
                          return_value=(None, store, catalog, "model")) as live, \
                    patch("automotive.assistant_eval.Ledger", return_value=ledger), \
                    patch.object(evaluator, "private_path", side_effect=Path):
                evaluator.run_live(args, items, output)
            records = [json.loads(line) for line in output.read_text().splitlines()]
        return records, store, client, ledger, live

    def test_bench_run_bills_nothing_and_hands_the_bench_client_to_the_app(self):
        records, store, client, ledger, live = self.bench_run()
        self.assertIs(live.call_args.kwargs["client"], client)
        ledger.append.assert_not_called()
        self.assertEqual(records[0]["runtime"], "subscription")
        # The fake catalogue has no rows: every gold id is recorded as not in stock.
        self.assertEqual(records[0]["gold_stock"], {"1": False, "2": False, "3": False})
        cases = [r for r in records if "case_id" in r]
        self.assertEqual(sorted(r["case_id"] for r in cases), ["a", "b"])
        self.assertTrue(all(r["status"] == "complete" for r in cases))
        client.close.assert_called_once()

    def test_a_subscription_refusal_stops_every_further_turn(self):
        records, store, client, ledger, live = self.bench_run(limited_at=1.0)
        store.submit.assert_not_called()
        self.assertEqual({r["status"] for r in records if "case_id" in r}, {"bounded"})

    def test_empty_selection_or_existing_artifact_never_starts_live_model(self):
        item = case()
        args = SimpleNamespace(split="heldout", report=None, budget=5)
        while evaluator.split_for(item["group_id"]) == "heldout":
            item["group_id"] += "x"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "results.jsonl"
            with patch("automotive.assistant_eval.live") as live:
                with self.assertRaises(ValueError):
                    evaluator.run_live(args, [item], output)
                args.split = evaluator.split_for(item["group_id"])
                output.touch()
                with self.assertRaises(ValueError):
                    evaluator.run_live(args, [item], output)
                live.assert_not_called()
            output.unlink()
            report = Path(folder) / "report.json"
            report.touch()
            args.report = str(report)
            with patch("automotive.assistant_eval.live") as live, \
                    patch.object(evaluator, "private_path", side_effect=Path):
                with self.assertRaises(ValueError):
                    evaluator.run_live(args, [item], output)
                live.assert_not_called()


def act_case(kind="greet", facts=None, name="act"):
    item = case(name)
    act = {"type": kind, **({"facts": facts} if facts is not None else {})}
    item["gold"] = {"kind": "act", "vehicle": None, "parts": None, "ambiguity": "unknown",
                    "act": act}
    return item


def seg(item, pool="measure", segment=1):
    item["annotation"] = {"pool": pool, "segment": segment}
    return item


INVENTED_FACTS = {"address": ["Invented Street 10, Tbilisi"], "phone": ["+995 555 12 34 56"],
                  "hours": ["10:00 - 19:00"], "email": []}


class ProtocolV3Tests(unittest.TestCase):
    """Segments with shop history, acts, the clarify-fact check and pooled runs."""

    def test_history_is_seeded_the_way_the_worker_records_staff_replies(self):
        history = [{"role": "user", "content": "invented pads request", "kind": "text"},
                   {"role": "assistant", "content": "invented: front or rear?", "kind": "text"},
                   {"role": "user", "content": "[NON_TEXT_ATTACHMENT]", "kind": "attachment"},
                   {"role": "user", "content": "invented front", "kind": "text"},
                   {"role": "assistant", "content": "invented: we have them", "kind": "text"},
                   {"role": "assistant", "content": "[NON_TEXT_ATTACHMENT]",
                    "kind": "attachment"}]
        state, waiting = evaluator.seeded_state(history)
        self.assertEqual(waiting, [])
        self.assertEqual([(t["user"], t["assistant"]) for t in state["memory"]["transcript"]],
                         [("invented pads request", "invented: front or rear?"),
                          ("invented front", "invented: we have them")])
        item = case()
        item["history"] = history
        store = Mock()
        store.restore.return_value = "seeded"
        store.submit.return_value = {"turn_id": "t"}
        store.get.return_value = turn()
        record = evaluator.replay_case(store, item, max_turns=2, turn_seconds=1,
                                       deadline=float("inf"))
        store.create.assert_not_called()
        self.assertEqual(store.restore.call_args.args[0]["memory"], state["memory"])
        store.submit.assert_called_once_with("seeded", "invented", language="auto")
        store.delete.assert_called_once_with("seeded")
        self.assertEqual(record["status"], "complete")

    def test_acts_are_graded_from_structured_fields(self):
        greet = act_case("greet")
        asked = turn((), question={"text": "How can we help?", "options": ["Oil", "Pads"]})
        self.assertTrue(evaluator.score_case(greet, result(greet, [asked]))["act"])
        self.assertFalse(evaluator.score_case(greet, result(greet, [turn(("1",))]))["act"])
        self.assertFalse(evaluator.score_case(greet, result(greet, [turn(())]))["act"])
        close = act_case("close")
        self.assertTrue(evaluator.score_case(close, result(close, [turn(())]))["act"])
        visit = act_case("visit")
        self.assertTrue(evaluator.score_case(visit, result(visit, [turn((), staff={
            "intent": "visit"})]))["act"])
        self.assertFalse(evaluator.score_case(visit, result(visit, [turn((), staff={
            "intent": "service"})]))["act"])
        where = act_case("shop_fact", ["address", "hours"])
        said = turn((), answer="We are at Invented Street 10, open 10:00 to 19:00.")
        self.assertTrue(evaluator.score_case(where, result(where, [said]),
                                             facts=INVENTED_FACTS)["act"])
        half = turn((), answer="We are at Invented Street 10.")
        self.assertFalse(evaluator.score_case(where, result(where, [half]),
                                              facts=INVENTED_FACTS)["act"])
        self.assertFalse(evaluator.score_case(where, result(where, [said]))["act"])
        mail = act_case("shop_fact", ["email"])
        self.assertTrue(evaluator.score_case(mail, result(mail, [turn((), answer="Write us.")]),
                                             facts=INVENTED_FACTS)["act"])
        self.assertIsNone(evaluator.score_case(greet, result(greet, [asked]))["outcome"])

    def test_a_question_must_ask_for_the_labeled_fact(self):
        item = case()
        item["gold"]["ambiguity"] = "clarify"
        item["gold"]["clarify_fact"] = "engine"
        question = {"text": "Which engine?", "options": ["1.8", "2.5"]}
        record = result(item, [turn((), question=question)])
        stock = {"1": True, "3": True}
        self.assertFalse(evaluator.score_case(item, record, stock)["outcome"])
        check = {"case_id": item["case_id"], "case_sha256": evaluator.case_digest(item),
                 "question_sha256": evaluator.question_digest(question), "asks": True}
        scored = evaluator.score_case(item, record, stock, questions={item["case_id"]: check})
        self.assertTrue(scored["outcome"])
        self.assertTrue(scored["asks_fact"])
        wrong = {**check, "asks": False}
        self.assertFalse(evaluator.score_case(item, record, stock,
                                              questions={item["case_id"]: wrong})["outcome"])
        other = {**check, "question_sha256": evaluator.question_digest({"text": "Year?"})}
        self.assertFalse(evaluator.score_case(item, record, stock,
                                              questions={item["case_id"]: other})["outcome"])

    def test_v3_gold_is_validated(self):
        evaluator.validate_cases([act_case("shop_fact", ["address"])])
        broken = [act_case("shop_fact"), act_case("unknown"), act_case("greet", ["address"])]
        parts_and_act = act_case("greet")
        parts_and_act["gold"]["parts"] = case()["gold"]["parts"]
        resolved_fact = case()
        resolved_fact["gold"]["clarify_fact"] = "engine"
        bad_history = case()
        bad_history["history"] = [{"role": "shop", "content": "x"}]
        for item in [*broken, parts_and_act, resolved_fact, bad_history]:
            with self.assertRaises(ValueError):
                evaluator.validate_cases([item])

    def test_pools_select_cases_and_numbers_keep_segments_apart(self):
        first = seg(case("p1"), segment=1)
        later = seg(case("p2"), segment=2)
        tuned = seg(case("p3"), pool="tune")
        greet = seg(act_case("greet", name="g1"), segment=2)
        cases = [first, later, tuned, greet]
        self.assertEqual([c["case_id"] for c in evaluator.selected_cases(cases, pool="measure")],
                         ["p1", "p2", "g1"])
        start = {"record_type": "run_metadata", "source_sha256": "s", "prompt_sha256": "p",
                 "catalog_sha256": "c", "gold_stock": {"1": True, "2": True, "3": True}}
        end = {"record_type": "run_end", "source_end_sha256": "s"}
        asked = turn((), question={"text": "How can we help?", "options": ["Oil", "Pads"]})
        rows = [start, result(first), result(later, [turn(("1", "3", "9"))]),
                result(greet, [asked]), end]
        report = evaluator.summarize(cases, rows, pool="measure")
        numbers = report["v3"]
        # A right row beside a wrong one is a right outcome: the wrong row costs precision.
        self.assertEqual((numbers["parts_outcome"]["passed"], numbers["parts_outcome"]["graded"]),
                         (2, 2))
        self.assertEqual(numbers["row_precision"], {"shown": 5, "wrong": 1, "rate": 0.8})
        self.assertEqual(numbers["act"]["passed"], 1)
        self.assertEqual(numbers["first_segments"]["parts_outcome"]["graded"], 1)
        self.assertEqual(numbers["later_segments"]["row_precision"]["wrong"], 1)
        self.assertTrue(numbers["targets_met"]["parts_outcome"])
        self.assertFalse(numbers["targets_met"]["row_precision"])
        self.assertTrue(numbers["targets_met"]["act"])

    def test_pool_report_needs_three_runs_of_one_candidate(self):
        item = seg(case("p1"))
        greet = seg(act_case("greet", name="g1"))
        cases = [item, greet]
        start = {"record_type": "run_metadata", "source_sha256": "s", "prompt_sha256": "p",
                 "catalog_sha256": "c", "gold_stock": {"1": True, "2": True, "3": True}}
        end = {"record_type": "run_end", "source_end_sha256": "s"}
        asked = turn((), question={"text": "How can we help?", "options": ["Oil", "Pads"]})

        def write(path, first):
            path.write_text("\n".join(evaluator.json.dumps(r) for r in (
                first, result(item), result(greet, [asked]), end)) + "\n")

        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for number in range(3):
                path = Path(folder) / f"run{number}.jsonl"
                write(path, start)
                paths.append(path)
            report = evaluator.pool_report(cases, paths, "measure")
            self.assertTrue(report["headline_met"])
            self.assertEqual(report["pooled"]["parts_outcome"]["graded"], 3)
            self.assertFalse(evaluator.pool_report(cases, paths[:2], "measure")["headline_met"])
            write(paths[2], dict(start, prompt_sha256="changed"))
            report = evaluator.pool_report(cases, paths, "measure")
            self.assertFalse(report["one_candidate"])
            self.assertFalse(report["headline_met"])


if __name__ == "__main__":
    unittest.main()
