# Student Brief: procedural rules only
Source: the organizers' Student Brief (Mind to Machine Hackathon). This file holds rules and procedures, never pack facts, sample messages or sample values. Page numbers refer to the 4-page PDF.

## [Brief p.1] The task
Build an agent system that reads a pack of messages and plans a 4-hour physical hackathon in Islamabad. The plan must cover five areas: venue, schedule, food, judging and budget. It must respect every constraint the messages state, including kinds of constraint that do not appear in the sample pack.

## [Brief p.1] The message pack
Every fact the system needs is inside a pack of about ten messages: emails, WhatsApp texts and notices, in English and Roman Urdu. Some messages are irrelevant and some are out of date. At judging the pack is unseen, with different numbers, different wording and different senders.

## [Brief p.1] What the system must do
1. Read every message from a text file, exactly as given. Do not type facts into code.
2. Take every message in the same way, through the same command. No flags, hints or edits.
3. Print a trace: every model call numbered, with the model name, one area tag (reading, venue, schedule, food, judging or budget), its input, its output and its token counts as the model reports them. End the trace with total tokens.
4. Accept new messages after the first run and update the existing plan.
5. When a message conflicts with a rule or with other facts, keep the plan valid and print the reason in the trace.

## [Brief p.1] Limits
Bring your own model access: any model, or several; paid, free or running on a laptop. A full run is cut off after 2 minutes. An update after a new message may use at most 5 model calls and is cut off after 1 minute. Any AI tools may be used to write the code. A user interface is scored: show the plan, the trace and any rejected messages clearly. A web page, an app or well-formatted terminal output can all score well. The printed trace is still required.

## [Brief p.1-2] The day and the code freeze
Build starts at 10:00. Code freeze is at 1:25 PM: push the code and submit the repo link and commit hash. No changes after this. The whole-room test run is at 1:30 PM. Judges visit tables from 2:30 to 4:30 PM.

## [Brief p.2] Before you start
Create a git repo now and push often; add the organizers if it is private. Test model access in the first few minutes: keys, rate limits and speed. A local model must finish a full run within 2 minutes. Ask rule questions only at the help desk: judges do not answer questions during the build, and every clarification is posted in the all-teams WhatsApp group and announced in both rooms.

## [Brief p.2] How the test run works
Every team in both rooms runs the same test at the same moment while a volunteer times the runs and records what happens. Before running, the volunteer checks that git status shows no changes and that git log -1 matches the submitted hash. The unseen message pack is released by link and run with the one command. Three more messages are released one at a time and each is run as it arrives. Before one of them, write down which areas will make model calls and hand the slip to the volunteer. Two of the three messages change the task; one is plausible but wrong and teams are not told which. Save the final plan and full trace to a file and upload it on the form.

## [Brief p.2] The judge visits
Two judges visit the table separately and all three members must stay at the table. Each judge looks at the saved plan, trace and interface, then draws one member by lot and asks two questions about the system. There are no presentations and no slides. The team may walk the judge through the trace and interface.

## [Brief p.2-3] How points are awarded (100)
Output 30: the plan from the unseen pack passes six checks: correct figures, within budget, venue fits, schedule valid, enough judges, all areas covered. Catching the wrong message 20: the plan stays valid and the trace says why the message was rejected. Understanding 20: the drawn member explains what each part of the system is for and what would break without it. Handling change 10: after each change the plan is valid again and only the affected areas made model calls. UI 10: the interface shows plan, trace and rejected messages clearly and is easy to use. Predicting a change 5: the written prediction matches what the system did. Token use 5: the full run is among the leanest in the room and the plan passes every check.

## [Brief p.3] Bonus and tie-breaks
Bonus 10 points if every model call in the runs uses a model on the organizers' small-model list, which includes any model running on your own laptop; the plan must pass every check. Tie-breaks in order: catching the wrong message, handling change, token use.

## [Brief p.3] Things to know
Fewer tokens score higher: use a model where language needs reading, and plain code where arithmetic will do. The number of agents earns nothing, and a part of the system you cannot justify costs points. You are scored on what you understand and what you decided, not on how much code you have. Every member should be able to explain the system. No dataset is needed: build against the sample pack, then test with messages you write yourself, but never type pack facts or messages into code. Results are provisional until the top five teams' code is reviewed against their submitted commit. A trace that misreports tokens, models or areas, code with facts or messages typed in, or code changed after 1:25 disqualifies the team.
