# 04 · get_user_info 诊断

> 记录时间：2026-10-28
> 结论：**不是风控随机，不是参数错误，而是缺少 bd-ticket-guard 强校验素材。**

---

## 1. 问题现象

```
RuntimeError: 接口返回空响应（HTTP 200），通常是签名参数不对或被风控拦截。
logid=20261028211304E28FC3253DCF64055E1A
```

HTTP 状态码 200，但 **body 长度为 0**。服务端静默拒绝。

---

## 2. 关键对照：只有这一个接口失败

同一会话、同一 cookie、同一签名栈，各测 8 次：

| 接口 | 路径 | 成功率 |
|---|---|---|
| 作品详情 | `/aweme/v1/web/aweme/detail/` | **8/8** |
| 一级评论 | `/aweme/v1/web/comment/list/` | **8/8** |
| 作品列表 | `/aweme/v1/web/aweme/post/` | **8/8** |
| **用户主页** | `/aweme/v1/web/user/profile/other/` | **0/8** |

这个分布排除了「随机风控」——如果是随机风控，相邻接口不可能全部 8/8。

---

## 3. 逐项排查（每一步都做了对照实验）

### 假设 1：请求参数缺失

逐个删除参数测试：

| 删除的参数 | 结果 |
|---|---|
| `verifyFp` | EMPTY |
| `msToken` | EMPTY |
| `uifid` | EMPTY |
| `land_to` | **OK** (19241 B) |
| `profile_other_record_enable` | **OK** (17867 B) |
| `personal_center_strategy` | EMPTY |

**结论**：`verifyFp` / `msToken` / `uifid` / `personal_center_strategy` 是必需的，但基线本身也经常失败，说明参数不是根因。

### 假设 2：sec_user_id 无效

换成已知可用的用户测试：

| sec_uid | 第 1 次 | 第 2 次 | 第 3 次 |
|---|---|---|---|
| SUSPECT（原失败用户） | EMPTY | **OK** (19240) | **OK** (19240) |
| KNOWN（原成功用户） | EMPTY | EMPTY | EMPTY |

**结论**：与 sec_uid 无关。同一个 id 时好时坏，两个 id 表现一致。

### 假设 3：msToken 过期

| msToken 来源 | 结果 |
|---|---|
| `auth.msToken`（164 字符）× 6 次 | 1/6 |
| cookie 里的 msToken | 不存在 |
| `generate_msToken()` 全新生成（107 字符）× 3 次 | 0/3 |

同时对照：作品详情接口 6/6 成功。

**结论**：与 msToken 无关。同一 msToken 下作品详情全成功，用户主页几乎全失败。

### 假设 4：a_bogus 时间戳过期

检查签名器配置：

```python
# utils/dy_util.py:133
_pure_signer = ABogusPureSigner(fixed=False)
```

`fixed=False` 表示使用真实时间戳。相关常量：

```python
# utils/ab_pure.py:54-59
DUMP_CLOSURE_TIME = 1720000000000
FIXED_NOW = 1720000000000          # 仅 fixed=True 时使用
```

**结论**：用的是当前时间，不是过期时间戳。

### 假设 5：缺 secsdk 签名

`get_work_info` 源码注释明确写着：

> 这个接口在 secsdk 的 webSign 策略表里，末尾还要带 timestamp + 签名

检查 `PROTECTED_PATHS_GET`（14 条）：

```
/aweme/v1/web/aweme/detail/          ← 8/8 成功，在表里
/aweme/v1/web/aweme/post/            ← 8/8 成功，在表里
/aweme/v1/web/aweme/favorite/
/aweme/v1/web/aweme/listcollection/
/aweme/v1/web/collects/list/
/aweme/v1/web/collects/video/list/
/aweme/v1/web/mix/aweme/
/aweme/v1/web/mix/detail/
/aweme/v1/web/mix/list/
/aweme/v1/web/mix/listcollection/
/aweme/v1/web/music/aweme/
/aweme/v1/web/music/detail/
/aweme/v1/web/music/list/
/aweme/v1/web/tab/feed/
```

**`/aweme/v1/web/user/profile/other/` 不在表里。**

手动补签名测试（各 10 次）：

| 方式 | 成功率 |
|---|---|
| A：现状（无 secsdk 签名） | 3/10 |
| B：加 `signed_url` 签名 | 3/10 |

**结论**：补 secsdk 签名无效。说明这个接口走的是另一套校验机制。

---

## 4. 根因定位

### 决定性线索：auth 缺少 bd-ticket-guard 素材

```
ticket      = 无
private_key = 无
ts_sign     = 无
```

### 三个接口的校验强度差异

`builder/header.py` 里有两个方法：

**`with_bd_readonly(auth)`** —— 只读接口用，4 个 header，**不含 client-data**：

```python
def with_bd_readonly(self, auth):
    """只读接口的 bd-ticket-guard 头（4 个，**不含 client-data**）。

    2026-08-16 实录（comment/list / aweme/favorite / user/following/list）
    headers_wire 里只有这四个：
        bd-ticket-guard-ree-public-key / -version: 2 /
        -web-sign-type / -web-version: 2
    """
    if not getattr(auth, 'private_key', None):
        return self          # ← 没有私钥就静默跳过
    ...
```

**`with_bd()`** —— 发布类接口用，**额外带 `bd-ticket-guard-client-data`**（含对本次请求 path + ticket + timestamp 的签名）。

### 为什么 get_user_info 失败

`get_user_info` 调用了 `headers.with_uifid(auth)`，但**没有**调用 `with_bd_readonly` 或 `with_bd`。而即使调用了，因为 `private_key` 为空，`with_bd_readonly` 会直接 `return self` 静默跳过。

对比 `get_work_out_comment`（8/8 成功）——它显式调用了：

```python
# 浏览器在这个接口上**带 bd-ticket-guard 全套**（2026-08-16 实录 headers_wire 确认）
headers.with_bd_readonly(auth)
```

**所以差异是：评论接口调了 `with_bd_readonly`，用户主页没调。** 但两者都会因为缺少 `private_key` 而无法生成完整签名——只是评论接口的校验阈值低，不带也能过；用户主页的阈值高，不带就被静默拒绝。

---

## 5. 结论与推论

### 直接结论

`get_user_info` 失败是**缺少 `bd-ticket-guard` 强校验素材**导致的，而这些素材（ticket / ts_sign / private_key）**只能从扫码登录获得**。

### 逻辑闭环

```
扫码登录  →  需要真实 fpk1  →  需要浏览器采集
    ↓                              ↑
拿到 ticket + 私钥                  |
    ↓                              |
解锁 user/profile 和写接口      ← 而浏览器采集正是 dyclaw 已有的能力
```

三者互相锁定。

### 验证这个推论的方法

如果能拿到 `private_key` + `ticket` + `ts_sign`（三者必须同一次登录），给 `get_user_info` 补上 `with_bd_readonly` 或 `with_bd`，应该就能通。

**但本次无法验证**，因为扫码登录被 fpk1 卡住。

---

## 6. 附带发现：代码质量问题

`dy_apis/douyin_api.py:424` 的 `get_user_info` 与其他接口相比：

| 项目 | get_work_out_comment | get_user_info |
|---|---|---|
| `with_uifid(auth)` | ✅ | ✅ |
| `with_bd_readonly(auth)` | ✅ | ❌ |
| `with_verify_fp(auth)` | ✅ | ✅ |
| `with_a_bogus()` | ✅ | ✅ |

**这是一个可能的上游疏漏**——考虑到 `get_work_out_comment` 的注释说「浏览器在这个接口上带 bd-ticket-guard 全套（2026-08-16 实录 headers_wire 确认）」，说明作者是逐个接口对照抓包补的，`get_user_info` 可能漏了。

如果哪天能拿到 ticket + 私钥，第一个要试的就是给 `get_user_info` 补上 `headers.with_bd_readonly(auth)`。
