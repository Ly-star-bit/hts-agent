# -*- coding: utf-8 -*-
"""
cross_desc.py —— 裁定正文「商品描述段」的抓取与语义索引（先例通道的第二个向量）

【为什么】cross_embed 嵌的是 subject：CBP 写的一句话摘要，中位数只有 3 个词（"coated fabric"、
"chemical mixture"），对用户贴进来的整段描述天然吃亏。裁定正文里"你申请归类的货物是……"那一段
（申请人自己的描述，200–400 字）才是和用户输入同一口径的东西。2026-09 的 60 条评测里，
8 条错是正确答案根本不在候选池，先例通道是三条召回里最强的，把它的文档侧换成描述段是
提高召回上限最直接的一招。

【范围】2015 年以后、未撤销、带税号的 NY 裁定（约 2.8 万条）。正文逐条从 CROSS 拉
（cross.fetch_ruling_text，永久缓存 .cache/cross_docs/），每次网络请求之间停 0.4 秒——
CROSS 无公开文档、无 SLA，礼貌一点。抓取可中断可续跑：已入 ruling_desc 表的不再抓。

【存储】cross.db 内：
  ruling_desc(number, desc, fetched_at)      描述段（抽不到存空串，标记"已看过"）
  vec_desc  vec0(emb float[1024])            rowid 与 rulings.rowid 对齐（同 vec_subjects）
  desc_embeddings(number, model, dims, at)   增量依据
cross.semantic_precedents 同时查 vec_subjects 与 vec_desc，同一裁定取更近的那个距离。

用法：
    python scripts/cross_desc.py fetch [--limit N] [--since 2015-01-01]   # 抓描述段（可中断续跑）
    python scripts/cross_desc.py embed                                    # 增量嵌入
    python scripts/cross_desc.py status
"""
import argparse
import datetime as dt
import os
import re
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cross
import cross_embed
from eval_classify import extract_description   # 与正文金标同一套抽取规则，口径一致

DB_PATH = cross.DB_PATH
EMBED_MODEL = cross_embed.EMBED_MODEL
EMBED_DIMS = cross_embed.EMBED_DIMS
BATCH = 32
FETCH_DELAY = 0.4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ruling_desc (
  number     TEXT PRIMARY KEY,
  desc       TEXT NOT NULL,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS desc_embeddings (
  number      TEXT PRIMARY KEY,
  model       TEXT NOT NULL,
  dims        INTEGER NOT NULL,
  embedded_at TEXT
);
"""


def open_db(path=DB_PATH):
    conn = cross_embed.open_db(path)
    conn.executescript(_SCHEMA)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_desc USING vec0(emb float[{EMBED_DIMS}])")
    return conn


def candidates(conn, since="2015-01-01"):
    """待抓的裁定：NY、未撤销、带税号、还没进 ruling_desc，新的在前。"""
    return conn.execute("""
        SELECT r.rowid, r.number, r.date FROM rulings r
        LEFT JOIN ruling_desc d ON d.number = r.number
        WHERE d.number IS NULL AND lower(r.collection)='ny' AND r.date >= ?
          AND r.revoked_by = '[]' AND r.tariffs <> ''
        ORDER BY r.date DESC""", (since,)).fetchall()


def fetch(db_path=DB_PATH, limit=0, since="2015-01-01", delay=FETCH_DELAY, log=print, _fetch=None):
    """抓描述段。返回统计。_fetch 供测试注入。"""
    fetch_text = _fetch or (lambda num, date: cross.fetch_ruling_text(num, "ny", date))
    conn = open_db(db_path)
    try:
        todo = candidates(conn, since)
        if limit:
            todo = todo[:limit]
        total, got, empty, t0 = len(todo), 0, 0, time.time()
        log(f"待抓 {total} 条（{since} 起，NY、未撤销、带税号），每次网络请求间隔 {delay}s")
        for i, (_rowid, num, date) in enumerate(todo, 1):
            cached = os.path.exists(os.path.join(cross.DOC_CACHE_DIR, f"{num}.txt"))
            text = fetch_text(num, date)
            if text is None:          # 网络失败：不记录，下次续跑再试
                if not cached:
                    time.sleep(delay)
                continue
            desc = extract_description(text) if text else ""
            if len(desc) < 40:
                desc, empty = "", empty + 1
            else:
                got += 1
            with conn:
                conn.execute("INSERT OR REPLACE INTO ruling_desc VALUES (?,?,?)",
                             (num, desc, dt.datetime.now().isoformat(timespec="seconds")))
            if not cached:
                time.sleep(delay)
            if i % 200 == 0 or i == total:
                rate = i / max(time.time() - t0, 1)
                log(f"  {i}/{total} 抽到 {got} 抽不到 {empty}（{rate:.1f} 条/s，剩余约 {(total - i) / max(rate, .1) / 60:.0f} 分钟）")
        return {"处理": total, "抽到": got, "抽不到": empty}
    finally:
        conn.close()


def pending_embeds(conn):
    return conn.execute("""
        SELECT r.rowid, d.number, d.desc FROM ruling_desc d
        JOIN rulings r ON r.number = d.number
        LEFT JOIN desc_embeddings e ON e.number = d.number
        WHERE e.number IS NULL AND d.desc <> ''""").fetchall()


def embed(db_path=DB_PATH, rebuild=False, log=print, _embed=None):
    """增量嵌入描述段 → vec_desc。换模型必须 --rebuild。"""
    embed_batch = _embed or cross_embed.embed_batch
    conn = open_db(db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        prev = meta.get("desc_embed_model")
        if rebuild:
            conn.execute("DELETE FROM desc_embeddings")
            conn.execute("DELETE FROM vec_desc")
            conn.commit()
        elif prev and prev != EMBED_MODEL:
            raise SystemExit(f"描述索引已用 {prev} 构建，当前为 {EMBED_MODEL}，换模型必须 --rebuild")
        todo = pending_embeds(conn)
        total, done, t0 = len(todo), 0, time.time()
        log(f"待嵌入描述段 {total} 条（{EMBED_MODEL}，{EMBED_DIMS} 维）")
        now = dt.datetime.now().isoformat(timespec="seconds")
        for i in range(0, total, BATCH):
            chunk = todo[i:i + BATCH]
            vecs = embed_batch([d for _, _, d in chunk])
            with conn:
                for (rowid, number, _), v in zip(chunk, vecs):
                    tv = cross_embed._truncate_norm(v)
                    conn.execute("INSERT OR REPLACE INTO vec_desc(rowid, emb) VALUES (?,?)",
                                 (rowid, struct.pack(f"{EMBED_DIMS}f", *tv)))
                    conn.execute("INSERT OR REPLACE INTO desc_embeddings VALUES (?,?,?,?)",
                                 (number, EMBED_MODEL, EMBED_DIMS, now))
            done += len(chunk)
            if done % (BATCH * 20) == 0 or done == total:
                r = done / max(time.time() - t0, 1)
                log(f"  {done}/{total}（{r:.0f} 条/s，剩余约 {(total - done) / max(r, .1) / 60:.0f} 分钟）")
        n = conn.execute("SELECT COUNT(*) FROM desc_embeddings").fetchone()[0]
        for k, v in {"desc_embed_model": EMBED_MODEL, "desc_embed_dims": EMBED_DIMS,
                     "desc_embed_count": n, "desc_last_embed": now}.items():
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        conn.commit()
        return {"新嵌入": done, "索引总数": n}
    finally:
        conn.close()


def status(db_path=DB_PATH):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        has = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ruling_desc" not in has:
            return {"描述段": 0, "已嵌入": 0}
        n = conn.execute("SELECT COUNT(*) FROM ruling_desc").fetchone()[0]
        ne = conn.execute("SELECT COUNT(*) FROM ruling_desc WHERE desc<>''").fetchone()[0]
        emb = conn.execute("SELECT COUNT(*) FROM desc_embeddings").fetchone()[0] if "desc_embeddings" in has else 0
        return {"已看过": n, "描述段": ne, "已嵌入": emb}
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="裁定描述段抓取 / 嵌入")
    ap.add_argument("cmd", choices=["fetch", "embed", "status"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--since", default="2015-01-01")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        print(fetch(limit=a.limit, since=a.since))
    elif a.cmd == "embed":
        print(embed(rebuild=a.rebuild))
    else:
        print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------- 查询时按需：候选编码 → 先例描述段 ----------
#
# 全量抓取是后台的事；线上查询走这里：候选池里的编码各反查几条最近的未撤销裁定，
# 库里没有描述段的当场拉正文抽取、落库（可选立刻嵌入）。索引随真实使用自己长。
# 每次查询的网络请求数封顶（max_fetch），有缓存的不算。

def _plain_conn(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def descriptions_for(numbers, fetch_missing=True, max_fetch=12, delay=0.2, embed_new=True,
                     db_path=DB_PATH, _fetch=None, _embed=None):
    """
    {裁定号: 描述段}。库里没有的按需拉正文（cross.fetch_ruling_text，永久缓存）抽取并落库；
    抽不到的落空串（下次不再拉）；网络失败的不落库（下次再试）。embed_new：新落库的立刻嵌入 vec_desc。
    """
    numbers = [n for n in dict.fromkeys(str(x) for x in numbers if x)]
    if not numbers:
        return {}
    conn = _plain_conn(db_path)
    try:
        ph = ",".join("?" * len(numbers))
        have = dict(conn.execute(f"SELECT number, desc FROM ruling_desc WHERE number IN ({ph})", numbers).fetchall())
        missing = [n for n in numbers if n not in have]
        new_rows = []
        if fetch_missing and missing:
            meta = {r[0]: (str(r[1] or "ny").lower(), r[2] or "") for r in conn.execute(
                f"SELECT number, collection, date FROM rulings WHERE number IN ({','.join('?' * len(missing))})", missing)}
            fetch_text = _fetch or (lambda num, coll, date: cross.fetch_ruling_text(num, coll, date))
            fetched = 0
            for n in missing:
                if fetched >= max_fetch:
                    break
                coll, date = meta.get(n, ("ny", ""))
                cached = os.path.exists(os.path.join(cross.DOC_CACHE_DIR, f"{n}.txt"))
                text = fetch_text(n, coll, date)
                if not cached:
                    fetched += 1
                    time.sleep(delay)
                if text is None:
                    continue
                desc = extract_description(text) if text else ""
                if len(desc) < 40:
                    desc = ""
                with conn:
                    conn.execute("INSERT OR REPLACE INTO ruling_desc VALUES (?,?,?)",
                                 (n, desc, dt.datetime.now().isoformat(timespec="seconds")))
                have[n] = desc
                if desc:
                    new_rows.append((n, desc))
    finally:
        conn.close()
    if embed_new and new_rows:
        _embed_rows(new_rows, db_path=db_path, _embed=_embed)
    return {n: d for n, d in have.items() if d}


def _embed_rows(rows, db_path=DB_PATH, _embed=None):
    """把新落库的描述段立刻嵌进 vec_desc；ollama 不在、模型不一致等一律静默跳过（下次 embed 任务补）。"""
    try:
        conn = open_db(db_path)
    except Exception:
        return 0
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        prev = meta.get("desc_embed_model")
        if prev and prev != EMBED_MODEL:
            return 0
        embed_batch = _embed or cross_embed.embed_batch
        vecs = embed_batch([d for _, d in rows])
        now = dt.datetime.now().isoformat(timespec="seconds")
        n = 0
        with conn:
            for (number, _), v in zip(rows, vecs):
                rowid = conn.execute("SELECT rowid FROM rulings WHERE number=?", (number,)).fetchone()
                if not rowid:
                    continue
                tv = cross_embed._truncate_norm(v)
                conn.execute("INSERT OR REPLACE INTO vec_desc(rowid, emb) VALUES (?,?)",
                             (rowid[0], struct.pack(f"{EMBED_DIMS}f", *tv)))
                conn.execute("INSERT OR REPLACE INTO desc_embeddings VALUES (?,?,?,?)",
                             (number, EMBED_MODEL, EMBED_DIMS, now))
                n += 1
            cnt = conn.execute("SELECT COUNT(*) FROM desc_embeddings").fetchone()[0]
            for k, v in {"desc_embed_model": EMBED_MODEL, "desc_embed_dims": EMBED_DIMS, "desc_embed_count": cnt}.items():
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        return n
    except Exception:
        return 0
    finally:
        conn.close()


def precedent_examples(codes, per_code=2, exclude=None, max_fetch=12, db_path=DB_PATH,
                       fetch_missing=True, _fetch=None, _embed=None):
    """
    {code8: [{"裁定号","日期","描述","主题"}, …]}：每个编码最近的未撤销裁定及其描述段
    （"海关实际把什么货判到了这个码"）。exclude：留一法剔除的裁定号。库里缺描述段的按需拉，
    整次调用网络请求 ≤ max_fetch；描述段拉不到的条目给主题兜底。
    """
    codes = [re.sub(r"\D", "", str(c))[:8] for c in codes]
    codes = [c for c in dict.fromkeys(codes) if len(c) == 8]
    if not codes:
        return {}
    conn = _plain_conn(db_path)
    try:
        cand = {}
        for c8 in codes:
            rows = conn.execute(
                "SELECT r.number, r.date, r.subject FROM ruling_codes c JOIN rulings r ON r.number = c.number "
                "WHERE c.code LIKE ? AND r.revoked_by='[]' AND r.number <> ? "
                "ORDER BY r.date DESC LIMIT ?", (c8 + "%", exclude or "", per_code * 3)).fetchall()
            cand[c8] = rows
    finally:
        conn.close()
    numbers = [n for rows in cand.values() for n, _, _ in rows[:per_code * 2]]
    descs = descriptions_for(numbers, fetch_missing=fetch_missing, max_fetch=max_fetch, db_path=db_path,
                             _fetch=_fetch, _embed=_embed)
    out = {}
    for c8, rows in cand.items():
        picked = [{"裁定号": n, "日期": d, "描述": descs.get(n, ""), "主题": s} for n, d, s in rows if descs.get(n)]
        if len(picked) < per_code:   # 描述段不够的用主题兜底，但排在有描述段的后面
            picked += [{"裁定号": n, "日期": d, "描述": "", "主题": s} for n, d, s in rows if not descs.get(n)]
        out[c8] = picked[:per_code]
    return out
