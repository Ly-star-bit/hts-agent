# -*- coding: utf-8 -*-
"""
cross_sync.py —— CROSS 裁定库元数据镜像（一期）

把 rulings.cbp.gov 的全量裁定**元数据**（约 22 万条）同步进 data/cross.db
（SQLite，单文件，标准库，不入 git）。裁定正文不在一期范围内——元数据会变
（撤销/修改），必须定期刷；正文不可变（改判用新裁定号），二期按需拉取即可。

为什么要镜像：API 的 term 是全文检索，"某编码的全部先例"这个查询它做不了
（命中的是正文提到该编码的 protest/drawback 案），且客户端只能在拉回的前
N 页里过滤——看不见的页里有多少先例，永远不知道。本地有全量 tariffs 字段后，
反向索引变成一条 SQL，还能给搜索结果表补「先例数」列。

【枚举策略】（2026-09 实测钉死的三个事实）
  - term=* 通配可用，与 term=the 各年份逐条数一致，不漏
  - 服务端 Elasticsearch 形态，10k 深翻页窗口：totalHits 封顶 10000，
    第 21 页 ×500 之后返回空——一把捞全量不可行
  - 按年切片绝大多数 <10k，但 2000 年代初有超限年份（2002 恰好 10000）
    → 切片命中 10k 上限时自动对半细分，递归到日级仍超限才放弃并如实报告

【撤销状态为什么每晚全刷】
  撤销/修改动的是**老**记录（N232914 是 2012 年的，2014 年才被撤销），
  增量按日期拉新裁定根本看不见这种变化。全量元数据只要 ~600 个请求、
  几分钟——这个成本换来的是：本地镜像的撤销状态永远不比"用户自己开网页查"旧。

【变动报告】
  沿用 build_db.py 的原则：影响数据的变动必须打出来，构建日志是唯一的人工
  核对点。尤其是状态变化（现行 → 已撤销）——它直接决定一条先例还能不能引用，
  逐条打印，绝不静默。

用法：
    python scripts/cross_sync.py                 # 全量同步（首次 ~10 分钟）
    python scripts/cross_sync.py --start-year 2024   # 只刷近两年（调试用）
  建议 cron 每晚跑一次全量。
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cross

DB_PATH = os.path.join(cross.BASE_DIR, "data", "cross.db")

# 官方说法是"1989 年至今"，实测不准：1988 年有 369 条，最早的日期到 1966
# （多半是录入笔误的年份，但确实在索引里，按日期切片就得从这里起，否则漏采）。
# 1966-1988 合计 ~375 条，代价是每晚多 ~23 个基本为空的切片请求，可忽略。
# 注意仍有 ~280 条无法用日期切片枚举到（推测 rulingDate 为空），
# 同步报告里的"差额"就是它们——差额必须打出来，静默缺口会被当成"没有先例"。
FIRST_YEAR = 1966
PAGE_SIZE = 500
ES_WINDOW = 10000     # 服务端深翻页窗口；totalHits 顶到这个数=被截断，须细分切片
MAX_STATUS_LINES = 50 # 状态变化逐条打印的上限（计数永不截断，只截明细）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rulings (
  number      TEXT PRIMARY KEY,
  date        TEXT,             -- ISO 'YYYY-MM-DD'
  collection  TEXT,             -- ny / hq
  subject     TEXT,
  categories  TEXT,
  tariffs     TEXT,             -- CBP 原样逗号串，展示用
  revoked_by  TEXT,             -- JSON 数组
  modified_by TEXT,             -- JSON 数组
  op_revoked  INTEGER DEFAULT 0,
  related     TEXT,             -- JSON 数组
  first_seen  TEXT,
  updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS ruling_codes (
  number TEXT NOT NULL,
  code   TEXT NOT NULL,         -- 纯数字（8 或 10 位），前缀检索用
  PRIMARY KEY (number, code)
);
CREATE INDEX IF NOT EXISTS idx_codes ON ruling_codes(code);
-- 8 位子目 → 先例数。同步末尾整表重建，给搜索结果的「先例数」列用：
-- 14954 个码逐个 COUNT 太慢，预聚合成键查
CREATE TABLE IF NOT EXISTS code8_counts (
  code8 TEXT PRIMARY KEY,
  n     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def open_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")  # 同步写入时不阻塞 Web 端读
    conn.executescript(_SCHEMA)
    return conn


# ---------- 枚举 ----------

def _fetch_slice(fr, to, sleep=0.15):
    """
    拉取一个日期切片的全部裁定。
    返回 (rows, total)；命中 10k 窗口返回 (None, total) 由调用方细分。
    网络错误向上抛 CrossError，由 sync() 统一记账。
    """
    out, page = [], 1
    while True:
        d = cross._get("/api/search", {
            "term": "*", "fromDate": fr, "toDate": to,
            "pageSize": PAGE_SIZE, "page": page, "sortBy": "DATE_DESC"})
        total = int(d.get("totalHits") or 0)
        if total >= ES_WINDOW:
            return None, total
        rows = d.get("rulings") or []
        out.extend(rows)
        if page * PAGE_SIZE >= total or not rows:
            return out, total
        page += 1
        time.sleep(sleep)


def enumerate_slices(start_year, end_year, sleep=0.15, log=print):
    """
    年度切片枚举，超限自动对半细分（递归）。
    生成 (切片描述, rows or None, 错误信息)；rows=None 表示该切片最终失败。
    """
    stack = [(f"{y}-01-01", f"{y}-12-31") for y in range(end_year, start_year - 1, -1)]
    while stack:
        fr, to = stack.pop()
        label = f"{fr}..{to}"
        try:
            rows, total = _fetch_slice(fr, to, sleep=sleep)
        except cross.CrossError as e:
            # 重试一次；再失败就如实报告，继续其余切片——同步是幂等的，
            # 明晚会自愈，但今晚必须知道少了哪块
            try:
                time.sleep(2)
                rows, total = _fetch_slice(fr, to, sleep=sleep)
            except cross.CrossError as e2:
                yield label, None, str(e2)
                continue
        if rows is None:
            a = dt.date.fromisoformat(fr)
            b = dt.date.fromisoformat(to)
            if a >= b:
                # 单日仍超 10k：真实存在才算数据灾难，如实报告
                yield label, None, f"单日超过 {ES_WINDOW} 条（{total}），无法完整枚举"
                continue
            mid = a + (b - a) // 2
            log(f"  切片 {label} 命中 {ES_WINDOW} 窗口（{total} 条），细分")
            stack.append(((mid + dt.timedelta(days=1)).isoformat(), to))
            stack.append((fr, mid.isoformat()))
            continue
        yield label, rows, ""
        time.sleep(sleep)


# ---------- 落库与变动检测 ----------

def _canon(raw):
    """一条原始记录 → 入库字段元组（也是变动比较的基准）"""
    date = str(raw.get("rulingDate") or "")[:10]
    return {
        "number": str(raw.get("rulingNumber") or ""),
        "date": date,
        "collection": str(raw.get("collection") or "").lower(),
        "subject": str(raw.get("subject") or ""),
        "categories": str(raw.get("categories") or ""),
        "tariffs": str(raw.get("tariffs") or ""),
        "revoked_by": json.dumps(sorted(str(x) for x in (raw.get("revokedBy") or []))),
        "modified_by": json.dumps(sorted(str(x) for x in (raw.get("modifiedBy") or []))),
        "op_revoked": 1 if raw.get("operationallyRevoked") else 0,
        "related": json.dumps(sorted(str(x) for x in (raw.get("relatedRulings") or []))),
    }


def _status_of(row):
    """与 cross._status 同一判定，但作用在库行上（报告用）"""
    if json.loads(row["revoked_by"]) or row["op_revoked"]:
        return "已撤销"
    if json.loads(row["modified_by"]):
        return "已修改"
    return "现行"


def upsert_rulings(conn, raws, now):
    """
    幂等写入一批原始记录，返回变动统计。
    状态变化（现行 ↔ 已撤销/已修改）单独记明细——它决定先例还能不能引用。
    """
    stats = {"新增": 0, "状态变化": [], "tariffs变化": 0, "subject变化": 0, "未变": 0}
    cur = conn.cursor()
    for raw in raws:
        c = _canon(raw)
        if not c["number"]:
            continue
        old = cur.execute(
            "SELECT date,collection,subject,categories,tariffs,"
            "revoked_by,modified_by,op_revoked,related FROM rulings WHERE number=?",
            (c["number"],)).fetchone()
        if old is None:
            cur.execute(
                "INSERT INTO rulings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["number"], c["date"], c["collection"], c["subject"],
                 c["categories"], c["tariffs"], c["revoked_by"], c["modified_by"],
                 c["op_revoked"], c["related"], now, now))
            _write_codes(cur, c["number"], c["tariffs"])
            stats["新增"] += 1
            continue
        keys = ("date", "collection", "subject", "categories", "tariffs",
                "revoked_by", "modified_by", "op_revoked", "related")
        oldd = dict(zip(keys, old))
        if all(oldd[k] == c[k] for k in keys):
            stats["未变"] += 1
            continue
        old_st, new_st = _status_of(oldd), _status_of(c)
        if old_st != new_st:
            stats["状态变化"].append(
                f"{c['number']}: {old_st} → {new_st}"
                f"（revokedBy={json.loads(c['revoked_by'])}"
                f" modifiedBy={json.loads(c['modified_by'])}）")
        if oldd["tariffs"] != c["tariffs"]:
            stats["tariffs变化"] += 1
        if oldd["subject"] != c["subject"]:
            stats["subject变化"] += 1
        cur.execute(
            "UPDATE rulings SET date=?,collection=?,subject=?,categories=?,"
            "tariffs=?,revoked_by=?,modified_by=?,op_revoked=?,related=?,"
            "updated_at=? WHERE number=?",
            (c["date"], c["collection"], c["subject"], c["categories"],
             c["tariffs"], c["revoked_by"], c["modified_by"], c["op_revoked"],
             c["related"], now, c["number"]))
        if oldd["tariffs"] != c["tariffs"]:
            cur.execute("DELETE FROM ruling_codes WHERE number=?", (c["number"],))
            _write_codes(cur, c["number"], c["tariffs"])
    return stats


def _write_codes(cur, number, tariffs):
    for t in str(tariffs or "").split(","):
        digits = "".join(ch for ch in t if ch.isdigit())
        if len(digits) >= 4:
            cur.execute("INSERT OR IGNORE INTO ruling_codes VALUES (?,?)",
                        (number, digits))


def rebuild_counts(conn):
    """8 位子目 → 先例数 预聚合（98/99 章剔除：加征条款不是归类结论）"""
    conn.execute("DELETE FROM code8_counts")
    conn.execute("""
        INSERT INTO code8_counts
        SELECT substr(code,1,8), COUNT(DISTINCT number) FROM ruling_codes
        WHERE length(code) >= 8 AND substr(code,1,2) NOT IN ('98','99')
        GROUP BY substr(code,1,8)""")


def _set_meta(conn, **kv):
    for k, v in kv.items():
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))


# ---------- 主流程 ----------

def sync(db_path=DB_PATH, start_year=FIRST_YEAR, end_year=None,
         sleep=0.15, log=print):
    """全量同步。返回汇总统计（测试与调用方用），报告打到 log。"""
    end_year = end_year or dt.date.today().year
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = open_db(db_path)
    total = {"新增": 0, "状态变化": [], "tariffs变化": 0, "subject变化": 0,
             "未变": 0, "切片": 0, "失败切片": []}
    t0 = time.time()
    try:
        for label, rows, err in enumerate_slices(start_year, end_year,
                                                 sleep=sleep, log=log):
            total["切片"] += 1
            if rows is None:
                total["失败切片"].append(f"{label}: {err}")
                log(f"  ✗ 切片 {label} 失败：{err}")
                continue
            st = upsert_rulings(conn, rows, now)
            for k in ("新增", "tariffs变化", "subject变化", "未变"):
                total[k] += st[k]
            total["状态变化"].extend(st["状态变化"])
            conn.commit()
        rebuild_counts(conn)

        # 与服务端对账：差额必须打出来，静默的缺口会被当成"没有先例"
        local_n = conn.execute("SELECT COUNT(*) FROM rulings").fetchone()[0]
        server_n = None
        try:
            stat = cross._get("/api/stat/lastupdate", {})
            server_n = int(stat.get("totalSearchableRulingsCount") or 0)
        except Exception:
            pass
        _set_meta(conn, last_sync=now, local_count=local_n,
                  server_count=server_n if server_n is not None else "",
                  failed_slices=len(total["失败切片"]))
        conn.commit()
    finally:
        conn.close()

    log(f"\n=== CROSS 同步报告（{now}，耗时 {time.time()-t0:.0f}s）===")
    log(f"切片 {total['切片']} 个，失败 {len(total['失败切片'])} 个")
    for f in total["失败切片"]:
        log(f"  ✗ {f}")
    log(f"本地 {local_n} 条 | 服务端 {server_n if server_n is not None else '未知'} 条"
        + (f" | 差额 {server_n - local_n}" if server_n is not None else ""))
    log(f"新增 {total['新增']} | tariffs变化 {total['tariffs变化']} | "
        f"subject变化 {total['subject变化']} | 未变 {total['未变']}")
    n_st = len(total["状态变化"])
    log(f"状态变化 {n_st} 条" + ("：" if n_st else ""))
    for line in total["状态变化"][:MAX_STATUS_LINES]:
        log(f"  ⚠ {line}")
    if n_st > MAX_STATUS_LINES:
        log(f"  …另有 {n_st - MAX_STATUS_LINES} 条（计数完整，仅明细截断）")
    total["本地条数"] = local_n
    total["服务端条数"] = server_n
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description="CROSS 裁定库元数据同步")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--start-year", type=int, default=FIRST_YEAR)
    ap.add_argument("--end-year", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=0.15,
                    help="请求间隔秒数（礼貌限速）")
    a = ap.parse_args(argv)
    r = sync(db_path=a.db, start_year=a.start_year, end_year=a.end_year,
             sleep=a.sleep)
    return 1 if r["失败切片"] else 0


if __name__ == "__main__":
    sys.exit(main())
