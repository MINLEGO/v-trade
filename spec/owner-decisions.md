# Owner decisions

Recorded: 2026-07-13.

## Resolved

- DeepSeek model slug: `deepseek/deepseek-v4-flash`.
- MiMo model slug: `xiaomi/mimo-v2.5-pro`.
- Maximum quantization: 8 bits.
- Cross-model fallback: forbidden.
- Prompt, transcript and reasoning retention: six months.
- Scheduling: each model/agent has its own start date, hourly schedule and immutable
  per-cycle data cutoff. Simultaneous start is not required. Adding/removing an agent
  does not change any existing agent.

The optional `cohort_cycles` relation may group deliberately synchronized comparison
cycles, but it is not required for normal scheduling and does not control membership.

## Still REQUIRED / owner_pending

- Paper fill rule.
- OpenRouter reasoning effort for each model, if explicitly set rather than provider default.
- OpenRouter provider allowlist/routes.
- Prompt/transcript/reasoning operator visibility.
- Prompt/transcript/reasoning redaction policy.
- Exa burst ceiling conflict: 50 in §9 versus 100 in §15 of the implementation plan.

