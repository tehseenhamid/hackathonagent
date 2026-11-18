/* PlanLedger dashboard. Read-only. All message text is inserted with textContent only:
   pack text is data and is never interpreted as markup or instructions. */
(function () {
  "use strict";

  var S = { state: null, tab: "overview", fp: null, evidence: { q: "", disp: "all", area: "all", run: "all", focus: null }, traceRun: "all", openTrace: {} };
  function tx(en, ur) { try { return localStorage.getItem("planledger.lang") === "ur" ? ur : en; } catch (e) { return en; } }
  var TABS = ["compose", "overview", "changes", "schedule", "budget", "trace", "evidence"];

  /* ------------------------------------------------------------ helpers */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) continue;
      if (k === "class") el.className = attrs[k];
      else if (k === "text") el.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), attrs[k]);
      else el.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(el, x); }); return; }
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function pkr(n) {
    if (n === null || n === undefined) return "unknown";
    var neg = n < 0, s = Math.abs(Math.round(n)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (neg ? "-Rs " : "Rs ") + s;
  }
  function u(x) { return x === null || x === undefined ? "unknown" : String(x); }
  function num(n) { return n === null || n === undefined ? "unavailable" : Number(n).toLocaleString("en-US"); }
  function fval(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
  function offset(m) { var s = Math.round(m); return "T+" + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  function clockMin(str) {
    if (!str) return null;
    var m = /(\d{1,2}):(\d{2})\s*(am|pm)?/i.exec(str);
    if (!m) return null;
    var hh = +m[1], mm = +m[2], ap = (m[3] || "").toLowerCase();
    if (ap === "pm" && hh < 12) hh += 12;
    if (ap === "am" && hh === 12) hh = 0;
    return hh * 60 + mm;
  }
  function tl(plan, m) {
    var c = clockMin(plan.event && plan.event.start_clock);
    if (c === null) return offset(m);
    var t = (c + m) % 1440;
    return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
  }
  function tag(label, cls) { return h("span", { class: "tag " + (cls || label) }, String(label).replace(/_/g, " ")); }
  function table(head, rows, cls) {
    return h("div", { class: "tablewrap" }, h("table", { class: cls || "" },
      h("thead", null, h("tr", null, head.map(function (x) { return h("th", { class: x.num ? "num" : "", scope: "col" }, x.t || x); }))),
      h("tbody", null, rows)));
  }
  function cell(c, numeric) { return h("td", { class: numeric ? "num" : "" }, c); }
  function empty(msg) { return h("div", { class: "empty" }, msg); }
  function kv(pairs) {
    var d = h("dl", { class: "kv" });
    pairs.forEach(function (p) { d.appendChild(h("dt", null, p[0])); d.appendChild(h("dd", null, p[1])); });
    return d;
  }
  function store(k, v) { try { localStorage.setItem("planledger." + k, v); } catch (e) { /* storage may be unavailable */ } }
  function load(k, d) { try { var v = localStorage.getItem("planledger." + k); return v === null ? d : v; } catch (e) { return d; } }
  function getJSON(url, opts) {
    return fetch(url, opts).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); });
  }

  /* ------------------------------------------------------------ preferences */
  function applyPrefs() {
    var theme = "light";
    var accent = load("accent", "green"), size = load("size", "normal");
    var root = document.documentElement;
    root.setAttribute("data-theme", theme); root.setAttribute("data-accent", accent); root.setAttribute("data-size", size);
    document.getElementById("accent-select").value = accent;
    document.getElementById("size-select").value = size;
  }
  function bindPrefs() {
    [["accent-select", "accent", "data-accent"], ["size-select", "size", "data-size"]].forEach(function (x) {
      document.getElementById(x[0]).addEventListener("change", function (e) {
        document.documentElement.setAttribute(x[2], e.target.value); store(x[1], e.target.value);
      });
    });
  }

  /* ------------------------------------------------------------ tabs */
  function showTab(name, focusTab) {
    if (TABS.indexOf(name) < 0) name = "compose";
    S.tab = name; store("tab", name);
    TABS.forEach(function (t) {
      var b = document.getElementById("tab-" + t), p = document.getElementById("panel-" + t), on = t === name;
      b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; p.hidden = !on;
      if (on && focusTab) b.focus();
    });
  }
  function applyMode() {
    var simple = load("mode", "simple") === "simple";
    document.body.classList.toggle("simple", simple);
    var t = document.getElementById("mode-toggle"); if (t) { t.textContent = simple ? tx("Show technical details", "تکنیکی تفصیل دکھائیں") : tx("Back to simple view", "آسان ویو پر واپس"); t.setAttribute("aria-pressed", simple ? "false" : "true"); }
    if (simple) showTab("compose");
  }
  function applyLang() {
    var ur = tx("en", "ur") === "ur";
    [["lang-en", !ur], ["lang-ur", ur]].forEach(function (x) { var b = document.getElementById(x[0]); if (b) b.setAttribute("aria-pressed", x[1] ? "true" : "false"); });
    document.documentElement.setAttribute("lang", ur ? "ur" : "en");
    var pc = document.getElementById("panel-compose"); if (pc) pc.setAttribute("dir", ur ? "rtl" : "ltr");
    var co = document.getElementById("chat-open"); if (co) co.textContent = tx("Ask the Plan", "پلان سے پوچھیں");
    var old = document.getElementById("compose-text"), keep = old ? old.value : "";
    buildCompose();
    var nt = document.getElementById("compose-text"); if (nt && keep) nt.value = keep;
    applyMode();
    if (S.state) renderAll(S.state);
  }
  function bindLang() {
    ["en", "ur"].forEach(function (l) {
      var b = document.getElementById("lang-" + l);
      if (b) b.addEventListener("click", function () { store("lang", l); applyLang(); });
    });
  }
  function bindTabs() {
    var mt = document.getElementById("mode-toggle");
    if (mt) mt.addEventListener("click", function () { store("mode", load("mode", "simple") === "simple" ? "technical" : "simple"); applyMode(); });
    var tabs = document.getElementById("tabs");
    tabs.addEventListener("click", function (e) { var b = e.target.closest("button[data-tab]"); if (b) showTab(b.getAttribute("data-tab")); });
    tabs.addEventListener("keydown", function (e) {
      var i = TABS.indexOf(S.tab), n = null;
      if (e.key === "ArrowRight") n = (i + 1) % TABS.length;
      else if (e.key === "ArrowLeft") n = (i + TABS.length - 1) % TABS.length;
      else if (e.key === "Home") n = 0; else if (e.key === "End") n = TABS.length - 1;
      if (n !== null) { e.preventDefault(); showTab(TABS[n], true); }
    });
  }

  /* ------------------------------------------------------------ status bar */
  function renderStatus(st) {
    var bar = document.getElementById("statusbar"); clear(bar);
    var a = st.active;
    if (!a) {
      bar.appendChild(h("span", { class: "chip warn" }, "No committed plan yet"));
    } else {
      bar.appendChild(h("span", { class: "chip " + (a.grade === "valid" ? "ok" : "warn") }, "Plan is ", h("b", null, a.grade === "valid" ? "VALID" : "PROVISIONAL")));
      bar.appendChild(h("span", { class: "chip" }, "Revision ", h("b", null, String(a.rev_no)), " of ", String(st.revisions.length)));
      bar.appendChild(h("span", { class: "chip " + (a.checks.failed.length ? "bad" : "ok") }, "Rules followed ", h("b", null, a.checks.passed + " of " + a.checks.total)));
      var b = a.plan.budget;
      bar.appendChild(h("span", { class: "chip " + (b.remaining < 0 ? "bad" : "") }, "Budget ", h("b", null, pkr(b.total)), " of ", pkr(b.cap)));
    }
    var t = st.totals;
    bar.appendChild(h("span", { class: "chip" }, "AI calls ", h("b", null, String(t.calls)), ", tokens used ", h("b", null, num(t.total)), t.partial ? " (some calls reported no usage)" : ""));
    if (st.running && st.running.length) bar.appendChild(h("span", { class: "chip warn" }, "Run in progress: " + st.running.join(", ")));
    var last = st.runs[st.runs.length - 1];
    if (last) bar.appendChild(h("span", { class: "chip " + (/rejected|blocked|failed/.test(last.outcome || last.status) ? "warn" : "") }, "Last run " + last.run_no + ": ", h("b", null, (last.outcome || last.status).replace(/_/g, " "))));
    if (st.provider_problem) bar.appendChild(h("span", { class: "chip warn" }, "Provider: " + st.provider_problem));
    else if (st.model) bar.appendChild(h("span", { class: "chip" }, "AI model: " + st.model));
  }

  /* ------------------------------------------------------------ overview */
  function noPlan(st) {
    var box = h("div");
    box.appendChild(empty("No plan has been committed yet."));
    box.appendChild(h("p", null, "Open the New input tab and paste the message pack to create the baseline plan."));
    if (st.provider_problem) box.appendChild(h("div", { class: "banner warn" }, h("h3", null, "Model access is not configured"), h("p", null, st.provider_problem)));
    if (st.runs.length) {
      box.appendChild(h("h3", null, "Runs so far"));
      box.appendChild(runsTable(st));
    }
    return box;
  }
  function runsTable(st) {
    return table(["Run", "Kind", "File", "Outcome", "Calls", "Seconds"], st.runs.slice().reverse().map(function (r) {
      return h("tr", null, cell(String(r.run_no)), cell(r.kind), cell(r.filename || ""), cell(tag((r.outcome || r.status || "").replace(/^update_|^baseline_/, "") || r.status, /rejected|blocked|failed/.test(r.outcome || r.status) ? "warn" : "ok")),
        cell(String(r.calls === undefined || r.calls === null ? "" : r.calls), true), cell(r.elapsed_ms == null ? "" : (r.elapsed_ms / 1000).toFixed(1), true));
    }));
  }

  /* ------------------------------------------------------------ plain-language summary */
  function plainSummary(st) {
    var a = st.active, plan = a.plan, ev = plan.event, v = plan.venue, f = plan.food, j = plan.judging, b = plan.budget;
    var box = h("div", { class: "summary" });
    var ok = a.grade === "valid";
    box.appendChild(h("div", { class: "verdict " + (ok ? "ok" : "warn") },
      h("h2", null, ok ? tx("The plan is valid", "پلان درست ہے") : tx("The plan needs attention", "پلان پر توجہ درکار ہے")),
      h("p", null, ok ? tx("All " + a.checks.total + " safety checks passed: it fits the venue, the budget, the time and the judging rules.", "تمام " + a.checks.total + " حفاظتی جانچیں پاس ہوئیں: پلان وینیو، بجٹ، وقت اور ججنگ کے اصولوں کے مطابق ہے۔")
                      : tx(a.checks.passed + " of " + a.checks.total + " checks passed. Some information is missing or could not be confirmed.", a.checks.total + " میں سے " + a.checks.passed + " جانچیں پاس ہوئیں۔ کچھ معلومات نامکمل ہیں یا تصدیق نہیں ہو سکیں۔"))));
    var lines = [];
    var hrs = ev.duration_min == null ? null : (ev.duration_min / 60).toFixed(ev.duration_min % 60 ? 1 : 0);
    lines.push([tx("The event", "ایونٹ"), tx(u(ev.students) + " students in " + u(ev.teams) + " teams, " + (hrs == null ? "unknown length" : hrs + " hours") + ", at " + (v.name || "no venue chosen yet") + (v.rent_total == null ? "" : " (hire " + pkr(v.rent_total) + ")."),
      u(ev.students) + " طلبہ، " + u(ev.teams) + " ٹیمیں، " + (hrs == null ? "دورانیہ معلوم نہیں" : hrs + " گھنٹے") + "، " + (v.name || "وینیو ابھی منتخب نہیں ہوا") + " میں" + (v.rent_total == null ? "" : " (کرایہ " + pkr(v.rent_total) + ")۔"))]);
    lines.push([tx("Food", "کھانا"), tx(u(f.qty) + " meals of " + u(f.option) + " so that everyone present is fed.", u(f.qty) + " کھانے (" + u(f.option) + ")، تاکہ موجود ہر شخص کو کھانا ملے۔")]);
    lines.push([tx("Judging", "ججنگ"), tx(u(j.deployed) + " judges. Every team gets " + u(j.slot_min) + " minutes with a judge, in " + u(j.rounds) + " rounds.", u(j.deployed) + " ججز۔ ہر ٹیم کو جج کے ساتھ " + u(j.slot_min) + " منٹ ملتے ہیں، " + u(j.rounds) + " راؤنڈز میں۔")]);
    lines.push([tx("Money", "رقم"), tx("Total cost " + pkr(b.total) + " out of " + pkr(b.cap) + ". " + (b.remaining == null ? "" : (b.remaining >= 0 ? pkr(b.remaining) + " is left." : "Over budget by " + pkr(-b.remaining) + ".")),
      "کل خرچ " + pkr(b.total) + "، " + pkr(b.cap) + " میں سے۔ " + (b.remaining == null ? "" : (b.remaining >= 0 ? pkr(b.remaining) + " باقی ہیں۔" : "بجٹ سے " + pkr(-b.remaining) + " زیادہ۔")))]);
    box.appendChild(kv(lines));
    var last = st.runs[st.runs.length - 1];
    if (last) {
      var o = (last.outcome || last.status || "");
      var txt, cls = "", reasonEl = null;
      if (/baseline/.test(o)) txt = tx("The first message pack was read and the plan was built.", "پہلا میسج پیک پڑھا گیا اور پلان بنا۔");
      else if (/accepted/.test(o)) txt = tx("The last message was accepted and the plan was updated", "آخری پیغام قبول ہوا اور پلان اپ ڈیٹ ہوا") + (last.delta && last.delta.changed_areas && last.delta.changed_areas.length ? tx(" Changed: ", " تبدیل: ") + last.delta.changed_areas.join(", ") + tx(".", "۔") : tx(".", "۔"));
      else if (/rejected|blocked/.test(o)) {
        cls = "warn";
        var why = st.statements.filter(function (s) { return s.run_no === last.run_no && (s.disposition === "rejected" || s.disposition === "needs_clarification"); }).map(function (s) { return s.reason; }).filter(Boolean);
        txt = tx("The last message was refused and the plan was left as it was.", "آخری پیغام مسترد ہوا اور پلان جیسا تھا ویسا ہی رہا۔") + (why.length ? tx(" Reason: ", " وجہ:") : ""); if (why.length) reasonEl = h("span", { class: "why", dir: "ltr" }, why[0]);
      } else if (/duplicate/.test(o)) txt = tx("That message was already received, so nothing changed.", "یہ پیغام پہلے مل چکا تھا، اس لیے کچھ نہیں بدلا۔");
      else if (last.status === "failed") { cls = "bad"; txt = tx("The last attempt failed and the plan was not changed.", "آخری کوشش ناکام ہوئی اور پلان نہیں بدلا۔"); }
      if (txt) box.appendChild(h("div", { class: "banner " + (cls || "ok") }, h("h3", null, tx("Last message", "آخری پیغام")), h("p", null, txt, reasonEl)));
    }
    return box;
  }

  function renderOverview(st) {
    var p = document.getElementById("panel-overview"); clear(p);
    var a = st.active;
    if (!a) { p.appendChild(noPlan(st)); return; }
    var plan = a.plan, last = st.runs[st.runs.length - 1];
    if (last && last.status === "failed") {
      p.appendChild(h("div", { class: "banner bad" }, h("h3", null, "Latest run failed"), h("p", null, (last.error && (last.error.detail || last.error)) || last.outcome)));
    }
    if (a.grade !== "valid") p.appendChild(h("div", { class: "banner warn" }, h("h3", null, "Provisional plan"), h("p", null, "At least one check could not be confirmed as passing. See the checks table below.")));

    p.appendChild(plainSummary(st));
    var ev = plan.event, v = plan.venue, f = plan.food, j = plan.judging, b = plan.budget, sc = plan.schedule;
    p.appendChild(h("h2", null, "The five parts of the plan"));
    p.appendChild(h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", null, "Event"), kv([
        ["Students", u(ev.students)], ["Teams", u(ev.teams) + (ev.teams_basis ? " (" + ev.teams_basis + ")" : "")], ["Staff", u(ev.staff)], ["Duration", ev.duration_min == null ? "unknown" : ev.duration_min + " minutes"],
        ["Start", ev.start_clock || "not stated (offsets shown)"]])),
      h("div", { class: "card" }, h("h3", null, "Venue"), h("div", { class: "big" }, v.name || "none chosen"), kv([
        ["Capacity", u(v.capacity) + " for " + u(v.headcount) + " on site"], ["Hire", v.rent_total == null ? "unknown" : pkr(v.rent_total) + " (" + v.rent_per + ")"]])),
      h("div", { class: "card" }, h("h3", null, "Food"), h("div", { class: "big" }, u(f.qty) + " meals"), kv([
        ["Menu", u(f.option)], ["Fed", u(f.fed) + " people"], ["Cost", pkr(f.lines.reduce(function (s, l) { return s + l.subtotal; }, 0))]])),
      h("div", { class: "card" }, h("h3", null, "Judging"), h("div", { class: "big" }, u(j.deployed) + " of " + u(j.confirmed) + " judges"), kv([
        ["Minimum needed", u(j.min_needed)], ["Rounds", j.rounds + " x " + u(j.slot_min) + " min"], ["Reserve", u(j.reserve)]])),
      h("div", { class: "card" }, h("h3", null, "Budget"), h("div", { class: "big" }, pkr(b.total)), kv([
        ["Cap", pkr(b.cap)], ["Remaining", pkr(b.remaining)]]), meter(b.total, b.cap))));

    p.appendChild(h("h2", null, "Does the plan follow the rules?"));
    p.appendChild(table(["Rule", "Followed?", "Detail"], a.checks.checks.map(function (c) {
      return h("tr", null, cell(c.label), cell(tag(c.status === "pass" ? "Yes" : c.status === "warn" ? "Not confirmed" : "No", c.status === "pass" ? "pass" : c.status === "warn" ? "warn" : "fail")), cell(c.detail));
    })));

    p.appendChild(h("h2", null, "Why these choices were made"));
    p.appendChild(h("h3", null, "Venue options"));
    p.appendChild(table(["Venue", "Capacity", "Hire", "Verdict", "Reason"], v.alternatives.map(function (x) {
      return h("tr", { class: x.verdict === "chosen" ? "hl" : "" }, cell(x.name), cell(String(x.capacity), true), cell(pkr(x.rent_total), true), cell(tag(x.verdict, x.verdict === "chosen" ? "ok" : x.verdict === "rejected" ? "bad" : "")), cell(x.reason));
    })));
    p.appendChild(h("h3", null, "Menu options"));
    p.appendChild(table(["Item", "Price per head", "Verdict", "Reason"], f.alternatives.map(function (x) {
      return h("tr", { class: x.verdict === "chosen" ? "hl" : "" }, cell(x.name), cell(pkr(x.price), true), cell(tag(x.verdict, x.verdict === "chosen" ? "ok" : "")), cell(x.reason));
    })));
    if (plan.solver) p.appendChild(h("p", { class: "muted small" }, "Planner checked " + plan.solver.candidates_checked + " candidate combinations. Tie-break: " + plan.solver.tie_break + "."));

    if (plan.assumptions && plan.assumptions.length) {
      p.appendChild(h("h2", null, "Assumptions (not facts from the messages)"));
      p.appendChild(h("ul", null, plan.assumptions.map(function (x) { return h("li", null, x); })));
    }
    if (plan.unknown && plan.unknown.length) {
      p.appendChild(h("h2", null, "Unknown inputs"));
      p.appendChild(h("ul", null, plan.unknown.map(function (x) { return h("li", null, h("b", null, x.item + ": "), x.why); })));
    }
    if (plan.discrepancies_info) p.appendChild(h("p", { class: "muted" }, "Noted difference: " + plan.discrepancies_info + "."));
    (plan.discrepancies || []).forEach(function (d) { p.appendChild(h("div", { class: "banner warn" }, fval(d))); });
    var unv = (a.facts && a.facts.unverified) || [];
    if (unv.length) {
      p.appendChild(h("h2", null, "Rules the checker could not verify"));
      p.appendChild(h("ul", null, unv.map(function (u) { return h("li", null, h("b", null, u.label + ": "), u.why); })));
    }
  }
  function meter(v, cap) {
    var pct = cap ? Math.max(0, Math.min(100, v / cap * 100)) : 0, m = h("div", { class: "meter" + (v > cap ? " over" : ""), role: "img", "aria-label": pkr(v) + " of " + pkr(cap) + " used" }, h("span"));
    m.firstChild.style.width = pct.toFixed(1) + "%";
    return m;
  }

  /* ------------------------------------------------------------ changes */
  function stmtCard(s, st) {
    var run = st.runs.filter(function (r) { return r.run_no === s.run_no; })[0];
    var kept = run && run.active_rev;
    return h("div", { class: "card" },
      h("h3", null, "Message " + s.msg_id + " from " + (s.sender || "unknown sender") + " (" + (s.role || "role unknown") + ")"),
      h("p", null, tag(s.disposition), " ", h("b", null, s.key + (s.ent ? "[" + s.ent + "]" : "") + " = " + fval(s.val))),
      s.quote ? h("div", { class: "quote" }, "“" + s.quote + "”", h("div", { class: "muted small" }, "Run " + s.run_no + ", statement " + s.id + (s.lines ? ", line " + s.lines[0] : ""))) : null,
      h("p", null, h("b", null, "Reason: "), s.reason || ""),
      kept ? h("p", { class: "muted" }, "Revision " + kept + " remained the active plan.") : null,
      h("button", { class: "btn secondary", type: "button", onclick: function () { focusEvidence(s.id); } }, "Open original message"));
  }
  function storyList(st) {
    if (!st.runs.length) return empty(tx("Nothing has been added yet.", "ابھی تک کچھ شامل نہیں ہوا۔"));
    return h("ol", { class: "story" }, st.runs.slice().reverse().map(function (r) {
      var o = r.outcome || r.status || "", d = r.delta, txt, cls = "", reasonEl = null;
      var why = st.statements.filter(function (s) { return s.run_no === r.run_no && (s.disposition === "rejected" || s.disposition === "needs_clarification"); });
      if (/baseline/.test(o)) txt = tx("The first message pack was read. Plan version " + (r.new_rev || 1) + " was built" + (r.grade ? " and graded " + String(r.grade).toUpperCase() : "") + ".", "پہلا میسج پیک پڑھا گیا۔ پلان ورژن " + (r.new_rev || 1) + " بنا" + (r.grade ? " اور گریڈ " + String(r.grade).toUpperCase() + " ملا" : "") + "۔");
      else if (/accepted/.test(o)) txt = tx("Accepted. The plan was updated" + (d && d.changed_areas && d.changed_areas.length ? ": " + d.changed_areas.join(", ") + " changed. " : ". ") + ((d && d.kept_valid_areas && d.kept_valid_areas.length) ? "Untouched and still valid: " + d.kept_valid_areas.join(", ") + "." : ""),
        "قبول ہوا۔ پلان اپ ڈیٹ ہوا" + (d && d.changed_areas && d.changed_areas.length ? ": " + d.changed_areas.join(", ") + " تبدیل ہوئے۔ " : "۔ ") + ((d && d.kept_valid_areas && d.kept_valid_areas.length) ? "جو چھیڑا نہیں گیا اور درست ہے: " + d.kept_valid_areas.join(", ") + "۔" : ""));
      else if (/rejected|blocked/.test(o)) { cls = "warn"; txt = tx("Refused. The plan stayed as it was.", "مسترد ہوا۔ پلان جیسا تھا ویسا ہی رہا۔") + (why.length && why[0].reason ? tx(" Why: ", " وجہ:") : ""); if (why.length && why[0].reason) reasonEl = h("span", { class: "why", dir: "ltr" }, why[0].reason); }
      else if (/duplicate/.test(o)) txt = tx("Already received earlier, so nothing was done.", "پہلے مل چکا تھا، اس لیے کچھ نہیں کیا گیا۔");
      else if (r.status === "failed") { cls = "bad"; txt = tx("Could not be processed: ", "پروسیس نہیں ہو سکا: ") + fval((r.error && (r.error.detail || r.error)) || o); }
      else txt = String(o).replace(/_/g, " ");
      return h("li", { class: cls }, h("b", null, tx("Step ", "مرحلہ ") + r.run_no + (r.filename ? " (" + r.filename + ")" : "") + ": "), txt, reasonEl);
    }));
  }

  function renderChanges(st) {
    var p = document.getElementById("panel-changes"); clear(p);
    var rej = st.statements.filter(function (s) { return s.disposition === "rejected" || s.disposition === "needs_clarification"; });
    p.appendChild(h("h2", null, "What happened, step by step"));
    p.appendChild(storyList(st));
    p.appendChild(h("h2", null, "Rejected and held back"));
    if (!rej.length) p.appendChild(empty("No message has been rejected or held back so far."));
    else p.appendChild(h("div", { class: "grid" }, rej.slice().reverse().map(function (s) { return stmtCard(s, st); })));

    var sup = st.statements.filter(function (s) { return s.disposition === "superseded"; });
    p.appendChild(h("h2", null, "Superseded and ignored"));
    if (!sup.length) p.appendChild(empty("Nothing has been superseded."));
    else p.appendChild(table(["Statement", "Key", "Value", "Why", "Quote"], sup.map(function (s) {
      return h("tr", { class: "clickable", onclick: function () { focusEvidence(s.id); } }, cell(s.id), cell(s.key + (s.ent ? "[" + s.ent + "]" : "")), cell(fval(s.val)), cell(s.reason || ""), cell(s.quote || ""));
    })));

    p.appendChild(h("h2", null, "Runs"));
    var runs = st.runs.slice().reverse();
    if (!runs.length) p.appendChild(empty("No runs yet."));
    runs.forEach(function (r) {
      var d = r.delta;
      var det = h("details", { open: r === runs[0] },
        h("summary", null, "Run " + r.run_no + ": " + r.kind + ", " + (r.outcome || r.status).replace(/_/g, " ") + (r.filename ? " (" + r.filename + ")" : "")),
        h("div", null,
          kv([["Active revision after run", String(r.active_rev == null ? "none" : r.active_rev)], ["New revision", r.new_rev == null ? "none (plan unchanged)" : String(r.new_rev)],
            ["Model calls", r.calls == null ? "" : String(r.calls)], ["Elapsed", r.elapsed_ms == null ? "" : (r.elapsed_ms / 1000).toFixed(1) + " s of " + r.budget_s + " s"],
            ["Source hash", (r.sha256 || "").slice(0, 16) + "..."]]),
          r.duplicate_of_run ? h("p", { class: "banner warn" }, "This file is byte-identical to the input of run " + r.duplicate_of_run + " and was not processed again.") : null,
          r.error ? h("p", { class: "banner bad" }, fval(r.error.detail || r.error)) : null,
          d ? h("div", null, h("h4", null, "What changed"), kv([
            ["Changed areas", (d.changed_areas || []).join(", ") || "none"], ["Recomputed by code", (d.recomputed_areas || []).join(", ") || "none"],
            ["Areas with model calls", (d.called_areas || []).join(", ") || "none"], ["Kept valid, untouched", (d.kept_valid_areas || []).join(", ") || "none"]]),
            d.items && d.items.length ? table(["Field", "Before", "After"], d.items.map(function (i) { return h("tr", null, cell(i.path), cell(fval(i.before)), cell(fval(i.after))); })) : null) : null,
          h("h4", null, "Messages in this run"),
          r.messages.length ? table(["Message", "Sender", "Role", "Treated as", "Counts", "Note"], r.messages.map(function (m) {
            return h("tr", null, cell(m.msg_id), cell(m.sender || ""), cell(m.role || ""), cell(tag(m.label)), cell(Object.keys(m.counts || {}).map(function (k) { return k + " " + m.counts[k]; }).join(", ")), cell(m.irrelevant || ""));
          })) : h("p", { class: "muted" }, "No messages were recorded for this run.")));
      p.appendChild(det);
    });

    p.appendChild(h("h2", null, "Plan revisions"));
    p.appendChild(st.revisions.length ? table(["Revision", "Created by run", "Grade", "Checks", "Total", "Remaining", "Changed areas"], st.revisions.slice().reverse().map(function (v) {
      return h("tr", null, cell(String(v.rev_no)), cell(String(v.run_no)), cell(tag(v.grade, v.grade === "valid" ? "ok" : "warn")), cell(v.checks.passed + " of " + v.checks.total), cell(pkr(v.total), true), cell(pkr(v.remaining), true), cell(((v.delta && v.delta.changed_areas) || []).join(", ") || "all (baseline)"));
    })) : empty("No revisions yet."));
  }

  /* ------------------------------------------------------------ schedule */
  function renderSchedule(st) {
    var p = document.getElementById("panel-schedule"); clear(p);
    var a = st.active;
    if (!a) { p.appendChild(empty("No plan yet.")); return; }
    var plan = a.plan, sc = plan.schedule, j = plan.judging;
    p.appendChild(h("h2", null, "Schedule (" + sc.duration_min + " minutes)"));
    var bar = h("div", { class: "timeline", role: "img", "aria-label": "Schedule timeline, described in the table below" });
    sc.segments.forEach(function (s) {
      var seg = h("div", { class: "seg " + s.kind }, h("b", null, s.label), h("span", null, tl(plan, s.start) + " to " + tl(plan, s.end)));
      seg.style.width = (s.dur / sc.duration_min * 100).toFixed(2) + "%"; bar.appendChild(seg);
    });
    p.appendChild(bar);
    p.appendChild(table(["Start", "End", "Minutes", "Activity"], sc.segments.map(function (s) {
      return h("tr", null, cell(tl(plan, s.start)), cell(tl(plan, s.end)), cell(String(s.dur), true), cell(s.label));
    })));
    p.appendChild(h("p", { class: "muted" }, plan.event.start_clock ? "Clock times use the stated start of " + plan.event.start_clock + "." : "No start time was stated, so times are shown as offsets from the start (T+hours:minutes)."));
    (sc.notes || []).forEach(function (n) { p.appendChild(h("p", { class: "muted small" }, n)); });

    p.appendChild(h("h2", null, "Judging"));
    p.appendChild(h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", null, "Coverage"), kv([["Teams", String(j.teams)], ["Judges per team", String(j.panel)], ["Slot", j.slot_min + " minutes"],
        ["Judges deployed", j.deployed + " of " + j.confirmed + " confirmed"], ["Minimum needed", String(j.min_needed)], ["Teams per judge limit", j.max_teams == null ? "none stated" : String(j.max_teams)],
        ["Window", tl(plan, j.start_min) + " to " + tl(plan, j.end_min)]])),
      h("div", { class: "card" }, h("h3", null, "Judge load"), kv(Object.keys(j.loads).map(function (k) { return [k, j.loads[k] + " team(s)"]; })))));
    p.appendChild(table(["Round", "Time", "Teams", "Judges used"], a.judging_rounds.map(function (r) {
      return h("tr", null, cell(String(r.round)), cell(tl(plan, r.start) + " to " + tl(plan, r.end)), cell(String(r.teams)), cell(r.count + " judge(s)"));
    })));
    p.appendChild(h("details", null, h("summary", null, "Every team assignment (" + j.assignments.length + ")"),
      h("div", null, table(["Team", "Round", "Time", "Judge(s)"], j.assignments.map(function (x) {
        return h("tr", null, cell(String(x.team)), cell(String(x.round)), cell(tl(plan, x.start) + " to " + tl(plan, x.end)), cell(x.judges.join(", ")));
      })))));
  }

  /* ------------------------------------------------------------ budget */
  function renderBudget(st) {
    var p = document.getElementById("panel-budget"); clear(p);
    var a = st.active;
    if (!a) { p.appendChild(empty("No plan yet.")); return; }
    var b = a.plan.budget;
    p.appendChild(h("div", { class: "banner " + (b.remaining < 0 ? "bad" : "ok") }, h("h3", null, b.remaining < 0 ? "Over budget" : "Within budget"),
      h("p", null, "Total " + pkr(b.total) + " against a cap of " + pkr(b.cap) + ". Remaining " + pkr(b.remaining) + "." + (b.lower_bound ? " This total is a lower bound because some amounts are unknown." : "")), meter(b.total, b.cap)));
    p.appendChild(table(["Line", "Quantity", "Rate", "Amount", "Basis", "Evidence"], b.lines.map(function (l) {
      return h("tr", null, cell(l.label), cell(l.qty == null ? "" : String(l.qty), true), cell(l.rate == null ? "" : pkr(l.rate), true), cell(pkr(l.amount), true), cell(l.note || ""), cell(evLinks(l.evidence)));
    }).concat([h("tr", { class: "hl" }, cell(h("b", null, "Total")), cell(""), cell(""), cell(h("b", null, pkr(b.total)), true), cell(""), cell(""))])));
    if (b.paid_toward && b.paid_toward.length) {
      p.appendChild(h("h3", null, "Already paid toward computed lines (not added twice)"));
      p.appendChild(table(["Item", "Amount", "Evidence"], b.paid_toward.map(function (x) { return h("tr", null, cell(x.label), cell(pkr(x.amount), true), cell(evLinks(x.evidence))); })));
    }
    if (b.excluded && b.excluded.length) {
      p.appendChild(h("h3", null, "Not budgeted"));
      p.appendChild(table(["Item", "Amount", "Reason", "Evidence"], b.excluded.map(function (x) { return h("tr", null, cell(x.label), cell(pkr(x.amount), true), cell(x.reason), cell(evLinks(x.evidence))); })));
    }
    if (b.unknown && b.unknown.length) {
      p.appendChild(h("h3", null, "Unknown amounts"));
      p.appendChild(h("ul", null, b.unknown.map(function (u) { return h("li", null, fval(u)); })));
    }
    p.appendChild(h("h3", null, "Cap evidence"));
    p.appendChild(h("p", null, evLinks(b.evidence)));
  }
  function evLinks(ids) {
    var w = h("span");
    (ids || []).forEach(function (id, i) {
      if (i) w.appendChild(document.createTextNode(", "));
      w.appendChild(h("a", { href: "#evidence", onclick: function (e) { e.preventDefault(); focusEvidence(id); } }, id));
    });
    if (!ids || !ids.length) w.appendChild(document.createTextNode("computed"));
    return w;
  }

  /* ------------------------------------------------------------ trace */
  function renderTrace(st) {
    var p = document.getElementById("panel-trace"); clear(p);
    var t = st.totals;
    p.appendChild(h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", null, "Session totals (provider-reported)"), kv([["Model calls", String(t.calls)], ["Input tokens", num(t.input)], ["Output tokens", num(t.output)], ["Total tokens", num(t.total)]]),
        t.partial ? h("p", { class: "banner warn" }, "Partial: " + t.missing_usage + " call(s) reported no usage, so this total may be lower than the real figure. Nothing is estimated.") : null),
      h("div", { class: "card" }, h("h3", null, "Limits in force"), kv([["Full run", "cut off at 2 minutes"], ["Update", "at most 5 model calls, 1 minute"]]))));
    var sel = h("select", { id: "trace-run", "aria-label": "Filter by run" }, h("option", { value: "all" }, "All runs"));
    st.runs.forEach(function (r) { sel.appendChild(h("option", { value: String(r.run_no) }, "Run " + r.run_no + " (" + r.kind + ")")); });
    sel.value = S.traceRun; sel.addEventListener("change", function (e) { S.traceRun = e.target.value; renderTrace(st); });
    p.appendChild(h("div", { class: "toolbar" }, h("label", null, "Show ", sel)));
    var items = st.trace.filter(function (i) { return S.traceRun === "all" || String(i.run_no) === S.traceRun; });
    if (!items.length) { p.appendChild(empty("No trace recorded yet.")); return; }
    var rows = [];
    items.forEach(function (i) {
      if (i.kind === "call") {
        var u = i.usage;
        var row = h("tr", { class: "clickable", tabindex: "0", "aria-expanded": S.openTrace[i.seq] ? "true" : "false", onclick: function () { toggleCall(i, row); }, onkeydown: function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleCall(i, row); } } },
          cell("Call #" + i.call_no), cell(i.tag), cell(i.resolved_model || "not reported"), cell(tag(i.outcome, i.outcome === "ok" ? "ok" : "bad")),
          cell(i.elapsed_ms == null ? "" : (i.elapsed_ms / 1000).toFixed(2) + " s", true),
          cell(u ? num(u.input) : "unavailable", true), cell(u ? num(u.output) : "unavailable", true), cell(u ? num(u.total) : "unavailable", true));
        row.setAttribute("data-seq", i.seq);
        rows.push(row);
        var det = h("tr", { class: "calldetail", "data-for": i.seq, hidden: !S.openTrace[i.seq] }, h("td", { colspan: "8" }, h("div", { class: "muted small" }, "Click the row again to hide the full input and output.")));
        rows.push(det);
        if (S.openTrace[i.seq]) loadCall(i.seq, det);
      } else {
        rows.push(h("tr", null, cell("Run " + i.run_no), cell(i.area || ""), cell(i.type.replace(/_/g, " ")), h("td", { colspan: "5" }, i.msg + (i.ids && i.ids.length ? " (evidence: " + i.ids.join(", ") + ")" : ""))));
      }
    });
    p.appendChild(table(["Item", "Area tag", "Model / type", "Outcome", "Time", { t: "Input tokens", num: true }, { t: "Output tokens", num: true }, { t: "Total tokens", num: true }], rows));
    p.appendChild(h("details", null, h("summary", null, "Update routing guide: which areas a change can touch"), h("div", null, h("pre", { class: "audit" }, st.routing_guide || "not available"))));
  }
  function toggleCall(i, row) {
    var det = row.nextSibling;
    S.openTrace[i.seq] = !S.openTrace[i.seq];
    det.hidden = !S.openTrace[i.seq];
    row.setAttribute("aria-expanded", S.openTrace[i.seq] ? "true" : "false");
    if (S.openTrace[i.seq]) loadCall(i.seq, det);
  }
  function loadCall(seq, det) {
    var td = det.firstChild; clear(td); td.appendChild(h("div", { class: "muted" }, "Loading the recorded input and output..."));
    getJSON("/api/trace_item?seq=" + seq).then(function (c) {
      clear(td);
      td.appendChild(h("h4", null, "Input sent to the model: system"));
      td.appendChild(h("pre", { class: "audit" }, (c.input && c.input.system) || ""));
      td.appendChild(h("h4", null, "Input sent to the model: user"));
      td.appendChild(h("pre", { class: "audit" }, (c.input && c.input.user) || ""));
      td.appendChild(h("h4", null, "Output returned"));
      td.appendChild(h("pre", { class: "audit" }, c.output == null ? "(no output: " + c.outcome + (c.error ? ", " + c.error : "") + ")" : c.output));
      var u = c.usage;
      td.appendChild(h("p", null, u ? "Tokens reported by the provider: input " + num(u.input) + ", output " + num(u.output) + ", total " + num(u.total) + (u.sub && Object.keys(u.sub).length ? " (" + Object.keys(u.sub).map(function (k) { return k + " " + u.sub[k]; }).join(", ") + ")" : "") : "The provider reported no token usage for this call."));
    }).catch(function (e) { clear(td); td.appendChild(h("div", { class: "banner bad" }, "Could not load this call: " + e.message)); });
  }

  /* ------------------------------------------------------------ evidence */
  function focusEvidence(id) {
    S.evidence = { q: "", disp: "all", area: "all", run: "all", focus: id };
    showTab("evidence"); renderEvidence(S.state);
    var el = document.querySelector('[data-stmt="' + id.replace(/"/g, "") + '"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "center" });
  }
  function renderEvidence(st) {
    var p = document.getElementById("panel-evidence"); clear(p);
    if (!st.statements.length && !st.sources.length) { p.appendChild(empty("No messages have been read yet.")); return; }
    var E = S.evidence;
    var q = h("input", { type: "search", value: E.q, placeholder: "Search quote, key, sender or reason", "aria-label": "Search evidence" });
    q.addEventListener("input", function (e) { E.q = e.target.value; E.focus = null; drawRows(); });
    function sel(label, key, opts) {
      var s = h("select", { "aria-label": label }, opts.map(function (o) { return h("option", { value: o }, o === "all" ? label + ": all" : String(o).replace(/_/g, " ")); }));
      s.value = E[key]; s.addEventListener("change", function (e) { E[key] = e.target.value; E.focus = null; drawRows(); }); return s;
    }
    function uniq(k) { var s = {}; st.statements.forEach(function (x) { if (x[k] != null) s[x[k]] = 1; }); return ["all"].concat(Object.keys(s).sort()); }
    p.appendChild(h("div", { class: "toolbar" }, q, sel("Disposition", "disp", uniq("disposition")), sel("Area", "area", uniq("area")), sel("Run", "run", uniq("run_no"))));
    var holder = h("div"); p.appendChild(holder);
    var viewer = h("div"); p.appendChild(viewer);
    function match(s) {
      if (E.disp !== "all" && s.disposition !== E.disp) return false;
      if (E.area !== "all" && s.area !== E.area) return false;
      if (E.run !== "all" && String(s.run_no) !== E.run) return false;
      if (E.q) { var hay = [s.quote, s.key, s.ent, s.sender, s.reason, s.id, fval(s.val)].join(" ").toLowerCase(); if (hay.indexOf(E.q.toLowerCase()) < 0) return false; }
      return true;
    }
    function drawRows() {
      clear(holder);
      var rows = st.statements.filter(match);
      holder.appendChild(h("p", { class: "muted small" }, rows.length + " of " + st.statements.length + " statements. Select a row to see it in the original message text."));
      holder.appendChild(table(["Id", "Sender (role)", "Key", "Value", "Treated as", "Reason", "Quote"], rows.map(function (s) {
        var r = h("tr", { class: "clickable" + (E.focus === s.id ? " hl" : ""), tabindex: "0", "data-stmt": s.id, onclick: function () { showSource(s); }, onkeydown: function (e) { if (e.key === "Enter") showSource(s); } },
          cell(s.id), cell((s.sender || "unknown") + " (" + (s.role || "?") + ")"), cell(s.key + (s.ent ? "[" + s.ent + "]" : "")), cell(fval(s.val)), cell(tag(s.disposition)), cell(s.reason || ""), cell(s.quote || ""));
        return r;
      })));
      if (E.focus) { var f = st.statements.filter(function (x) { return x.id === E.focus; })[0]; if (f) showSource(f); }
    }
    function showSource(s) {
      clear(viewer);
      var src = st.sources.filter(function (x) { return x.source_id === s.source_id || (x.run_no === s.run_no); })[0];
      viewer.appendChild(h("h3", null, "Original text for " + s.id));
      if (!src || src.text == null) { viewer.appendChild(h("p", { class: "muted" }, "The original text is not available as text (it may not be valid UTF-8).")); return; }
      var lines = src.text.split("\n"), lo = s.lines ? s.lines[0] : 0, hi = s.lines ? s.lines[1] : 0;
      viewer.appendChild(h("p", { class: "muted small" }, (src.filename || "input") + ", sha256 " + (src.sha256 || "").slice(0, 16) + "..., " + lines.length + " lines. The highlighted lines contain the quoted evidence."));
      var box = h("div", { class: "audit" , role: "region", "aria-label": "Original message text"});
      box.className = "audit";
      lines.forEach(function (ln, i) { box.appendChild(h("div", { class: "src-line" + (i + 1 >= lo && i + 1 <= hi ? " hit" : "") }, h("span", { class: "ln" }, String(i + 1)), h("span", null, ln || " "))); });
      viewer.appendChild(box);
    }
    drawRows();
  }

  /* ------------------------------------------------------------ chat */
  var chatOpener = null;
  function openChat() {
    var c = document.getElementById("chat"); c.hidden = false; c.setAttribute("aria-hidden", "false");
    document.getElementById("chat-open").setAttribute("aria-expanded", "true");
    chatOpener = document.getElementById("chat-open"); document.getElementById("chat-input").focus();
  }
  function closeChat() {
    var c = document.getElementById("chat"); c.hidden = true; c.setAttribute("aria-hidden", "true");
    document.getElementById("chat-open").setAttribute("aria-expanded", "false"); if (chatOpener) chatOpener.focus();
  }
  var SAMPLES = ["How many judges are assigned and does every team have one?", "Budget mein kitna bacha hai?", "Which messages were rejected and why?", "Ghalat message kaunsa tha aur kyun reject hua?",
    "Which areas changed in the last update?", "Which quotes were superseded?", "Why was this venue chosen?", "What are the deadlines and limits?", "How many tokens and model calls were used?"];
  function ask(question) {
    var log = document.getElementById("chat-log");
    log.appendChild(h("div", { class: "msg q" }, question));
    var wait = h("div", { class: "msg a muted" }, "Looking in the plan and evidence..."); log.appendChild(wait); log.scrollTop = log.scrollHeight;
    getJSON("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: question }) }).then(function (r) {
      log.removeChild(wait);
      var cites = h("div", { class: "cites" });
      (r.citations || []).forEach(function (c) {
        if (c.type === "message") cites.appendChild(h("button", { type: "button", onclick: function () { focusEvidence(c.id); } }, c.label || c.id));
        else if (c.type === "trace") cites.appendChild(h("button", { type: "button", onclick: function () { showTab("trace"); } }, c.label));
        else cites.appendChild(h("span", { class: "tag" }, c.label));
      });
      log.appendChild(h("div", { class: "msg a" }, h("div", { class: "src" }, "Source: " + r.collection), h("div", { class: "body" }, r.answer), cites));
      log.scrollTop = log.scrollHeight;
    }).catch(function (e) { log.removeChild(wait); log.appendChild(h("div", { class: "msg a" }, h("div", { class: "banner bad" }, "Could not answer: " + e.message))); });
  }
  function bindChat() {
    var samples = document.getElementById("chat-samples");
    SAMPLES.forEach(function (s) { samples.appendChild(h("button", { type: "button", onclick: function () { ask(s); } }, s)); });
    document.getElementById("chat-open").addEventListener("click", openChat);
    document.getElementById("chat-close").addEventListener("click", closeChat);
    document.getElementById("chat-form").addEventListener("submit", function (e) {
      e.preventDefault(); var i = document.getElementById("chat-input"), v = i.value.trim(); if (!v) return; i.value = ""; ask(v);
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !document.getElementById("chat").hidden) closeChat(); });
  }


  /* ------------------------------------------------------------ compose (paste a pack or a message) */
  var C = { polling: null, armed: false };
  function postJSON(url, body) {
    return getJSON(url, { method: "POST", headers: { "Content-Type": "application/json", "X-PlanLedger": "compose" }, body: JSON.stringify(body) });
  }

  /* ------------------------------------------------------------ simple home view */
  function clockOff(m) { var h2 = Math.floor(m / 60), mm = m % 60; return h2 + ":" + (mm < 10 ? "0" : "") + mm; }
  function renderHome(st) {
    var box = document.getElementById("home-result"); if (!box) return; clear(box);
    var a = st.active;
    var hs = document.getElementById("hero-stats"); if (hs) {
      clear(hs);
      if (a) [[a.checks.passed + tx(" of ", " / ") + a.checks.total, tx("rules followed", "اصول پورے ہوئے")], [String(st.messages.length), tx("messages read", "پیغامات پڑھے گئے")], [String(st.revisions.length), tx("plan versions", "پلان ورژن")]].forEach(function (x) {
        hs.appendChild(h("div", { class: "stat" }, h("b", null, x[0]), h("span", null, x[1])));
      });
    }
    if (!a) {
      box.appendChild(h("div", { class: "empty big" }, h("h2", null, tx("No plan yet", "ابھی کوئی پلان نہیں")),
        h("p", null, tx("Paste your messages in the box above and press Make the plan. The first time, paste all the messages together. Later, paste one new message at a time.", "اوپر باکس میں اپنے پیغامات پیسٹ کریں اور پلان بنائیں دبائیں۔ پہلی بار تمام پیغامات ایک ساتھ پیسٹ کریں۔ بعد میں ایک وقت میں ایک نیا پیغام۔"))));
      return;
    }
    var plan = a.plan;
    box.appendChild(plainSummary(st));
    var sec = function (title, el) { box.appendChild(h("section", { class: "simple-sec" }, h("h2", null, title), el)); };
    sec(tx("The day, hour by hour", "دن کا شیڈول"), h("ol", { class: "daylist" }, plan.schedule.segments.map(function (s) {
      return h("li", null, h("b", null, clockOff(s.start) + tx(" to ", " سے ") + clockOff(s.end)), " " + s.label + " (" + s.dur + tx(" minutes)", " منٹ)"));
    })));
    var lines = plan.budget.lines.map(function (l) { return h("li", null, h("span", null, l.label + (l.qty ? " (" + l.qty + " x " + pkr(l.rate) + ")" : "")), h("b", null, pkr(l.amount))); });
    lines.push(h("li", { class: "total" }, h("span", null, tx("Total", "کل")), h("b", null, pkr(plan.budget.total))));
    sec(tx("Where the money goes", "رقم کہاں جاتی ہے"), h("ul", { class: "moneylist" }, lines));
    sec(tx("What happened to each message", "ہر پیغام کا کیا ہوا"), storyList(st));
    var rej = st.statements.filter(function (s) { return s.disposition === "rejected"; });
    var held = st.statements.filter(function (s) { return s.disposition === "needs_clarification"; });
    if (rej.length) {
      sec(tx("Messages that were refused", "مسترد کیے گئے پیغامات"), h("div", { class: "refused-list" }, rej.slice().reverse().map(function (s) {
        return h("div", { class: "refused" }, h("b", null, tx("From " + (s.sender || "someone") + ":", (s.sender || "کسی") + " کی طرف سے:")), h("blockquote", { dir: "auto" }, s.quote || ""), h("p", null, tx("Why it was refused: ", "مسترد ہونے کی وجہ:") + " ", h("span", { class: "why", dir: "ltr" }, s.reason || "")));
      })));
    }
    if (held.length) {
      sec(tx("Could not be confirmed (ignored for safety)", "تصدیق نہیں ہو سکی (حفاظت کے لیے نظرانداز)"), h("p", { class: "muted" }, tx(held.length + " statement(s) could not be matched to the original words, so the plan does not use them. Open the technical details to see which.", held.length + " بیانات اصل الفاظ سے میل نہیں کھائے، اس لیے پلان انہیں استعمال نہیں کرتا۔ کون سے ہیں، یہ دیکھنے کے لیے تکنیکی تفصیل کھولیں۔")));
    }
  }

  function buildCompose() {
    var p = document.getElementById("panel-compose"); clear(p);
    p.appendChild(h("h2", { class: "hero" }, tx("Meet PlanLedger, Your Event Plan Checker", "PlanLedger سے ملیے، آپ کا ایونٹ پلان چیکر")));
    p.appendChild(h("p", { class: "muted", id: "compose-hint" }, tx("Paste all your emails and chats about the event. We build the plan, check nine rules, and refuse any message that would break it.", "ایونٹ کی تمام ای میلز اور چیٹس یہاں پیسٹ کریں۔ ہم پلان بناتے ہیں، نو اصول جانچتے ہیں، اور جو پیغام پلان کو توڑے اسے مسترد کر دیتے ہیں۔")));
    p.appendChild(h("div", { class: "chips-row" }, tx(["Reads messy messages", "Checks nine rules", "Refuses wrong updates", "Shows every decision"],
      ["بکھرے پیغامات پڑھتا ہے", "نو اصول جانچتا ہے", "غلط اپ ڈیٹ مسترد کرتا ہے", "ہر فیصلہ دکھاتا ہے"]).map(function (t) { return h("span", null, t); })));
    var ta = h("textarea", { id: "compose-text", rows: "7", dir: "auto", placeholder: tx("Paste the message pack or a new message here...", "میسج پیک یا نیا پیغام یہاں پیسٹ کریں..."), "aria-label": tx("Message text", "پیغام کا متن") });
    var send = h("button", { class: "btn", id: "compose-send", type: "button" }, tx("Make the plan", "پلان بنائیں"));
    var reset = h("button", { class: "btn secondary", id: "compose-reset", type: "button" }, tx("Start over", "دوبارہ شروع کریں"));
    var status = h("p", { id: "compose-status", class: "muted", "aria-live": "polite" }, "");
    var log = h("pre", { class: "audit", id: "compose-log", hidden: "hidden", "aria-label": "Run output" });
    p.appendChild(h("div", { class: "composer" }, ta, h("div", { class: "toolbar" }, send, reset, status)));
    p.appendChild(log);
    p.appendChild(h("div", { id: "hero-stats", class: "hero-stats" }));
    p.appendChild(h("div", { id: "home-result", "aria-live": "polite" }));
    p.appendChild(h("details", { class: "howto", open: true }, h("summary", null, tx("How does this work?", "یہ کیسے کام کرتا ہے؟")), h("div", { class: "steps", role: "list" },
      tx([["1. Paste", "Paste the emails and chats about the event, exactly as received."], ["2. Read", "An AI model reads them and marks each fact with the words that prove it."],
       ["3. Plan", "Ordinary code, not the AI, chooses the venue, food, schedule and judges."], ["4. Check", "Nine rules are checked. A wrong message is refused and explained."]],
      [["1. پیسٹ", "ایونٹ کی ای میلز اور چیٹس جیسے ملیں ویسے ہی پیسٹ کریں۔"], ["2. پڑھنا", "اے آئی ماڈل انہیں پڑھتا ہے اور ہر بات کے ساتھ وہ الفاظ لکھتا ہے جو اسے ثابت کرتے ہیں۔"],
       ["3. منصوبہ", "اے آئی نہیں، عام کوڈ وینیو، کھانا، شیڈول اور ججز منتخب کرتا ہے۔"], ["4. جانچ", "نو اصول جانچے جاتے ہیں۔ غلط پیغام مسترد ہوتا ہے اور وجہ بتائی جاتی ہے۔"]]).map(function (x) {
        return h("div", { class: "step", role: "listitem" }, h("b", null, x[0]), h("span", null, x[1]));
      }))));
    function setBusy(b) { send.disabled = b; reset.disabled = b; ta.disabled = b; }
    function watch() {
      getJSON("/api/job").then(function (j) {
        log.hidden = !j.lines.length; log.textContent = j.lines.join("\n"); log.scrollTop = log.scrollHeight;
        if (j.running) { status.textContent = tx("Working on it. Reading your messages and building the plan takes about one to two minutes. Please wait.", "کام جاری ہے۔ پیغامات پڑھنے اور پلان بنانے میں ایک سے دو منٹ لگتے ہیں۔ براہِ کرم انتظار کریں۔"); C.polling = setTimeout(watch, 1000); return; }
        setBusy(false);
        status.textContent = j.code === 0 ? tx("Done. The plan is below.", "ہو گیا۔ پلان نیچے ہے۔") : tx("Something went wrong. Open the technical details to see what.", "کچھ غلط ہو گیا۔ وجہ دیکھنے کے لیے تکنیکی تفصیل کھولیں۔");
        refresh().then(function () { if (j.code === 0 && !document.body.classList.contains("simple")) showTab("overview"); });
      }).catch(function (e) { status.textContent = "Lost contact with the local server: " + e.message; setBusy(false); });
    }
    send.addEventListener("click", function () {
      var v = ta.value; if (!v.trim()) { status.textContent = tx("Nothing to send yet.", "ابھی بھیجنے کے لیے کچھ نہیں ہے۔"); return; }
      setBusy(true); status.textContent = tx("Sending...", "بھیج رہے ہیں...");
      postJSON("/api/ingest", { text: v }).then(function () { ta.value = ""; watch(); }).catch(function (e) { status.textContent = "Not sent: " + e.message; setBusy(false); });
    });
    reset.addEventListener("click", function () {
      if (!C.armed) { C.armed = true; reset.textContent = tx("Press again to erase everything", "سب مٹانے کے لیے دوبارہ دبائیں"); setTimeout(function () { C.armed = false; reset.textContent = tx("Start over", "دوبارہ شروع کریں"); }, 5000); return; }
      C.armed = false; reset.textContent = tx("Start over", "دوبارہ شروع کریں");
      postJSON("/api/reset", { confirm: "DELETE" }).then(function () { log.hidden = true; status.textContent = tx("All data cleared. Paste the message pack to start.", "تمام ڈیٹا صاف۔ شروع کرنے کے لیے میسج پیک پیسٹ کریں۔"); refresh(); })
        .catch(function (e) { status.textContent = "Not cleared: " + e.message; });
    });
    getJSON("/api/job").then(function (j) { if (j.running) { setBusy(true); watch(); } });
  }

  /* ------------------------------------------------------------ data loop */
  function renderAll(st) {
    S.state = st;
    renderStatus(st); renderHome(st); renderOverview(st); renderChanges(st); renderSchedule(st); renderBudget(st); renderTrace(st); renderEvidence(st);
  }
  function refresh() {
    return getJSON("/api/state").then(function (st) { S.fp = st.fingerprint; renderAll(st); }).catch(function (e) {
      var bar = document.getElementById("statusbar"); clear(bar); bar.appendChild(h("span", { class: "chip bad" }, "Cannot reach the local server: " + e.message));
    });
  }
  function poll() {
    if (document.hidden) return;
    getJSON("/api/version").then(function (v) { if (v.fingerprint !== S.fp) refresh(); }).catch(function () { /* keep showing the last good state */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyPrefs(); bindPrefs(); bindTabs(); bindChat(); bindLang(); buildCompose();
    showTab(load("tab", "compose")); applyMode(); applyLang();
    refresh();
    setInterval(poll, 2500);
  });
})();
