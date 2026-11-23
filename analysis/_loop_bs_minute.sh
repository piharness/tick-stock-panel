#!/usr/bin/env bash
# 分钟回补无人值守驱动：预算逐级加码。
# 为什么加码而不是一次不限：3 并发已实测 30M 行无事，但不知道上限在哪；
# 每轮 +40M，被封时 ABORT 立即退出（不写坏 state），下一轮 --wait-unban 等解封。
# 终止条件只有「待跑 0」，不看退出码——QUOTA_STOP / ABORT 都是计划内状态。
cd /d/DSH/tick-stock-panel || exit 1
PY=backend/.venv/Scripts/python.exe
LOG=data/backfill_bs_minute/_run_1120loop.log
for B in 70000000 110000000 150000000 190000000 230000000 400000000; do
  echo "===== $(date '+%F %T') round budget=$B =====" | tee -a "$LOG"
  $PY analysis/backfill_bs_minute.py --workers 3 --order random --seed 42 \
      --max-rows "$B" --wait-unban 8 2>&1 | tee -a "$LOG"
  if $PY analysis/backfill_bs_minute.py --status 2>/dev/null | grep -q "待跑          0"; then
    echo "===== $(date '+%F %T') ALL DONE =====" | tee -a "$LOG"
    break
  fi
done
