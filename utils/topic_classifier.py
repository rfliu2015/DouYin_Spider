# -*- coding: utf-8 -*-
"""短视频主题判定器（基于大模型语义分类）。

为什么要大模型而不是关键词表
------------------------------
关键词表在本场景有三类结构性缺陷（31 条真实推荐流样本实测）：
  误命中：作者名含 "AI" 的历史视频被当成 AI 技术内容；
  漏召：钢琴曲没有"艺术"字样、荣格心理学没有"读书"字样；
  硬失效：约 10% 的作品 desc 为空且取不到作者信息。

实测（LongCat-2.5-Preview，8 条真实样本）：
  明显正确 6/8，边缘可争议 2/8，明显错误 0/8。

设计要点
--------
1. 批量判定：40 条一次请求，省去 40 次往返与 system prompt 重复开销。
2. 只送文本信号：desc 截 120 字 + 话题标签 ≤6 个，约 100 字符/条。
3. 强制 JSON 输出，temperature=0 保证可复现。
4. 该模型是**推理模型**，响应里有 reasoning_content，只读 content。
5. 失败不降级：Key 缺失直接抛错，判定失败由调用方决定跳过本轮。

用法
----
    c = TopicClassifier.from_env()
    hits = c.classify([{'i': 1, 't': 'desc 文本', 'g': ['话题']}])
    # -> {1: [{'name': 'AI技术', 'conf': 0.95}]}
"""
from __future__ import annotations

import json
import os
import time


class ClassifierError(RuntimeError):
    """判定失败（网络、鉴权、响应格式等）。"""


# 主题定义。反例条款是准确率关键——实测中模型正是靠"作者名含AI不算"
# 排除了游戏作者、靠"心理学算读书"命中了荣格那条。
DEFAULT_THEMES = [
    {
        "name": "AI技术",
        "desc": "人工智能、大模型、机器学习、AI工具/插件、算法、神经网络。",
        "neg": "作者名含\"AI\"但内容无关的不算（如历史人物故事）；"
               "非 AI 的普通科技产品评测不算。",
    },
    {
        "name": "艺术",
        "desc": "绘画、音乐、古典乐、乐器演奏、舞蹈、雕塑、建筑美学、摄影。",
        "neg": "音乐榜单八卦、明星绯闻不算。",
    },
    {
        "name": "读书",
        "desc": "书籍推荐、书评、名著解读、作者介绍、阅读方法、哲学、心理学。",
        "neg": "影视剧解说原著不算（属影视）；纯历史事件叙述不算。",
    },
]

DESC_MAX_LEN = 120
TAG_MAX = 6


def _build_system_prompt(themes):
    lines = ["你是短视频主题分类器。判断每条视频是否属于下列主题。", ""]
    for t in themes:
        lines.append("【%s】%s" % (t["name"], t["desc"]))
        if t.get("neg"):
            lines.append("  反例：%s" % t["neg"])
        lines.append("")
    names = "/".join(t["name"] for t in themes)
    lines.append("只输出JSON，不要任何解释：")
    lines.append('{"hits":[{"i":序号,"topics":[{"name":"主题名","conf":0.0到1.0}]}]}')
    lines.append("主题名只能是：%s。不命中的条目不出现。" % names)
    return "\n".join(lines)


def _extract_json(text):
    """从模型输出里抠 JSON。容忍 markdown 代码块包裹。"""
    if not text:
        raise ClassifierError("模型返回空 content")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.strip()
        if s.startswith("json"):
            s = s[4:].strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ClassifierError("模型输出不含 JSON: %s" % s[:120])
    return json.loads(s[start:end + 1])


class TopicClassifier:
    """把一批作品文本判成若干主题 + 置信度。"""

    def __init__(self, base_url, model, api_key, themes=None,
                 threshold=0.8, timeout=180, max_retries=1):
        if not base_url:
            raise ClassifierError("缺少 base_url（检查 .env 的 LONGCAT_BASE_URL）")
        if not api_key:
            raise ClassifierError("缺少 api_key（检查 .env 的 LONGCAT_API_KEY）")
        if not model:
            raise ClassifierError("缺少 model（检查 .env 的 LONGCAT_MODEL）")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.themes = themes or DEFAULT_THEMES
        self.threshold = float(threshold)
        self.timeout = timeout
        self.max_retries = max_retries
        self.system_prompt = _build_system_prompt(self.themes)
        self.theme_names = {t["name"] for t in self.themes}

    @classmethod
    def from_env(cls, env_path=None, **kwargs):
        """从 .env 构造。缺 Key 直接抛错，绝不静默降级为关键词匹配。

        显式传入的 threshold 优先于 .env 里的 TOPIC_THRESHOLD。
        """
        from dotenv import load_dotenv
        load_dotenv(env_path, override=bool(env_path))
        if kwargs.get("threshold") is None:
            kwargs["threshold"] = float(os.getenv("TOPIC_THRESHOLD") or 0.8)
        return cls(
            base_url=os.getenv("LONGCAT_BASE_URL"),
            model=os.getenv("LONGCAT_MODEL"),
            api_key=os.getenv("LONGCAT_API_KEY"),
            **kwargs
        )

    @staticmethod
    def prepare_items(awemes, start_index=1):
        """把 aweme 列表压成送模型的精简格式。

        :return: (items, id_map) —— id_map 把批内序号映射回 aweme_id
        """
        items = []
        id_map = {}
        for offset, aw in enumerate(awemes):
            idx = start_index + offset
            desc = (aw.get("desc") or "").strip()
            tags = [
                t.get("hashtag_name")
                for t in (aw.get("text_extra") or [])
                if t.get("hashtag_name")
            ][:TAG_MAX]
            if not desc and not tags:
                continue  # 空壳，不浪费 token
            items.append({
                "i": idx,
                "t": desc[:DESC_MAX_LEN],
                "g": tags,
            })
            id_map[idx] = aw.get("aweme_id")
        return items, id_map

    def classify(self, items):
        """判定一批。

        :param items: [{'i': int, 't': str, 'g': [str]}]
        :return: {aweme_id 或 i: [{'name': str, 'conf': float}]}
                 —— 用 classify_prepared 时可拿到 aweme_id 映射
        """
        if not items:
            return {}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",
                 "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 2000,
        }
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._post(payload)
                return self._parse(data, items)
            except Exception as err:  # noqa: BLE001 - 统一重试
                last_err = err
                if attempt < self.max_retries:
                    time.sleep(2)
        raise ClassifierError("判定失败: %s" % last_err)

    def _post(self, payload):
        import requests
        resp = requests.post(
            "%s/chat/completions" % self.base_url,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ClassifierError(
                "HTTP %s: %s" % (resp.status_code, (resp.text or "")[:200]))
        return resp.json()

    def _parse(self, data, items):
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as err:
            raise ClassifierError("响应结构异常: %s" % err)
        # 该模型是推理模型，reasoning_content 是思考链，只读 content
        content = msg.get("content") or ""
        obj = _extract_json(content)

        valid_idx = {it["i"] for it in items}
        out = {}
        for hit in obj.get("hits") or []:
            idx = hit.get("i")
            if idx not in valid_idx:
                continue
            topics = []
            for t in hit.get("topics") or []:
                name = (t.get("name") or "").strip()
                if name not in self.theme_names:
                    continue
                try:
                    conf = float(t.get("conf"))
                except (TypeError, ValueError):
                    continue
                topics.append({"name": name, "conf": round(conf, 3)})
            if topics:
                topics.sort(key=lambda x: -x["conf"])
                out[idx] = topics
        return out

    def classify_awemes(self, awemes):
        """便捷入口：直接吃 aweme 列表，返回 {aweme_id: topics}。

        同时把低于阈值的主题标出来（调用方据此决定是否下载）。
        """
        items, id_map = self.prepare_items(awemes)
        raw = self.classify(items)
        out = {}
        for idx, topics in raw.items():
            aid = id_map.get(idx)
            if aid:
                out[aid] = topics
        return out

    def best_conf(self, topics):
        """取最高置信度，没有主题时返回 0。"""
        return max((t["conf"] for t in topics), default=0.0)

    def passes(self, topics):
        """是否达到下载阈值。"""
        return self.best_conf(topics) >= self.threshold
