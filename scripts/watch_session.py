#!/usr/bin/env python3
"""CoC 智能工人排程值守扩展（非侵入式：仅调用上游 coc_patrol2 / coc_config / coc_duration）。

核心思路（不做无意义的防掉线划屏，掉了就重新载入/重开）：
1. 读取建筑列表顶部的「正在升级」倒计时 + 本次会话派出的施工记录，记住每个工人的预计完工时间；
2. 有空闲工人时，按候选列表检查升级时长（从确认页读取「升级时间」，并缓存到 logs/upgrade_durations.json 避免重复点开）：
   - 优先把耗时 <= short_max_min（默认 10 分钟）的短任务全部按时长从短到长做完；
   - 短任务都做完后，如未开启 --short-only，再按时长从短到长（其次按价格）派长任务；
3. 工人全部派满后，精确休眠到「最早完工的工人时间 + 20~45 秒随机缓冲」（不干等、不乱划屏）；
4. 到点醒来时若游戏已掉线（「还在吗？」弹窗或进程退出），自动点重新载入或重新拉起游戏进村；
5. 若所有工人的完工时间都超出本次值守窗口（默认 ~2 小时），则提前体面收工，不白挂机。
"""
import argparse
import json
import os
import re
import random
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
DUR_CACHE_PATH = os.path.join(REPO_DIR, "logs", "upgrade_durations.json")
_DUR_EXTRACT_RE = re.compile(
    r"((?:\d+\s*天\s*)?(?:\d+\s*小时\s*)?(?:\d+\s*分钟\s*)?(?:\d+\s*秒)?)"
)


def load_dur_cache():
    try:
        with open(DUR_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_dur_cache(cache):
    try:
        os.makedirs(os.path.dirname(DUR_CACHE_PATH), exist_ok=True)
        with open(DUR_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def extract_duration_sec(text):
    """从 OCR 文本中提取最合理的升级/倒计时秒数（支持「升级时间 5分钟」「1天2小时」等）。"""
    if not text:
        return None
    s = str(text).strip()
    if not any(u in s for u in ("天", "小时", "分钟", "秒")):
        return None
    # 先尝试整串解析
    val = dur.parse_duration(s)
    if val is not None and val > 0:
        return val
    # 再从长句里抠出时长片段（例如「升级时间 10分钟」）
    best = None
    for m in _DUR_EXTRACT_RE.finditer(s):
        part = m.group(1).strip()
        if not part or not any(u in part for u in ("天", "小时", "分钟", "秒")):
            continue
        v = dur.parse_duration(part)
        if v is not None and v > 0:
            if best is None or v > best:
                best = v
    return best


def read_in_progress_countdowns(im):
    """从建筑列表图里读出所有正在施工建筑的剩余秒数列表。"""
    if im is None:
        return []
    tx = p.ocr_all(im, (1680, 50, 2220, 1010))
    out = []
    for t in tx:
        txt = t.get("t", "")
        if any(u in txt for u in ("天", "小时", "分钟", "秒")):
            sec = extract_duration_sec(txt)
            if sec is not None and 5 <= sec <= 30 * 86400:
                out.append(sec)
    out.sort()
    return out


def read_confirm_duration(cf_img):
    """从升级确认页截图中读取「升级时间」秒数。"""
    if cf_img is None:
        return None
    # 确认页下半部及中下部常显示升级时间（如「10分钟」「2天1小时」）
    tx = p.ocr_all(cf_img, (400, 480, 1950, 1040))
    cands = []
    for t in tx:
        sec = extract_duration_sec(t.get("t", ""))
        if sec is not None and 1 <= sec <= 30 * 86400:
            cands.append((t.get("y", 0), sec, t.get("t", "")))
    if not cands:
        # 兜底全图扫一次
        for t in p.ocr_all(cf_img):
            sec = extract_duration_sec(t.get("t", ""))
            if sec is not None and 1 <= sec <= 30 * 86400:
                cands.append((t.get("y", 0), sec, t.get("t", "")))
    if not cands:
        return None
    # 通常升级时间在确认按钮附近或卡片中下区
    cands.sort(key=lambda z: -z[0])
    return cands[0][1]


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

    # 恢复不了就强杀重开一次
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
    p.log(f"[watch] 确认升级 {item['name']} ({item['price']:,} {item['kind']}) @{pos}")
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
    # 注意：read_workers 返回 (空闲, 总数)，派出去 1 个工人后空闲数减少
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


def pick_and_upgrade_short_first(cfg, res_now, w_now, dur_cache, short_max_s, short_only=False, dry=False):
    """优先挑选 <= short_max_s（默认 10 分钟）的建筑升级。

    返回 (upgraded_record_or_None, in_progress_countdowns)。
    upgraded_record = dict(name, price, kind, duration_s, finish_ts)
    """
    bl = p.open_build_list()
    if bl is None:
        return None, []
    countdowns = read_in_progress_countdowns(bl)
    items = p.read_build_list(bl)
    if not items:
        p.clear_panels()
        return None, countdowns

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
        key = f"{it['name']}|{it['price']}"
        cached_s = dur_cache.get(key)
        it_copy = dict(it)
        it_copy["cache_key"] = key
        it_copy["duration_s"] = cached_s
        pool.append(it_copy)

    if not pool:
        p.clear_panels()
        return None, countdowns

    # 排序：已知 <=short_max_s 最优先（按时长升序）；其次未知时长（按价格升序，便宜的往往耗时短）；最后已知长任务（按时长升序）
    def _rank(it):
        ds = it["duration_s"]
        if ds is not None and ds <= short_max_s:
            return (0, ds, it["price"])
        if ds is None:
            return (1, it["price"], 0)
        return (2, ds, it["price"])

    pool.sort(key=_rank)
    p.log(
        "[watch] 可升级候选(短任务优先): "
        + ", ".join(
            f"{i['name']}({i['price']:,},{i['kind']},"
            f"{dur.fmt_duration(i['duration_s']) if i['duration_s'] else '时长待测'})"
            for i in pool[:5]
        )
    )

    if dry:
        p.clear_panels()
        chosen = pool[0]
        p.log(f"[watch] [dry] 优先候选: {chosen['name']} ({chosen['price']:,} {chosen['kind']})")
        return None, countdowns

    # 最多探查前 4 个候选，找到 <=short_max_s 的立刻升；否则选探到最短的（若未开 short_only）
    best_long = None  # (duration_s, item)
    for idx, it in enumerate(pool[:4]):
        # 如果已经没有短任务且当前项已知 > short_max_s
        if it["duration_s"] is not None and it["duration_s"] > short_max_s:
            if short_only:
                break
            if best_long is None or it["duration_s"] < best_long[0]:
                best_long = (it["duration_s"], it)
            break

        if idx > 0:
            # 重新打开列表确保行位置有效
            bl2 = p.open_build_list()
            if bl2 is None:
                break
            fresh = p.read_build_list(bl2)
            match = next(
                (x for x in fresh if x["name"] == it["name"] and x["price"] == it["price"]),
                None,
            )
            if not match:
                continue
            it["y"] = match["y"]

        cf = open_confirm_for_item(it)
        if cf is None:
            p.clear_panels()
            continue

        sec = read_confirm_duration(cf)
        if sec is not None:
            dur_cache[it["cache_key"]] = sec
            save_dur_cache(dur_cache)
            it["duration_s"] = sec
            p.log(f"[watch] 测得时长: {it['name']} = {dur.fmt_duration(sec)} ({sec}s)")

        # 命中短任务（<= short_max_s）→ 立刻确认升级！
        if sec is not None and sec <= short_max_s:
            ok = confirm_upgrade_now(cf, it, budget, w_now)
            if ok:
                return dict(
                    name=it["name"],
                    price=it["price"],
                    kind=it["kind"],
                    duration_s=sec,
                    finish_ts=time.time() + sec,
                ), countdowns
            continue

        # 不是短任务：记进备选长任务，关闭确认框继续看下一项有没有短任务
        if sec is not None:
            if best_long is None or sec < best_long[0]:
                best_long = (sec, it)
        elif best_long is None:
            best_long = (999999, it)
        p.clear_panels()
        time.sleep(0.6)

    # 没有 <= short_max_s 的短任务了
    if short_only or best_long is None:
        p.clear_panels()
        if short_only:
            p.log(f"[watch] 当前没有 <= {short_max_s // 60} 分钟的短任务（--short-only），本轮不升长任务")
        return None, countdowns

    # 升级最短的长任务
    sec, it = best_long
    bl3 = p.open_build_list()
    if bl3 is None:
        return None, countdowns
    fresh = p.read_build_list(bl3)
    match = next((x for x in fresh if x["name"] == it["name"] and x["price"] == it["price"]), None)
    if not match:
        p.clear_panels()
        return None, countdowns
    cf = open_confirm_for_item(match)
    if cf is None:
        p.clear_panels()
        return None, countdowns
    ok = confirm_upgrade_now(cf, match, budget, w_now)
    if ok:
        est_s = sec if sec < 999999 else None
        return dict(
            name=match["name"],
            price=match["price"],
            kind=match["kind"],
            duration_s=est_s,
            finish_ts=(time.time() + est_s) if est_s else None,
        ), countdowns
    return None, countdowns


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
    dur_cache = load_dur_cache()
    short_max_s = int(args.short_max_min * 60)
    total_min = max(1.0, args.minutes + random.uniform(-args.jitter, args.jitter))
    t0 = time.time()
    end_ts = t0 + total_min * 60

    p.log(
        f"[watch] 启动智能排程值守: 窗口={total_min:.1f}分钟 "
        f"短任务优先<={int(args.short_max_min)}分钟 short_only={args.short_only} dry={args.dry}"
    )
    p.init_capture()

    tracked_builds = []  # [{"name": ..., "finish_ts": ..., "duration_s": ...}]
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

            # 1. 收气泡 + 读资源和工人
            ok_c, tot_c, res_now = collect_bubbles_once(cfg, dry=args.dry)
            total_collected += ok_c
            if res_start is None and res_now:
                res_start = res_now
            if res_now:
                res_end = res_now
            w_now = p.read_workers(p.grab("_watch_w"))
            free_w = w_now[0] if (w_now and w_now[0] is not None) else 0
            p.log(f"[watch] 第 {round_no} 轮 | 收气泡 {ok_c}/{tot_c} | 资源={res_now} | 工人={w_now}")

            # 清理已到期的本地追踪记录
            now = time.time()
            tracked_builds = [b for b in tracked_builds if b.get("finish_ts") and b["finish_ts"] > now - 10]

            # 2. 如果有空闲工人，尽量把空闲工位填满（短任务优先）
            countdowns = []
            min_free = int(cfg.get("min_free_workers") or 1)
            if cfg.get("upgrade", True) and free_w >= min_free:
                # 最多循环派出当前所有空闲工人
                for _slot in range(free_w):
                    if time.time() >= end_ts:
                        break
                    rec, cds = pick_and_upgrade_short_first(
                        cfg, res_now, w_now, dur_cache, short_max_s,
                        short_only=args.short_only, dry=args.dry,
                    )
                    if cds:
                        countdowns = cds
                    if not rec:
                        break
                    total_upgrades.append(rec)
                    if rec.get("finish_ts"):
                        tracked_builds.append(rec)
                        eta = time.strftime("%H:%M:%S", time.localtime(rec["finish_ts"]))
                        p.log(f"[watch] 已记排程: {rec['name']} 预计 {eta} 完工")
                    if args.dry:
                        break
                    # 刷新资源和工人数
                    res_now = p.read_res_vote(2) or res_now
                    res_end = res_now
                    w_now = p.read_workers(p.grab("_watch_w2"))
                    free_w = w_now[0] if (w_now and w_now[0] is not None) else 0
                    if free_w < min_free:
                        break
            else:
                # 没有空闲工人：开一次列表读取在建倒计时，知道什么时候来操作
                bl = p.open_build_list()
                if bl is not None:
                    countdowns = read_in_progress_countdowns(bl)
                    p.clear_panels()

            # 3. 计算下次需要操作的时间（最早完工的工人）
            now = time.time()
            next_finish_candidates = []
            for b in tracked_builds:
                if b.get("finish_ts") and b["finish_ts"] > now:
                    next_finish_candidates.append(b["finish_ts"] - now)
            for cd in countdowns:
                if cd > 0:
                    next_finish_candidates.append(float(cd))

            remain_window = end_ts - now
            if args.dry:
                p.log("[watch] dry 模式仅演示一轮即结束")
                break

            if next_finish_candidates:
                earliest_s = min(next_finish_candidates)
                wait_s = earliest_s + random.uniform(20, 45)
                if wait_s > remain_window:
                    eta = time.strftime("%H:%M:%S", time.localtime(now + earliest_s))
                    p.log(
                        f"[watch] 最早完工在 {dur.fmt_duration(int(earliest_s))} 后（{eta}），"
                        f"已超出剩余值守窗口（{int(remain_window // 60)}分钟），提前收工"
                    )
                    break
                eta = time.strftime("%H:%M:%S", time.localtime(now + wait_s))
                p.log(
                    f"[watch] 工人已排满，最早 {dur.fmt_duration(int(earliest_s))} 后完工；"
                    f"静默等待 {int(wait_s)}s 到 {eta} 再操作（期间不做无意义划屏，掉了自动重连）"
                )
            else:
                # 没读到倒计时（比如没钱或无短任务），默认等 12~18 分钟再看一次
                wait_s = random.uniform(720, 1080)
                if wait_s > remain_window:
                    p.log("[watch] 剩余窗口不足以进行下一轮，收工")
                    break
                eta = time.strftime("%H:%M:%S", time.localtime(now + wait_s))
                p.log(f"[watch] 未检测到短期完工倒计时，等待 {int(wait_s)}s 到 {eta} 再巡检一次")

            # 静默等待到下次可操作时刻（不再主动划屏防掉线）
            time.sleep(wait_s)

        # 结束前汇总
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
