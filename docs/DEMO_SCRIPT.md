# Judge demo script (about 4 minutes)

Before you start: `lsof -ti :8765 | xargs kill`, then `rm -rf .planledger-data`, then `python3 -m planledger.web`, then open http://127.0.0.1:8765.
Warm the model once: `curl -s localhost:11434/api/generate -d '{"model":"qwen2.5-7b-8k","prompt":"hi","keep_alive":-1,"stream":false}'`

## One sentence for the judges
"PlanLedger reads messy emails and chats, plans the 4-hour hackathon, and refuses any message that would break the plan, and it shows the proof for every decision."

## Steps
1. Tab "1. Add messages". Paste the whole of `demo_inputs/1_first_pack.txt`, press Send. Wait about one minute.
   Say: "These are eight real-looking messages: emails, WhatsApp, old prices and new prices, a DJ request, a judge saying he may be late."
2. Tab "2. The plan". Point at the green box: valid, 9 of 9 rules followed, then the four lines (event, food, judging, money).
   Say: "The AI only reads. Normal code makes the plan. Then nine independent rules check it."
   Point out: caterer price 1,000 was replaced by the newer 1,200; the DJ request is not in the budget because nobody approved it; the judge's lateness is ignored.
3. Back to "1. Add messages". Paste `demo_inputs/2_correct_update.txt` (printing bill is Rs 15,000, not 10,000). Send.
   Open "3. What changed..." : only the budget changed; venue, food, judging, schedule were kept and not recomputed.
4. Paste `demo_inputs/3_wrong_update.txt` (caterer cuts the order to 100 boxes). Send.
   Open "3. What changed...": REFUSED, with the reason (100 meals for 167 people) and the original words as proof. The plan is unchanged.
   Say: "A wrong message does not damage the plan."
5. Optional: paste `demo_inputs/4_not_allowed_update.txt` (a student society tries to remove the prayer break). Expect it to be refused because that sender has no authority. Try it once before the demo.
6. Tab "How it was decided": every model call with tokens and time. Say: "Five calls at most per update, one minute limit."
7. Press "Ask the Plan" and type: `Why Hall A?` then `budget kitna bacha hai?` Answers come from the saved plan with evidence, in English or Roman Urdu.
8. Official path: `python3 -m planledger demo_inputs/1_first_pack.txt` and later `python3 -m planledger demo_inputs/2_correct_update.txt` give the same result in the terminal.

## If something goes wrong
- Plan says PROVISIONAL: say it is deliberate, the system refuses to claim VALID when the small model missed a number. Press Clear all data and send again.
- Model slow: it is a 7B model on a laptop; the limit is 2 minutes for the first pack.
