# Realtime Text Source Discovery

本实验只评估 side-channel realtime text arms 是否足以支撑未来的 realtime structured assistance；正式 `transcript.md`、`structured.md` 和 publish 语义保持 finalized-only truth model，不因本实验改变。

## Runner

执行命令：

```bash
python -m live_note.app.realtime_text_experiment --fixtures tests/fixtures/realtime_text_eval --output .sisyphus/evidence/final/realtime-text-report
```

输出：

- `.sisyphus/evidence/final/realtime-text-report.json`
- `.sisyphus/evidence/final/realtime-text-report.md`

## Fixtures

- 固定输入目录：`tests/fixtures/realtime_text_eval`
- 每次运行重放同一组 replay checkpoints
- 不读真实用户数据，不改写任何 canonical session artifacts

## Arms

- `A0 current_live_text_baseline`：直接重放 `live_draft` checkpoint 文本
- `A1 finalized_segment_window`：仅基于 `canonical_final`，按 finalized segment window 发射
- `A2 stabilized_rolling_window`：固定 cadence 发射 rolling window，稳定后冻结 chunk
- `A3 mini_refine_recent_window`：对最近窗口做轻量 decode，输出不可回写 canonical artifacts
- `A4 funasr_phase2`：deferred phase-2；报告中列为 not-run，不计为 failed

## Metrics And Thresholds

- Primary OEC：`Lag-Bounded Assistance F1 (LBA-F1) >= 0.80`
- `precision >= 0.85`
- `P95 latency <= 12000ms`
- `unsupported item rate <= 0.10/min`
- `retraction rate <= 0.10`
- `usable checkpoint coverage >= 0.70`
- `WER/CER` 仅作 diagnostic，不参与 winner logic

## Verdict Contract

- 任一 arm 未满足绝对阈值，即该 arm `failed_thresholds`
- 若所有 `A0-A3` 都失败，canonical verdict 为 `REJECT_ALL_ARMS`
- 若至少一条 arm 过绝对阈值，但没有形成 pilot-ready 相对优势，canonical verdict 为 `PROMOTE_BEST_ARM_TO_SHADOW`
- 若某 arm 相比 `A1 finalized_segment_window` Pareto-better，或在 `0.02` LBA-F1 内且 `P95 latency` 改善至少 `30%`，canonical verdict 为 `READY_FOR_REALTIME_ASSISTANCE_PILOT`

## Smoke Procedure

1. 运行 runner 命令
2. 确认 JSON/Markdown 均生成
3. 确认报告包含 `A0-A3` 全量 metrics
4. 确认 `A4 funasr_phase2` 显示为 deferred phase-2 / not-run
5. 记录 canonical verdict，不额外声称 launch readiness
