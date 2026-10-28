# -*- coding: utf-8 -*-
"""从浏览器 profile 提取 bd-ticket-guard 素材并写入 .env。

背景
----
扫码登录会由项目自己生成 P-256 密钥，但服务端不认识这个新设备指纹
（fpk1 用的是内置 fixture），轮询 30 秒左右就返回 error_code=7 限频。
而浏览器登录时，服务端已经把 ticket / ts_sign / client_cert 签发给浏览器
自己生成的那把密钥，这套素材持久化在 localStorage 里，提取出来注入 auth
就能复用「已被服务端信任」的身份，不需要扫码。

提取来源
--------
    security-sdk/s_sdk_crypt        -> ec_privateKey
    sign_data_key/web_protect       -> ticket / ts_sign
    security-sdk/s_sdk_cert_key     -> client_cert (pub.xxx)

写入 .env 的键
--------------
    DY_TICKET / DY_TS_SIGN / DY_CLIENT_CERT / DY_PRIVATE_KEY

用法
----
    python browser_credential.py                  # 自动搜索 Edge / Chrome
    python browser_credential.py --profile <dir>  # 指定 profile 目录
    python browser_credential.py --list           # 只列出找到的 profile
    python browser_credential.py --dry-run        # 提取但不写 .env

注意
----
LevelDB 用了前缀压缩，私钥文本会被切成两段并在中间插入控制字节，
直接按可见字符过滤拼接会丢掉 qGSM49AwEHBG0wawIBAQQg 这类片段，
解出来是畸形 DER。这里改用「滑窗 + 公钥验证」：把每个 43 字符窗口
当作 32 字节私钥，推导公钥后与 DER 里内嵌的公钥（或 cookie 里的
ree_public_key）比对，只有数学上一致才算命中。
"""
from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import os
import re
import sys

LOCAL = os.environ.get("LOCALAPPDATA") or ""

# 便携版 Chrome 的 user-data-dir 不在默认位置，需要额外探测：
# 常见做法是读运行中进程的 --user-data-dir 参数。
PORTABLE_HINTS = [
    r"D:\Software\C-serial\Chrome\Data",
    os.path.join(os.environ.get("ProgramFiles", ""), "Chrome", "Data"),
    # Edge 的「Edge Internal」变体：某些安装/企业策略下浏览器实际写这里，
    # 而 %LOCALAPPDATA%\Microsoft\Edge\User Data 会长期不更新。
    os.path.join(LOCAL, "Microsoft", "Edge Internal", "User Data"),
]

BROWSERS = [
    ("Edge", os.path.join(LOCAL, "Microsoft", "Edge", "User Data")),
    ("Chrome", os.path.join(LOCAL, "Google", "Chrome", "User Data")),
    ("Edge Beta", os.path.join(LOCAL, "Microsoft", "Edge Beta", "User Data")),
    ("Chrome Beta", os.path.join(LOCAL, "Google", "Chrome Beta", "User Data")),
]

LEVELDB_REL = os.path.join("Local Storage", "leveldb")
ENV_FILE = ".env"

# 抖音登录态里必须齐备的 cookie 名（缺 UIFID 会被判 Uifid Not Found）
DOUYIN_COOKIE_HOSTS = ("douyin.com", "amemv.com", "bytedance.com")


def running_chrome_user_data_dirs():
    """从运行中的 chrome/msedge 进程参数里提取 --user-data-dir。"""
    dirs = []
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='chrome.exe' or name='msedge.exe'",
             "get", "CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return dirs
    for m in re.finditer(r'--user-data-dir=("?)([^"\r\n]+)\1', out):
        d = m.group(2).strip().rstrip("\\")
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def all_browser_roots():
    """返回 [(标签, user-data 根目录)]，含默认位置与便携版探测结果。"""
    roots = list(BROWSERS)
    for d in running_chrome_user_data_dirs():
        roots.append(("running / %s" % os.path.basename(d), d))
    for d in PORTABLE_HINTS:
        if d and os.path.isdir(d):
            roots.append(("portable / %s" % os.path.basename(d), d))
    seen = set()
    out = []
    for label, root in roots:
        if not root or not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        out.append((label, root))
    return out


def find_leveldb_dirs(explicit=None):
    """返回 [(标签, leveldb 目录)]，按最近修改优先排序。"""
    found = []

    if explicit:
        p = os.path.abspath(explicit)
        if os.path.basename(p).lower() == "leveldb":
            found.append((p, p))
        elif os.path.isdir(os.path.join(p, LEVELDB_REL)):
            found.append((p, os.path.join(p, LEVELDB_REL)))
        elif os.path.isdir(os.path.join(p, "Default", LEVELDB_REL)):
            found.append((p, os.path.join(p, "Default", LEVELDB_REL)))
        else:
            pat = os.path.join(p, "**", LEVELDB_REL)
            for h in glob.glob(pat, recursive=True):
                label = os.path.basename(os.path.dirname(os.path.dirname(h)))
                found.append((label, h))
        return found

    for label, root in all_browser_roots():
        for prof in glob.glob(os.path.join(root, "*")):
            ldb = os.path.join(prof, LEVELDB_REL)
            if os.path.isdir(ldb):
                found.append(("%s / %s" % (label, os.path.basename(prof)), ldb))

    cwd = os.getcwd()
    for pat in (os.path.join(cwd, "**", "*profile", LEVELDB_REL),
                os.path.join(cwd, "**", "*profile", "Default", LEVELDB_REL)):
        for h in glob.glob(pat, recursive=True):
            outer = os.path.dirname(os.path.dirname(os.path.dirname(h)))
            found.append(("local / %s" % os.path.basename(outer), h))

    def newest(item):
        ldb = item[1]
        try:
            return max(os.path.getmtime(os.path.join(ldb, f))
                       for f in os.listdir(ldb))
        except Exception:
            return 0

    found.sort(key=newest, reverse=True)
    return found


def read_blobs(leveldb_dir):
    """把 leveldb 目录里所有 .ldb / .log 读成 bytes，大的排前面。"""
    blobs = []
    for f in glob.glob(os.path.join(leveldb_dir, "*")):
        if not f.lower().endswith((".ldb", ".log")):
            continue
        try:
            blobs.append((os.path.basename(f), io.open(f, "rb").read()))
        except Exception:
            continue
    blobs.sort(key=lambda kv: len(kv[1]), reverse=True)
    return blobs


def public_key_from_cookie(blobs):
    """从 cookie 里的 guard 记录取 ree_public_key。"""
    for _name, data in blobs:
        i = data.find(b"bd_ticket_guard_client_data")
        if i >= 0:
            seg = data[i:i + 900]
            pat = rb"ree-public-key[^A-Za-z0-9+/]{1,8}([A-Za-z0-9+/=]{80,96})"
            m = re.search(pat, seg)
            if m:
                return m.group(1).decode()
        i = data.find(b"bd-web-sign-page-guard")
        if i >= 0:
            seg = data[i:i + 2000]
            pat = rb"ree_public_key[^A-Za-z0-9+/]{1,8}([A-Za-z0-9+/=]{80,96})"
            m = re.search(pat, seg)
            if m:
                return m.group(1).decode()
    return None


def extract_private_key(blobs, known_pub_b64=None):
    """滑窗找 32 字节私钥，用推导公钥做验证。"""
    from ecdsa import SigningKey, NIST256p

    for _name, data in blobs:
        i = data.find(b"ec_privateKey")
        if i < 0:
            continue
        region = data[i:i + 1500]
        text = region.decode("latin-1")

        # DER 里的公钥和 cookie 里的 base64 起始偏移不同（DER 中是
        # "...DQgAEbFdF..." 而不是 "BGxXRZ..."），所以不能直接比字符串。
        # 统一还原成原始字节后做子串比对。
        embedded = b""
        runs = re.findall(r"[A-Za-z0-9+/]{60,}", text)
        if runs:
            for run in sorted(runs, key=len, reverse=True)[:4]:
                for off in range(4):
                    cand = run[off:]
                    cand = cand[: len(cand) - len(cand) % 4]
                    if len(cand) < 60:
                        continue
                    try:
                        embedded += base64.b64decode(cand)
                    except Exception:
                        continue

        known_pub_raw = b""
        if known_pub_b64:
            try:
                known_pub_raw = base64.b64decode(known_pub_b64)
            except Exception:
                known_pub_raw = b""

        for run in re.finditer(r"[A-Za-z0-9+/]{43,}", text):
            s = run.group()
            for off in range(0, len(s) - 43 + 1):
                cand = s[off:off + 43]
                try:
                    raw = base64.b64decode(cand + "=")
                except Exception:
                    continue
                if len(raw) != 32:
                    continue
                try:
                    sk = SigningKey.from_string(raw, curve=NIST256p)
                    vk = sk.get_verifying_key()
                except Exception:
                    continue
                point = b"\x04" + vk.to_string()
                derived = base64.b64encode(point).decode()

                if known_pub_raw:
                    if point != known_pub_raw:
                        continue
                elif not embedded or point[1:] not in embedded:
                    continue
                return sk, raw, derived
    return None, None, None


def extract_ticket_and_sign(blobs):
    """从 sign_data_key/web_protect 提取 ticket 与 ts_sign。"""
    ticket = None
    ts_sign = None
    for _name, data in blobs:
        i = data.find(b"sign_data_key/web_protect")
        if i < 0:
            continue
        seg = data[i:i + 2000].decode("latin-1")
        if not ticket:
            m = re.search(r"hash\.[A-Za-z0-9+/=]{20,}", seg)
            if m:
                ticket = m.group()
        if not ts_sign:
            m = re.search(r"ts\.\d+\.[0-9a-fA-F]{40,}", seg)
            if m:
                ts_sign = m.group()
        if ticket and ts_sign:
            break
    return ticket, ts_sign


# ---------------------------------------------------------------- cookie 提取

def _dpapi_unprotect(data):
    """Windows DPAPI 解密（当前用户上下文）。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob(data)), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise RuntimeError("DPAPI 解密失败")
    raw = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return raw


def _aes_key_from_local_state(local_state_path):
    """从 Local State 取 Chrome 的 AES-256 主密钥。"""
    with io.open(local_state_path, encoding="utf-8") as f:
        state = json.load(f)
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc[:5] == b"DPAPI":
        enc = enc[5:]
    return _dpapi_unprotect(enc)


# Chromium 127+ 在 AES-GCM 明文前加了 32 字节的 app-bound 校验前缀
# （SHA256(host_key) 派生，用于把 cookie 绑定到站点）。
# 实测 Edge 156 的 v10 记录明文 = <32 字节前缀><真实值>，必须切掉。
_COOKIE_APP_BOUND_PREFIX = 32


def _decrypt_cookie_value(encrypted, key):
    if not encrypted:
        return ""
    if encrypted[:3] in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = encrypted[3:15]
        plain = AESGCM(key).decrypt(nonce, encrypted[15:], None)
        if len(plain) > _COOKIE_APP_BOUND_PREFIX:
            plain = plain[_COOKIE_APP_BOUND_PREFIX:]
        return plain.decode("utf-8", "replace")
    return encrypted.decode("utf-8", "replace")


def _read_locked_file(path):
    """Chrome 运行时会独占锁库，用 FILE_SHARE_* 打开再复制内容。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    h = k32.CreateFileW(path, 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        err = k32.GetLastError()
        raise RuntimeError(
            "Cookies 被浏览器独占（err=%d）。请先完全退出浏览器再运行。" % err)
    buf = bytearray()
    chunk = ctypes.create_string_buffer(1 << 20)
    read = wintypes.DWORD()
    try:
        while True:
            if not k32.ReadFile(h, chunk, len(chunk), ctypes.byref(read), None):
                break
            if read.value == 0:
                break
            buf += chunk.raw[:read.value]
    finally:
        k32.CloseHandle(h)
    return bytes(buf)


def extract_cookies(profile_dir):
    """读取指定 profile 的抖音 cookie，返回 dict[name] = value。"""
    import shutil
    import sqlite3
    import tempfile

    db = os.path.join(profile_dir, "Network", "Cookies")
    if not os.path.exists(db):
        db = os.path.join(profile_dir, "Cookies")
    if not os.path.exists(db):
        return {}

    root = os.path.dirname(os.path.abspath(profile_dir))
    local_state = os.path.join(root, "Local State")
    if not os.path.exists(local_state):
        local_state = os.path.join(os.path.dirname(root), "Local State")
    key = _aes_key_from_local_state(local_state)

    tmp = tempfile.mktemp(suffix=".db")
    with io.open(tmp, "wb") as f:
        f.write(_read_locked_file(db))

    con = sqlite3.connect(tmp)
    try:
        rows = con.execute(
            "SELECT host_key, name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%douyin%' OR host_key LIKE '%amemv%'")
        rows = rows.fetchall()
    finally:
        con.close()
        try:
            os.remove(tmp)
        except Exception:
            pass

    out = {}
    for _host, name, enc in rows:
        try:
            out[name] = _decrypt_cookie_value(enc, key)
        except Exception:
            continue
    return out


def extract_client_cert(blobs):
    """从 s_sdk_cert_key 提取 pub.xxx 形态的 client_cert。"""
    for _name, data in blobs:
        i = data.find(b"s_sdk_cert_key")
        if i < 0:
            continue
        seg = data[i:i + 900].decode("latin-1")
        m = re.search(r"pub\.[A-Za-z0-9+/=]{40,}", seg)
        if m:
            return m.group()
    return None


def write_env(values, dry_run=False):
    """把素材写进 .env，保留其它键。"""
    from dotenv import set_key

    path = os.path.abspath(ENV_FILE)
    if dry_run:
        for k, v in values.items():
            print("  [dry-run] %-18s = %s..." % (k, str(v)[:40]))
        return path

    if not os.path.exists(path):
        io.open(path, "w", encoding="utf-8").close()
    for k, v in values.items():
        # .env 按行解析，多行 PEM 必须压成单行。
        # 不用 "\n" 转义——python-dotenv 读回来是字面量 backslash-n，
        # 不会还原成换行，SigningKey.from_pem 会直接失败。
        # 这里改成 base64 单行 + 前缀标记，读取时再解码。
        text = str(v)
        if "\n" in text.strip():
            import base64 as _b64
            flat = "b64:" + _b64.b64encode(text.encode()).decode()
        else:
            flat = text
        set_key(path, k, flat, quote_mode="always")
    return path


def harvest(leveldb_dir, with_cookies=True):
    """从一个 leveldb 目录提取全部素材（含 cookie）。"""
    blobs = read_blobs(leveldb_dir)
    if not blobs:
        return {}

    known_pub = public_key_from_cookie(blobs)
    sk, raw, derived = extract_private_key(blobs, known_pub)
    ticket, ts_sign = extract_ticket_and_sign(blobs)
    client_cert = extract_client_cert(blobs)

    got = {}
    if sk is not None:
        got["private_key_pem"] = sk.to_pem().decode()
        got["private_key_hex"] = raw.hex()
        got["public_key_b64"] = derived
        got["pubkey_matched"] = (derived == known_pub) if known_pub else True
    if ticket:
        got["ticket"] = ticket
    if ts_sign:
        got["ts_sign"] = ts_sign
    if client_cert:
        got["client_cert"] = client_cert
    if known_pub:
        got["cookie_ree_public_key"] = known_pub

    if with_cookies:
        # leveldb 目录的父级是 profile 根（.../Default/Local Storage/leveldb）
        profile_dir = os.path.dirname(os.path.dirname(os.path.abspath(leveldb_dir)))
        try:
            cookies = extract_cookies(profile_dir)
        except Exception as err:
            got["cookie_error"] = str(err)[:160]
            cookies = {}
        if cookies:
            got["cookies"] = cookies
    return got


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="browser_credential.py",
        description="从浏览器 profile 提取 bd-ticket-guard 素材并写入 .env")
    ap.add_argument("--profile", help="浏览器 profile 目录，或 leveldb 目录")
    ap.add_argument("--list", action="store_true", help="只列出找到的 profile")
    ap.add_argument("--dry-run", action="store_true", help="提取但不写 .env")
    args = ap.parse_args(argv)

    targets = find_leveldb_dirs(args.profile)
    if not targets:
        print("没有找到任何浏览器 profile。")
        print("请用 --profile 指定 profile 或 leveldb 目录。")
        return 2

    if args.list:
        print("找到 %d 个 profile：" % len(targets))
        for label, ldb in targets:
            print("  %-40s %s" % (label, ldb))
        return 0

    print("扫描 %d 个 profile（最近修改优先）..." % len(targets))

    best = None
    for label, ldb in targets:
        got = harvest(ldb)
        keys = [k for k in ("private_key_pem", "ticket", "ts_sign",
                            "client_cert") if k in got]
        n_ck = len(got.get("cookies") or {})
        note = ""
        if got.get("cookie_error"):
            note = "  [cookie: %s]" % got["cookie_error"][:60]
        print("  %-40s 素材 %d 项, cookie %d 个%s"
              % (label, len(keys), n_ck, note))
        score = len(keys) + (1 if n_ck else 0)
        if score >= 5:
            best = (label, got)
            break
        if got and (best is None or score > (len(best[1]) > 0 and 99 or 0)):
            if best is None:
                best = (label, got)
            else:
                prev = best[1]
                prev_score = len([k for k in ("private_key_pem", "ticket",
                                              "ts_sign", "client_cert")
                                  if k in prev]) + (1 if prev.get("cookies") else 0)
                if score > prev_score:
                    best = (label, got)

    if not best:
        print()
        print("没有提取到任何素材。可能原因：")
        print("  1. 该 profile 从未登录过抖音")
        print("  2. 登录后未触发过写操作（ticket 只在登录时下发）")
        print("  3. leveldb 被浏览器独占锁定，先完全退出浏览器再试")
        return 1

    label, got = best
    print()
    print("采用: %s" % label)
    print()
    print("提取结果:")
    if "private_key_pem" in got:
        print("  私钥        %s..." % got["private_key_hex"][:32])
        print("  推导公钥    %s..." % got["public_key_b64"][:48])
        ok = "通过" if got.get("pubkey_matched") else "未验证"
        print("  公钥验证    %s" % ok)
    for k, name in (("ticket", "ticket"), ("ts_sign", "ts_sign"),
                    ("client_cert", "client_cert")):
        if k in got:
            print("  %-11s %s..." % (name, got[k][:48]))
        else:
            print("  %-11s 缺失" % name)

    cookies = got.get("cookies") or {}
    if cookies:
        print("  %-11s %d 个" % ("cookie", len(cookies)))
        for must in ("sessionid", "UIFID", "ttwid"):
            print("    %-24s %s" % (must, "有" if cookies.get(must) else "缺失"))
    else:
        print("  %-11s 未提取到%s"
              % ("cookie", ("（%s）" % got["cookie_error"][:50])
                 if got.get("cookie_error") else ""))

    missing = [k for k in ("private_key_pem", "ticket", "ts_sign",
                           "client_cert") if k not in got]
    if missing:
        print()
        print("警告：缺少 %s，写接口仍不可用。" % missing)
    if not cookies:
        print("警告：没有 cookie，换账号时 DY_COOKIES 会与新 ticket 不匹配。")

    values = {}
    if "ticket" in got:
        values["DY_TICKET"] = got["ticket"]
    if "ts_sign" in got:
        values["DY_TS_SIGN"] = got["ts_sign"]
    if "client_cert" in got:
        values["DY_CLIENT_CERT"] = got["client_cert"]
    if "private_key_pem" in got:
        values["DY_PRIVATE_KEY"] = got["private_key_pem"]
    if cookies:
        # 与浏览器 wire 顺序无关，但保持稳定排序便于 diff
        values["DY_COOKIES"] = "; ".join(
            "%s=%s" % (k, v) for k, v in cookies.items())

    print()
    path = write_env(values, dry_run=args.dry_run)
    head = "已写入（dry-run 未落盘）: " if args.dry_run else "已写入: "
    print(head + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
