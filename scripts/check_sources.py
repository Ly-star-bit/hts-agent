# -*- coding: utf-8 -*-
"""
check_sources.py —— 三份官方源文件的更新检测 / 下载

本工具的一切税负判定都建立在仓库根目录的三个官方文件上；官方每月都可能改版，
而本地文件不会自己变。这个脚本回答"官方现在的版本和我手里这份是不是同一份"，
并可按需把新版拉下来替换。

【三份文件的官方地址】（2026-09 实测钉死）
  htsdata.csv        USITC 全量税率表导出
                     https://hts.usitc.gov/reststop/exportList?from=0100&to=9999&format=CSV&styles=false
                     当前版本号另有一个极便宜的探针：/reststop/currentRelease → {"name": "2026HTSRev17"}
  China Tariffs.pdf  USTR 301 中国清单——注意托管方是 USITC 而非 USTR，
                     文件名随 HTS 版本改（Rev15 → Rev17）但内容常常一个字节不变：
                     https://hts.usitc.gov/reststop/file?release=currentRelease&filename=China+Tariffs
  FLIP 301 FRN       USTR 最终行动通知，固定 URL，服务端给 ETag / Last-Modified：
                     https://ustr.gov/sites/default/files/files/Press/Releases/2026/FLIP%20301%20...FINAL.pdf

【判定原则】"有没有更新"以**内容哈希**为准，不信版本号也不信文件名：
  - USITC 每次发版都把 China Tariffs 改名（Rev15→Rev17），内容却可能完全一样——按名判会误报
  - CSV 导出的换行符（CRLF/LF）会漂，哈希前抹平，否则每次都"更新"
  - 版本号 / ETag 只用来省流量：和上次一样就不重新下载 4MB 的 CSV / 2.7MB 的 PDF
  - 校验和比对的对象是**本地文件本身**（不是上次记录的状态）——用户手动替换过文件也不会错判

【FLIP FRN 的局限】URL 指向 7-23-26 这一份具体通知。USTR 若发布**新的**修改通知，
  是新 URL、新文件，这里探测不到——只能发现同一份被静默修订。新通知需人工关注
  USTR 新闻页（脚本报告里会印出这个提醒）。

【本地文件名不改】China Tariffs 本地固定叫 "..._2026HTSRev15.pdf"（build_db.py /
  app.py 按名引用）；--apply 时原地覆盖内容，远端版本号记进 data/.sources_state.json，
  由页面「数据来源」弹窗展示，不依赖文件名。

用法：
    python scripts/check_sources.py                 # 检查并打印报告；有更新退出码 3
    python scripts/check_sources.py --json          # 机器可读
    python scripts/check_sources.py --apply         # 有更新则下载覆盖（旧文件备份到 output/sources_backup/<时间>/）
    python scripts/check_sources.py --apply --rebuild   # 覆盖后自动重跑 build_db.py（FLIP 变了再跑 extract_flip_scopes.py）
    python scripts/check_sources.py --notify        # 有更新/出错时弹 macOS 通知（launchd 用）
    python scripts/check_sources.py --only htsdata,ustr_pdf
  退出码：0 全部最新 / 2 检查出错（网络等） / 3 发现更新
  建议 launchd 每天早上跑一次（见 README「数据更新方法」）。
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "data", ".sources_state.json")
BACKUP_DIR = os.path.join(BASE_DIR, "output", "sources_backup")

USITC_RELEASE_URL = "https://hts.usitc.gov/reststop/currentRelease"

SOURCES = {
    "htsdata": {
        "path": "htsdata.csv",
        "label": "USITC 全量税率表",
        "url": "https://hts.usitc.gov/reststop/exportList?from=0100&to=9999&format=CSV&styles=false",
        "kind": "csv",
    },
    "ustr_pdf": {
        "path": "China Tariffs_2026HTSRev15.pdf",
        "label": "USTR 301 中国清单",
        "url": "https://hts.usitc.gov/reststop/file?release=currentRelease&filename=China+Tariffs",
        "kind": "pdf",
    },
    "flip_frn": {
        "path": "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf",
        "label": "FLIP 301 Final Action FRN",
        "url": ("https://ustr.gov/sites/default/files/files/Press/Releases/2026/"
                "FLIP%20301%20Investigation%20Final%20Action%20FRN%207-23-26%20FINAL.pdf"),
        "kind": "pdf",
    },
}

# 远端探针里印出的"人工关注"提醒——脚本探测不到的那类变化
MANUAL_WATCH = {
    "flip_frn": "新的 FLIP 301 修改通知会是新 URL，本脚本只盯这一份；请关注 "
                "https://ustr.gov/about/policy-offices/press-office/press-releases",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (hts-agent source checker)"}
TIMEOUT = httpx.Timeout(60.0, read=180.0)
EXIT_OK, EXIT_ERROR, EXIT_UPDATED = 0, 2, 3


# ------------------------------------------------------------
# 哈希与状态
# ------------------------------------------------------------

def content_sha256(data: bytes, kind: str) -> str:
    """内容哈希。CSV 抹平 CRLF/LF 与 BOM——USITC 导出的换行符会漂，不抹就每次都"更新"。"""
    if kind == "csv":
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
    return hashlib.sha256(data).hexdigest()


def local_sha256(key: str):
    info = SOURCES[key]
    path = os.path.join(BASE_DIR, info["path"])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return content_sha256(f.read(), info["kind"])


def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------
# 远端探测
# ------------------------------------------------------------

def _client(transport=None):
    return httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, transport=transport)


def _get(client, url) -> httpx.Response:
    """GET 带一次重试——USITC 偶发 503，第二次通常就好。"""
    last = None
    for _ in range(2):
        try:
            r = client.get(url)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except httpx.HTTPError as e:
            last = e
    raise last


def _content_filename(resp) -> str:
    m = re.search(r'filename="?([^";]+)"?', resp.headers.get("content-disposition", ""))
    return m.group(1) if m else ""


def _pdf_last_updated(data: bytes) -> str:
    """China Tariffs 首页有 "(Last Updated July 28, 2026)"，抓出来给人看；抓不到不算错。"""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = pdf.pages[0].extract_text() or ""
        m = re.search(r"Last Updated\s+([A-Za-z]+ \d{1,2}, \d{4})", text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _csv_line_diff(old: bytes, new: bytes, limit=8):
    """行级集合差异（不是顺序 diff）：报告"新增/删除了哪些行"，让人一眼看出改的是哪些编码。"""
    def lines(b):
        b = b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return set(b.decode("utf-8-sig", errors="replace").split("\n"))
    o, n = lines(old), lines(new)
    added, removed = sorted(n - o), sorted(o - n)
    def head(s):
        """样例只印编码；结构行（HTS Number 为空，如 "Hydraulic fluid power pumps:"）印品名。"""
        import csv
        try:
            cols = next(csv.reader([s]))
        except (csv.Error, StopIteration):
            return s[:60]
        code = (cols[0] if cols else "").strip()
        desc = (cols[2] if len(cols) > 2 else "").strip()
        return code or f"[{desc[:40]}]" or s[:60]
    return {
        "added": len(added), "removed": len(removed),
        "added_sample": [head(s) for s in added[:limit]],
        "removed_sample": [head(s) for s in removed[:limit]],
    }


def _probe_htsdata(client, prev, local_hash, force):
    """先问版本号（100 字节），版本没变且本地文件没动就不下 4MB。"""
    r = _get(client, USITC_RELEASE_URL)
    release = (r.json() or {}).get("name", "")
    res = {"remote_version": release}
    if (not force and release and prev.get("remote_version") == release
            and prev.get("local_sha256") == local_hash and prev.get("remote_sha256")):
        res.update(remote_sha256=prev["remote_sha256"], skipped_download=True)
        return res, None
    data = _get(client, SOURCES["htsdata"]["url"]).content
    res["remote_sha256"] = content_sha256(data, "csv")
    res["remote_size"] = len(data)
    return res, data


def _probe_ustr_pdf(client, prev, local_hash, force):
    """400KB，直接下。文件名里的 Rev 号是 USITC 的 HTS 版本，不等于清单本身改了。"""
    r = _get(client, SOURCES["ustr_pdf"]["url"])
    data = r.content
    res = {
        "remote_version": _content_filename(r) or "",
        "remote_sha256": content_sha256(data, "pdf"),
        "remote_size": len(data),
        "remote_last_updated": _pdf_last_updated(data),
    }
    return res, data


def _probe_flip_frn(client, prev, local_hash, force):
    """HEAD 拿 ETag/Last-Modified；都没变且本地文件没动就不下 2.7MB。"""
    h = client.head(SOURCES["flip_frn"]["url"])
    h.raise_for_status()
    tag = h.headers.get("etag", "") or h.headers.get("last-modified", "")
    res = {"remote_version": tag, "remote_last_modified": h.headers.get("last-modified", "")}
    if (not force and tag and prev.get("remote_version") == tag
            and prev.get("local_sha256") == local_hash and prev.get("remote_sha256")):
        res.update(remote_sha256=prev["remote_sha256"], skipped_download=True)
        return res, None
    data = _get(client, SOURCES["flip_frn"]["url"]).content
    res["remote_sha256"] = content_sha256(data, "pdf")
    res["remote_size"] = len(data)
    return res, data


PROBES = {"htsdata": _probe_htsdata, "ustr_pdf": _probe_ustr_pdf, "flip_frn": _probe_flip_frn}


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def check(keys=None, apply=False, force=False, transport=None, state_path=STATE_PATH,
          backup_dir=BACKUP_DIR):
    """
    逐源探测，返回 {checked_at, updated: [key...], errors: [key...], sources: {key: {...}}}。
    apply=True 时把有更新的文件原地覆盖（旧文件先备份），并在结果里标 applied=True。
    一个源失败不影响其他源——三份文件三家服务器，谁挂了就报谁。
    """
    keys = keys or list(SOURCES)
    state = load_state(state_path)
    prev_sources = state.get("sources", {})
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = {"checked_at": now, "updated": [], "errors": [], "applied": [], "sources": {}}
    backup_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    with _client(transport) as client:
        for key in keys:
            info = SOURCES[key]
            prev = prev_sources.get(key, {})
            local_hash = local_sha256(key)
            entry = {"label": info["label"], "path": info["path"], "url": info["url"],
                     "local_sha256": local_hash, "local_exists": local_hash is not None}
            try:
                res, data = PROBES[key](client, prev, local_hash, force or apply)
                entry.update(res)
                entry["updated"] = bool(entry.get("remote_sha256")) and entry["remote_sha256"] != local_hash
                if entry["updated"] and key == "htsdata" and local_hash:
                    if data is not None:
                        with open(os.path.join(BASE_DIR, info["path"]), "rb") as f:
                            entry["diff"] = _csv_line_diff(f.read(), data)
                    elif prev.get("diff"):
                        entry["diff"] = prev["diff"]     # 没重下（版本没变）→ 沿用上次算好的差异
                if entry["updated"]:
                    result["updated"].append(key)
                    if apply and data is not None:
                        _apply(key, data, backup_dir, backup_stamp)
                        entry["applied"] = True
                        entry["local_sha256"] = entry["remote_sha256"]
                        result["applied"].append(key)
            except Exception as e:           # 网络/HTTP/解析都归为"检查失败"，不抛
                entry["error"] = f"{type(e).__name__}: {e}"
                result["errors"].append(key)
            if key in MANUAL_WATCH:
                entry["manual_watch"] = MANUAL_WATCH[key]
            result["sources"][key] = entry

    # 状态只记本次成功探测到的源；失败的源保留上次记录，下次还能继续省流量
    merged = dict(prev_sources)
    for key, entry in result["sources"].items():
        if "error" not in entry:
            merged[key] = dict(entry)
            merged[key]["checked_at"] = now
        else:
            merged.setdefault(key, {})["last_error"] = entry["error"]
            merged[key]["last_error_at"] = now
    save_state({"checked_at": now, "updated": result["updated"], "errors": result["errors"],
                "sources": merged}, state_path)
    return result


def _apply(key, data: bytes, backup_dir, stamp):
    """原地覆盖前先备份——这三个文件是所有判定的根，覆盖错了要能一步退回。"""
    info = SOURCES[key]
    dst = os.path.join(BASE_DIR, info["path"])
    if os.path.exists(dst):
        bdir = os.path.join(backup_dir, stamp)
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(dst, os.path.join(bdir, info["path"]))
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def rebuild(updated_keys):
    """FLIP FRN 变了要先重提豁免范围（build_db 摄入的是它的产物），再统一重建数据库。"""
    py = sys.executable
    steps = []
    if "flip_frn" in updated_keys:
        steps.append([py, os.path.join(BASE_DIR, "scripts", "extract_flip_scopes.py")])
    steps.append([py, os.path.join(BASE_DIR, "scripts", "build_db.py")])
    for cmd in steps:
        print(f"\n$ {' '.join(os.path.relpath(c, BASE_DIR) if c.startswith(BASE_DIR) else c for c in cmd)}")
        rc = subprocess.call(cmd, cwd=BASE_DIR)
        if rc != 0:
            print(f"!! 退出码 {rc}，中止后续步骤")
            return rc
    return 0


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------

def format_report(result) -> str:
    lines = [f"官方源文件更新检查  {result['checked_at']}", "=" * 60]
    for key, e in result["sources"].items():
        lines.append(f"\n[{e['label']}]  {e['path']}")
        if "error" in e:
            lines.append(f"  ✗ 检查失败：{e['error']}")
        else:
            ver = e.get("remote_version") or "-"
            extra = f"  (Last Updated {e['remote_last_updated']})" if e.get("remote_last_updated") else ""
            lines.append(f"  远端版本：{ver}{extra}")
            if not e.get("local_exists"):
                lines.append("  ⚠ 本地文件缺失")
            if e.get("updated"):
                mark = "已下载覆盖" if e.get("applied") else "有更新（未下载）"
                lines.append(f"  ⚠ 内容与本地不同 → {mark}")
                d = e.get("diff")
                if d:
                    lines.append(f"     行差异：+{d['added']} / -{d['removed']}")
                    if d["added_sample"]:
                        lines.append(f"     新增样例：{', '.join(d['added_sample'])}")
                    if d["removed_sample"]:
                        lines.append(f"     删除样例：{', '.join(d['removed_sample'])}")
            else:
                note = "（版本未变，未重新下载）" if e.get("skipped_download") else ""
                lines.append(f"  ✓ 与本地一致{note}")
        if e.get("manual_watch"):
            lines.append(f"  ℹ {e['manual_watch']}")
    lines.append("")
    if result["updated"]:
        lines.append(f"结论：{len(result['updated'])} 个源有更新：{', '.join(result['updated'])}")
        if not result["applied"]:
            lines.append("      下载覆盖并重建：python scripts/check_sources.py --apply --rebuild")
    elif result["errors"]:
        lines.append(f"结论：检查未完成，{len(result['errors'])} 个源失败：{', '.join(result['errors'])}")
    else:
        lines.append("结论：全部与官方一致")
    return "\n".join(lines)


def notify(title, message):
    """macOS 通知中心；非 macOS 或失败静默——通知只是锦上添花，日志才是记录。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{message}" with title "{title}"'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="三份官方源文件更新检测")
    ap.add_argument("--only", help="只检查这些源（逗号分隔）：" + ",".join(SOURCES))
    ap.add_argument("--apply", action="store_true", help="有更新则下载覆盖（旧文件备份到 output/sources_backup/）")
    ap.add_argument("--rebuild", action="store_true", help="--apply 覆盖后重跑 build_db.py")
    ap.add_argument("--force", action="store_true", help="忽略版本号缓存，强制重新下载比对")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--notify", action="store_true", help="有更新/出错时弹 macOS 通知")
    a = ap.parse_args(argv)

    keys = None
    if a.only:
        keys = [k.strip() for k in a.only.split(",") if k.strip()]
        bad = [k for k in keys if k not in SOURCES]
        if bad:
            ap.error(f"未知源：{', '.join(bad)}（可选：{', '.join(SOURCES)}）")

    result = check(keys, apply=a.apply, force=a.force)
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else format_report(result))

    if a.notify:
        if result["updated"]:
            labels = "、".join(result["sources"][k]["label"] for k in result["updated"])
            notify("HTS 官方数据有更新", labels + ("（已下载）" if result["applied"] else ""))
        elif result["errors"]:
            notify("HTS 源文件检查失败", "、".join(result["errors"]))

    if a.rebuild and result["applied"]:
        rc = rebuild(result["applied"])
        if rc != 0:
            return rc

    if result["updated"]:
        return EXIT_UPDATED
    return EXIT_ERROR if result["errors"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
