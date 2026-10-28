# Douyin_Spider 逆向研究笔记

> 项目：[`cv-cat/Douyin_Spider`](https://github.com/cv-cat/Douyin_Spider)
> 本地路径：`E:\Code\Projects\ReverseProjects\Douyin_Spider`
> 记录时间：2026-10-28
> 目的：跑通项目、评估能力边界、记录踩坑与结论

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [01-环境搭建.md](01-环境搭建.md) | 依赖安装、protobuf 冲突解决、环境验证 |
| [02-运行实测.md](02-运行实测.md) | 各功能实测结果与成功率数据 |
| [03-扫码登录攻坚.md](03-扫码登录攻坚.md) | 纯 HTTP 扫码登录的完整尝试过程与失败根因 |
| [04-get_user_info诊断.md](04-get_user_info诊断.md) | 唯一失败读接口的完整排查实验 |
| [05-能力边界与结论.md](05-能力边界与结论.md) | 最终能力矩阵、与自研 dyclaw 的对比、后续建议 |
| [06-文章核对-发文风控参数.md](06-文章核对-发文风控参数.md) | 外部文章与项目源码逐条核对，含 fpk1 突破思路 |
| [07-浏览器登录提取方案.md](07-浏览器登录提取方案.md) | **⭐ 突破方案**：从浏览器提取私钥与凭证，绕过 fpk1 |
| [08-写接口测试与blocked分析.md](08-写接口测试与blocked分析.md) | 写接口实测、换账号验证、blocked 环境级限制分析 |
| [09-迁移到新机器指南.md](09-迁移到新机器指南.md) | **⭐ 换机器/换网络验证的完整步骤** |

---

## 一句话结论

**读能力完整可用；写接口受阻于环境级 blocked，与代码无关。**

- 扫码登录被 fpk1 拦截 → **已绕过**：从浏览器 localStorage 提取私钥与 ticket 素材注入 auth（见 [07](07-浏览器登录提取方案.md)）
- 用户主页从 **0/8 → 7/8**
- 写接口技术前提全部就绪（ticket/私钥/dtrait/csrf 齐全），但 `profile/self` 返回 `blocked`，**换账号后仍然 blocked** → 判定为 IP/设备级限制（见 [08](08-写接口测试与blocked分析.md)）
- 下一步：换机器换网络验证（见 [09](09-迁移到新机器指南.md)）

---

## 快速状态

```
项目版本   d151f9c（与上游 origin/master 完全一致，无更新）
Python     3.12.4
依赖       22/22 全部安装并可导入
测试       13 passed / 8 failed（均为上游既有问题，与本次操作无关）
代码规模   65 个 py 文件 / 27213 行
当前状态   cookie 已配置，读链路可用
```

## 已验证可用的能力

| 能力 | 状态 | 证据 |
|---|---|---|
| 依赖安装 / 模块导入 | ✅ | 22 个依赖全部 import 成功 |
| 签名栈（a_bogus / msToken / secsdk） | ✅ | 纯 Python 本地计算，无需浏览器 |
| 作品详情 | ✅ 8/8 | 抓到真实数据 |
| 一级评论 | ✅ 8/8 | 43 条评论 |
| 内层回复 | ✅ | 展开 11 条回复 |
| 作品列表 | ✅ 8/8 | — |
| Excel 导出 | ✅ | 生成 6456 B 文件 |
| 直播信息（匿名） | ✅ | 真实返回房间数据 |
| 直播 WebSocket（匿名） | ⚠️ | 握手成功，房间未开播 |

## 已知阻塞（已解决 / 仍存在）

| 问题 | 状态 | 说明 |
|---|---|---|
| 扫码登录失败 | ⚠️ 未解决，但已绕过 | fpk1 用内置 fixture → 服务端限频。改用浏览器提取方案 |
| `get_user_info` 0/8 | ✅ 已解决 | 提取 ticket + 私钥注入后 → 7/8 |
| 8 个测试失败 | ⚠️ 仍存在 | 上游 HEAD 既有问题，不影响主流程 |

## 新增工具

```bash
# 从浏览器 profile 提取 ticket-guard 素材并写入 .env
python browser_credential.py
python browser_credential.py --list      # 列出可用 profile
python browser_credential.py --dry-run   # 只提取不写入
```

写入 `DY_TICKET` / `DY_TS_SIGN` / `DY_CLIENT_CERT` / `DY_PRIVATE_KEY`。
详见 [07-浏览器登录提取方案.md](07-浏览器登录提取方案.md)。

---

## 重要提醒

- `.env` 含真实登录 cookie，已在 `.gitignore` 中，**切勿提交**
- 限频（`error_code=7`）是服务端冷却，**反复重试会加重**，实测间隔 3 天仍会触发
- 项目根目录的 `_*.py` 与 `_*.txt` 是本次调试产生的临时脚本，非项目原有文件
