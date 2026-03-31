# Realtime Text Decision Template

## REJECT_ALL_ARMS

- Canonical verdict: `REJECT_ALL_ARMS`
- Interpretation: 当前 phase-1 arms 没有任何一条同时满足 LBA-F1、precision、latency、unsupported rate、retraction rate、coverage 的绝对阈值
- Action: 维持 finalized-only product truth model，不推进 realtime assistance；如需继续，进入 phase-2 并单独评估 `A4 funasr_phase2`

## PROMOTE_BEST_ARM_TO_SHADOW

- Canonical verdict: `PROMOTE_BEST_ARM_TO_SHADOW`
- Interpretation: 至少一条 phase-1 arm 达到绝对阈值，但相对 `A1 finalized_segment_window` 还不够 pilot-safe
- Action: 仅允许进入 shadow evaluation；不改变 publish path，不开启用户可见 realtime assistance

## READY_FOR_REALTIME_ASSISTANCE_PILOT

- Canonical verdict: `READY_FOR_REALTIME_ASSISTANCE_PILOT`
- Interpretation: 至少一条 phase-1 arm 在绝对阈值之上，并相对 `A1 finalized_segment_window` 达到 pilot 条件
- Action: 可以进入 realtime assistance pilot 设计与验证，但不代表产品 launch readiness
