# -*- coding: utf-8 -*-
"""
hts_embed.py —— 税则行语义索引（归类召回的第二条通道）

把 1–97 章全部 8 位子目的**完整品名**（归类路径 + 自身品名）嵌入成向量，
存进 data/hts_vec.db 的 sqlite-vec 虚拟表。之后中英文商品描述可以直接按语义
找税则行，不再依赖"品名里恰好出现了哪个英文词"。

【为什么要有这一条】关键词检索是精确匹配：'bluetooth speaker' 在税则里
一个词都不出现（官方写法是 loudspeakers），'电热水壶' 词表没收录就是 0 条。
2026-09 实测 26 组常见商品，关键词检索约 5 组命中，本索引（qwen3-embedding:8b）
前五含正确品目 18 组；与裁定库 kNN（cross.semantic_precedents）并集 21 组。
两条通道都只做**召回**：候选仍进现有精排，税率与判定条件仍来自本地税则。

【与 cross_embed 的关系】同一个嵌入模型、同一套 MRL 截断与查询前缀，
只是文档侧换成税则行。模型/维度记在 meta 里，查询时必须用同一个模型。

【增量】按 (编码, 品名哈希) 记录，税则重建后只重嵌品名变过的行；
消失的编码从索引删除。换模型必须 --rebuild。

用法：
    python scripts/hts_embed.py            # 增量（首次即全量，8b 约 18 分钟）
    python scripts/hts_embed.py --rebuild  # 换模型 / 换嵌入口径后清空重建
    python scripts/hts_embed.py --status   # 只看索引状态
"""
import argparse
import datetime as dt
import hashlib
import os
import re
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "hts_vec.db")
EMBED_MODEL = "qwen3-embedding:8b"
EMBED_DIMS = 1024
BATCH = 32
# 查询侧前缀（文档侧存纯文本）。2026-09 探针：8b 带/不带前缀都是 18/22，
# 带前缀与 cross_embed 口径一致，统一用它。
QUERY_INSTRUCT = ("Instruct: Given a product description, retrieve the matching "
                  "US HTS tariff line\nQuery: ")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lines (
  id          INTEGER PRIMARY KEY,   -- vec 表的 rowid
  code8       TEXT UNIQUE NOT NULL,
  text_hash   TEXT NOT NULL,
  text        TEXT NOT NULL,
  embedded_at TEXT
);
"""


def _load_vec(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def open_db(path=DB_PATH, dims=EMBED_DIMS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    _load_vec(conn)
    conn.executescript(_SCHEMA)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_hts USING vec0(emb float[{dims}])")
    return conn


def db_available(path=DB_PATH):
    return os.path.exists(path)


def _truncate_norm(vec, dims=EMBED_DIMS):
    v = vec[:dims]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def embed_batch(texts, model=EMBED_MODEL, timeout=600):
    """调 ollama 嵌入一批文本；失败抛异常。单独成函数：测试用假嵌入器整体替换。"""
    import httpx
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    r = httpx.post(f"{url}/api/embed", json={"model": model, "input": texts}, timeout=timeout)
    r.raise_for_status()
    return r.json()["embeddings"]


def line_texts(db, expansion=None):
    """
    编码 → 嵌入文本（完整品名，可选拼上该编码下裁定的 subject）。98/99 章不进索引。

    expansion：{code8: [subject, …]}，来自 build_expansion()。官方品名是法律用语
    （"Other garments, of the type described in…"），用户描述是产品用语（"women's raincoat"），
    把海关实际判到该行的货物名拼进去，语义通道才会说"产品话"。每行最多 8 条、600 字。
    """
    import rate
    out = {}
    for code in db.get("rates_8") or {}:
        if code[:2] in ("98", "99"):
            continue
        text = re.sub(r"\s+", " ", rate.full_desc(db, code)).strip()
        if not text:
            continue
        subs = (expansion or {}).get(code) or []
        if subs:
            tail = "; ".join(subs[:EXPAND_PER_LINE])[:EXPAND_MAX_CHARS]
            text = f"{text} | 海关判到此行的货物：{tail}"
        out[code] = text
    return out


EXPAND_PER_LINE = 8
EXPAND_MAX_CHARS = 600
_SUBJ_PREFIX = re.compile(r"^\s*(?:re:\s*)?the\s+(?:tariff\s+)?classification\s+of\s+(?:an?\s+|the\s+)?", re.I)
_SUBJ_FROM = re.compile(r"\s+(?:from|manufactured in|made in|produced in)\s+[A-Z][A-Za-z .,'()-]*$")


def eval_ruling_numbers(base_dir=BASE_DIR):
    """评测金标里的裁定号：文档扩展必须排除它们，否则语义通道的评测数字是假的（自己找自己）。"""
    import glob
    import json
    nums = set()
    for fp in glob.glob(os.path.join(base_dir, "data", "eval", "*.json")):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            for it in (d.get("items") if isinstance(d, dict) else d) or []:
                if isinstance(it, dict) and it.get("裁定号"):
                    nums.add(str(it["裁定号"]))
        except (OSError, ValueError):
            continue
    return nums


def build_expansion(db, cross_db_path=None, exclude=None, per_line=EXPAND_PER_LINE, log=print):
    """
    从裁定库反查：每个现行 8 位编码 → 最近判到它的、未撤销裁定的 subject（去套话、去产地）。
    exclude：要排除的裁定号（默认 = 评测金标里的全部裁定号）。裁定库不可用返回 {}。
    """
    import cross
    path = cross_db_path or cross.DB_PATH
    if not os.path.exists(path):
        log("裁定库不存在，文档扩展跳过")
        return {}
    exclude = set(exclude) if exclude is not None else eval_ruling_numbers()
    alive = {c for c in (db.get("rates_8") or {}) if c[:2] not in ("98", "99")}
    out, seen = {}, {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT number, date, subject, tariffs FROM rulings "
            "WHERE revoked_by='[]' AND tariffs<>'' AND subject<>'' ORDER BY date DESC").fetchall()
    finally:
        conn.close()
    n_used = 0
    for number, _date, subject, tariffs in rows:
        if number in exclude:
            continue
        s = _SUBJ_FROM.sub("", _SUBJ_PREFIX.sub("", subject or "")).strip().rstrip(".").strip()
        if len(s) < 4:
            continue
        codes = {re.sub(r"\D", "", c)[:8] for c in re.split(r"[,;\s]+", tariffs) if c.strip()}
        used = False
        for c8 in codes:
            if len(c8) < 8 or c8 not in alive:
                continue
            lst = out.setdefault(c8, [])
            key = s.lower()
            if len(lst) >= per_line or key in seen.setdefault(c8, set()):
                continue
            seen[c8].add(key)
            lst.append(s)
            used = True
        n_used += used
    log(f"文档扩展：{len(out)} 个编码拼上了裁定 subject（用到 {n_used} 条裁定，排除 {len(exclude)} 条评测裁定）")
    return out


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sync(db, db_path=DB_PATH, rebuild=False, log=print, _embed=None, model=EMBED_MODEL,
         dims=EMBED_DIMS, expand=False, expansion=None):
    """
    增量嵌入。返回统计 dict。db 为 core.load_db() 的结果。
    expand=True 时嵌入文本拼上裁定 subject（build_expansion），文本哈希变化会自动触发重嵌；
    meta.expansion 记录当前索引是否带扩展，status() 里能看到。
    """
    embed = _embed or (lambda texts: embed_batch(texts, model=model))
    if expand and expansion is None:
        expansion = build_expansion(db, log=log)
    conn = open_db(db_path, dims)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        prev_model = meta.get("embed_model")
        if rebuild:
            conn.execute("DELETE FROM lines")
            conn.execute("DELETE FROM vec_hts")
            conn.commit()
        elif prev_model and prev_model != model:
            raise SystemExit(f"索引已用 {prev_model} 构建，当前为 {model}。换模型必须 --rebuild。")

        texts = line_texts(db, expansion if expand else None)
        have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT code8, text_hash, id FROM lines")}
        # 消失的编码：删索引，不留幽灵行
        gone = [c for c in have if c not in texts]
        with conn:
            for c in gone:
                conn.execute("DELETE FROM vec_hts WHERE rowid=?", (have[c][1],))
                conn.execute("DELETE FROM lines WHERE code8=?", (c,))
        todo = [(c, t) for c, t in texts.items()
                if c not in have or have[c][0] != _hash(t)]
        total, done, t0 = len(todo), 0, time.time()
        log(f"税则行 {len(texts)} 条，待嵌入 {total} 条，删除 {len(gone)} 条"
            f"（模型 {model}，{dims} 维）")
        now = dt.datetime.now().isoformat(timespec="seconds")
        for i in range(0, total, BATCH):
            chunk = todo[i:i + BATCH]
            vecs = embed([t for _, t in chunk])
            with conn:  # 每批一个事务：中断后重跑自动从断点继续
                for (code, text), v in zip(chunk, vecs):
                    tv = _truncate_norm(v, dims)
                    conn.execute(
                        "INSERT INTO lines(code8, text_hash, text, embedded_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(code8) DO UPDATE SET text_hash=excluded.text_hash, "
                        "text=excluded.text, embedded_at=excluded.embedded_at",
                        (code, _hash(text), text, now))
                    rowid = conn.execute("SELECT id FROM lines WHERE code8=?", (code,)).fetchone()[0]
                    conn.execute("DELETE FROM vec_hts WHERE rowid=?", (rowid,))
                    conn.execute("INSERT INTO vec_hts(rowid, emb) VALUES (?,?)",
                                 (rowid, struct.pack(f"{dims}f", *tv)))
            done += len(chunk)
            if done % (BATCH * 20) == 0 or done == total:
                speed = done / max(time.time() - t0, 1)
                eta = (total - done) / max(speed, 0.1)
                log(f"  {done}/{total}（{speed:.0f} 条/s，剩余约 {eta/60:.0f} 分钟）")
        n = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        for k, v in {"embed_model": model, "embed_dims": dims, "embed_count": n,
                     "last_embed": now, "expansion": "1" if expand else "0",
                     "db_built_at": (db.get("meta") or {}).get("built_at", "")}.items():
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        conn.commit()
        log(f"完成：索引 {n} 条，耗时 {(time.time()-t0)/60:.1f} 分钟")
        return {"新嵌入": done, "删除": len(gone), "索引总数": n}
    finally:
        conn.close()


def status(db_path=DB_PATH):
    """索引状态：未建 → {"built": False}；已建 → 模型/维度/条数/时间。"""
    if not db_available(db_path):
        return {"built": False, "message": "税则行语义索引未构建，请运行 python scripts/hts_embed.py"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
    except Exception as e:
        return {"built": False, "message": f"索引不可读：{e}"}
    n = int(meta.get("embed_count") or 0)
    if not n:
        return {"built": False, "message": "税则行语义索引为空，请运行 python scripts/hts_embed.py"}
    return {"built": True, "model": meta.get("embed_model"), "dims": int(meta.get("embed_dims") or 0),
            "count": n, "last_embed": meta.get("last_embed", ""), "expansion": meta.get("expansion") == "1"}


def search_codes(query, limit=20, db_path=DB_PATH, _embed=None):
    """
    语义找税则行：描述 → [{"编码": norm8, "相似度": 0~1, "完整品名": text}]，相似度降序。

    失败一律 {"error": ...}（索引未建 / ollama 离线 / sqlite-vec 缺失）——
    这是召回通道之一，不可用时调用方退回关键词检索，主链路不受影响。
    """
    query = (query or "").strip()
    if not query:
        return {"error": "请输入商品描述"}
    st = status(db_path)
    if not st.get("built"):
        return {"error": st.get("message", "语义索引不可用")}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        _load_vec(conn)
    except Exception as e:
        return {"error": f"语义索引不可用（sqlite-vec）：{e}"}
    try:
        model, dims = st["model"], st["dims"]
        embed = _embed or (lambda texts: embed_batch(texts, model=model))
        qv = _truncate_norm(embed([QUERY_INSTRUCT + query])[0], dims)
        rows = conn.execute(
            "SELECT l.code8, l.text, v.distance FROM ("
            "  SELECT rowid, distance FROM vec_hts WHERE emb MATCH ? ORDER BY distance LIMIT ?) v "
            "JOIN lines l ON l.id = v.rowid ORDER BY v.distance",
            (struct.pack(f"{dims}f", *qv), int(limit))).fetchall()
    except Exception as e:
        return {"error": f"语义检索失败：{e}"}
    finally:
        conn.close()
    # sqlite-vec 默认 L2 距离；向量已归一化，余弦相似度 = 1 - d²/2
    return [{"编码": c, "完整品名": t, "相似度": round(max(0.0, 1.0 - (d * d) / 2.0), 4)}
            for c, t, d in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description="税则行语义索引（归类召回第二通道）")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--rebuild", action="store_true", help="清空重建（换嵌入模型后必须）")
    ap.add_argument("--status", action="store_true", help="只看索引状态")
    ap.add_argument("--expand", action="store_true",
                    help="嵌入文本拼上裁定库里判到该行的货物名（文档扩展；评测金标裁定自动排除）。"
                         "文本变化会触发全量重嵌（8b 约 18 分钟）")
    a = ap.parse_args(argv)
    if a.status:
        print(status(a.db))
        return 0
    import core
    db = core.load_db()
    sync(db, db_path=a.db, rebuild=a.rebuild, expand=a.expand)
    return 0


if __name__ == "__main__":
    sys.exit(main())
