import json
import unittest
from pathlib import Path
from unittest import mock

from planledger import pipeline, reader
from tests.support import ScriptedAdapter, UPD_GENUINE, UPD_GENUINE_SCRIPT, make_cfg, tmpdir, write


def stmt_obj(quote, status):
    return {"messages": [{"id": "1", "from": "Someone", "role": "caterer", "stmts": [
        {"key": "food.order_qty", "val": 100, "status": status, "quote": quote}]}]}


class HedgeTests(unittest.TestCase):
    def test_plain_statement_labelled_tentative_is_treated_as_firm(self):
        text = "Vendor: we reduced the order to 100 boxes to save cost."
        _, st, _ = reader.extract(stmt_obj("we reduced the order to 100 boxes", "tentative"), text, 1, "S1", True)
        self.assertEqual(st[0]["status"], "confirmed")
        self.assertIn("no hedging wording", st[0]["note"])

    def test_real_hedge_stays_tentative(self):
        for text in ("Vendor: we may reduce the order to 100 boxes.", "Vendor: shayad order 100 boxes ho jaye."):
            q = text.split(": ", 1)[1].rstrip(".")
            _, st, _ = reader.extract(stmt_obj(q, "tentative"), text, 1, "S1", True)
            self.assertEqual(st[0]["status"], "tentative", text)

    def test_compact_output_without_optional_fields_parses(self):
        raw = '{"messages":[{"id":"1","from":"Finance","role":"finance","stmts":[{"key":"cost.commitment","ent":"printing","val":{"amt":15000},"quote":"came in at Rs 15,000"}]},{"id":"2","from":"A","irrelevant":"traffic note"}]}'
        obj, err = reader.parse_reader_output(raw)
        self.assertIsNone(err)
        msgs, st, issues = reader.extract(obj, "Finance: printing came in at Rs 15,000.", 1, "S1", True)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(st[0]["status"], "confirmed")
        self.assertEqual(issues, [])


class IncompleteBaselineTests(unittest.TestCase):
    def test_unknowns_are_written_as_unknown_not_none(self):
        td = tmpdir()
        tmp = Path(td.name)
        cfg = make_cfg(tmp / "data")
        buf = []
        with mock.patch.object(pipeline, "make_adapter", lambda c: ScriptedAdapter([("<<<PACK", UPD_GENUINE_SCRIPT)])):
            pipeline.run_ingest(str(write(tmp, "p.txt", UPD_GENUINE)), cfg, out=buf.append)
        out = "\n".join(buf)
        self.assertIn("PROVISIONAL", out)
        self.assertNotIn("None", out)
        td.cleanup()


if __name__ == "__main__":
    unittest.main()


class ReaderGuardTests(unittest.TestCase):
    def test_recover_quote_finds_sentence_with_numbers(self):
        from planledger import reader
        text = "Hello team. Hall A seats 200 and costs Rs 120,000. Bye."
        loc = reader.recover_quote(text, {"cap": 200, "price": 120000}, "Hall A")
        self.assertIsNotNone(loc)
        self.assertIn("seats 200", text[loc[0]:loc[1]])

    def test_derive_ent_from_quote(self):
        from planledger import reader
        self.assertEqual(reader.derive_ent("venue.option", "Hall B seats 180 and costs Rs 150,000."), "hall b")
        self.assertEqual(reader.derive_ent("event.break_min", "A 15 minute prayer break is needed."), "prayer")

    def test_hours_become_minutes(self):
        from planledger import reader
        self.assertIn(240, reader._numbers_in("The event is 4 hours long."))

    def test_unsupported_numbers_removed(self):
        from planledger import reader
        out = reader._strip_unsupported("venue.option", {"cap": 200, "price": 120000, "open": 9}, "Hall A seats 200 at Rs 120,000.")
        self.assertNotIn("open", out if isinstance(out, dict) else {})


class CompletenessTests(unittest.TestCase):
    def test_group_statement_is_split(self):
        from planledger import reader
        q = "Hall A seats 200 at Rs 120,000."
        out = reader.expand_groups([{"quote": q, "key": "venue", "ent": "Hall A", "val": {"cap": 200, "rent": 120000}}], q)
        self.assertEqual([x["key"] for x in out], ["venue.option"])
        self.assertEqual(out[0]["val"]["cap"], 200)

    def test_missed_item_in_a_list_is_reported(self):
        from planledger import reader
        text = "Already ordered: prizes Rs 100,000, internet Rs 20,000, printing Rs 10,000."
        stmts = [{"key": "cost.commitment", "ent": "prizes", "val": {"amt": 100000, "for": "other"}, "span": [0, len(text)], "msg_idx": 1}]
        gaps = reader.missed_numbers(text, stmts, [])
        self.assertEqual(len(gaps), 1)
        self.assertIn("20000", gaps[0])

    def test_old_value_in_a_correction_is_not_a_gap(self):
        from planledger import reader
        text = "The printing bill is Rs 15,000, not Rs 10,000."
        stmts = [{"key": "cost.commitment", "ent": "printing", "val": {"amt": 15000, "for": "other"}, "span": [0, len(text)], "msg_idx": 1}]
        self.assertEqual(reader.missed_numbers(text, stmts, []), [])


class CorrectionTests(unittest.TestCase):
    def test_corrected_amount_is_filed_on_the_existing_commitment(self):
        from planledger import reader
        text = "The printing bill came in at Rs 15,000, not Rs 10,000."
        st = [{"key": "cost.fee", "ent": "printing", "val": {"amt": 15000, "for": "other"}, "span": [0, len(text)], "note": "", "status": "tentative"}]
        prior = {"cost.commitment": {"printing": {"v": {"amt": 10000, "for": "other"}}}}
        self.assertEqual(reader.apply_corrections(st, text, prior), 1)
        self.assertEqual(st[0]["key"], "cost.commitment")
        self.assertEqual(st[0]["val"]["amt"], 15000)

    def test_unrelated_fee_is_left_alone(self):
        from planledger import reader
        text = "A banner fee of Rs 5,000 applies."
        st = [{"key": "cost.fee", "ent": "banner", "val": {"amt": 5000, "for": "other"}, "span": [0, len(text)], "note": ""}]
        prior = {"cost.commitment": {"printing": {"v": {"amt": 10000, "for": "other"}}}}
        self.assertEqual(reader.apply_corrections(st, text, prior), 0)


class SplicedAnswerTests(unittest.TestCase):
    def test_message_spliced_beside_the_list_is_put_back(self):
        from planledger import reader
        raw = '{"messages":[{"id":"1","from":"A","stmts":[]},"{"],"id":"2","from":"B","role":"venue","stmts":[{"key":"info","quote":"x"}]}'
        obj, err = reader.parse_reader_output(raw)
        self.assertIsNone(err)
        self.assertEqual([m["id"] for m in obj["messages"]], ["1", "2"])

    def test_scalar_event_value_is_mapped_by_its_label(self):
        from planledger import reader
        out = reader.expand_groups([{"quote": "Eight staff will be present.", "key": "event", "ent": "staff", "val": 8}], "Eight staff will be present.")
        self.assertEqual(out[0]["key"], "event.staff")


class CostAreaTests(unittest.TestCase):
    def test_guessed_area_label_is_dropped_when_the_sentence_does_not_say_it(self):
        from planledger import reader
        s = "Already ordered from the event budget: prizes Rs 100,000, backup internet Rs 20,000, printing Rs 10,000."
        for ent in ("prizes", "backup internet", "printing"):
            val, changed = reader.check_cost_for({"amt": 1, "for": "venue"}, s, ent)
            self.assertEqual(val["for"], "other")
            self.assertTrue(changed)

    def test_real_area_label_is_kept(self):
        from planledger import reader
        val, changed = reader.check_cost_for({"amt": 1, "for": "venue"}, "Hall A rent Rs 120,000 already paid.", "Hall A rent")
        self.assertEqual(val["for"], "venue")
        self.assertFalse(changed)

    def test_message_ids_with_and_without_hash_are_the_same_message(self):
        from planledger import reader
        base = {"messages": [{"id": "2", "from": "Hall", "stmts": [{"key": "venue.option", "ent": "A", "val": {"cap": 1, "rent": 2}, "quote": "x"}]}]}
        extra = {"messages": [{"id": "#2", "from": "Hall", "stmts": [{"key": "venue.option", "ent": "B", "val": {"cap": 1, "rent": 2}, "quote": "y"}]}]}
        out = reader.merge_gap_fill(base, extra, "x y")
        self.assertEqual(len(out["messages"]), 1)
        self.assertEqual(len(out["messages"][0]["stmts"]), 2)
