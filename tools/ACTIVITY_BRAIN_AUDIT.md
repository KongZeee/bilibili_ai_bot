# Activity brain audit (P0)

Snapshot after research/jul19-brain SelfSnapshot + MotiveQueue + working_memory.

| Activity | begin | finish | domain archive | generation injects memory | Notes |
|----------|-------|--------|----------------|---------------------------|-------|
| dream | yes `write_dream` | yes | `dream` | yes via `_recall_life_evidence` | OK |
| diary | yes `write_diary` | yes | `diary` | yes | OK |
| daily plan | yes `create_daily_plan` | yes | `life_plan` | yes | disabled path may skip brain |
| life detail | yes `expand_life_detail` | yes | `life_plan` | yes | OK |
| explore | yes `explore_topic` | yes | `web_reference` | yes | begin before search |
| creative project | yes | yes | `creative` | yes | OK |
| creative chunk | yes | yes (incl. empty→failed) | `creative` status line | yes | empty path fixed exp60 |
| reply comment | yes | yes | bot_action | yes | OK |
| private message | yes (exp57) | yes on terminals | `private_message` + bot_action | partial (recall + activity) | OK |
| proactive video eval | yes | yes via archive | video + experience + bot_action | yes | OK |
| video like | yes | yes | bot_action | yes | begin before API |
| video coin | yes | yes | bot_action | yes | begin before API |
| bangumi episode | yes | yes (exp47) | bangumi/bot_action | yes | OK |
| weekly summary | yes | yes via archive | weekly | yes | OK |
| dynamic post | yes | yes via archive | bot_action 动态 | yes | OK |

## Known residual gaps (P1+)

1. favourite (fav) may still be terminal-only without begin context.
2. ~~creative chunk archive is a short status line~~ — fixed exp63 (full prose capped).
3. companion JSON store dual-track with memory_brain (SelfSnapshot partial unify; not single SQLite self table).
4. ~~generation queries still bag-of-words~~ — exp64 scene recipes + SelfState needles (QA path unchanged).
5. ~~scheduler video/PM paths do not push LifeState~~ — fixed exp62 (video/dynamic/PM/comment/like).

## Metrics

- Gate: `tools/bench_brain_coverage.py` coverage_score=100
- Gate: `tools/bench_brain_e2e_real.py` e2e_score≈100
- Climb: `tools/bench_brain_behavior.py` behavior_score
- Climb: `tools/bench_brain_completeness.py` completeness_score (0-10)
