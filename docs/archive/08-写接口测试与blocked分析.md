# 08 · 写接口测试与 blocked 分析

> 记录时间：2026-10-28
> 结论：**写接口技术前提全部就绪，但账号层被服务端标记 blocked，属环境级限制。**

---

## 1. 测试目标

读能力已在 [02](02-运行实测.md) / [07](07-浏览器登录提取方案.md) 中验证完毕。本节验证写接口：

- 点赞 `/aweme/v1/web/commit/item/digg/`
- 收藏 `/aweme/v1/web/aweme/collect/`
- 发评论 `/aweme/v1/web/comment/publish`

---

## 2. 技术前提检查（不发任何写请求）

| 素材 | 状态 |
|---|---|
| `ticket` | ✅ 有 |
| `ts_sign` | ✅ 有 |
| `client_cert` | ✅ 有 |
| `private_key` | ✅ 有 |
| `ticket_matches_session()` | ✅ True |
| `dtrait_profile` | ✅ 有（8 个键） |
| `dtrait` 头生成 | ✅ 成功，840 字符，`d0_` 前缀 |
| `csrf token` | ✅ token_1 + token_2 |
| `uid` | ✅ 有 |

**所有技术前提齐备。**

---

## 3. 点赞实测（可逆测试）

为不留下痕迹，采用「点赞 → 确认 → 取消 → 确认恢复」流程：

```
=== 初始状态 ===
  digg_count = 5330

=== 1. 点赞 ===
  digg(type=1) 失败: RuntimeError: 接口返回空响应（HTTP 403）
      通常是签名参数不对或被风控拦截。logid=20261028220517226AD71B600A02F1AD6F
  点赞后 digg_count = 5330   ← 未改变

=== 2. 取消点赞 ===
  digg(type=0) -> False

=== 结果 ===
  初始 5330 -> 最终 5330
  状态已恢复: True
```

**账号状态未被修改。**

---

## 4. 深入诊断：403 的真实原因

手动构造完整请求并打印所有细节：

### 请求头（19 个，全部正确）

```
accept                           application/json, text/plain, */*
accept-language                  zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6
bd-ticket-guard-ree-public-key   <REDACTED-PUBKEY>...
bd-ticket-guard-version          2
bd-ticket-guard-web-sign-type    1
bd-ticket-guard-web-version      2
content-type                     application/x-www-form-urlencoded; charset=UTF-8
origin                           https://www.douyin.com
referer                          https://www.douyin.com/discover?modal_id=7686857842418126114
sec-ch-ua                        "Google Chrome";v="155", "Chromium";v="155", ...
sec-fetch-dest                   empty
sec-fetch-mode                   cors
sec-fetch-site                   same-origin
uifid                            c8ccd135b9ded2d8b409ba6e99a2ddb1...
user-agent                       Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...
x-secsdk-csrf-token              000100000001c768c75329108a4c73b67e06cc9b...
x-tt-session-dtrait              d0_clWTZT5q5Klfzl12gwx2XYWjyhXJ5mFwh56ycMJKClIB9nK...
```

### 响应

```json
{"status_code":8,"status_msg":"用户未登录","log_pb":{"impr_id":"..."}}
```

**注意**：这次返回的是 `status_code: 8 用户未登录`，而不是之前的 403。说明请求格式是对的，问题在**会话识别**。

---

## 5. 关键对照实验

### 5.1 profile/self 探针

```
带完整签名 → {"status_code":0,"status_msg":"blocked","user":null}
裸 cookie   → {"status_code":8,"status_msg":"用户未登录"}
```

**这个对比极有信息量**：

| 请求方式 | 响应 | 含义 |
|---|---|---|
| 裸 cookie（无签名） | 用户未登录 | 服务端根本不认这个 cookie |
| 带完整签名 | blocked | 签名通过了前置校验，但**账号层被拦** |

### 5.2 cookie 完整性检查

```
sessionid      len=32   ✅
sessionid_ss   len=32   ✅
sid_tt         len=32   ✅
sid_guard      len=93   ✅
uid_tt         len=32   ✅
UIFID          len=384  ✅
d_ticket       len=37   ✅
passport_auth_status      缺失  ← 注意
passport_auth_status_ss   缺失  ← 注意
```

`passport_auth_status` 缺失是异常信号，正常登录态应该有这个字段。

### 5.3 sessionid 有效期

```
签发时间: 2026-10-20 20:02:41
有效期  : 60 天
过期时间: 2026-12-19 20:02:41
现在    : 2026-10-28 22:06:02
是否过期: False    ← 未过期
```

**cookie 没过期，但仍然 blocked。**

---

## 6. 换账号验证

### 6.1 换账号的完整流程

为了排除「单个账号被封」的可能，用另一个账号重新走了一遍：

1. 在 **Edge** 中登录小号
2. 关闭 Edge 释放 Cookies 库锁
3. 用 `browser_credential.py` 提取全套素材 + 63 个 cookie
4. 写入 `.env`

### 6.2 新账号素材

```
私钥        a506d53c0bce1b264d871db19e01a29a...
推导公钥    <REDACTED-PUBKEY>...
公钥验证    通过
ticket      hash.<REDACTED-48-chars>...
ts_sign     ts.<ver>.<REDACTED-48-chars>...
client_cert pub.<REDACTED-48-chars>...
cookie      63 个（sessionid / UIFID / ttwid 全部有）
```

### 6.3 结果：**同样 blocked**

```json
{"status_code":0,"status_msg":"blocked","user":null,"user_collect_count":null}
```

读接口正常：
```
作品详情  OK
一级评论  OK
dtrait   成功 len=840
```

**两个完全不同的账号，返回完全相同的 blocked。**

---

## 7. 结论：环境级限制

### 证据链

| 检查项 | 账号 A | 账号 B（新） |
|---|---|---|
| sessionid 有效 | ✅ | ✅ |
| UIFID | ✅ | ✅ |
| ticket / 私钥 | ✅ | ✅ |
| ticket_match | ✅ True | ✅ True |
| dtrait 可生成 | ✅ | ✅ |
| 读接口 | ✅ | ✅ |
| **profile/self** | ❌ blocked | ❌ **blocked** |

### 推断

既然换账号无效，问题必然在**账号之外**的因素：

| 可能性 | 评估 |
|---|---|
| **设备指纹被标记** | ⭐ 最可能。`ttwid` / `odin_tt` 是设备级标识，两个账号共用同一台机器 → 同一设备指纹 |
| **公网 IP 被标记** | 可能。之前扫码登录时的大量请求（200+ 次 `check_qrconnect`）可能触发了 IP 级风控 |
| 项目签名有缺陷 | 可能性较低。同会话下其他接口全部正常，且 `profile/other/` 能通（7/8） |

**注意一个矛盾点**：`user/profile/other/`（查别人）能通 7/8，但 `user/profile/self/`（查自己）返回 blocked。

这说明**签名和 cookie 本身没问题**，而是「确认你是登录用户本人」这一步被拦。这通常是账号级或设备级的会话校验。

---

## 8. 对项目能力的最终评价

### 读能力：完整可用 ✅

| 接口 | 成功率 |
|---|---|
| 作品详情 | 8/8 |
| 一级评论 | 8/8 |
| 内层回复 | ✅ |
| 用户主页 | 7/8 |
| 作品列表 | 8/8 |
| 直播信息 | ✅ |
| 直播 WebSocket | ✅ |

### 写能力：环境受限 ❌

| 接口 | 状态 |
|---|---|
| 点赞 | ❌ 用户未登录 |
| 收藏 | ❌ 403 ArgusSecurityPlugin |
| 发评论 | ❌ 未测（前置被拦） |

**与项目代码无关**，与 `browser_credential.py` 工具也无关。凭证提取是成功的（公钥验证通过、`ticket_match: True`）。

---

## 9. 待验证的假设（换网络环境后）

把项目 `git pull` 到另一台电脑、换一套网络后，按以下顺序验证：

```bash
# 1. 在新机器上登录抖音（浏览器）
# 2. 提取凭证
python browser_credential.py

# 3. 先测 profile/self —— 这是判断 blocked 的关键
python -c "
import dy_apis.douyin_api
from utils.common_util import load_env
import utils.http_client as rq
a = load_env()
r = rq.get('https://www.douyin.com/aweme/v1/web/user/profile/self/', cookies=a.cookie, verify=False)
print(r.status_code, r.content[:300])
"

# 4. 若 profile/self 正常返回用户信息 -> 写接口可以继续测
# 5. 若仍 blocked -> 确认是账号/设备级，需要全新账号
```

### 判据

| profile/self 结果 | 说明 | 下一步 |
|---|---|---|
| 返回 `user` 有内容 | 环境正常 | 继续测写接口 |
| `status_msg: blocked` | 仍是环境/账号问题 | 换全新账号或等解封 |
| `用户未登录` | cookie 无效 | 重新登录 |

---

## 10. 重要提醒

1. **不要频繁重试**。限频（`error_code=7`）和 blocked 都会因重试加重。
2. **写接口测试用可逆操作**。点赞后立即取消，避免留下痕迹。
3. **`.env` 含真实凭证**，已在 `.gitignore`，切勿提交。
4. 换机器后 `.env` 需要重新生成（cookie 与设备绑定）。
