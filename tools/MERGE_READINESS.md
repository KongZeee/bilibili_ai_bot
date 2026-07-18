# Merge readiness: `research/jul17-brain-coverage` → `main`

Date: 2026-07-18  
HEAD: `210bb52` (exp64)  
Base: `main` @ `c4e74c3`  
Commits ahead: **66**  
Diff: **15 files**, +4923 / −88

## Goal of this branch

Make the bot's memory a real continuous "brain":

1. Accurate self-memory recall (QA precision)
2. Every activity reads + writes memory (begin/finish lifecycle)
3. Continuous SelfState (`salient_recent` / `ongoing_threads`) injected into generation
4. Behavior-grounding metrics prove memory is *used while acting*, not only answerable later

## Gate results (must stay green)

| Gate | Result |
|------|--------|
| `pytest tests/test_memory_brain_fallback_precision.py` | **18 passed** |
| `python tools/bench_brain_coverage.py` | **coverage_score 100** (12/12 scenes, 8/8 hard precision) |
| `python tools/bench_brain_behavior.py` | **behavior_score 100** (injection/lifecycle/self_state/qa/reject) |
| `python tools/bench_brain_e2e_real.py --no-llm` | **e2e_score 100** (probe 16/16, hit 8/8, reject 5/5) |

Primary climb metric going forward: **`behavior_score`**.  
Frozen QA harnesses are **regression gates only**.

## What landed (by layer)

### Recall precision (`bilibot/memory_brain/recall.py`)

- Utility/smalltalk fail-closed
- Self-memory genres: dynamic / open-watch / schedule / weekly / dream / PM / like / self-comment / open-recent / exploration
- ATRI Latin-entity title exclusivity
- Intent hidden when completed outcome exists
- Open-watch: bangumi exclusion, cap 2, same-title dedupe, score-gap
- Open-recent: diversify by source_type + bot_action guarantee

### Production lifecycle

- `reply.py`: begin/finish activity on comment/PM generation
- `scheduler.py`: PM begin/finish; video/dynamic/comment/like SelfState hooks
- `bangumi.py`: finish_activity after episode eval
- `companion/service.py`: dream/diary/plan/explore/creative begin+finish; empty creative → `failed`

### Continuous self (`LifeState`)

- `salient_recent` + `ongoing_threads`
- Updated on companion finishes + scheduler operational finishes
- `get_prompt_surface()` injects `进行中` / `刚经历` into replies
- Generation recall attaches `【连续自我状态】` + scene recipes

### Harnesses / docs

- `tools/bench_brain_coverage.py` — frozen hard-precision judge
- `tools/bench_brain_e2e_real.py` — real DB e2e (copies DB, no live mutate)
- `tools/bench_brain_behavior.py` — **behavior grounding climb metric**
- `tools/ACTIVITY_BRAIN_AUDIT.md` — activity begin/finish matrix
- `tests/test_memory_brain_fallback_precision.py` — 18 regressions

## Working tree hygiene before PR

Untracked (do **not** commit secrets; these are local research logs):

- `results_brain.tsv` — experiment log (safe text metrics; optional commit)
- `results.tsv` — unrelated older perf log

No secrets found in those TSV files by keyword scan.  
Recommend: either add `results_brain.tsv` as research artifact **or** gitignore it; leave `results.tsv` untracked.

## Known residual risks (non-blocking)

1. **PM preview in SelfState** — uses already-redacted `safe_reply_text` (bot outgoing). Still lands in local `life_state.json`; acceptable for persona bot text, not user inbound raw.
2. **like/coin/fav** still write-only (no begin_activity) — intentional.
3. **Day consolidator** not implemented — P2.
4. **Real LLM grounded generation judge** not automated — optional.
5. **66 commits** — squash or keep history? Research branch history is valuable for experiment trail; PR can squash if desired.
6. **`results_brain.tsv` duplicate exp63 line** — cosmetic if committed.

## Suggested PR title / body

**Title:** Memory brain: continuous self + behavior grounding (recall precision + activity lifecycle)

**Body sketch:**

```markdown
## Summary
- Harden multi-genre self-memory recall (watch/dynamic/dream/PM/like/comment/open-recent)
- Close activity begin/finish gaps (PM, bangumi, empty creative)
- Add LifeState continuous self (salient_recent / ongoing_threads) for generation + replies
- New climb metric: tools/bench_brain_behavior.py (injection under noise + SelfState)

## Test plan
- [x] pytest tests/test_memory_brain_fallback_precision.py
- [x] python tools/bench_brain_coverage.py
- [x] python tools/bench_brain_behavior.py
- [x] python tools/bench_brain_e2e_real.py --no-llm
- [ ] optional: e2e with --llm if provider available
```

## Merge recommendation

**Ready to open PR** after:

1. Decide whether to include `results_brain.tsv`
2. Optional: fix any review findings that come back as CONFIRMED
3. Optional squash strategy for 66 commits

Do **not** merge without human review of `scheduler.py` PM path and `recall.py` genre hard-zeros (high blast radius).
