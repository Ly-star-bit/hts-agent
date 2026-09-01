# -*- coding: utf-8 -*-
"""
test_cross_sync.py —— CROSS 元数据镜像（cross_sync.py + cross.code_precedents）

不联网：cross._get 整体替换成按 (fromDate,toDate,page) 出预设数据的假服务端。

锁五件事：
  1. 10k 窗口触发切片自动细分，细分后数据不丢不重
  2. upsert 幂等；状态变化（现行→已撤销）被逐条报出——它决定先例还能不能引用
  3. code8_counts 预聚合正确且剔除 98/99 章
  4. code_precedents 反查是**完整**的（这正是镜像存在的理由），
     10 位统计码/短码前缀两个方向都匹配
  5. 库缺失/损坏一律降级为 {"error": ...}，不抛异常
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import cross
import cross_sync


def raw(number, tariffs, date="2023-05-01", collection="ny", subject="x",
        revoked_by=None, modified_by=None):
    return {"rulingNumber": number, "subject": subject, "categories": "Classification",
            "rulingDate": date + "T00:00:00", "collection": collection,
            "relatedRulings": [], "modifiedBy": modified_by or [], "modifies": [],
            "revokedBy": revoked_by or [], "revokes": [],
            "tariffs": tariffs, "operationallyRevoked": False,
            "commodityGrouping": ""}


class FakeServer:
    """按日期切片吐数据的假 CROSS。rulings 是 {日期: [raw...]}。"""

    def __init__(self, by_date, cap_ranges=()):
        self.by_date = by_date
        # cap_ranges: 命中这些 (fr,to) 时返回 totalHits=10000（模拟窗口截断）
        self.cap_ranges = set(cap_ranges)
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/api/stat/lastupdate":
            n = sum(len(v) for v in self.by_date.values())
            return {"totalSearchableRulingsCount": n}
        fr, to = params["fromDate"], params["toDate"]
        if (fr, to) in self.cap_ranges:
            return {"rulings": [], "totalHits": 10000}
        rows = [r for d, v in self.by_date.items() if fr <= d <= to for r in v]
        page, size = int(params["page"]), int(params["pageSize"])
        return {"rulings": rows[(page - 1) * size: page * size],
                "totalHits": len(rows)}


class SyncTestCase(unittest.TestCase):

    def setUp(self):
        self._get = cross._get
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "cross.db")
        self.logs = []

    def tearDown(self):
        cross._get = self._get
        self._tmp.cleanup()

    def serve(self, by_date, cap_ranges=()):
        srv = FakeServer(by_date, cap_ranges)
        cross._get = srv
        return srv

    def sync(self, **kw):
        kw.setdefault("db_path", self.db)
        kw.setdefault("start_year", 2023)
        kw.setdefault("end_year", 2023)
        kw.setdefault("sleep", 0)
        kw.setdefault("log", self.logs.append)
        return cross_sync.sync(**kw)


class TestEnumeration(SyncTestCase):

    def test_basic_sync(self):
        self.serve({"2023-03-01": [raw("A", "8507.60.0020")],
                    "2023-09-01": [raw("B", "8506.50.0000")]})
        r = self.sync()
        self.assertEqual(r["新增"], 2)
        self.assertEqual(r["本地条数"], 2)
        self.assertEqual(r["失败切片"], [])

    def test_window_cap_triggers_split_without_loss(self):
        """全年切片被 10k 窗口截断 → 对半细分后两条都拿到，不丢不重"""
        srv = self.serve(
            {"2023-03-01": [raw("A", "8507.60.0020")],
             "2023-09-01": [raw("B", "8506.50.0000")]},
            cap_ranges={("2023-01-01", "2023-12-31")})
        r = self.sync()
        self.assertEqual(r["新增"], 2, "细分后数据不能丢")
        self.assertEqual(r["本地条数"], 2)
        # 确认真的走了细分：请求里应出现子区间
        subs = [p for _, p in srv.calls
                if p.get("fromDate", "").startswith("2023") and
                   p.get("toDate") != "2023-12-31"]
        self.assertTrue(subs, "应发出细分后的子区间请求")

    def test_single_day_still_capped_reported_not_silent(self):
        """递归到单日仍超限：必须报失败切片，绝不静默少数据"""
        cap = {("2023-01-01", "2023-12-31")}
        a, b = dt.date(2023, 1, 1), dt.date(2023, 12, 31)
        # 把所有可能的细分区间全部封死，直到单日
        stack = [(a, b)]
        while stack:
            x, y = stack.pop()
            cap.add((x.isoformat(), y.isoformat()))
            if x >= y:
                continue
            mid = x + (y - x) // 2
            stack.append((x, mid))
            stack.append((mid + dt.timedelta(days=1), y))
        self.serve({}, cap_ranges=cap)
        r = self.sync()
        self.assertTrue(r["失败切片"])
        self.assertIn("无法完整枚举", r["失败切片"][0])

    def test_network_failure_recorded_and_continues(self):
        calls = {"n": 0}
        def flaky(path, params):
            if path == "/api/stat/lastupdate":
                return {"totalSearchableRulingsCount": 0}
            calls["n"] += 1
            raise cross.CrossError("boom")
        cross._get = flaky
        r = self.sync()
        self.assertTrue(r["失败切片"])
        self.assertGreaterEqual(calls["n"], 2, "应重试一次再放弃")


class TestUpsert(SyncTestCase):

    def test_idempotent_rerun(self):
        data = {"2023-03-01": [raw("A", "8507.60.0020")]}
        self.serve(data)
        self.sync()
        r2 = self.sync()
        self.assertEqual(r2["新增"], 0)
        self.assertEqual(r2["未变"], 1)
        self.assertEqual(r2["状态变化"], [])

    def test_revocation_change_reported_line_by_line(self):
        """核心回归：撤销动的是老记录，全量刷才看得见，且必须逐条报出"""
        self.serve({"2023-03-01": [raw("A", "8507.60.0020")]})
        self.sync()
        self.serve({"2023-03-01": [raw("A", "8507.60.0020",
                                       revoked_by=["H999999"])]})
        r = self.sync()
        self.assertEqual(len(r["状态变化"]), 1)
        self.assertIn("A: 现行 → 已撤销", r["状态变化"][0])
        self.assertIn("H999999", r["状态变化"][0])
        self.assertTrue(any("现行 → 已撤销" in l for l in self.logs),
                        "状态变化必须出现在同步报告里")

    def test_tariffs_change_rewrites_code_index(self):
        self.serve({"2023-03-01": [raw("A", "8507.60.0020")]})
        self.sync()
        self.serve({"2023-03-01": [raw("A", "8506.50.0000")]})
        self.sync()
        got = cross.code_precedents(["8506.50.00"], db_path=self.db)
        self.assertEqual([x["裁定号"] for x in got["先例"]], ["A"])
        old = cross.code_precedents(["8507.60.00"], db_path=self.db)
        self.assertEqual(old["先例"], [], "旧编码的索引行应被清掉")


class TestCounts(SyncTestCase):

    def test_code8_counts_aggregates_and_drops_ch99(self):
        self.serve({"2023-03-01": [
            raw("A", "8507.60.0020, 9903.88.15"),
            raw("B", "8507.60.0000"),
            raw("C", "8507.60.00"),
        ]})
        self.sync()
        got = cross.code_precedents(["8507.60.00"], db_path=self.db)
        self.assertEqual(got["每码先例数"]["8507.60.00"], 3)
        # 9903 不该有计数行
        import sqlite3
        conn = sqlite3.connect(self.db)
        n99 = conn.execute(
            "SELECT COUNT(*) FROM code8_counts WHERE code8 LIKE '99%'").fetchone()[0]
        conn.close()
        self.assertEqual(n99, 0)


class TestCodePrecedents(SyncTestCase):

    def _build(self):
        self.serve({"2023-03-01": [
            raw("NEW", "8507.60.0020", date="2024-06-01"),
            raw("OLD", "8507.60.0000", date="2012-03-01"),
            raw("HQ1", "8507.60.0050", date="2015-07-13", collection="hq"),
            raw("DEAD", "8507.60.0020", date="2019-01-01",
                revoked_by=["H1"]),
            raw("SHORT", "850760", date="2001-05-01"),        # 只写到 6 位的老裁定
            raw("OTHER", "3926.90.9989", date="2023-01-01"),  # 无关码
        ]})
        self.sync()

    def test_complete_recall_and_prefix_both_ways(self):
        """完整反查 + 双向前缀：10 位统计码、6 位短码都算命中"""
        self._build()
        got = cross.code_precedents(["8507.60.00"], db_path=self.db)
        names = {x["裁定号"] for x in got["先例"]}
        self.assertEqual(names, {"NEW", "OLD", "HQ1", "DEAD", "SHORT"})
        self.assertNotIn("OTHER", names)

    def test_order_current_hq_then_date(self):
        self._build()
        got = cross.code_precedents(["8507.60.00"], db_path=self.db)
        order = [x["裁定号"] for x in got["先例"]]
        self.assertEqual(order[0], "HQ1", "现行 HQ 排最前")
        self.assertEqual(order[-1], "DEAD", "已撤销的沉底但保留")
        cur_ny = [n for n in order if n in ("NEW", "OLD", "SHORT")]
        self.assertEqual(cur_ny, ["NEW", "OLD", "SHORT"],
                         "现行 NY 组内按日期新在前（2024 > 2012 > 2001）")

    def test_status_and_version_note_computed_at_read(self):
        self._build()
        got = cross.code_precedents(["8507.60.00"], db_path=self.db)
        by = {x["裁定号"]: x for x in got["先例"]}
        self.assertEqual(by["DEAD"]["状态"], "已撤销")
        self.assertIn("H1", by["DEAD"]["状态说明"])
        self.assertIn("HS 2022", by["OLD"]["版本提示"])
        self.assertEqual(by["NEW"]["版本提示"], "")

    def test_missing_db_degrades(self):
        got = cross.code_precedents(["8507.60.00"],
                                    db_path=os.path.join(self._tmp.name, "nope.db"))
        self.assertIn("error", got)
        self.assertIn("cross_sync", got["error"])

    def test_corrupt_db_degrades_not_raises(self):
        with open(self.db, "w") as f:
            f.write("not a sqlite file at all")
        got = cross.code_precedents(["8507.60.00"], db_path=self.db)
        self.assertIn("error", got)

    def test_short_candidate_rejected(self):
        self._build()
        self.assertIn("error", cross.code_precedents(["8507"], db_path=self.db))

    def test_precedent_counts_bulk(self):
        """搜索表「先例数」列的键查：查过没有 → 0，短码 → 跳过"""
        self._build()
        got = cross.precedent_counts(
            ["8507.60.00", "3926.90.99", "1111.11.11", "85"], db_path=self.db)
        # SHORT(850760) 不足 8 位不进计数表；NEW/OLD/HQ1/DEAD 4 条计入
        self.assertEqual(got["8507.60.00"], 4)
        self.assertEqual(got["3926.90.99"], 1)
        self.assertEqual(got["1111.11.11"], 0, "查过了没有 → 0，不是缺席")
        self.assertNotIn("85", got, "短码无法与 8 位表对账，跳过")

    def test_precedent_counts_missing_db_empty(self):
        """镜像没建 → {}，一列数字的缺失不能挡住搜索主链路"""
        got = cross.precedent_counts(
            ["8507.60.00"], db_path=os.path.join(self._tmp.name, "nope.db"))
        self.assertEqual(got, {})

    def test_local_status(self):
        self._build()
        st = cross.local_status(self.db)
        self.assertTrue(st["可用"])
        self.assertEqual(st["条数"], 6)
        self.assertTrue(st["上次同步"])
        st2 = cross.local_status(os.path.join(self._tmp.name, "nope.db"))
        self.assertFalse(st2["可用"])


if __name__ == "__main__":
    unittest.main()
