# -*- coding: utf-8 -*-
"""抖音推荐流采集器：定时抓取 → 主题筛选 → 视频 + 评论落盘。

一轮干什么
----------
1. 抓取：refresh_index 1..N 调 DouyinAPI.get_tab_feed()，按 aweme_id 去重
2. 预过滤：desc 空且无话题的丢弃（约 10%，不浪费判定 token）
3. 判定：批量调 TopicClassifier，拿到主题 + 置信度
4. 分流：conf >= 阈值进下载队列，其余只存元数据（可事后调阈值回溯）
5. 采集：截断到 --max-download，下载视频 + 抓评论 + 落库
6. 记统计

失败策略
--------
- 判定失败 → 本轮不下载任何内容（宁可少采，不乱采）
- 限频（error_code=7）→ 放弃本轮，不重试
- 单条下载/评论失败 → 记日志跳过，不中断整轮

用法
----
    python feed_crawler.py                    # 正常跑一轮
    python feed_crawler.py --dry-run          # 只抓+判定，不下载
    python feed_crawler.py --max-download 5
    python feed_crawler.py --threshold 0.9
    python feed_crawler.py --stats            # 只看库统计
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

# 必须先导入 douyin_api，规避 builder.auth 的循环导入（见 docs/主题/T01 §5）
import dy_apis.douyin_api as douyin_api
from dy_apis.douyin_api import DouyinAPI
from utils.common_util import load_env
from utils.feed_store import FeedStore
from utils.topic_classifier import ClassifierError, TopicClassifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "datas", "feed")
DB_PATH = os.path.join(DATA_DIR, "feed.db")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
COMMENT_DIR = os.path.join(DATA_DIR, "comments")
LOG_DIR = os.path.join(DATA_DIR, "logs")

REFRESH_ROUNDS = 10        # 每轮刷几屏（Q3）
MAX_DOWNLOAD = 20          # 每轮最多下载几条（Q3）
CLASSIFY_BATCH = 20        # 每次送模型多少条
COMMENT_LIMIT = 1000       # 每作品最多取多少条一级评论（原 Q4 定为 200）。
                           # 200 会截断热门作品：实测 760 评论的那条一级就有
                           # 约 440 条，只取到 200 条、漏掉近半。
COMMENT_PAGE_SIZE = '20'   # 一级评论每页条数。浏览器实际用 10~20；上游
                           # dy_apis 硬编码 5，翻页次数是这里的 4 倍。
COMMENT_THROTTLE = 1.0     # 一级评论翻页间隔（秒）。上游完全没有 sleep，
                           # 一条千条评论的作品会连打 200 次，是风控主因。
REPLY_DELAY_MIN = 0.5      # 回复请求的随机延迟下限（秒）
REPLY_DELAY_MAX = 2.0      # 回复请求的随机延迟上限（秒）。固定间隔的请求
                           # 节奏太规律，抖动更像真人、也避免踩同一时间窗。
REPLY_ABORT_AFTER = 5      # 连续失败几次就放弃剩余回复。被风控时接口返回
                           # 空响应（HTTP 200），继续打点只会加重封禁风险。
                           # 取 5 是为了让指数回退有充分试探余地：2→4→8→16
                           # 共 30 秒冷却仍不恢复，才判定为真风控。
REPLY_BACKOFF_BASE = 2.0   # 连续失败后的指数回退起始秒数：2 → 4 → 8 → 16
REPLY_BACKOFF_MAX = 60.0   # 回退封顶（秒），避免等太久
REPLY_LIMIT = 200          # 每条一级评论最多保留多少条回复
REPLY_TOP_PCT = 0.45       # 只抓「按点赞排序前 45%」的一级评论下的回复。
                           # 实测 digg 前 30% 覆盖 69% 的回复量，前 50% 覆盖
                           # 81%；取 45% 是覆盖度与请求量的折中。
                           # 设为 1.0 即抓全部（回到旧行为）。
REPLY_BUDGET = 35          # 单条作品的回复请求数硬上限（0 = 不限）。这是唯一
                           # 能保证请求量的手段：无论作品多大，最多发这么多次。
                           # ⚠️ 它**不是风控的解决方案**：实测同一 IP 上触发点
                           # 随累计消耗下降（预算 100 时第 42 次、预算 35 时
                           # 第 16 次），连发就会挂、单发能过，是间歇性的。
                           # 取 35 只是"少发请求、减小影响面"；要稳定采集
                           # 必须换 IP（feed_crawler 尚未接入 proxies）。
ROUND_DEADLINE = 3300      # 单轮硬超时（秒）。任务计划每小时一次，
                           # 留 5 分钟余量，避免上一轮没收尾就叠上下一轮。

# 允许执行的时段（含边界）。任务计划已配了 06:05-22:05 的触发窗口，
# 这里再加一层代码级防护：手动运行、系统补跑、或任务配置被误改时，
# 凌晨也不会真的去打接口。
ACTIVE_HOUR_START = 6
ACTIVE_HOUR_END = 22


def log(msg):
    """同时打到 stdout 和文件日志。

    定时任务模式下 stdout 无人接收，只看得到文件日志——所以这里必须
    双写，否则排查线上问题时日志是空的。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)
    _file_log(msg)


def _ensure_dirs():
    for d in (DATA_DIR, MEDIA_DIR, COMMENT_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


_ROUND_TIMER = {"handle": None, "fired": False}


def _arm_round_deadline():
    """给整轮加软超时。

    Windows 没有 signal.SIGALRM，所以用 threading.Timer 起一个看门狗线程，
    到点置标志位。真正的阻塞点（requests 无 timeout）杀不掉，但可以让主流程
    在下一个检查点主动收尾，不至于被 IgnoreNew 策略永久挡住后续轮次。
    """
    import threading

    def _fire():
        _ROUND_TIMER["fired"] = True

    t = threading.Timer(ROUND_DEADLINE, _fire)
    t.daemon = True
    t.start()
    _ROUND_TIMER["handle"] = t


def _deadline_hit():
    return _ROUND_TIMER["fired"]


def _in_active_window(now=None):
    """当前是否处于允许采集的时段。"""
    h = (now or datetime.now()).hour
    return ACTIVE_HOUR_START <= h <= ACTIVE_HOUR_END


def _disarm_round_deadline():
    t = _ROUND_TIMER.get("handle")
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    _ROUND_TIMER["handle"] = None
    _ROUND_TIMER["fired"] = False


def _file_log(msg):
    try:
        p = os.path.join(LOG_DIR, "feed_%s.log" % datetime.now().strftime("%Y%m%d"))
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# --------------------------------------------------------------- 抓取

def fetch_feed(auth, rounds=REFRESH_ROUNDS):
    """刷 rounds 屏，返回去重后的 aweme 列表。"""
    seen = {}
    for ri in range(1, rounds + 1):
        try:
            res = DouyinAPI.get_tab_feed(auth, count="10", refresh_index=str(ri))
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if "error_code=7" in msg or "限频" in msg:
                raise RuntimeError("限频（error_code=7），放弃本轮: %s" % msg)
            log("  第 %d 屏失败: %s" % (ri, msg[:120]))
            continue
        items = res.get("aweme_list") or []
        added = 0
        for aw in items:
            aid = aw.get("aweme_id")
            if aid and aid not in seen:
                seen[aid] = aw
                added += 1
        log("  第 %d 屏: %d 条，新增 %d，累计 %d" % (ri, len(items), added, len(seen)))
        if not items:
            break
        if _deadline_hit():
            log("  已达单轮时限，停止抓取")
            break
        time.sleep(1)
    return list(seen.values())


# --------------------------------------------------------------- 下载

def _fetch_all_out_comments(auth, url, aid=None):
    """自己翻页拉一级评论，可控每页条数与间隔。

    不复用 dy_apis.get_work_all_out_comment()：它是通用 API 层，翻页
    count 硬编码 5 且**完全没有 sleep**。实测一条 1000 条一级评论的作品，
    那等于连续打约 200 次请求，是触发风控的主因（回复那边有 1.5 秒节流，
    反而比它温和）。把节奏控制留在采集器里，不动通用层的默认行为。
    """
    cursor = "0"
    out = []
    pages = 0
    while True:
        res = DouyinAPI.get_work_out_comment(auth, url, cursor, COMMENT_PAGE_SIZE)
        pages += 1
        comments = res.get("comments") or []
        if not comments:
            break
        out.extend(comments)
        # 提前退出：够量就不再翻页，省下的请求都是风控额度
        if len(out) >= COMMENT_LIMIT:
            break
        if res.get("has_more") != 1:
            break
        cursor = str(res.get("cursor"))
        time.sleep(COMMENT_THROTTLE)
    if aid:
        log("  %s 一级评论 %d 条（%d 页 × %s 条，间隔 %.1fs）"
            % (aid, len(out), pages, COMMENT_PAGE_SIZE, COMMENT_THROTTLE))
    return out


def _dedup_by_cid(comments):
    """按 cid 去重，保持原顺序。

    一级评论和二级回复的翻页都会重叠：实测某作品一级评论取 1000 条、
    唯一只有 783 条（重复 217 条）；二级回复也有同样的现象。存储层有
    UNIQUE(aweme_id, comment_id) 会静默吞掉重复，所以库里看不出来，
    但 JSON 和日志计数会虚高，且白占 COMMENT_LIMIT 的名额。
    """
    seen, uniq = set(), []
    for c in comments or []:
        cid = c.get("cid")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        uniq.append(c)
    return uniq


def _reply_delay(streak):
    """两次回复请求之间的等待秒数。

    正常情况（streak=0）在 REPLY_DELAY_MIN~MAX 之间随机，避免固定节奏；
    连续失败后（streak>=1）指数回退：2 → 4 → 8 → 16 …，封顶
    REPLY_BACKOFF_MAX。回退期间不再叠加随机抖动——此时要的是明确变慢，
    随机反而会让等待时长不可预期。
    """
    if streak <= 0:
        return random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
    return min(REPLY_BACKOFF_BASE * (2 ** (streak - 1)), REPLY_BACKOFF_MAX)


def _select_reply_targets(comments, top_pct=None, budget=None):
    """挑出「值得补抓回复」的一级评论，返回 (选中列表, 说明文本)。

    两级筛选，目的是压住请求数——风控的根源是请求次数，不是数据量：
      1. 按点赞降序取前 top_pct（默认 REPLY_TOP_PCT）
         实测 digg 前 30% 覆盖 69% 的回复量，前 50% 覆盖 81%
      2. 再截断到 budget 条（默认 REPLY_BUDGET，0=不限）
         这是唯一能硬保证请求量上限的环节，无论作品多大都不超发

    `is_hot` 不参与筛选：实测带回复的评论里 95% 都是 is_hot=True，等于没筛。
    """
    if top_pct is None:
        top_pct = REPLY_TOP_PCT
    if budget is None:
        budget = REPLY_BUDGET

    todo = [c for c in comments if int(c.get("reply_comment_total") or 0) > 0]
    if not todo:
        return [], "无带回复的评论"

    total = len(todo)
    # 按点赞降序；点赞相同则回复多的优先（讨论更热）
    todo.sort(key=lambda c: (-int(c.get("digg_count") or 0),
                             -int(c.get("reply_comment_total") or 0)))
    pct = max(0.0, min(1.0, float(top_pct)))
    if pct < 1.0:
        keep = max(1, int(round(total * pct)))   # 至少留 1 条，避免全滤光
        picked = todo[:keep]
    else:
        picked = todo[:]

    note = "带回复 %d 条" % total
    if pct < 1.0:
        note += "，按点赞取前 %.0f%% → %d 条" % (pct * 100, len(picked))
    if budget and len(picked) > budget:
        note += "，预算截断 → %d 条" % budget
        picked = picked[:budget]
    return picked, note


def _fill_replies(auth, comments, aid, top_pct=None, budget=None):
    """给一级评论补抓二级回复，写回 reply_comment。

    comment/list/ 返回的 reply_comment 只是**预览**（实测某条 total=59 却只带
    0~3 条），真正的回复要打 comment/list/reply/。dy_apis 里已有
    get_work_all_comment() 做同样的事，但它没有节流也没有容错——单条失败就
    整批中断。这里逐条抓、失败只跳过该条，并留出间隔避免 error_code=7。

    不是所有评论都抓：先按 _select_reply_targets() 筛选（点赞前 N% + 请求
    预算），没被选中的评论保留接口自带的预览回复。

    连续失败即中止：被风控时接口返回空响应（HTTP 200），若继续逐条打点，
    既拿不到数据又把账号往封禁推。实测 2026-10-31 一条 261 评论的作品，
    风控后仍硬打了 116 次全失败。

    节奏策略（见 _reply_delay）：
      正常 → REPLY_DELAY_MIN~MAX 之间随机（0.5~2 秒）
      连续失败 → 指数回退 2→4→8→16…秒，一旦成功立刻恢复正常随机
    回退给了服务端冷却时间；但回退有上限次数，超过 REPLY_ABORT_AFTER 次
    连续失败仍判定为真风控，放弃剩余。
    """
    todo, note = _select_reply_targets(comments, top_pct, budget)
    if not todo:
        return True
    log("  %s 补抓二级回复：%s（实际请求 %d 次）" % (aid, note, len(todo)))
    got = 0
    streak = 0          # 连续失败次数
    aborted = False
    for i, c in enumerate(todo):
        try:
            reps = DouyinAPI.get_work_all_inner_comment(auth, c)
            c["reply_comment"] = _dedup_by_cid(reps)[:REPLY_LIMIT]
            got += len(c["reply_comment"])
            if streak:
                log("    cid=%s 已恢复（此前连续失败 %d 次）"
                    % (c.get("cid"), streak))
            streak = 0
        except Exception as err:  # noqa: BLE001
            streak += 1
            # 保留接口自带的预览回复，不要清空
            log("    cid=%s 回复抓取失败（连续 %d 次，将回退 %.0fs），保留预览: %s"
                % (c.get("cid"), streak, _reply_delay(streak), str(err)[:90]))
            if streak >= REPLY_ABORT_AFTER:
                log("  %s 连续 %d 次失败（已指数回退仍未恢复），"
                    "判定已被限频/风控，放弃剩余 %d 条回复"
                    % (aid, streak, len(todo) - i - 1))
                aborted = True
                break
        if i + 1 < len(todo):
            time.sleep(_reply_delay(streak))
    log("  %s 二级回复 %d 条（%d/%d 条评论成功）%s"
        % (aid, got, len([c for c in todo if len(c.get("reply_comment") or [])]),
           len(todo), "，已中止" if aborted else ""))
    return not aborted


def download_one(auth, aw, store, round_id, reply_top_pct=None, reply_budget=None):
    """下载视频 + 抓评论。返回 (ok, comment_count)。"""
    from utils.data_util import download_work, handle_work_info

    aid = aw.get("aweme_id")

    # 推荐流返回的 play_addr 主机是 v11-web-prime，实测直链一律 403
    # （content-length: 0，Lego Server 直接拒绝）。同样的请求头去打
    # 作品详情接口拿到的 v11-weba / v26-web 主机就能 200。
    # 所以下载地址不能复用推荐流的，必须回源到作品详情再取一次。
    try:
        detail = DouyinAPI.get_work_info(auth, "https://www.douyin.com/video/%s" % aid)
        aweme = detail.get("aweme_detail") or {}
        if not aweme:
            raise RuntimeError("作品详情为空")
        work_info = handle_work_info(aweme)
    except Exception as err:  # noqa: BLE001
        log("  %s 取作品详情失败，回退用推荐流数据: %s" % (aid, str(err)[:90]))
        try:
            work_info = handle_work_info(aw)
        except Exception as err2:  # noqa: BLE001
            log("  %s 元数据解析失败: %s" % (aid, str(err2)[:100]))
            return False, 0

    ok = False
    try:
        # data_util.download_media() 用原生 requests 且没设 timeout，
        # 单条视频可能永久阻塞。Windows 没有 SIGALRM，改为把 download_work
        # 丢进子线程，主线程 join 180 秒；超时就放弃该条继续下一条。
        # 泄漏的下载线程自身有守护属性，不会阻止进程退出。
        import threading

        box = {}

        def _do():
            try:
                download_work(work_info, MEDIA_DIR, "media")
                box["ok"] = True
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        th = threading.Thread(target=_do, daemon=True)
        th.start()
        th.join(180)
        if th.is_alive():
            log("  %s 视频下载超时（180s），跳过" % aid)
        elif box.get("ok"):
            ok = True
            log("  %s 视频已下载" % aid)
        else:
            log("  %s 视频下载失败: %s"
                % (aid, str(box.get("err"))[:120]))
    except Exception as err:  # noqa: BLE001
        log("  %s 视频下载异常: %s" % (aid, str(err)[:120]))

    n_comment = 0
    try:
        url = "https://www.douyin.com/video/%s" % aid
        comments = _fetch_all_out_comments(auth, url, aid)
        # 必须先去重再截断：翻页重叠时若先截断，重复项会白占 COMMENT_LIMIT
        # 的名额，实际拿到的唯一评论数远小于上限
        comments = _dedup_by_cid(comments)[:COMMENT_LIMIT]
        if not _fill_replies(auth, comments, aid, reply_top_pct, reply_budget):
            # 回复被风控中止：保留已抓到的部分，但仍落盘，不要连一级评论一起丢
            log("  %s 回复抓取不完整（可能已限频），保存已获取部分" % aid)
        n_comment = store.save_comments(aid, comments)
        store.commit()
        # 原始评论另存一份，便于人工查看
        p = os.path.join(COMMENT_DIR, "%s.json" % aid)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)
        log("  %s 评论 %d 条" % (aid, n_comment))
    except Exception as err:  # noqa: BLE001
        log("  %s 评论抓取失败: %s" % (aid, str(err)[:120]))

    if ok:
        store.mark_collected(aid, round_id)
        store.commit()
    return ok, n_comment


# --------------------------------------------------------------- 主流程

def run(args):
    _ensure_dirs()
    _arm_round_deadline()
    store = FeedStore(DB_PATH)
    round_id = store.start_round()
    counts = {"fetched": 0, "classified": 0, "hit": 0,
              "downloaded": 0, "comments": 0}

    try:
        log("加载凭证…")
        auth = load_env()
        log("ticket_match=%s，cookies=%d" % (auth.ticket_matches_session(), len(auth.cookie)))

        log("判定器初始化…")
        sp = os.path.join(DATA_DIR, "themes.json")
        themes = None
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                themes = json.load(f)
            log("  使用自定义主题文件: %s" % sp)
        classifier = TopicClassifier.from_env(themes=themes, threshold=args.threshold)
        log("  model=%s，阈值=%.2f，主题=%s"
            % (classifier.model, classifier.threshold, "/".join(sorted(classifier.theme_names))))

        # 1. 抓取
        log("抓取推荐流（%d 屏）…" % args.rounds)
        awemes = fetch_feed(auth, args.rounds)
        counts["fetched"] = len(awemes)
        log("本轮抓取 %d 条唯一作品" % len(awemes))

        # 2. 跨轮去重
        ids = [a.get("aweme_id") for a in awemes if a.get("aweme_id")]
        new_ids = set(store.filter_new(ids))
        fresh = [a for a in awemes if a.get("aweme_id") in new_ids]
        log("其中新作品 %d 条（已入库 %d 条跳过）"
            % (len(fresh), len(awemes) - len(fresh)))
        if not fresh:
            log("本轮无新作品，结束")
            store.finish_round(round_id, **counts, note="no-new-aweme")
            return 0

        # 3. 判定
        log("主题判定（%d 条）…" % len(fresh))
        judge_map = {}
        for i in range(0, len(fresh), CLASSIFY_BATCH):
            batch = fresh[i:i + CLASSIFY_BATCH]
            try:
                got = classifier.classify_awemes(batch)
                judge_map.update(got)
            except ClassifierError as err:
                log("判定失败，本轮不下载任何内容: %s" % str(err)[:200])
                for aw in fresh:
                    store.save_aweme(aw, [], args.threshold, round_id)
                store.commit()
                store.finish_round(round_id, **counts, note="classifier-error")
                return 1
            log("  批次 %d: %d 条有主题" % (i // CLASSIFY_BATCH + 1, len(got)))
            time.sleep(1)
        # 空壳（desc 空且无话题）在 prepare_items 里就被丢掉，永远不会命中，
        # 不能算进"已判定"——否则统计里的判定量比实际送模型的多出一截
        # （2026-10-30 实测一轮：15 条新作品里 5 条是空壳，占 1/3）。
        signaled = [aw for aw in fresh if classifier.has_signal(aw)]
        counts["classified"] = len(signaled)
        if len(signaled) < len(fresh):
            log("其中 %d 条无 desc 也无话题，跳过判定"
                % (len(fresh) - len(signaled)))

        # 4. 落元数据 + 分流
        to_download = []
        for aw in fresh:
            aid = aw.get("aweme_id")
            topics = judge_map.get(aid, [])
            store.save_aweme(aw, topics, args.threshold, round_id)
            if classifier.passes(topics):
                to_download.append((aw, topics))
        store.commit()
        counts["hit"] = len(to_download)
        log("命中阈值 %d 条" % len(to_download))

        # 打印判定明细
        for aw in fresh[:30]:
            aid = aw.get("aweme_id")
            tp = judge_map.get(aid, [])
            desc = (aw.get("desc") or "")[:36].replace("\n", " ")
            if tp:
                tag = ", ".join("%s(%.2f)" % (t["name"], t["conf"]) for t in tp)
            else:
                tag = "-"
            log("    %s %-38s %s" % (aid, desc, tag))

        if args.dry_run:
            log("--dry-run，跳过下载")
            store.finish_round(round_id, **counts, note="dry-run")
            return 0

        # 5. 采集
        to_download = to_download[:args.max_download]
        log("开始采集 %d 条…" % len(to_download))
        for aw, topics in to_download:
            if _deadline_hit():
                log("  已达单轮时限，停止采集")
                break
            aid = aw.get("aweme_id")
            conf = max((t["conf"] for t in topics), default=0)
            topic = topics[0]["name"] if topics else "-"
            log("  [%s %.2f] %s" % (topic, conf, aid))
            ok, n = download_one(auth, aw, store, round_id,
                reply_top_pct=args.reply_top_pct,
                reply_budget=args.reply_budget)
            counts["downloaded"] += 1 if ok else 0
            counts["comments"] += n
            time.sleep(2)

        store.finish_round(round_id, **counts, note="ok")
        log("本轮完成: 抓取 %d / 命中 %d / 下载 %d / 评论 %d"
            % (counts["fetched"], counts["hit"],
               counts["downloaded"], counts["comments"]))
        _file_log("round done: %s" % json.dumps(counts, ensure_ascii=False))
        return 0
    finally:
        _disarm_round_deadline()
        store.close()


# --------------------------------------------------------------- 单条入库

def _resolve_short_link(url):
    """跟随短链跳转，拿到最终的 /video/ 或 /note/ 地址。

    抖音 App 的"复制链接"给的是 v.douyin.com 短链，302 两次才落到
    www.douyin.com/note/<id>（图文）或 /video/<id>（视频）。
    """
    import requests

    resp = requests.get(url, allow_redirects=True, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
    })
    return resp.url


def _parse_aweme_id(s):
    """从分享链接、App 分享文案或纯 ID 里取出 aweme_id。

    支持：
        https://www.douyin.com/video/7455720996486548795
        https://www.douyin.com/note/7455720996486548795   （图文）
        https://www.douyin.com/discover?modal_id=7455720996486548795
        8.94 复制打开抖音，看看【xx的作品】... https://v.douyin.com/xxxxx/
            —— App "复制链接"的整段文案，URL 前后还带着分享码，直接粘进来即可
        7455720996486548795           （纯数字）
    """
    s = (s or "").strip()
    if s.isdigit():
        return s

    # App 分享文案里 URL 混在中文和分享码之间，先把 URL 抠出来
    m = re.search(r"https?://\S+", s)
    if m:
        s = m.group(0)

    m = re.search(r"/(?:video|note|slides)/(\d{15,25})", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]modal_id=(\d{15,25})", s)
    if m:
        return m.group(1)

    # 短链：自己解析不了，跟随跳转后再抠一次
    if "v.douyin.com" in s:
        try:
            final = _resolve_short_link(s)
        except Exception as err:  # noqa: BLE001
            log("短链解析失败: %s" % str(err)[:120])
            return None
        log("  短链跳转 -> %s" % final)
        return _parse_aweme_id(final)
    return None


def run_one(args):
    """把一条指定作品按推荐流同样的流程处理：判定 → 下载 → 评论 → 入库。

    与 run() 的区别只在「作品从哪来」：这里是用户指定的，不走推荐流，
    因此也没有 refresh_index 翻页和跨轮去重（本来就是指定要采的）。
    """
    _ensure_dirs()
    store = FeedStore(DB_PATH)
    try:
        aid = _parse_aweme_id(args.one)
        if not aid:
            log("无法从输入里解析出 aweme_id: %s" % args.one)
            log("请给完整视频链接（含 /video/<id>）或纯数字 ID")
            return 1
        log("=== 单条采集 aweme_id=%s ===" % aid)

        log("加载凭证…")
        auth = load_env()

        log("拉取作品详情…")
        try:
            detail = DouyinAPI.get_work_info(
                auth, "https://www.douyin.com/video/%s" % aid)
        except Exception as err:  # noqa: BLE001
            log("拉取失败: %s" % str(err)[:200])
            return 1
        aw = detail.get("aweme_detail") or {}
        if not aw:
            log("作品详情为空，可能已删除、私密或链接有误")
            return 1
        log("  %s | %s" % ((aw.get("author") or {}).get("nickname"),
                           (aw.get("desc") or "")[:50]))

        # 判定
        log("主题判定…")
        themes_path = os.path.join(DATA_DIR, "themes.json")
        themes = None
        if os.path.exists(themes_path):
            with open(themes_path, encoding="utf-8") as f:
                themes = json.load(f)
        classifier = TopicClassifier.from_env(
            themes=themes, threshold=args.threshold)
        try:
            topics = classifier.classify_awemes([aw]).get(aid, [])
        except ClassifierError as err:
            log("判定失败: %s" % str(err)[:160])
            log("（继续采集，主题留空）")
            topics = []
        if topics:
            log("  命中: %s" % ", ".join(
                "%s(%.2f)" % (t["name"], t["conf"]) for t in topics))
        else:
            log("  未命中任何主题")

        # 入库（指定采集不因为低于阈值而跳过）
        round_id = store.start_round()
        store.save_aweme(aw, topics, args.threshold, round_id)
        store.commit()

        ok, n = download_one(auth, aw, store, round_id,
            reply_top_pct=args.reply_top_pct,
            reply_budget=args.reply_budget)
        store.finish_round(round_id, fetched=1,
                           classified=1 if classifier.has_signal(aw) else 0,
                           hit=1 if topics else 0,
                           downloaded=1 if ok else 0, comments=n,
                           note="manual")
        log("=== 完成：视频 %s / 评论 %d 条 ==="
            % ("已下载" if ok else "失败", n))
        return 0 if ok else 1
    finally:
        store.close()


def show_stats():
    if not os.path.exists(DB_PATH):
        print("数据库不存在: %s" % DB_PATH)
        return 0
    store = FeedStore(DB_PATH)
    try:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="抖音推荐流采集器")
    ap.add_argument("--rounds", type=int, default=REFRESH_ROUNDS,
                    help="每轮刷几屏推荐（默认 %d）" % REFRESH_ROUNDS)
    ap.add_argument("--max-download", type=int, default=MAX_DOWNLOAD,
                    help="每轮最多下载几条（默认 %d）" % MAX_DOWNLOAD)
    ap.add_argument("--threshold", type=float,
                    default=float(os.getenv("TOPIC_THRESHOLD") or 0.8),
                    help="置信度阈值（默认 0.8）")
    ap.add_argument("--dry-run", action="store_true", help="不下载")
    ap.add_argument("--stats", action="store_true", help="只看统计")
    ap.add_argument("--force", action="store_true",
                    help="忽略时段限制，强制运行")
    ap.add_argument("--one", metavar="URL_OR_ID",
                    help="只采集指定的一条作品（抖音链接或纯 aweme_id），"
                         "按推荐流同样的流程判定+下载+评论+入库")
    ap.add_argument("--reply-top-pct", type=float,
                    default=float(os.getenv("REPLY_TOP_PCT") or REPLY_TOP_PCT),
                    help="只抓按点赞排序前百分之几的评论下的回复，"
                         "默认 %.2f；设为 1.0 抓全部" % REPLY_TOP_PCT)
    ap.add_argument("--reply-budget", type=int,
                    default=int(os.getenv("REPLY_BUDGET") or REPLY_BUDGET),
                    help="单条作品回复请求数硬上限，默认 %d；0 表示不限"
                         % REPLY_BUDGET)
    args = ap.parse_args(argv)

    if args.stats:
        return show_stats()

    if args.one:
        log("=== 指定作品采集 ===")
        try:
            return run_one(args)
        except Exception as err:  # noqa: BLE001
            log("失败: %s: %s" % (type(err).__name__, err))
            return 1

    if not args.force and not _in_active_window():
        log("当前 %s 不在采集时段（%d:00-%d:59），跳过。"
            "如需强制运行加 --force"
            % (datetime.now().strftime("%H:%M"),
               ACTIVE_HOUR_START, ACTIVE_HOUR_END))
        return 0

    log("=== 推荐流采集开始 ===")
    try:
        code = run(args)
    except RuntimeError as err:
        log("中止: %s" % err)
        code = 1
    except TimeoutError as err:
        log("超出单轮时限，强制收尾: %s" % err)
        code = 2
    except Exception as err:  # noqa: BLE001
        log("未预期错误: %s: %s" % (type(err).__name__, err))
        code = 1
    log("=== 结束，exit=%d ===" % code)
    return code


if __name__ == "__main__":
    sys.exit(main())
