# DouYin_Spider 逆向研究笔记

> 项目：[cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider)
> 本地路径：`E:\Code\Projects\DouYin_Spider`
> fork HEAD：`7f721cf`
> 最后更新：2026-10-29

---

## 一句话结论

**读能力完整可用，写能力全部不可用。**

- ✅ 作品 / 评论 / 搜索 / 用户主页 / 直播 —— 全部跑通
- ❌ 点赞 / 收藏 / 评论 / 发布 / 私信 —— 卡在 `bd-ticket-guard-key-sign-result: 2`

---

## 能力矩阵

| 能力 | 状态 | 备注 |
|---|---|---|
| 作品详情 | ✅ | 稳定 |
| 作品列表 | ✅ | — |
| 评论 / 回复 | ✅ | — |
| 搜索 | ✅ | — |
| 用户主页 | ✅ | 需 ticket 注入 |
| Excel 导出 | ✅ | — |
| 直播信息 / WebSocket | ✅ | 免登录 |
| 点赞 / 收藏 | ❌ | `key-sign-result: 2` |
| 发评论 / 发布 / 私信 | ❌ | 同上 |

---

## 文档结构

本文档按**信息半衰期**分三层：

```
docs/
├── README.md          ← 你在这里
│
├── 状态/              【可变项】反映"现在"，每次实验后更新
│   ├── 当前状态.md     一页纸：能力 / 凭证 / 风险 / 下一步
│   ├── 进度日志.md     倒序追加，只记事实
│   └── 未解问题.md     悬案 + 实验队列
│
├── 主题/              【不变项】有定论，只写结论与必要步骤
│   ├── 环境搭建.md     依赖 / protobuf 冲突 / 循环导入
│   ├── 签名栈.md       a_bogus / secsdk / msToken / dtrait / bd-ticket
│   ├── 凭证提取.md     leveldb 原理 + 浏览器加密对照
│   ├── 读能力.md       作品 / 评论 / 直播 / 用户主页
│   ├── 写能力.md       为什么不通
│   ├── 扫码登录.md     fpk1 根因（已判死刑）
│   └── 工具手册.md     browser_credential.py / main.py
│
└── archive/           历史原文，不再维护
    └── 01 ~ 10
```

**阅读建议**：

| 你想知道 | 去哪看 |
|---|---|
| 现在能做什么 | [状态/当前状态](状态/当前状态.md) |
| 怎么装环境 | [主题/环境搭建](主题/环境搭建.md) |
| 怎么拿凭证 | [主题/凭证提取](主题/凭证提取.md) + [工具手册](主题/工具手册.md) |
| 签名怎么算 | [主题/签名栈](主题/签名栈.md) |
| 写接口为什么不通 | [主题/写能力](主题/写能力.md) |
| 当初踩了哪些坑 | `archive/` |

---

## 快速开始

```powershell
$env:PYTHONIOENCODING='utf-8'

# 1. 装依赖（protobuf 有硬冲突，不能用 -r requirements.txt）
Get-Content requirements.txt |
  Where-Object { $_ -notmatch '^(protobuf|blackboxprotobuf)' -and $_ -notmatch '^#' -and $_.Trim() } |
  Set-Content req_rest.txt
pip install -r req_rest.txt
pip install 'protobuf>=5.27,<6'
pip install --no-deps blackboxprotobuf

# 2. 验证
python -c "import main; print('OK')"      # → OK
python -m pytest tests -q                  # → 13 passed, 8 failed

# 3. 提取凭证（必须先完全退出浏览器）
python browser_credential.py --list
python browser_credential.py --profile "<profile 目录>" --dry-run
python browser_credential.py --profile "<profile 目录>"
```

详见 [环境搭建](主题/环境搭建.md) / [工具手册](主题/工具手册.md)。

---

## 项目规模

| 项 | 数据 |
|---|---|
| 上游 | `cv-cat/DouYin_Spider`（最后提交 2026-10-23） |
| 代码 | **59 个 py 文件 / 27377 行** |
| 模块 | `dy_live` 11304 · `dy_apis` 9421 · `utils` 3324 · `builder` 1873 · `tests` 378 |
| 依赖 | 22 个，纯 Python，无浏览器依赖 |

---

## 这个项目最强的地方

**纯 Python 重写了 5 类反爬签名**，不需要浏览器跑 JS：

| 模块 | 行数 | 亮点 |
|---|---|---|
| `utils/secsdk_web_sign.py` | 187 | webSign 纯算（同类项目都要开 headless） |
| `utils/ab_pure.py` | ~400 | a_bogus 按 host 分 **4 套** `(aid, page_id)` |
| `utils/dtrait_features.py` | 225 | dtrait 内层 VM，解出头字节位域 |
| `utils/bd_ticket.py` | ~300 | ECDH + HKDF + HMAC/ECDSA 双模式 |
| `dy_live` | 11304 | 直播 WebSocket / PK / 礼物 / 9 个 proto |

详见 [签名栈](主题/签名栈.md)。

---

## fork 加了什么

`7f721cf` 相对上游 `d151f9c`（+2961 / −1）：

| 文件 | 变化 | 性质 |
|---|---|---|
| `browser_credential.py` | +592 行 | **全新**，凭证提取工具 |
| `utils/common_util.py` | +21/−1 | `DY_PRIVATE_KEY` 支持 b64 单行 |
| `docs/` | — | 研究笔记 |

> ⚠️ **写接口路线是 fork 加的旁路，不是原项目的主路径。**
> 原项目写接口走扫码登录，被 fpk1 拦死（见 [扫码登录](主题/扫码登录.md)）。

---

## 重要提醒

| 项 | 说明 |
|---|---|
| `.env` | 含真实凭证，**已在 `.gitignore`，切勿提交** |
| 凭证不能跨机器复制 | 绑定本机设备指纹 |
| 写接口会作废 ticket | 失败后读能力也会中断 |
| 限频 `error_code=7` | 服务端冷却，**反复重试会加重** |
| `main.py` 第 4 段 | **会真的发私信**，别误触 |
| 别动真实 profile | 曾有 Cookies 库被清空事故，见 [未解问题](状态/未解问题.md) Q4 |

---

## 适用判断

| 你的目标 | 建议 |
|---|---|
| 采集作品 / 评论 / 直播数据 | ✅ **值得用**，读能力完整 |
| 只要签名算法 | ✅ 抄 `secsdk_web_sign.py` + `ab_pure.py`，性价比极高 |
| 自动化互动（点赞/评论/发布） | ❌ **不建议**，写接口不通且会连带作废会话 |
| 长期生产使用 | ⚠️ 注意浏览器 App-Bound 会导致提取通道收窄 |
