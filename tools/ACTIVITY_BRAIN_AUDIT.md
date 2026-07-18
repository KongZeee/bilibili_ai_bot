# Activity brain audit (P0)

Snapshot after exp59 + behavior harness. Update when paths change.

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
| proactive video eval | yes | yes via archive | video + experience + bot_action | yes | like/coin write-only |
| bangumi episode | yes | yes (exp47) | bangumi/bot_action | yes | OK |
| weekly summary | yes | yes via archive | weekly | yes | OK |
| dynamic post | yes | yes via archive | bot_action 动态 | yes | OK |

## Known residual gaps (P1+)

1. like/coin/fav: terminal archive only, no begin context (acceptable write-only).
2. creative chunk archive is a short status line, not full prose (hurts chapter continuity retrieval).
3. companion JSON store dual-track with memory_brain (SelfState not unified).
4. generation queries still bag-of-words (task retrieval recipes not split from QA).
5. no SelfState.ongoing_threads yet.

## Metrics

- Gate: `tools/bench_brain_coverage.py` coverage_score=100
- Gate: `tools/bench_brain_e2e_real.py` e2e_score≈100
- Climb: `tools/bench_brain_behavior.py` behavior_score
