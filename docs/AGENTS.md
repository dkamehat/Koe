# Tiered development protocol (design ↑ / implement ↓ / review ↑)

How this project converts expensive-model judgment into cheap-model execution
without losing quality. This is the **cost-efficiency system**: follow it and
the token cost of a feature is dominated by one spec + one review, not by an
expensive model reading the repo over and over.

## Roles

| Role | Model tier | Does | Never does |
|------|-----------|------|------------|
| **Architect** | top model (Fable/Opus class) | writes specs in `docs/specs/`, makes design decisions, appends DECISIONS.md, reviews diffs, resolves escalations | line-by-line implementation of an already-written spec |
| **Implementer** | mid tier (Sonnet class) — `.claude/agents/koe-implementer.md` | implements exactly one spec per run: code + tests, self-check table | design decisions, deviating from the spec, touching files the spec doesn't name |
| **Mechanic** | small tier (Haiku class), optional | mechanical sync: README JA/EN mirroring, changelog, formatting | anything requiring judgment |

## The flow (1 spec = 1 implementer run = 1 review)

1. **Architect writes the spec** (`docs/specs/<name>.md`, template in
   `docs/specs/TEMPLATE.md`). The spec must be *verbatim-implementable*: exact
   files, signatures, constants with WHY, acceptance criteria, test list.
   Ambiguity found later is a spec bug, charged to the Architect.
2. **Implementer runs with minimal context**: it reads `CLAUDE.md`, the spec,
   and only the files the spec names. It does NOT explore the repo broadly —
   the spec is the context. Output: working tree changes + a self-check table
   (one row per acceptance criterion: pass/fail/evidence).
3. **Architect reviews the diff** (not the transcript): `git diff` + run
   `python -m pytest -q` + spot-check against DECISIONS.md invariants.
   Findings go back to the Implementer as a numbered list; the Implementer
   fixes. **Max 2 review rounds** — a third round means the spec was bad:
   Architect takes over and fixes the spec for next time.
4. Architect handles docs that need judgment (README positioning, ROADMAP,
   DECISIONS), commits with the standard message style, pushes.

## Token-efficiency rules (the point of all this)

- **Handoffs are files, not conversation.** Specs, self-check tables, and
  review findings live in the repo/PR text. Never make a cheap model inherit
  an expensive model's chat history.
- **Fresh, narrow context per run.** An implementer session that reads 5 files
  costs a fraction of one that wanders the repo. Specs name every file to read.
- **Front-load the thinking.** Every decision the Architect bakes into the
  spec (constants, edge cases, exact signatures) is a decision the cheap model
  can't get wrong and the reviewer doesn't argue about.
- **Review the diff, not the process.** The reviewer never re-reads the
  implementer's reasoning; tests + acceptance criteria carry the proof.
- **Escalate instead of retry.** If the implementer is stuck or contradicts
  the spec/DECISIONS, stop immediately and return to the Architect. Burned
  retries on an under-specified task are the main source of waste.
- **Right-size upward only on evidence.** Start implementation at mid tier;
  drop to small tier for mechanical work; move a task up a tier only after it
  has failed a review round for judgment (not compliance) reasons.

## How to run it

- In Claude Code: `/implement-spec docs/specs/<name>.md` (see
  `.claude/commands/implement-spec.md`) — spawns the implementer subagent at
  its configured tier and then guides the review.
- Manually: open a session with the mid-tier model and paste the prompt from
  the command file, substituting the spec path.

## Quality gates (unchanged from CLAUDE.md, enforced on every run)

`python -m pytest -q` green under CI constraints (pytest+requests+numpy only),
`python -m compileall koe` clean, lazy Windows imports, no renamed config keys,
graceful degradation, manual on-Windows checks listed in the handoff.
