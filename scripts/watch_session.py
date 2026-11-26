#!/usr/bin/env python3
"""CoC 拟人化在线值守扩展（非侵入式：仅调用上游 coc_patrol2 / coc_config，不修改任何上游文件）。

行为设计：
- 进村后在指定时长内（默认 120 分钟 ±10 分钟随机）保持游戏前台在线；
- 每 2.5~3.8 分钟随机做一次轻微拟人操作（小幅随机拖动地图），防止触发 5 分钟「还在吗？」断线；
- 每 10~18 分钟随机做一轮完整巡检（收气泡 + 有空闲工人按 config.json 预算自动升级）；
- 遇到断线弹窗自动点「重新载入」；遇到登录页立刻安全退出，绝不点登录；
- 检测到游戏进程被手动关闭时自动结束，绝不强行抢回；
- 到点后输出整场收益汇总并按 config.json 决定是否关闭游戏。
"""
import argparse
import os
import random
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import coc_config
import coc_patrol2 as p

LOGIN_WORDS = ("QQ登录", "微信登录", "用户协议", "隐私政策", "同意并继续",
               "实名认证", "切换账号", "授权登录", "登录游戏", "游客登录")


def human_nudge(dry=False):
    """在安全区域做一次小幅随机拖动（模拟真人滑动看村庄），防止 5 分钟无操作断线。"""
    cx = random.randint(820, 1380)
    cy = random.randint(360, 680)
    dx = random.randint(-140, 140)
    dy = random.randint(-90, 90)
    if abs(dx) < 35 and abs(dy) < 35:
        dx = 70 if random.random() < 0.5 else -70
    dur = random.randint(240, 480)
    if dry:
        p.log(f"[watch] dry 拟人微滑 ({cx},{cy})->({cx+dx},{cy+dy}) {dur}ms")
        return
    p.sh(f"input swipe {cx} {cy} {cx + dx} {cy + dy} {dur}")
    time.sleep(random.uniform(0.6, 1.3))
    # 约一半概率轻滑回来，避免视角一直飘到地图边角
    if random.random() < 0.65:
        bx = max(300, min(1900, cx + dx + random.randint(-20, 20)))
        by = max(250, min(800, cy + dy + random.randint(-20, 20)))
        p.sh(f"input swipe {bx} {by} {cx} {cy} {random.randint(240, 460)}")


def ensure_online(dry=False):
    """检查画面状态：正常返回 'ok'，重连返回 'reconnected'，登录页/退出返回 'abort'。"""
    if p.pid_of() is None:
        p.log("[watch] 检测到游戏进程已关闭，结束值守")
        return "closed"
    img = p.grab("_watch_chk")
    if img is None:
        return "retry"
    sk = p.screen_kind(img)
    txt = sk.get("text", "")
    if any(w in txt for w in LOGIN_WORDS):
        p.log("[watch] 检测到登录/授权页，安全退出（绝不自动点登录）")
        return "abort"
    kind = sk.get("kind")
    if kind == "village":
        return "ok"
    if dry:
        p.log(f"[watch] dry 画面状态={kind}，不点击处理")
        return "ok"
    if kind in ("reconnect", "ad", "popup", "panel", "quit_confirm", "loading"):
        p.log(f"[watch] 画面状态={kind}，尝试恢复进村...")
        ok_img = p.enter_village(max_steps=12, warmup=0)
        return "reconnected" if ok_img is not None else "retry"
    # 可能有残留建筑面板
    if p.panel_open(img):
        p.clear_panels()
    return "ok"


def do_patrol_cycle(cfg, dry=False):
    """执行一轮轻量收菜 + 有空工人则按预算升级，返回统计 dict。"""
    stats = {"bubbles": 0, "collected": 0, "upgrades": [], "res": None, "workers": None}
    img = p.grab("_watch_cycle")
    if img is None:
        return stats

    if p.panel_open(img):
        if not dry:
            p.clear_panels()
            time.sleep(0.8)
            img = p.grab("_watch_cycle2")
            if img is None:
                return stats

    res_before = p.read_res_vote(2)
    bubbles = p.find_bubbles(img) if cfg.get("collect", True) else []
    stats["bubbles"] = len(bubbles)
    p.log(f"[watch] 巡检: 资源={res_before} 气泡={len(bubbles)}")

    if bubbles and not dry:
        # 拟人化：随机打乱点击顺序，每次点击间加 0.5~1.6s 随机停顿
        order = list(bubbles)
        random.shuffle(order)
        for kind, x, y, _sc in order:
            jx = x + random.randint(-6, 6)
            jy = y + random.randint(-6, 6)
            st, _ = p.collect_one(jx, jy)
            if st in ("direct", "collect"):
                stats["collected"] += 1
            time.sleep(random.uniform(0.5, 1.6))
        p.clear_panels()
        time.sleep(2.0)

    res_after = p.read_res_vote(2) if not dry else res_before
    w = p.read_workers(p.grab("_watch_w"))
    stats["res"] = res_after
    stats["workers"] = w
    p.log(f"[watch] 收集结果: {stats['collected']}/{stats['bubbles']} | 资源={res_after} | 工人={w}")

    free_w = w[0] if (w and w[0] is not None) else 0
    rounds = int(cfg.get("max_upgrades") or 1) if cfg.get("upgrade", True) else 0
    min_free = int(cfg.get("min_free_workers") or 1)

    if free_w >= min_free and rounds > 0:
        bl = p.open_build_list()
        if bl is not None:
            items = p.read_build_list(bl)
            p.log(f"[watch] 可升级 {len(items)} 项")
            for n in range(rounds):
                up = p.upgrade_from_list(
                    max_price=cfg.get("max_price"),
                    kinds=tuple(cfg.get("upgrade_kinds") or ("gold", "elixir")),
                    items=items if n == 0 else None,
                    res=res_after if n == 0 else None,
                    w0=w if n == 0 else None,
                    dry=dry,
                )
                if not up:
                    break
                info = up[0]
                stats["upgrades"].append(f"{info['name']}({info['price']:,},{info['kind']})")
                p.log(f"[watch] {'[dry] 候选升级' if dry else '升级成功'}: {stats['upgrades'][-1]}")
                if dry:
                    break
                free_w -= 1
                if free_w < min_free:
                    break
            p.clear_panels()
    return stats


def main():
    ap = argparse.ArgumentParser(description="CoC 拟人化在线值守模式（非侵入扩展）")
    ap.add_argument("--minutes", type=float, default=120.0, help="目标在线时长（分钟，默认 120）")
    ap.add_argument("--jitter", type=float, default=10.0, help="总时长随机浮动分钟数（±jitter，默认 10）")
    ap.add_argument("--nudge-min", type=int, default=150, help="拟人保活微滑最小间隔秒数（默认 150）")
    ap.add_argument("--nudge-max", type=int, default=225, help="拟人保活微滑最大间隔秒数（默认 225）")
    ap.add_argument("--patrol-min", type=int, default=600, help="收菜/升级巡检最小间隔秒数（默认 600=10分钟）")
    ap.add_argument("--patrol-max", type=int, default=1080, help="收菜/升级巡检最大间隔秒数（默认 1080=18分钟）")
    ap.add_argument("--dry", action="store_true", help="只读不点击气泡/不确认升级")
    ap.add_argument("--keep-game", action="store_true", help="结束时不强杀游戏")
    args = ap.parse_args()

    cfg = coc_config.load()
    total_min = max(1.0, args.minutes + random.uniform(-args.jitter, args.jitter))
    total_sec = int(total_min * 60)
    t0 = time.time()
    end_ts = t0 + total_sec

    p.log(f"[watch] 启动值守模式: 计划时长={total_min:.1f} 分钟 ({total_sec}s), dry={args.dry}")
    p.init_capture()

    res_start = None
    res_end = None
    total_collected = 0
    total_upgrades = []
    reconnects = 0
    cycles = 0

    try:
        # 进村：若游戏未跑则启动；若已在跑直接检查进村
        if p.pid_of() is None:
            p.sh("input keyevent KEYCODE_WAKEUP")
            time.sleep(1.5)
            p.sh(f"am start -n {p.PKG}/{p.ACT}")
            img = p.enter_village(max_steps=25, warmup=16)
        else:
            img = p.enter_village(max_steps=15, warmup=0)

        if img is None:
            p.log("[watch] 进村失败，退出值守")
            return 1

        time.sleep(3.0)
        res_start = p.read_res_vote(3)
        p.log(f"[watch] 初始资源: {res_start}")

        # 进村后先做一次巡检
        st0 = do_patrol_cycle(cfg, dry=args.dry)
        cycles += 1
        total_collected += st0["collected"]
        total_upgrades.extend(st0["upgrades"])
        if st0["res"]:
            res_end = st0["res"]

        next_patrol = time.time() + random.randint(args.patrol_min, args.patrol_max)

        while time.time() < end_ts:
            remain = end_ts - time.time()
            wait_s = min(remain, random.randint(args.nudge_min, args.nudge_max))
            if wait_s <= 0:
                break
            p.log(f"[watch] 待机 {int(wait_s)}s（剩余 {int(remain // 60)}分{int(remain % 60)}秒）")
            # 分段睡，方便更快响应游戏被手动关闭
            slept = 0.0
            while slept < wait_s and time.time() < end_ts:
                step = min(15.0, wait_s - slept)
                time.sleep(step)
                slept += step
                if p.pid_of() is None:
                    p.log("[watch] 检测到游戏已被手动关闭，提前结束值守")
                    return 0

            if time.time() >= end_ts:
                break

            state = ensure_online(dry=args.dry)
            if state in ("closed", "abort"):
                return 0 if state == "closed" else 1
            if state == "reconnected":
                reconnects += 1

            if time.time() >= next_patrol:
                st = do_patrol_cycle(cfg, dry=args.dry)
                cycles += 1
                total_collected += st["collected"]
                total_upgrades.extend(st["upgrades"])
                if st["res"]:
                    res_end = st["res"]
                next_patrol = time.time() + random.randint(args.patrol_min, args.patrol_max)
            else:
                human_nudge(dry=args.dry)

        # 结束前读一次最终资源
        if p.pid_of() is not None:
            res_end = p.read_res_vote(2) or res_end
            w_end = p.read_workers(p.grab("_watch_end"))
            star_end = p.read_star(p.grab("_watch_star"))
            p.log(f"最终 {res_end} | 工人 {w_end} | 星星 {star_end}")

        gain = {}
        if res_start and res_end:
            for k in ("gold", "elixir", "dark"):
                if res_start.get(k) is not None and res_end.get(k) is not None:
                    gain[k] = res_end[k] - res_start[k]
        p.log(
            f"[watch] 值守完成: 耗时={int(time.time() - t0)}s 巡检={cycles}轮 "
            f"收气泡={total_collected} 升级={len(total_upgrades)}次 重连={reconnects}次 收益={gain}"
        )
        return 0
    finally:
        p.close_capture()
        if not args.keep_game and cfg.get("close_game_after_run", True) and p.pid_of() is not None:
            p.sh(f"am force-stop {p.PKG}")
            p.log("[watch] 已关闭游戏")


if __name__ == "__main__":
    sys.exit(main())
