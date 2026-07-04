---
description: Run one docs/specs/ spec through the tiered implement→review flow (cheap-model implementation, top-model review)
---

Run the tiered development flow from docs/AGENTS.md for the spec at: $ARGUMENTS

1. Verify the spec file exists and its status is `ready`. If not, stop and say so.
2. Spawn the `koe-implementer` agent (it runs on its configured cheaper model)
   with exactly this prompt — do not paste your conversation history into it:

   "Implement the spec at <spec path> in this repository, following your agent
   instructions. Work in the current working tree."

3. When it returns, YOU are the reviewer (top model). Review the DIFF, not the
   transcript: `git diff` / `git status`, run `python -m pytest -q` and
   `python -m compileall koe` yourself, and check the self-check table against
   the spec's acceptance criteria and CLAUDE.md invariants (especially: lazy
   Windows imports, no config key renames, graceful degradation, D-numbered
   decisions in docs/DECISIONS.md).
4. If there are findings: send them to the SAME implementer agent as a
   numbered list via SendMessage (keep its context — cheaper than a fresh
   run). Maximum 2 review rounds; if a 3rd would be needed, take over, fix it
   yourself, and record what the spec should have said (update the spec).
5. On pass: write/adjust any judgment-bearing docs yourself (README JA+EN,
   ROADMAP, DECISIONS if applicable), set the spec's status line to
   `implemented`, commit in the repo's `Area: what changed` style, and push to
   the designated branch.
6. Report: what shipped, review rounds used, and the manual on-Windows checks
   for the owner.
