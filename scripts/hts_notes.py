# -*- coding: utf-8 -*-
"""
hts_notes.py —— HTS 类注 / 章注 / 附加美国注释（归类的法律依据层）

【为什么要有它】本地税则库只有品名文本，决定归类的类注、章注、附加美国注释都不在其中
——README 一直把这条写成"结构性边界"。2026-09 在 60 条正文金标上的实测：云端模型平铺
精排错的 25 条里 23 条是 6 位就错（方向错），而"先读注释再定品目"的逐级链在这些错题上
救回 7/21、对照组 15 条只做坏 1 条。注释是这条链的数据基础。

【数据来源】USITC 各章 PDF（https://hts.usitc.gov/reststop/file?release=currentRelease&filename=Chapter+NN）
前几页是注释，表格页从含 "Heading/ Stat." 的页开始。类注印在该类首章的 PDF 开头
（第 XI 类在第 50 章、第 XVI 类在第 84 章）。PDF 缓存在 .cache/hts_chapters/（不入 git），
解析结果落 data/hts_notes.json（入 git：文本可读、只随 HTS 修订变化）。

【与税则版本】USITC 当前版本可能比 htsdata.csv 新一两个 Revision；年内注释基本不变，
meta 里记录抓取时的 release，README 建议每次 HTS 修订后重跑。

用法：
    python scripts/hts_notes.py            # 抓取并解析 1–97 章（已缓存的 PDF 不重下）
    python scripts/hts_notes.py --refresh  # 重新下载全部 PDF
    python scripts/hts_notes.py --status   # 只看现有 JSON 状态
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_FILE = os.path.join(BASE_DIR, "data", "hts_notes.json")
PDF_DIR = os.path.join(BASE_DIR, ".cache", "hts_chapters")
URL = "https://hts.usitc.gov/reststop/file?release=currentRelease&filename=Chapter+{n}"
RELEASE_URL = "https://hts.usitc.gov/reststop/currentRelease"

# 类 → 首章（类注印在该章 PDF 开头）。第 77 章为保留章，无 PDF。
SECTION_FIRST = {1: "I", 6: "II", 15: "III", 16: "IV", 25: "V", 28: "VI", 39: "VII", 41: "VIII",
                 44: "IX", 47: "X", 50: "XI", 64: "XII", 68: "XIII", 71: "XIV", 72: "XV",
                 84: "XVI", 86: "XVII", 90: "XVIII", 93: "XIX", 94: "XX", 97: "XXI"}
RESERVED = {77}
# 每页重复的页眉 / 页码行
_HDR = re.compile(r"^(Harmonized Tariff Schedule of the United States.*|Annotated for Statistical Reporting Purposes"
                  r"|[IVX]+\s*\|?\s*\d{1,2}-\d+|\d{1,2}-\d+\s*[IVX]*)\s*$")
_TABLE = re.compile(r"Heading/\s*Stat\.|Subheading\s+Suf-")

_CACHE = {"path": None, "mtime": None, "data": None}


def section_first_chapter(ch):
    return max(c for c in SECTION_FIRST if c <= ch)


def section_id(ch):
    return SECTION_FIRST[section_first_chapter(ch)]


# ---------- 抓取与解析 ----------

def _download(ch, refresh=False):
    os.makedirs(PDF_DIR, exist_ok=True)
    p = os.path.join(PDF_DIR, f"ch{ch:02d}.pdf")
    if refresh or not os.path.exists(p) or os.path.getsize(p) < 10000:
        import httpx
        r = httpx.get(URL.format(n=ch), timeout=180, follow_redirects=True)
        r.raise_for_status()
        with open(p, "wb") as f:
            f.write(r.content)
    return p


def preamble_from_pdf(path):
    """表格之前的全部文字（类注 + 章注 + 附加美国注释 + 统计注释），去掉页眉页码。"""
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if _TABLE.search(t):
                break
            out.append("\n".join(ln for ln in t.splitlines() if not _HDR.match(ln.strip())))
    return "\n".join(out).strip()


def split_preamble(text, ch):
    """
    → {"section_title", "section_notes", "chapter_title", "chapter_notes", "us_notes"}
    类首章的 PDF 开头是 "SECTION XI ... Notes ..."，随后 "CHAPTER 50 ..."；非首章直接从 CHAPTER 起。
    附加美国注释（Additional U.S. Notes）与统计注释一起归入 us_notes：8 位档的定义多在这里。
    """
    m = re.search(rf"^\s*CHAPTER\s+{ch}\b", text, re.M)
    section, rest = ("", text) if not m else (text[:m.start()].strip(), text[m.start():])
    mu = re.search(r"^\s*Additional U\.S\. Notes?", rest, re.M)
    chapter, us = (rest, "") if not mu else (rest[:mu.start()].strip(), rest[mu.start():].strip())

    def _title(block, head_re):
        mm = re.search(head_re, block, re.M)
        if not mm:
            return ""
        body = block[mm.end():]
        mn = re.search(r"^\s*(Notes?|Subheading Notes?|Additional U\.S\. Notes?)\s*$", body, re.M)
        lines = (body[:mn.start()] if mn else body[:200]).splitlines()
        # 标题行之后常跟一行孤立的类号（"XVI"），那是页眉残留，不是标题
        lines = [ln for ln in lines if ln.strip() and not re.fullmatch(r"[IVX]+", ln.strip())]
        return re.sub(r"\s+", " ", " ".join(lines)).strip(" |")

    return {
        "section_title": _title(section, r"^\s*SECTION\s+[IVX]+\s*$") if section else "",
        "section_notes": section,
        "chapter_title": _title(rest, rf"^\s*CHAPTER\s+{ch}\s*$"),
        "chapter_notes": chapter,
        "us_notes": us,
    }


def build(refresh=False, log=print, chapters=None):
    """抓取解析全部章 → data/hts_notes.json。返回 (data, failures)。"""
    release = ""
    try:
        import httpx
        release = (httpx.get(RELEASE_URL, timeout=30).json() or {}).get("name", "")
    except Exception:
        pass
    data = {"meta": {"built_at": dt.datetime.now().isoformat(timespec="seconds"), "release": release,
                     "source": URL.replace("{n}", "NN")},
            "sections": {}, "chapters": {}}
    failures = []
    for ch in (chapters or range(1, 98)):
        if ch in RESERVED:
            continue
        try:
            parts = split_preamble(preamble_from_pdf(_download(ch, refresh)), ch)
        except Exception as e:  # 单章失败不拖垮整体，最后点名
            failures.append((ch, str(e)[:120]))
            log(f"  第 {ch} 章失败：{e}")
            continue
        sid = section_id(ch)
        if section_first_chapter(ch) == ch:
            data["sections"][sid] = {"title": parts["section_title"], "notes": parts["section_notes"],
                                     "first_chapter": ch}
        data["chapters"][str(ch)] = {"section": sid, "title": parts["chapter_title"],
                                     "notes": parts["chapter_notes"], "us_notes": parts["us_notes"]}
        if ch % 10 == 0:
            log(f"  已解析到第 {ch} 章")
    data["meta"]["chapters"] = len(data["chapters"])
    data["meta"]["failed"] = [c for c, _ in failures]
    os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
    tmp = NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, NOTES_FILE)
    return data, failures


# ---------- 查询接口（Web / 归类链共用） ----------

def load_notes(path=None):
    """读取 data/hts_notes.json（按 mtime 缓存）；不存在返回 None。"""
    path = path or NOTES_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _CACHE.update({"path": path, "mtime": mtime, "data": data})
    return data


def available(path=None):
    return load_notes(path) is not None


def notes_for(ch, notes=None):
    """
    某章的注释：{"section_id","section","chapter","us","title"}。
    notes 可注入（测试用）；没有数据时各段为空串，调用方按"注释缺席"处理而不是报错。
    """
    ch = int(ch)
    data = notes if notes is not None else load_notes()
    empty = {"section_id": SECTION_FIRST.get(section_first_chapter(ch), ""), "section": "", "chapter": "",
             "us": "", "title": ""}
    if not data:
        return empty
    c = (data.get("chapters") or {}).get(str(ch)) or {}
    s = (data.get("sections") or {}).get(c.get("section") or empty["section_id"]) or {}
    return {"section_id": c.get("section") or empty["section_id"], "section": s.get("notes", ""),
            "chapter": c.get("notes", ""), "us": c.get("us_notes", ""), "title": c.get("title", "")}


def status(path=None):
    data = load_notes(path)
    if not data:
        return {"available": False, "file": path or NOTES_FILE}
    m = data.get("meta") or {}
    return {"available": True, "file": path or NOTES_FILE, "release": m.get("release", ""),
            "built_at": m.get("built_at", ""), "chapters": len(data.get("chapters") or {}),
            "sections": len(data.get("sections") or {}), "failed": m.get("failed", [])}


def main(argv=None):
    ap = argparse.ArgumentParser(description="HTS 类注/章注抓取与解析")
    ap.add_argument("--refresh", action="store_true", help="重新下载全部章 PDF")
    ap.add_argument("--status", action="store_true", help="只看现有 JSON 状态")
    ap.add_argument("--chapters", default="", help="只处理这些章（逗号分隔，调试用）")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        return 0
    chs = [int(x) for x in a.chapters.split(",") if x.strip()] or None
    print("抓取并解析 HTS 各章注释（PDF 缓存在 .cache/hts_chapters/）…")
    data, failures = build(refresh=a.refresh, chapters=chs)
    print(f"完成：{data['meta']['chapters']} 章，{len(data['sections'])} 类，release {data['meta']['release'] or '未知'}"
          f" → {NOTES_FILE}" + (f"；失败 {len(failures)} 章：{[c for c, _ in failures]}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
