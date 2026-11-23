#!/usr/bin/env bash
# 分钟回补无人值守驱动（2026-11-22 版）：预算逐级加码 + 跨日自动续跑。
#
# 为什么是逐级加码而不是 --max-rows 0（不限）：
#   撞日配额被封会赔掉当天剩余的全部时间（11-20 赔了 6 小时以上），主动停只损失几分钟。
#   实测读数：30.1M 无事（11-20 凌晨）、41M 无事（11-21 08:30-12:20，之后封）、
#   45.2M 无事（11-22 凌晨）、**69.5M 处封**（11-22 06:25-07:03）。
#   ⇒ 配额不是固定阈值，是波动的（40M 封顶那条判断已被 45.2M 推翻）；
#   逐轮 +10M 只是「小步试边界」，不是配额估计。
# 为什么循环里 sleep 1800：
#   --max-rows 的计数器从 _state.jsonl 的 ts 复算，**跨 0 点自动重置**；
#   当天预算耗尽后空转 30 分钟再起，等于等新的一天，不需要人守。
cd /d/DSH/tick-stock-panel || exit 1
PY=backend/.venv/Scripts/python.exe
LOG=data/backfill_bs_minute/_run_1122loop.log
for B in 50000000 60000000 70000000 80000000; do
  echo "===== $(date '+%F %T') round budget=$B =====" | tee -a "$LOG"
  $PY analysis/backfill_bs_minute.py --workers 3 --order random --seed 42 \
      --max-rows "$B" --wait-unban 8 2>&1 | tee -a "$LOG"
  # 判「全部下完」用的是 --status 的 **待跑 数字**，不是固定空格子串匹配：
  # 原来写 `grep -q "待跑          0"`，11-22 08:11 曾打出过一条假的 ALL DONE
  # （日志里 ALL DONE 之后 python 还在跑、待跑仍有 119 只）——靠空格数匹配的判据
  # 一旦输出格式/对齐变化就会静默失准，这正是本项目「绿但无感知」的老毛病。
  LEFT=$($PY analysis/backfill_bs_minute.py --status 2>/dev/null | awk '/^待跑/{print $2}')
  echo "----- $(date '+%F %T') 剩余待跑=${LEFT:-<status 读取失败>} -----" | tee -a "$LOG"
  if [ "${LEFT:-x}" = "0" ]; then
    echo "===== $(date '+%F %T') ALL DONE =====" | tee -a "$LOG"
    break
  fi
  sleep 1800
done
