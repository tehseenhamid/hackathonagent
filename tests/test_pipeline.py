import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from planledger import pipeline
from planledger.store import Store
from tests.support import (PACK_A, SCRIPT_A, ScriptedAdapter, UPD_GENUINE, UPD_GENUINE_SCRIPT, UPD_IMPOSSIBLE,
                           UPD_IMPOSSIBLE_SCRIPT, UPD_OUTSIDER, UPD_OUTSIDER_SCRIPT, UPD_WRONG, UPD_WRONG_SCRIPT,
                           make_cfg, tmpdir, write)


def run(cfg, path, adapter):
    buf = []
    with mock.patch.object(pipeline, "make_adapter", lambda c: adapter):
        code = pipeline.run_ingest(str(path), cfg, out=buf.append)
    return code, "\n".join(buf)


ROUTES = [("<<<PACK kind=baseline>>>", SCRIPT_A), ("banners bill is Rs 22,000", UPD_GENUINE_SCRIPT),
          ("trimmed your order to 90 boxes", UPD_WRONG_SCRIPT), ("must have a projector", UPD_IMPOSSIBLE_SCRIPT),
          ("cancel the prayer break", UPD_OUTSIDER_SCRIPT)]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.td = tmpdir()
        self.tmp = Path(self.td.name)
        self.cfg = make_cfg(self.tmp / "data")
        self.pack = write(self.tmp, "pack.txt", PACK_A)
        self.ad = ScriptedAdapter(ROUTES)

    def tearDown(self):
        self.td.cleanup()

    def baseline(self):
        code, out = run(self.cfg, self.pack, self.ad)
        self.assertEqual(code, 0, out)
        return out

    def test_baseline_valid_plan_and_exact_numbers(self):
        out = self.baseline()
        st = Store(self.cfg.data_dir)
        rev = st.latest_revision()
        p = rev["plan"]
        self.assertEqual(rev["grade"], "valid", out)
        self.assertEqual(p["event"]["teams"], 30)
        self.assertEqual(p["judging"]["min_needed"], 8)
        self.assertEqual(p["judging"]["deployed"], 8)
        self.assertEqual(p["food"]["fed"], 120 + 8 + 8)
        self.assertEqual(p["venue"]["name"], "hall x")
        # 60000 + 136*700 + 8*4000 + 15000 + 30000
        self.assertEqual(p["budget"]["total"], 60000 + 136 * 700 + 8 * 4000 + 45000)
        self.assertEqual(p["budget"]["remaining"], 500000 - p["budget"]["total"])
        self.assertIn("fireworks", [x["label"] for x in p["budget"]["excluded"]])
        self.assertEqual(rev["checks"]["failed"], [])

    def test_source_bytes_stored_exactly(self):
        self.baseline()
        st = Store(self.cfg.data_dir)
        self.assertEqual(st.source_raw("S1"), self.pack.read_bytes())

    def test_prompt_contains_whole_file_verbatim(self):
        self.baseline()
        self.assertIn(PACK_A, self.ad.calls[0]["user"])

    def test_trace_totals_reconcile_with_adapter(self):
        out = self.baseline()
        want_total = sum(110 * (i + 1) for i in range(len(self.ad.calls)))
        self.assertIn(f"TOTAL TOKENS {want_total:,}", out)
        self.assertIn("MODEL CALL #1", out)
        self.assertIn("area tag: reading", out)
        self.assertNotIn("sk-test-secret-123", out)

    def test_genuine_update_accepted_only_reading_calls(self):
        self.baseline()
        p2 = write(self.tmp, "u1.txt", UPD_GENUINE)
        code, out = run(self.cfg, p2, self.ad)
        self.assertEqual(code, 0, out)
        st = Store(self.cfg.data_dir)
        self.assertEqual(st.latest_revision()["rev_no"], 2)
        b1, b2 = st.get_revision(1)["plan"]["budget"], st.get_revision(2)["plan"]["budget"]
        self.assertEqual(b2["total"], b1["total"] + 7000)
        d = st.get_revision(2)["delta"]
        self.assertEqual(d["called_areas"], ["reading"])
        self.assertIn("budget", d["recomputed_areas"])
        self.assertEqual(d["changed_areas"], ["budget"])
        self.assertLessEqual(self.ad.calls.__len__(), 2)

    def test_wrong_update_rejected_and_plan_retained(self):
        self.baseline()
        st = Store(self.cfg.data_dir)
        before = json.dumps(st.latest_revision()["plan"], sort_keys=True)
        code, out = run(self.cfg, write(self.tmp, "w.txt", UPD_WRONG), self.ad)
        self.assertEqual(code, 0, out)
        st = Store(self.cfg.data_dir)
        self.assertEqual(st.latest_revision()["rev_no"], 1)
        self.assertEqual(json.dumps(st.latest_revision()["plan"], sort_keys=True), before)
        r = st.runs()[-1]
        self.assertEqual(r["outcome"], "update_rejected")
        self.assertEqual(r["active_rev"], 1)
        self.assertIn("Food covers everyone", out)   # the reason names the violated rule
        s = [x for x in st.all_statements() if x["key"] == "food.order_qty"][0]
        self.assertEqual(s["disposition"], "rejected")
        self.assertIn("90", s["reason"] + str(s["val"]))

    def test_out_of_scope_sender_rejected(self):
        self.baseline()
        code, out = run(self.cfg, write(self.tmp, "o.txt", UPD_OUTSIDER), self.ad)
        st = Store(self.cfg.data_dir)
        s = [x for x in st.all_statements() if x["key"] == "event.break_min" and x["run_no"] == 2][0]
        self.assertEqual(s["disposition"], "rejected")
        self.assertIn("no authority", s["reason"])
        self.assertEqual(st.latest_revision()["rev_no"], 1)

    def test_impossible_authoritative_requirement_is_blocked_not_applied(self):
        self.baseline()
        code, out = run(self.cfg, write(self.tmp, "i.txt", UPD_IMPOSSIBLE), self.ad)
        st = Store(self.cfg.data_dir)
        self.assertEqual(st.latest_revision()["rev_no"], 1)
        self.assertEqual(st.runs()[-1]["outcome"], "update_blocked")
        s = [x for x in st.all_statements() if x["key"] == "venue.require"][0]
        self.assertEqual(s["disposition"], "needs_clarification")

    def test_duplicate_file_is_safe_and_logged(self):
        self.baseline()
        n = len(self.ad.calls)
        code, out = run(self.cfg, self.pack, self.ad)
        self.assertEqual(code, 0)
        self.assertIn("DUPLICATE", out)
        self.assertEqual(len(self.ad.calls), n)
        st = Store(self.cfg.data_dir)
        self.assertEqual(st.latest_revision()["rev_no"], 1)
        self.assertEqual(st.runs()[-1]["outcome"], "duplicate_ignored")

    def test_invalid_utf8_logged_not_trimmed(self):
        p = self.tmp / "bad.txt"
        p.write_bytes(b"hello \xff\xfe world")
        code, out = run(self.cfg, p, self.ad)
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", out)
        self.assertEqual(len(self.ad.calls), 0)

    def test_unconfigured_provider_is_an_honest_failure(self):
        cfg = make_cfg(self.tmp / "d2", PLANLEDGER_PROVIDER="")
        code, out = run(cfg, self.pack, self.ad)
        self.assertEqual(code, 1)
        self.assertIn("not configured", out)
        self.assertIsNone(Store(cfg.data_dir).latest_revision())

    def test_timeout_leaves_state_untouched_and_counts_the_attempt(self):
        self.baseline()
        slow = ScriptedAdapter(ROUTES, delay=7.0)
        cfg = make_cfg(self.cfg.data_dir, PLANLEDGER_UPDATE_BUDGET_S=8, PLANLEDGER_RESERVE_S=4)
        code, out = run(cfg, write(self.tmp, "u1.txt", UPD_GENUINE), slow)
        self.assertEqual(code, 1)
        st = Store(cfg.data_dir)
        self.assertEqual(st.latest_revision()["rev_no"], 1)
        self.assertEqual(st.runs()[-1]["outcome"], "failed_timeout")
        calls = [i for i in st.trace_items() if i["kind"] == "call"]
        self.assertTrue(any(c["outcome"] == "timeout" for c in calls))

    def test_malformed_output_triggers_one_counted_repair(self):
        bad_then_good = ScriptedAdapter([("<<<PACK", "this is not json")])
        calls = {"n": 0}
        orig = bad_then_good.complete

        def seq(system, user, t, m, j):
            calls["n"] += 1
            if calls["n"] == 1:
                return orig(system, user, t, m, j)
            bad_then_good.routes = [("<<<PACK", SCRIPT_A)]
            return orig(system, user, t, m, j)
        bad_then_good.complete = seq
        code, out = run(self.cfg, self.pack, bad_then_good)
        self.assertEqual(code, 0, out)
        st = Store(self.cfg.data_dir)
        cs = [i for i in st.trace_items() if i["kind"] == "call"]
        self.assertEqual(len(cs), 2)
        self.assertEqual(cs[1]["attempt_of"], cs[0]["call_no"])
        self.assertIn("schema_failure", out)

    def test_call_numbers_increase_across_runs_and_restart(self):
        self.baseline()
        run(self.cfg, write(self.tmp, "u1.txt", UPD_GENUINE), self.ad)
        nums = [i["call_no"] for i in Store(self.cfg.data_dir).trace_items() if i["kind"] == "call"]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(len(set(nums)), len(nums))
        self.assertEqual(nums[-1], len(nums))

    def test_missing_usage_is_unavailable_and_total_partial(self):
        ad = ScriptedAdapter(ROUTES, usage=False)
        code, out = run(self.cfg, self.pack, ad)
        self.assertEqual(code, 0, out)
        self.assertIn("tokens: unavailable", out)
        self.assertIn("PARTIAL", out)

    def test_export_contains_plan_trace_and_totals(self):
        self.baseline()
        run(self.cfg, write(self.tmp, "w.txt", UPD_WRONG), self.ad)
        txt = (self.cfg.data_dir / "exports" / "final_plan_and_trace.txt").read_text(encoding="utf-8")
        for needle in ("FINAL PLAN AND FULL TRACE", "PLANLEDGER PLAN - revision 1", "REJECTED", "SESSION TOTAL TOKENS", "MODEL CALL #2"):
            self.assertIn(needle, txt)

    def test_restart_preserves_state(self):
        self.baseline()
        st = Store(self.cfg.data_dir)
        st.close()
        st2 = Store(self.cfg.data_dir)
        self.assertEqual(st2.latest_revision()["rev_no"], 1)
        self.assertTrue(st2.has_committed_revision())


if __name__ == "__main__":
    unittest.main()
