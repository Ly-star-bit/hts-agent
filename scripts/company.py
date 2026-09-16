# -*- coding: utf-8 -*-
"""
company.py —— 本公司先例库（审核员每次采纳 / 改正都存一条，让工具越用越准）

【为什么】CROSS 裁定金标衡量的是"陌生商品第一次归"。日常流量多是报过的品类，而工具每天从零开始。
审核员把 6210.30.30 改成 .50，这条品名 + 描述 + 结论应该存下来：下次类似品名进来，先例通道
高权重召回它，结果里明说"上次你们这么报的"；完全相同的品名直接给结论，不再花模型调用。
这也把人工复核变成了积累——每一次改正都是一条训练数据。

【存储】data/company.db（不入 git：含公司品名）：
  precedents(id, name, name_norm, description, code8, origin, action 采纳|改正, who, note, created_at)
  vec_company vec0(emb float[1024])  rowid = id，品名（+描述）的向量，与税则行 / 裁定同一嵌入模型
  没有 sqlite-vec 或 ollama 时退化为精确 / 子串匹配，照样能用。

【检索】company.search(text)：精确品名 → 相似度 1.0；子串 → 0.85；向量近邻 → 余弦相似度（≥ 0.6）。
rate.hybrid_search 把它当第四条通道（权重 4，最高），行上带「公司先例」字段。
"""
import datetime as dt
import os
import re
import sqlite3
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "company.db")
EMBED_DIMS = 1024
SIM_MIN = 0.6
ACTIONS = ("采纳", "改正")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS precedents (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  name_norm   TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  code8       TEXT NOT NULL,
  origin      TEXT NOT NULL DEFAULT '',
  action      TEXT NOT NULL DEFAULT '采纳',
  who         TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prec_norm ON precedents(name_norm);
CREATE INDEX IF NOT EXISTS idx_prec_code ON precedents(code8);
"""


def norm_name(s):
    """品名归一：小写、去空白与标点，中英文都适用。"""
    return re.sub(r"[\s\W_]+", "", str(s or "").lower())


def _connect(db_path=None, vec=True):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(_SCHEMA)
    has_vec = False
    if vec:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_company USING vec0(emb float[{EMBED_DIMS}])")
            has_vec = True
        except Exception:
            has_vec = False
    return conn, has_vec


def _embed(texts):
    """与裁定 / 税则行同一嵌入模型；不可用时抛异常，调用方降级。"""
    import cross_embed
    return [cross_embed._truncate_norm(v) for v in cross_embed.embed_batch(texts)]


def count(db_path=None):
    if not os.path.exists(db_path or DB_PATH):
        return 0
    conn, _ = _connect(db_path, vec=False)
    try:
        return conn.execute("SELECT COUNT(*) FROM precedents").fetchone()[0]
    finally:
        conn.close()


def record(name, code, description="", origin="", action="采纳", who="", note="", db_path=None, _embed=None):
    """
    存一条先例。code 归一到 8 位数字；同一品名（归一后）再次记录时覆盖旧结论（最新的申报口径为准），
    返回 {"id", "更新": bool}。向量嵌入失败不影响入库（下次 search 时按子串匹配也能找到）。
    """
    name = str(name or "").strip()
    code8 = re.sub(r"\D", "", str(code or ""))[:8]
    if not name or len(code8) != 8:
        raise ValueError("需要品名与 8 位编码")
    action = action if action in ACTIONS else "采纳"
    conn, has_vec = _connect(db_path)
    try:
        now = dt.datetime.now().isoformat(timespec="seconds")
        nn = norm_name(name)
        old = conn.execute("SELECT id FROM precedents WHERE name_norm=?", (nn,)).fetchone()
        with conn:
            if old:
                pid = old[0]
                conn.execute("UPDATE precedents SET name=?, description=?, code8=?, origin=?, action=?, who=?, note=?, created_at=? WHERE id=?",
                             (name, description or "", code8, origin or "", action, who or "", note or "", now, pid))
            else:
                cur = conn.execute("INSERT INTO precedents(name, name_norm, description, code8, origin, action, who, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (name, nn, description or "", code8, origin or "", action, who or "", note or "", now))
                pid = cur.lastrowid
        if has_vec:
            try:
                vec = (_embed or _embed_default)([f"{name}。{description}".strip("。")])[0]
                with conn:
                    conn.execute("DELETE FROM vec_company WHERE rowid=?", (pid,))
                    conn.execute("INSERT INTO vec_company(rowid, emb) VALUES (?,?)", (pid, struct.pack(f"{EMBED_DIMS}f", *vec)))
            except Exception:
                pass
        return {"id": pid, "更新": bool(old)}
    finally:
        conn.close()


def _embed_default(texts):
    return _embed(texts)


def _row(r):
    return {"id": r[0], "品名": r[1], "描述": r[2], "编码": f"{r[3][:4]}.{r[3][4:6]}.{r[3][6:8]}", "code8": r[3],
            "原产地": r[4], "来源": r[5], "谁": r[6], "说明": r[7], "时间": r[8]}


_COLS = "id, name, description, code8, origin, action, who, note, created_at"


def search(text, limit=10, db_path=None, _embed=None):
    """
    品名 / 描述 → 本公司先例，按相似度降序：精确品名 1.0 > 子串 0.85 > 向量近邻（≥ SIM_MIN）。
    库不存在或为空返回 []；出错返回 {"error"}。
    """
    text = str(text or "").strip()
    path = db_path or DB_PATH
    if not text or not os.path.exists(path):
        return []
    try:
        conn, has_vec = _connect(path)
    except Exception as e:
        return {"error": f"公司先例库不可用：{e}"}
    try:
        if conn.execute("SELECT COUNT(*) FROM precedents").fetchone()[0] == 0:
            return []
        nn = norm_name(text)
        out, seen = [], set()
        for r in conn.execute(f"SELECT {_COLS} FROM precedents WHERE name_norm=? ORDER BY created_at DESC", (nn,)):
            d = _row(r); d["相似度"] = 1.0; out.append(d); seen.add(d["id"])
        if len(nn) >= 2:   # 中文品名两个字就有意义（"雨衣"）
            for r in conn.execute(f"SELECT {_COLS} FROM precedents WHERE (instr(?, name_norm) > 0 OR instr(name_norm, ?) > 0) "
                                  f"AND name_norm <> '' ORDER BY created_at DESC LIMIT ?", (nn, nn, limit * 2)):
                d = _row(r)
                if d["id"] not in seen:
                    d["相似度"] = 0.85; out.append(d); seen.add(d["id"])
        if has_vec and len(out) < limit:
            try:
                qv = (_embed or _embed_default)([text])[0]
                rows = conn.execute("SELECT rowid, distance FROM vec_company WHERE emb MATCH ? ORDER BY distance LIMIT ?",
                                    (struct.pack(f"{EMBED_DIMS}f", *qv), limit * 2)).fetchall()
                for rowid, dist in rows:
                    sim = round(max(0.0, 1.0 - (dist * dist) / 2.0), 4)
                    if sim < SIM_MIN or rowid in seen:
                        continue
                    r = conn.execute(f"SELECT {_COLS} FROM precedents WHERE id=?", (rowid,)).fetchone()
                    if r:
                        d = _row(r); d["相似度"] = sim; out.append(d); seen.add(rowid)
            except Exception:
                pass   # ollama 离线：只剩精确 / 子串
        out.sort(key=lambda d: (-d["相似度"], d["时间"]), reverse=False)
        return out[:limit]
    finally:
        conn.close()


def list_recent(limit=50, db_path=None):
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return []
    conn, _ = _connect(path, vec=False)
    try:
        return [_row(r) for r in conn.execute(f"SELECT {_COLS} FROM precedents ORDER BY created_at DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def delete(pid, db_path=None):
    conn, has_vec = _connect(db_path)
    try:
        with conn:
            n = conn.execute("DELETE FROM precedents WHERE id=?", (pid,)).rowcount
            if has_vec:
                conn.execute("DELETE FROM vec_company WHERE rowid=?", (pid,))
        return n
    finally:
        conn.close()
