#!/usr/bin/env python3
"""CoC 智能工人排程值守扩展（非侵入式：仅调用上游 coc_patrol2 / coc_config / coc_duration）。

核心思路（不做无意义的防掉线划屏，掉了就重新载入/重开）：
1. 直接解析建筑列表的上下两区（以「临时工人 / 排队建造」行为分界）：
   - 上半区：正在施工的建筑剩余倒计时（自动排除「实验室」研究，避免干扰工人完工判断）；
   - 下半区：可升级建筑的 名称 + 升级时长 + 价格（支持 <1000 的低级建筑，如 350/375/750）+ 币种；
2. 有空闲工人时，直接按时长从短到长优先做完 <= short_max_min（默认 10 分钟）的短任务；
   短任务做完后（若未开 --short-only）再按时长从短到长升长任务；
3. 工人全部派满后，精确静默等待到「最早完工的工人时间 + 15~30 秒随机缓冲」；
4. 到点时若游戏已掉线（「还在吗？」或退出），自动点重新载入或重启游戏进村；
5. 若所有工人的完工时间都超出本次值守窗口，则提前体面收工，不白挂机。
"""
import argparse
import os
import random
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import coc_config
import coc_duration as dur
import coc_patrol2 as p

LOGIN_WORDS = ("QQ登录", "微信登录", "用户协议", "隐私政策", "同意并继续",
               "实名认证", "切换账号", "授权登录", "登录游戏", "游客登录")
CAPTURE_PREF_PATH = os.path.join(REPO_DIR, "logs", "capture_pref.json")
_DUR_EXTRACT_RE = re.compile(
    r"((?:\d+\s*天\s*)?(?:\d+\s*小时\s*)?(?:\d+\s*分钟\s*)?(?:\d+\s*秒)?)"
)


def init_capture_smart():
    """自适应抓帧优先级：首次探测成功谁就记住谁（存 logs/capture_pref.json），下次直连不再反复握手失败。"""
    import json
    pref = None
    try:
        with open(CAPTURE_PREF_PATH, "r", encoding="utf-8") as f:
            pref = (json.load(f) or {}).get("mode")
    except Exception:
        pref = None

    if pref == "screencap":
        p._mc = None
        p._mc_tried = True
        p._mc_fail = 99
        p.log("[capture] 已按历史优先级直连 screencap（跳过 minicap 探测）")
        return "screencap"

    ok = p.init_capture()
    mode = "minicap" if ok else "screencap"
    if not ok:
        p._mc_fail = 99   # 立刻锁定 screencap，避免 grab() 再重复尝试 3 次
    try:
        os.makedirs(os.path.dirname(CAPTURE_PREF_PATH), exist_ok=True)
        with open(CAPTURE_PREF_PATH, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
        p.log(f"[capture] 已记录高优先级抓帧模式: {mode} -> logs/capture_pref.json")
    except Exception:
        pass
    return mode


def extract_duration_sec(text):
    """从 OCR 文本中提取升级/倒计时秒数（如 '1分钟14秒' -> 74, '36分钟1秒' -> 2161）。"""
    if not text:
        return None
    s = str(text).strip()
    if not any(u in s for u in ("天", "小时", "分钟", "秒")):
        return None
    val = dur.parse_duration(s)
    if val is not None and val > 0:
        return val
    best = None
    for m in _DUR_EXTRACT_RE.finditer(s):
        part = m.group(1).strip()
        if not part or not any(u in part for u in ("天", "小时", "分钟", "秒")):
            continue
        v = dur.parse_duration(part)
        if v is not None and v > 0 and (best is None or v > best):
            best = v
    return best


def parse_build_list_full(im):
    """一次性解析建筑列表的上下两区 -> (in_progress_list, available_items)。

    in_progress_list: [{"name", "remaining_s", "y"}]（仅在建建筑，已排除实验室）
    available_items : [{"name", "price", "kind", "duration_s", "y"}]（支持低价 <1000）
    """
    if im is None:
        return [], []
    tx = p.ocr_all(im, (1600, 50, 2220, 1010))
    if not tx:
        return [], []

    # 1. 找上下半区分界 y（「临时工人」「排队建造」那一行）
    split_ys = [
        t["y"] for t in tx
        if any(k in t["t"] for k in ("临时工人", "排队建造", "建议升级"))
    ]
    if split_ys:
        y_split = max(split_ys) + 18
    else:
        # 兜底：若存在「工人空闲中」，取它下方 75px；否则按第一条纯数字价格反推
        idle_ys = [t["y"] for t in tx if "工人空闲" in t["t"]]
        if idle_ys:
            y_split = max(idle_ys) + 75
        else:
            y_split = 420

    # 2. 上半区（y < y_split）：在建倒计时
    top_names = []
    top_durs = []
    for t in tx:
        if t["y"] >= y_split:
            continue
        txt = t["t"].strip()
        if any(k in txt for k in ("工人空闲", "临时工人", "排队建造", "升级中")):
            continue
        sec = extract_duration_sec(txt)
        if sec is not None and t["x"] >= 2000:
            top_durs.append((sec, t["y"], txt))
        elif t["x"] < 2000 and len(txt) >= 2:
            top_names.append(t)

    in_progress = []
    for sec, dy, raw_t in top_durs:
        cand = [n for n in top_names if abs(n["y"] - dy) < 28]
        name = min(cand, key=lambda n: abs(n["y"] - dy))["t"].strip() if cand else "施工中"
        # 实验室不占建筑工人，不计入工人最早完工时间
        if "实验室" in name:
            continue
        in_progress.append({"name": name, "remaining_s": sec, "y": dy, "raw": raw_t})
    in_progress.sort(key=lambda z: z["remaining_s"])

    # 3. 下半区（y >= y_split）：可升级建筑（名称 + 时长 + 价格 + 币种）
    bot_names = []
    bot_durs = []
    bot_prices = []
    for t in tx:
        if t["y"] < y_split:
            continue
        txt = t["t"].strip()
        if any(k in txt for k in ("临时工人", "排队建造", "建议升级")):
            continue
        sec = extract_duration_sec(txt)
        if sec is not None and t["x"] >= 2000:
            bot_durs.append((sec, t["y"], txt))
            continue
        digits = "".join(c for c in txt if c.isdigit())
        if t["x"] >= 2040 and len(digits) >= 2 and digits == txt.replace(",", "").replace(" ", ""):
            bot_prices.append((int(digits), t["x"], t["y"]))
            continue
        if t["x"] < 2000 and len(txt) >= 2 and not txt.replace("×", "").isdigit():
            bot_names.append(t)

    items = []
    used_names = set()
    used_durs = set()
    for price, px, py in bot_prices:
        if price < 100:
            continue
        # 价格通常在建筑名下方 15~40px，也可能与建筑名同行
        ncands = [n for n in bot_names if -15 <= (py - n["y"]) <= 48 and id(n) not in used_names]
        if not ncands:
            continue
        n = min(ncands, key=lambda z: abs(py - z["y"] - 25))
        used_names.add(id(n))

        # 找同行的升级时长（在价格正上方约 20~40px，与建筑名同行）
        dcands = [
            (idx, sec, dy) for idx, (sec, dy, _) in enumerate(bot_durs)
            if idx not in used_durs and abs(dy - n["y"]) < 26
        ]
        duration_s = None
        if dcands:
            best_d = min(dcands, key=lambda z: abs(z[2] - n["y"]))
            used_durs.add(best_d[0])
            duration_s = best_d[1]

        kind = p._kind_at(im, py)
        items.append({
            "name": n["t"].strip(),
            "price": price,
            "kind": kind,
            "duration_s": duration_s,
            "y": n["y"],
            "price_y": py,
        })

    return in_progress, items


def ensure_in_village():
    """确保游戏在前台且处于干净村庄；掉了/弹窗就重连或重启游戏。返回 'ok' | 'login' | 'fail'。"""
    if p.pid_of() is None:
        p.log("[watch] 游戏未运行，重新启动并进村...")
        p.sh("input keyevent KEYCODE_WAKEUP")
        time.sleep(1.2)
        p.sh(f"am start -n {p.PKG}/{p.ACT}")
        img = p.enter_village(max_steps=25, warmup=16)
        return "ok" if img is not None else "fail"

    img = p.grab("_watch_chk")
    if img is not None:
        sk = p.screen_kind(img)
        txt = sk.get("text", "")
        if any(w in txt for w in LOGIN_WORDS):
            p.log("[watch] 检测到登录/授权页，安全退出（绝不自动点登录）")
            return "login"
        if sk.get("kind") == "village":
            if p.panel_open(img):
                p.clear_panels()
            return "ok"

    p.log("[watch] 当前不在村庄（可能掉线或有弹窗），尝试恢复进村...")
    img = p.enter_village(max_steps=18, warmup=0)
    if img is not None:
        return "ok"

    p.log("[watch] 恢复进村失败，重启游戏再试...")
    p.sh(f"am force-stop {p.PKG}")
    time.sleep(1.5)
    p.sh(f"am start -n {p.PKG}/{p.ACT}")
    img = p.enter_village(max_steps=25, warmup=16)
    return "ok" if img is not None else "fail"


def collect_bubbles_once(cfg, dry=False):
    """收一遍当前画面上的资源气泡，返回 (collected, total, res_after)。"""
    img = p.grab("_watch_col")
    if img is None:
        return 0, 0, {}
    bubbles = p.find_bubbles(img) if cfg.get("collect", True) else []
    ok = 0
    if bubbles and not dry:
        order = list(bubbles)
        random.shuffle(order)
        for _kind, x, y, _sc in order:
            st, _ = p.collect_one(x + random.randint(-5, 5), y + random.randint(-5, 5))
            if st in ("direct", "collect"):
                ok += 1
            time.sleep(random.uniform(0.35, 0.9))
        p.clear_panels()
        time.sleep(2.0)
    res_after = p.read_res_vote(2)
    return ok, len(bubbles), res_after


def open_confirm_for_item(item):
    """点击列表中的一项，弹出「升至」确认框并返回截图；失败返回 None。"""
    for _ in range(5):
        p.tap(p.LIST_ROW_X, item["y"], 0.9)
        cf = p.grab("_watch_cf")
        if cf is None:
            continue
        head = " ".join(t["t"] for t in p.ocr_all(cf) if t["y"] < 140)
        if "升至" in head:
            return cf
    return None


def confirm_upgrade_now(cf, item, budget, w_start):
    """在确认页点击绿色确认并校验扣款/工人数变化，返回 True/False。"""
    btn = p.find_green_button(cf)
    pos = (btn[0], btn[1]) if btn else p.CONFIRM_BTN
    dur_txt = dur.fmt_duration(item["duration_s"]) if item.get("duration_s") else "?"
    p.log(f"[watch] 确认升级 {item['name']} ({item['price']:,} {item['kind']}, 时长={dur_txt}) @{pos}")
    for _ in range(3):
        p.tap(pos[0], pos[1], 1.3)
        time.sleep(1.0)
        chk = p.grab("_watch_chkcf")
        if chk is None:
            continue
        if not any("升至" in t["t"] for t in p.ocr_all(chk) if t["y"] < 140):
            break

    time.sleep(2.0)
    res1 = p.read_res_until(3, tries=3, gap=1.2)
    p.clear_panels()
    time.sleep(1.0)
    w1 = p.read_workers(p.grab("_watch_w1"))

    cur = res1.get(item["kind"]) if res1 else None
    spent = (budget.get(item["kind"], 0) - cur) if cur is not None else None
    paid = (item["price"] > 0 and spent is not None and spent >= item["price"] * 0.85)
    worker_used = (
        w_start and w1 and w_start[0] is not None and w1[0] is not None
        and w1[0] < w_start[0]
    )
    if paid or worker_used:
        why = f"扣 {item['kind']} {spent:,}" if paid else f"空闲工人 {w_start[0]}->{w1[0]}"
        p.log(f"[watch] ✓ 升级成功: {item['name']}（{why}）")
        return True
    p.log(f"[watch] ✗ 未确认升级生效: {item['name']} (spent={spent}, workers={w_start}->{w1})")
    return False


def pick_and_upgrade_short_first(cfg, res_now, w_now, short_max_s, short_only=False, dry=False):
    """从建筑列表直接按「时长 <= short_max_s 最短优先」挑一个升级。

    返回 (upgraded_record_or_None, in_progress_list)。
    """
    bl = p.open_build_list()
    if bl is None:
        return None, []
    in_progress, items = parse_build_list_full(bl)
    if in_progress:
        p.log(
            "[watch] 正在施工: "
            + ", ".join(f"{b['name']}({dur.fmt_duration(b['remaining_s'])})" for b in in_progress)
        )
    if not items:
        p.clear_panels()
        p.log("[watch] 建筑列表未解析到可升级项")
        return None, in_progress

    kinds = tuple(cfg.get("upgrade_kinds") or ("gold", "elixir"))
    max_price = cfg.get("max_price")
    budget = {
        "gold": res_now.get("gold") or 0,
        "elixir": res_now.get("elixir") or 0,
        "dark": res_now.get("dark") or 0,
    }

    pool = []
    for it in items:
        if it["kind"] not in kinds:
            continue
        if "工人小屋" in it["name"] and it["price"] <= 5000:
            continue
        if max_price and it["price"] > max_price:
            continue
        if it["price"] > budget.get(it["kind"], 0) * 0.95:
            continue
        if short_only and (it["duration_s"] is None or it["duration_s"] > short_max_s):
            continue
        pool.append(it)

    if not pool:
        p.clear_panels()
        p.log("[watch] 无符合预算/时长条件的可升级项")
        return None, in_progress

    # 排序：<=short_max_s 最优先（按时长升序），其次按时长升序，最后按价格升序
    def _rank(it):
        ds = it["duration_s"]
        if ds is not None and ds <= short_max_s:
            return (0, ds, it["price"])
        if ds is not None:
            return (1, ds, it["price"])
        return (2, it["price"], 0)

    pool.sort(key=_rank)
    p.log(
        "[watch] 可升级候选(短任务优先): "
        + ", ".join(
            f"{i['name']}({i['price']:,},{i['kind']},"
            f"{dur.fmt_duration(i['duration_s']) if i['duration_s'] else '?'})"
            for i in pool[:5]
        )
    )

    chosen = pool[0]
    if dry:
        p.clear_panels()
        p.log(f"[watch] [dry] 将优先升级: {chosen['name']} ({chosen['price']:,} {chosen['kind']})")
        return None, in_progress

    cf = open_confirm_for_item(chosen)
    if cf is None:
        p.clear_panels()
        p.log(f"[watch] 未弹出 {chosen['name']} 确认框")
        return None, in_progress

    ok = confirm_upgrade_now(cf, chosen, budget, w_now)
    if ok:
        ds = chosen.get("duration_s")
        return dict(
            name=chosen["name"],
            price=chosen["price"],
            kind=chosen["kind"],
            duration_s=ds,
            finish_ts=(time.time() + ds) if ds else None,
        ), in_progress
    return None, in_progress


def main():
    ap = argparse.ArgumentParser(description="CoC 智能工人排程值守（优先做完 <=10 分钟短任务，按完工时间精准操作）")
    ap.add_argument("--minutes", type=float, default=120.0, help="总值守窗口（分钟，默认 120）")
    ap.add_argument("--jitter", type=float, default=10.0, help="总时长随机浮动分钟数（默认 ±10）")
    ap.add_argument("--short-max-min", type=float, default=10.0, help="优先短任务阈值（分钟，默认 10）")
    ap.add_argument("--short-only", action="store_true", help="只做 <= short-max-min 的短任务，不升长任务")
    ap.add_argument("--dry", action="store_true", help="只读演练，不点击确认")
    ap.add_argument("--keep-game", action="store_true", help="结束时不强杀游戏")
    args = ap.parse_args()

    cfg = coc_config.load()
    short_max_s = int(args.short_max_min * 60)
    total_min = max(1.0, args.minutes + random.uniform(-args.jitter, args.jitter))
    t0 = time.time()
    end_ts = t0 + total_min * 60

    p.log(
        f"[watch] 启动智能排程值守: 窗口={total_min:.1f}分钟 "
        f"短任务优先<={int(args.short_max_min)}分钟 short_only={args.short_only} dry={args.dry}"
    )
    init_capture_smart()

    tracked_builds = []
    total_collected = 0
    total_upgrades = []
    res_start = None
    res_end = None
    round_no = 0

    try:
        while time.time() < end_ts:
            round_no += 1
            st = ensure_in_village()
            if st == "login":
                return 1
            if st != "ok":
                p.log("[watch] 进村失败，60s 后重试")
                time.sleep(min(60, max(1, end_ts - time.time())))
                continue

            ok_c, tot_c, res_now = collect_bubbles_once(cfg, dry=args.dry)
            total_collected += ok_c
            if res_start is None and res_now:
                res_start = res_now
            if res_now:
                res_end = res_now
            w_now = p.read_workers(p.grab("_watch_w"))
            free_w = w_now[0] if (w_now and w_now[0] is not None) else 0
            p.log(f"[watch] 第 {round_no} 轮 | 收气泡 {ok_c}/{tot_c} | 资源={res_now} | 工人={w_now}")

            now = time.time()
            tracked_builds = [b for b in tracked_builds if b.get("finish_ts") and b["finish_ts"] > now - 10]

            in_progress = []
            min_free = int(cfg.get("min_free_workers") or 1)
            if cfg.get("upgrade", True) and free_w >= min_free:
                for _slot in range(free_w):
                    if time.time() >= end_ts:
                        break
                    rec, ip = pick_and_upgrade_short_first(
                        cfg, res_now, w_now, short_max_s,
                        short_only=args.short_only, dry=args.dry,
                    )
                    if ip:
                        in_progress = ip
                    if not rec:
                        break
                    total_upgrades.append(rec)
                    if rec.get("finish_ts"):
                        tracked_builds.append(rec)
                        eta = time.strftime("%H:%M:%S", time.localtime(rec["finish_ts"]))
                        p.log(f"[watch] 已记排程: {rec['name']} 预计 {eta} 完工")
                    if args.dry:
                        break
                    res_now = p.read_res_vote(2) or res_now
                    res_end = res_now
                    w_now = p.read_workers(p.grab("_watch_w2"))
                    free_w = w_now[0] if (w_now and w_now[0] is not None) else 0
                    if free_w < min_free:
                        break
            else:
                bl = p.open_build_list()
                if bl is not None:
                    in_progress, _ = parse_build_list_full(bl)
                    p.clear_panels()

            if args.dry:
                p.log("[watch] dry 模式仅演示一轮即结束")
                break

            # 计算下次操作时间：取本地刚派出的完工时间 & 上半区真实在建倒计时的最小值
            now = time.time()
            next_finish_candidates = []
            for b in tracked_builds:
                if b.get("finish_ts") and b["finish_ts"] > now:
                    next_finish_candidates.append(b["finish_ts"] - now)
            for b in in_progress:
                if b.get("remaining_s", 0) > 0:
                    next_finish_candidates.append(float(b["remaining_s"]))

            remain_window = end_ts - now
            if next_finish_candidates:
                earliest_s = min(next_finish_candidates)
                wait_s = earliest_s + random.uniform(15, 30)
                if wait_s > remain_window:
                    eta = time.strftime("%H:%M:%S", time.localtime(now + earliest_s))
                    p.log(
                        f"[watch] 最早完工在 {dur.fmt_duration(int(earliest_s))} 后（{eta}），"
                        f"超出剩余窗口（{int(remain_window // 60)}分钟），提前收工"
                    )
                    break
                eta = time.strftime("%H:%M:%S", time.localtime(now + wait_s))
                p.log(
                    f"[watch] 工人已排满，最早 {dur.fmt_duration(int(earliest_s))} 后完工；"
                    f"静默等待 {int(wait_s)}s 到 {eta} 再操作（掉了自动重连）"
                )
            else:
                wait_s = random.uniform(600, 900)
                if wait_s > remain_window:
                    p.log("[watch] 剩余窗口不足以进行下一轮，收工")
                    break
                eta = time.strftime("%H:%M:%S", time.localtime(now + wait_s))
                p.log(f"[watch] 无短期倒计时，等待 {int(wait_s)}s 到 {eta} 再检查")

            time.sleep(wait_s)

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
        up_str = ", ".join(
            f"{u['name']}({dur.fmt_duration(u['duration_s']) if u.get('duration_s') else '?'})"
            for u in total_upgrades
        ) or "无"
        p.log(
            f"[watch] 值守完成: 耗时={int(time.time() - t0)}s 轮次={round_no} "
            f"收气泡={total_collected} 升级={len(total_upgrades)}次[{up_str}] 收益={gain}"
        )
        return 0
    finally:
        p.close_capture()
        if not args.keep_game and cfg.get("close_game_after_run", True) and p.pid_of() is not None:
            p.sh(f"am force-stop {p.PKG}")
            p.log("[watch] 已关闭游戏")


if __name__ == "__main__":
    sys.exit(main())
