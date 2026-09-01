# -*- coding: utf-8 -*-
"""
cross_embed.py —— 裁定 subject 语义索引（二期）

把 cross.db 里全部裁定的 subject 嵌入成向量，存进同库的 sqlite-vec 虚拟表。
之后中文商品描述可以直接语义找先例（"羊毛大衣" → wool coat 裁定），
不再依赖先把描述翻成英文关键词。

【选型依据（2026-09 实测，1411 条真实裁定 + 7 组中英查询）】
  - qwen3-embedding:8b + instruct 前缀：P@5 0.86；0.6b 为 0.77，且小模型对
    prompt 敏感（"羊毛大衣"加前缀反而从 0.6 崩到 0.2）
  - MRL 截断：2048 维与满维 4096 持平，1024 维差距在采样噪声内 → 存 1024 维
    （0.9GB vs 3.6GB），221k 规模 top-50 检索实测 36ms
  - instruct 前缀只加在**查询侧**，文档侧存纯文本——与基准测试的做法一致

【嵌入的是 subject 不是全文】
  subject 是 CBP 写的一句话摘要（"The tariff classification of a lithium-ion
  battery from China"），信息密度高且全库都有。全文是三期的事。

【增量与重建】
  embeddings 表记录每条裁定用什么模型/维度嵌入过。每晚 cross_sync 之后跑本
  脚本，只嵌新增裁定（日常几十条，秒级）。meta 里的 embed_model/embed_dims
  变了（换模型）→ 必须 --rebuild 全量重来，混两种模型的向量空间是纯粹的错误，
  脚本检测到不一致会拒绝增量并明说。

用法：
    python scripts/cross_embed.py              # 增量（首次即全量，约 4.5 小时）
    python scripts/cross_embed.py --rebuild    # 清空重建（换模型后用）
"""
import argparse
import datetime as dt
import os
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

import cross

DB_PATH = cross.DB_PATH
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = "qwen3-embedding:8b"
EMBED_DIMS = 1024          # MRL 截断后重归一化
BATCH = 64                 # 8b 实测 ~14 条/s，批次太大单请求超时风险高
QUERY_INSTRUCT = ("Instruct: Given a product description, retrieve CBP customs "
                  "rulings classifying similar merchandise\nQuery: ")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
  number     TEXT PRIMARY KEY,   -- 裁定号
  model      TEXT NOT NULL,
  dims       INTEGER NOT NULL,
  embedded_at TEXT
);
"""


def _load_vec(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def open_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    _load_vec(conn)
    conn.executescript(_SCHEMA)
    # vec0 虚拟表的 rowid 直接用 rulings 的隐式 rowid 作外键：
    # rulings 只 UPDATE 不重插，rowid 稳定
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_subjects "
        f"USING vec0(emb float[{EMBED_DIMS}])")
    return conn


def _truncate_norm(vec, dims=EMBED_DIMS):
    """MRL 截断 + 重归一化。截断后不归一化，余弦距离就不对了。"""
    v = vec[:dims]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def embed_batch(texts, model=EMBED_MODEL, timeout=300):
    """调 ollama 嵌入一批文本。失败抛异常，由调用方决定重试还是终止。
    单独成函数：测试用假嵌入器整体替换，不依赖 ollama 在线。"""
    r = httpx.post(f"{OLLAMA_URL}/api/embed",
                   json={"model": model, "input": texts}, timeout=timeout)
    r.raise_for_status()
    return r.json()["embeddings"]


def pending_rulings(conn):
    """还没按当前模型嵌入的裁定（增量的依据）"""
    return conn.execute("""
        SELECT r.rowid, r.number, r.subject FROM rulings r
        LEFT JOIN embeddings e ON e.number = r.number
        WHERE e.number IS NULL AND r.subject != ''""").fetchall()


def sync_embeddings(db_path=DB_PATH, rebuild=False, log=print):
    """增量嵌入。返回统计 dict。"""
    conn = open_db(db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        prev_model = meta.get("embed_model")
        if rebuild:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM vec_subjects")
            conn.commit()
        elif prev_model and prev_model != EMBED_MODEL:
            # 混两种模型的向量空间是纯粹的错误：距离没有可比性，
            # 检索结果看着正常实际是乱的——必须显式重建
            raise SystemExit(
                f"索引已用 {prev_model} 构建，当前配置为 {EMBED_MODEL}。"
                f"换模型必须 --rebuild 全量重建，不能增量混嵌。")

        todo = pending_rulings(conn)
        total, done, t0 = len(todo), 0, time.time()
        log(f"待嵌入 {total} 条（模型 {EMBED_MODEL}，{EMBED_DIMS} 维）")
        now = dt.datetime.now().isoformat(timespec="seconds")
        for i in range(0, total, BATCH):
            chunk = todo[i:i + BATCH]
            vecs = embed_batch([s for _, _, s in chunk])
            with conn:  # 每批一个事务：中断后重跑自动从断点继续
                for (rowid, number, _), v in zip(chunk, vecs):
                    tv = _truncate_norm(v)
                    conn.execute(
                        "INSERT OR REPLACE INTO vec_subjects(rowid, emb) VALUES (?,?)",
                        (rowid, struct.pack(f"{EMBED_DIMS}f", *tv)))
                    conn.execute(
                        "INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                        (number, EMBED_MODEL, EMBED_DIMS, now))
            done += len(chunk)
            if done % (BATCH * 20) == 0 or done == total:
                rate = done / max(time.time() - t0, 1)
                eta = (total - done) / max(rate, 0.1)
                log(f"  {done}/{total}（{rate:.0f} 条/s，剩余约 {eta/60:.0f} 分钟）")

        n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        for k, v in {"embed_model": EMBED_MODEL, "embed_dims": EMBED_DIMS,
                     "embed_count": n, "last_embed": now}.items():
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        conn.commit()
        log(f"完成：索引 {n} 条 / 库内 "
            f"{conn.execute('SELECT COUNT(*) FROM rulings').fetchone()[0]} 条，"
            f"耗时 {(time.time()-t0)/60:.1f} 分钟")
        return {"新嵌入": done, "索引总数": n}
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="CROSS 裁定 subject 语义索引")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--rebuild", action="store_true",
                    help="清空重建（换嵌入模型后必须）")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print("cross.db 不存在，请先运行 python scripts/cross_sync.py")
        return 1
    sync_embeddings(db_path=a.db, rebuild=a.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
