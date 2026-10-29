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
COMMENT_LIMIT = 200        # 每作品最多取多少条一级评论（Q4）
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

def download_one(auth, aw, store, round_id):
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
        comments = DouyinAPI.get_work_all_out_comment(auth, url)
        comments = comments[:COMMENT_LIMIT]
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
        counts["classified"] = len(fresh)

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
            ok, n = download_one(auth, aw, store, round_id)
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
    args = ap.parse_args(argv)

    if args.stats:
        return show_stats()

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
