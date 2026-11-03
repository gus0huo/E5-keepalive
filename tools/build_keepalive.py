#!/usr/bin/env python3
"""
tools/build_keepalive.py —— 读取 schedule.yml，生成 .github/workflows/keepalive.yml

纯 exact 模式：
- 每个时间段（北京时间）为次日抽取 1 个精确随机时刻，换算 UTC 写入 cron
- 随机种子 = 目标日期 + 波次：同一天重复运行结果相同（幂等），每天各不相同
- 每晚由 Rebuild Schedule 调用一次并提交；生成失败时旧时刻继续生效（降级不中断）
- 一般不需要手动运行：Actions -> Rebuild Schedule 会自动调用本脚本
"""

from __future__ import annotations

import random
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    import yaml
except ImportError:
    sys.exit("缺少 PyYAML，请先安装：pip install pyyaml")

CONFIG_PATH = "schedule.yml"
OUTPUT_PATH = ".github/workflows/keepalive.yml"


def die(msg: str) -> None:
    sys.exit(f"[配置错误] {msg}")


def parse_hhmm(text, where: str) -> int:
    """'05:30' -> 当日分钟数 330"""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        die(f"{where} 应为 HH:MM 格式（如 05:00），实际是：{text!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        die(f"{where} 应为 HH:MM 格式（如 05:00），实际是：{text!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        die(f"{where} 超出 00:00-23:59 范围：{text!r}")
    return h * 60 + m


def parse_positive_int(value, where: str) -> int:
    """'70' / 70 -> 70，必须为正整数"""
    if value is None or isinstance(value, bool):
        die(f"{where} 缺少字段")
    if isinstance(value, (int, float)):
        v = int(value)
    else:
        s = str(value).strip()
        if not s.isdigit():
            die(f"{where} 应为正整数，实际是：{value!r}")
        v = int(s)
    if v <= 0:
        die(f"{where} 需为正数：{value!r}")
    return v


def parse_percent(value, where: str) -> str:
    """'30-50' -> '30-50'；'40' -> '40'；'100' / 'rest' -> 'rest'"""
    s = str(value if value is not None else "").strip().replace("%", "").replace("％", "")
    if s.lower() in ("rest", "剩余", "100"):
        return "rest"
    m = re.match(r"^(\d{1,3})(?:\s*[-~—]\s*(\d{1,3}))?$", s)
    if not m:
        die(f'{where} percent 应为 "30-50"、"40" 或 "100"（跑完剩余），实际是：{value!r}')
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    for v in (lo, hi):
        if not 1 <= v <= 100:
            die(f"{where} percent 需在 1-100 之间：{value!r}")
    return f"{lo}-{hi}" if lo != hi else str(lo)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        die(f"找不到 {CONFIG_PATH}（应位于仓库根目录）")
    except yaml.YAMLError as exc:
        die(f"{CONFIG_PATH} 不是合法 YAML：{exc}")

    # exact 模式已移除的旧字段，出现时提示并忽略
    for legacy in ("candidate_points", "rotate"):
        if legacy in cfg:
            print(f"[提示] {legacy} 已在精确时刻模式中移除，此字段被忽略")

    waves_raw = cfg.get("waves") or []
    if not waves_raw:
        die("waves 为空，至少配置一个时间段")

    parsed = []
    plan_ref = None
    for i, w in enumerate(waves_raw):
        where = f"waves[{i}]"

        if "plan" in w:
            die(f"{where} 的 plan 字段已更名，请改用 plan_min 和 plan_max（如 plan_min: 70, plan_max: 90）")

        time_str = str(w.get("time", ""))
        if "-" not in time_str:
            die(f'{where} 缺少 time 或格式错误，应为 "HH:MM-HH:MM"，如 "05:00-07:00"')
        left, right = time_str.split("-", 1)
        start = parse_hhmm(left, f"{where}.time")
        end = parse_hhmm(right, f"{where}.time")
        if start == end:
            die(f"{where} 起止时间相同：{time_str}")

        plan_min = parse_positive_int(w.get("plan_min"), f"{where}.plan_min")
        plan_max = parse_positive_int(w.get("plan_max"), f"{where}.plan_max")
        if plan_min > plan_max:
            die(f"{where} plan_min（{plan_min}）不能大于 plan_max（{plan_max}）")
        if plan_ref is None:
            plan_ref = (plan_min, plan_max)
        elif (plan_min, plan_max) != plan_ref:
            die(
                f"各行的 plan_min/plan_max 必须一致（这是每天总量）："
                f"{where} 是 {plan_min}~{plan_max}，与第一行 {plan_ref[0]}~{plan_ref[1]} 不同"
            )

        percent = parse_percent(w.get("percent"), where)
        parsed.append({
            "index": i,
            "label": time_str,
            "start": start,
            "end": end,
            "notify": "true" if w.get("notify") else "false",
            "percent": percent,
        })

    # 校验：最后一波必须 100%（rest），其他波次不能是 rest
    last = parsed[-1]
    if last["percent"] != "rest":
        die(f'最后一波（{last["label"]}）的 percent 必须是 100（跑完剩余），当前是：{last["percent"]}')
    for w in parsed[:-1]:
        if w["percent"] == "rest":
            die(f'只有最后一波可以填 100/rest，waves[{w["index"]}]（{w["label"]}）不是最后一波')

    sum_hi = 0
    for w in parsed[:-1]:
        sum_hi += int(w["percent"].split("-")[-1])
    if sum_hi > 100:
        print(f"[提示] 前几波 percent 上限之和为 {sum_hi}%，超出部分会被自动压缩，不影响每天恰好完成总计划")

    plan_env = f"{plan_ref[0]}-{plan_ref[1]}" if plan_ref[0] != plan_ref[1] else str(plan_ref[0])
    return parsed, plan_env


def target_run_date() -> str:
    """随机时刻所属的日期：北京时间 12 点前运行算今天，12 点后算明天。
    生成器固定每晚 20:07 运行 → 生成次日的时刻；同日重复运行幂等。"""
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    day = now.date() if now.hour < 12 else now.date() + timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def pick_exact_point(wave: dict, period: str, used: set) -> tuple[int, int]:
    """在时间段内抽 1 个精确随机时刻（北京时间），换算为 UTC (小时, 分钟)。"""
    duration = (wave["end"] - wave["start"]) % 1440
    rng = random.Random(f"exact|{period}|{wave['index']}|{wave['label']}")
    offset = rng.randrange(duration)
    utc_minutes = (wave["start"] + offset - 8 * 60) % 1440   # 北京时间 - 8h = UTC
    key = (utc_minutes // 60, utc_minutes % 60)
    while key in used:            # 跨波次撞点时顺延 1 分钟
        utc_minutes = (utc_minutes + 1) % 1440
        key = (utc_minutes // 60, utc_minutes % 60)
    used.add(key)
    return key


# --------------------------------------------------------------------------- #
# keepalive.yml 模板（@@xxx@@ 为占位符，由 build() 替换）
# --------------------------------------------------------------------------- #
TEMPLATE = """# ============================================================
# 此文件由 tools/build_keepalive.py 自动生成，请勿直接修改！
# 要调整运行时间/波次/每天总量：改仓库根目录的 schedule.yml，
# 然后运行 Actions -> Rebuild Schedule（每晚 20:07 也会自动重抽时刻）
# 生成时间：@@GENERATED_AT@@ / 适用日期：@@PERIOD@@ / 每天总量：@@PLAN@@
# ============================================================
name: E5 KeepAlive

on:
  workflow_dispatch:          # 手动触发（照跑 + 推送，方便测试）
  schedule:
@@SCHEDULE@@

permissions:
  contents: read

concurrency:
  group: e5-keepalive
  cancel-in-progress: false

jobs:
  keepalive:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      # 场次识别：把触发时刻映射为波次参数（精确时刻模式，无抽签）
      - name: Decide slot
        id: decide
        env:
          EVENT: ${{ github.event_name }}
          SCHEDULE: ${{ github.event.schedule }}
        run: |
          python3 - <<'PYEOF'
          import os

          # (UTC小时, UTC分钟) -> (波次名, 是否末波, 是否推送, 本波百分比)。由生成器注入
          SLOTS = @@SLOTS@@

          event = os.environ.get("EVENT", "")
          schedule = os.environ.get("SCHEDULE", "")

          if event != "schedule":
              run, notify, window, is_last, pct = "true", "true", "manual", "true", "rest"
              print("手动触发 -> window=manual（照跑 + 推送）")
          else:
              parts = schedule.split()
              key = (int(parts[1]), int(parts[0]))   # cron 格式：分 时 * * *
              window, is_last, notify, pct = SLOTS.get(key, ("w?", "false", "false", "30-50"))
              run = "true"
              print(f"schedule={schedule!r} -> wave={window} is_last={is_last} pct={pct}")

          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
              f.write(f"run={run}\\nnotify={notify}\\nwindow={window}\\nis_last={is_last}\\npercent={pct}\\n")
          PYEOF

      - name: Checkout
        if: steps.decide.outputs.run == 'true'
        uses: actions/checkout@v4

      - name: Setup Python
        if: steps.decide.outputs.run == 'true'
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        if: steps.decide.outputs.run == 'true'
        run: pip install -r requirements.txt

      # 取回今天的账本（昨天的会自动作废）
      - name: Restore daily state
        if: steps.decide.outputs.run == 'true'
        uses: actions/cache/restore@v4
        with:
          path: .e5_state.json
          key: e5-state-${{ github.run_id }}
          restore-keys: |
            e5-state-

      - name: Run keepalive
        if: steps.decide.outputs.run == 'true'
        env:
          # ---- 运行场次（由 Decide slot 输出）----
          RUN_WINDOW: ${{ steps.decide.outputs.window }}
          IS_LAST: ${{ steps.decide.outputs.is_last }}
          WAVE_PERCENT: ${{ steps.decide.outputs.percent }}
          # ---- E5 账户（JSON 内 daily_calls 已失效，每天总量统一来自 schedule.yml）----
          E5_ACCOUNTS: ${{ secrets.E5_ACCOUNTS }}
          AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
          AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
          AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
          TARGET_USERS: ${{ vars.TARGET_USERS }}
          DAILY_CALLS: "@@PLAN@@"
          MIN_CALLS: ${{ vars.MIN_CALLS }}
          MAX_CALLS: ${{ vars.MAX_CALLS }}
          ENABLE_WRITES: ${{ vars.ENABLE_WRITES }}
          # ---- 通知 ----
          NOTIFY_ENABLED: ${{ steps.decide.outputs.notify }}
          NOTIFY_SERVERCHAN_KEY: ${{ secrets.NOTIFY_SERVERCHAN_KEY }}
          NOTIFY_TELEGRAM_BOT_TOKEN: ${{ secrets.NOTIFY_TELEGRAM_BOT_TOKEN }}
          NOTIFY_TELEGRAM_CHAT_ID: ${{ secrets.NOTIFY_TELEGRAM_CHAT_ID }}
          NOTIFY_SMTP_HOST: ${{ secrets.NOTIFY_SMTP_HOST }}
          NOTIFY_SMTP_PORT: ${{ secrets.NOTIFY_SMTP_PORT }}
          NOTIFY_SMTP_USER: ${{ secrets.NOTIFY_SMTP_USER }}
          NOTIFY_SMTP_PASS: ${{ secrets.NOTIFY_SMTP_PASS }}
          NOTIFY_SMTP_TO: ${{ secrets.NOTIFY_SMTP_TO }}
        run: python main.py

      # 保存今天的账本（即使运行失败也保存）
      - name: Save daily state
        if: always() && steps.decide.outputs.run == 'true'
        uses: actions/cache/save@v4
        with:
          path: .e5_state.json
          key: e5-state-${{ github.run_id }}
"""


def build() -> None:
    waves, plan_env = load_config()
    period = target_run_date()

    used: set = set()
    slots: dict = {}
    schedule_lines: list[str] = []
    for i, wave in enumerate(waves):
        name = f"w{i + 1}"
        is_last = "true" if i == len(waves) - 1 else "false"
        h, m = pick_exact_point(wave, period, used)
        slots[(h, m)] = (name, is_last, wave["notify"], wave["percent"])
        bj = (h * 60 + m + 480) % 1440
        pct_label = wave["percent"] if wave["percent"] != "rest" else "100%跑完剩余"
        schedule_lines.append(
            f"    # ==== {name}：北京时间 {wave['label']} → 抽定时刻 "
            f"{bj // 60:02d}:{bj % 60:02d}（{period}）｜每天总量 {plan_env}，本波 {pct_label} ===="
        )
        schedule_lines.append(f"    - cron: '{m} {h} * * *'")
        schedule_lines.append("")

    slots_lines = ",\n".join(f"            {key}: {slots[key]!r}" for key in sorted(slots))
    content = (
        TEMPLATE
        .replace("@@GENERATED_AT@@", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        .replace("@@PERIOD@@", period)
        .replace("@@PLAN@@", plan_env)
        .replace("@@SCHEDULE@@", "\n".join(schedule_lines).rstrip())
        .replace("@@SLOTS@@", "{\n" + slots_lines + ",\n          }")
    )
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"已生成 {OUTPUT_PATH}（适用日期 {period}，每天总量 {plan_env}，{len(slots)} 个精确时刻）")


if __name__ == "__main__":
    build()
