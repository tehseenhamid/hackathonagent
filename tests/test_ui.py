import json
import re
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from planledger import chat, pipeline, web
from planledger.store import Store
from planledger.viewmodel import build_state
from tests.support import (PACK_A, ScriptedAdapter, UPD_GENUINE, UPD_WRONG, make_cfg, tmpdir, write)
from tests.test_pipeline import ROUTES

STATIC = Path(web.__file__).parent / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def lum(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def theme_vars():
    themes = {}
    for m in re.finditer(r'\[data-theme="(\w+)"\]\s*\{([^}]*)\}', CSS):
        themes[m.group(1)] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(2)))
    acc = {}
    for m in re.finditer(r'\[data-theme="(\w+)"\]\[data-accent="(\w+)"\]\s*\{\s*--accent:\s*(#[0-9a-fA-F]{6})', CSS):
        acc[(m.group(1), m.group(2))] = m.group(3)
    return themes, acc


class ContrastTests(unittest.TestCase):
    def test_all_theme_accent_pairs_meet_aa(self):
        themes, acc = theme_vars()
        self.assertEqual(set(themes), {"light", "dark", "eye"})
        self.assertEqual(len(acc), 12)
        for (theme, name), a in acc.items():
            v = themes[theme]
            for bg in ("bg", "surface", "surface2"):
                self.assertGreaterEqual(ratio(a, v[bg]), 4.5, f"accent {name} on {bg} in {theme}")
            self.assertGreaterEqual(ratio(v["on-accent"], a), 4.5, f"on-accent text on accent {name} in {theme}")
        for theme, v in themes.items():
            for fg in ("text", "muted", "ok", "warn", "bad"):
                for bg in ("bg", "surface", "surface2"):
                    self.assertGreaterEqual(ratio(v[fg], v[bg]), 4.5, f"{fg} on {bg} in {theme}")

    def test_fonts_and_no_glyphs(self):
        self.assertIn('"Times New Roman"', CSS)
        heads = re.findall(r"([^{}]+)\{[^}]*Times New Roman", CSS)
        self.assertTrue(all(set(re.findall(r"[a-z0-9]+", h)) <= {"h1", "h2", "h3"} for h in heads), heads)
        self.assertIn("prefers-reduced-motion", CSS)
        bad = re.compile("[←-⇿▶►▼▲•✓✔✕×➡⬅-⬇\U0001F300-\U0001FAFF☀-➿]")
        for f in ("index.html", "app.css", "app.js"):
            self.assertIsNone(bad.search((STATIC / f).read_text(encoding="utf-8")), f)
        self.assertNotIn("<svg", (STATIC / "index.html").read_text(encoding="utf-8").lower())
        self.assertNotIn("innerHTML", (STATIC / "app.js").read_text(encoding="utf-8"))


class ChatAndWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tmpdir()
        tmp = Path(cls.td.name)
        cls.cfg = make_cfg(tmp / "data")
        ad = ScriptedAdapter(ROUTES)
        for name, text in (("p.txt", PACK_A), ("u1.txt", UPD_WRONG), ("u2.txt", UPD_GENUINE)):
            with mock.patch.object(pipeline, "make_adapter", lambda c: ad):
                pipeline.run_ingest(str(write(tmp, name, text)), cls.cfg, out=lambda s: None)
        st = Store(cls.cfg.data_dir, readonly=True)
        cls.state = build_state(st, cls.cfg)
        st.close()
        cls.srv = web.serve(cls.cfg, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.td.cleanup()

    def test_budget_answer_matches_plan_in_both_languages(self):
        total = f"{self.state['active']['plan']['budget']['total']:,}"
        for q in ("What is the current spend and how much budget is left?", "Budget mein kitna bacha hai?"):
            r = chat.answer(q, self.state)
            self.assertEqual(r["collection"], "Live plan and evidence")
            self.assertIn(total, r["answer"])
            self.assertTrue(r["citations"])

    def test_rejected_answer_names_rule_and_message(self):
        r = chat.answer("Which messages were rejected and why?", self.state)
        self.assertIn("R2.M1.S1", r["answer"])
        self.assertIn("Food covers everyone", r["answer"])
        self.assertTrue(any(c["type"] == "message" and c.get("quote") for c in r["citations"]))

    def test_unknown_question_is_not_guessed(self):
        for q in ("what is the weather", "kya projector chahiye?", ""):
            r = chat.answer(q, self.state)
            self.assertEqual(r["collection"], "Not verifiable")
            self.assertEqual(r["citations"], [])

    def test_brief_rules_are_a_separate_collection(self):
        r = chat.answer("What are the deadlines and limits?", self.state)
        self.assertEqual(r["collection"], "Student Brief rules")
        self.assertIn("2 minutes", r["answer"])

    def test_empty_state_answers_brief_only(self):
        empty = web._empty_state(self.cfg, "x")
        r = chat.answer("How many judges are assigned?", empty)
        self.assertEqual(r["collection"], "Not verifiable")

    def _get(self, path, **kw):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", **kw)
        return urllib.request.urlopen(req)

    def test_endpoints_and_read_only(self):
        self.assertEqual(json.loads(self._get("/api/version").read())["fingerprint"], self.state["fingerprint"])
        st = json.loads(self._get("/api/state").read())
        self.assertEqual(st["active"]["rev_no"], 2)
        txt = self._get("/api/export").read().decode()
        self.assertIn("MODEL CALL #1", txt)
        r = self._get("/api/chat", data=json.dumps({"question": "Which areas changed in the last update?"}).encode(), method="POST")
        self.assertIn("budget", json.loads(r.read())["answer"])
        for bad in ("/../planledger/web.py", "/%2e%2e/config.py", "/api/nope"):
            with self.assertRaises(urllib.error.HTTPError):
                self._get(bad)
        with self.assertRaises(urllib.error.HTTPError):
            self._get("/api/state", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError):
            self._get("/api/state", data=b"{}", method="PUT")
        self.assertIn("Content-Security-Policy", dict(self._get("/").headers))


if __name__ == "__main__":
    unittest.main()
