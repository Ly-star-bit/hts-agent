# -*- coding: utf-8 -*-
"""
test_search_assist.py —— 搜索页的 AI 补充分析（assist_search）

背景：原先「税率搜索」与「AI 助手」是两个割裂的 tab，用户的心智是
"本地搜不准再上 AI"。但 classify_product 的候选池本来就是 rate.search
的结果——本地召回不到的，AI 精排也看不见。所以合并后 AI 必须作用在
**检索之前**（改写检索词）而不只是之后（精排）。

这里锁三件事：
  1. 章号只加权不过滤（模型猜错章不能把正确候选滤没）
  2. AI 结果是"并入"本地结果，不是替换（模型抽风时本地结果仍在）
  3. AI 不可用时静默降级为 error 字典，绝不抛异常打断本地查询

用真实 db + 假 provider，不联网。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ai
import core
import rate


class FakeProvider:
    """按调用顺序吐预设 JSON；chat_json 的两次调用分别对应出词与精排"""

    def __init__(self, keywords=None, chapters=None, picks=None, raise_on=None):
        self._r1 = {"keywords": keywords or [], "chapters": chapters or []}
        self._r2 = {"picks": picks or []}
        self._raise_on = raise_on
        self.calls = 0

    def chat_json(self, messages, fallback=None):
        self.calls += 1
        if self._raise_on == self.calls:
            raise ai.AIProviderError("模拟服务不可用")
        return self._r1 if self.calls == 1 else self._r2


class TestRankByChapters(unittest.TestCase):
    """章号是模型的猜测，猜错不能丢数据"""

    ROWS = [{"编码": "3926.20.60"}, {"编码": "6201.40.35"}, {"编码": "6202.93.55"}]

    def _codes(self, rows):
        return [r["编码"] for r in rows]

    def test_suggested_chapter_ranks_first(self):
        out = ai._rank_by_chapters(self.ROWS, ["62"])
        self.assertEqual(self._codes(out)[:2], ["6201.40.35", "6202.93.55"])

    def test_wrong_chapter_drops_nothing(self):
        """核心回归：旧实现是硬过滤，猜错章会返回空并报'税则库未找到'"""
        out = ai._rank_by_chapters(self.ROWS, ["99"])
        self.assertEqual(len(out), 3)
        self.assertEqual(set(self._codes(out)), set(self._codes(self.ROWS)))

    def test_no_chapters_keeps_order(self):
        self.assertEqual(self._codes(ai._rank_by_chapters(self.ROWS, [])),
                         self._codes(self.ROWS))

    def test_limit_applied(self):
        self.assertEqual(len(ai._rank_by_chapters(self.ROWS, ["62"], limit=2)), 2)


class TestAssistSearchDegrades(unittest.TestCase):
    """AI 不可用时必须返回 error 字典，让前端保留本地结果"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig = ai.get_provider

    def tearDown(self):
        ai.get_provider = self._orig

    def test_no_provider(self):
        ai.get_provider = lambda: None
        r = ai.assist_search(self.db, "lithium battery")
        self.assertIn("error", r)
        self.assertNotIn("精排", r)

    def test_provider_raises_first_round(self):
        ai.get_provider = lambda: FakeProvider(raise_on=1)
        r = ai.assist_search(self.db, "lithium battery")
        self.assertIn("error", r)

    def test_provider_raises_second_round(self):
        ai.get_provider = lambda: FakeProvider(keywords=["battery"], raise_on=2)
        r = ai.assist_search(self.db, "lithium battery")
        self.assertIn("error", r)

    def test_empty_keywords(self):
        ai.get_provider = lambda: FakeProvider(keywords=[])
        r = ai.assist_search(self.db, "lithium battery")
        self.assertIn("error", r)


class TestAssistSearchMerge(unittest.TestCase):
    """AI 召回是并入而非替换本地结果"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig = ai.get_provider

    def tearDown(self):
        ai.get_provider = self._orig

    def test_new_rows_exclude_local_hits(self):
        """AI 关键词与本地检索词一致时，新增候选应为空——不能把本地结果重复一遍"""
        ai.get_provider = lambda: FakeProvider(keywords=["battery"])
        r = ai.assist_search(self.db, "battery")
        local = {x["编码"] for x in rate.search(self.db, "battery", limit=40, sort="relevance")}
        self.assertTrue(local, "前置条件：本地应能搜到 battery")
        for row in r["新增候选"]:
            self.assertNotIn(row["编码"], local, "新增候选与本地结果重复")

    def test_ai_keywords_add_unreachable_rows(self):
        """
        词表覆盖不到的中文描述：本地搜出的是一批，AI 换词后能带来新候选。
        这正是合并的意义——AI 的价值在检索之前。
        """
        ai.get_provider = lambda: FakeProvider(keywords=["woven", "jacket", "coated"])
        r = ai.assist_search(self.db, "梭织涂层夹克")
        self.assertNotIn("error", r)
        self.assertTrue(r["新增候选"], "AI 改写检索词后应带来本地搜不到的候选")

    def test_new_rows_tagged(self):
        ai.get_provider = lambda: FakeProvider(keywords=["woven", "jacket", "coated"])
        r = ai.assist_search(self.db, "梭织涂层夹克")
        for row in r["新增候选"]:
            self.assertEqual(row.get("来源"), "AI")

    def test_new_rows_have_same_columns_as_local(self):
        """前端把两批行渲染进同一张表，字段缺一列就会错位"""
        ai.get_provider = lambda: FakeProvider(keywords=["woven", "jacket", "coated"])
        r = ai.assist_search(self.db, "梭织涂层夹克")
        local = rate.search(self.db, "梭织涂层夹克", limit=5, sort="relevance")
        if not (local and r["新增候选"]):
            self.skipTest("语料未同时提供本地与 AI 候选")
        need = {"编码", "商品描述", "一般税率", "税率类型", "等效从价", "301判定", "301加征"}
        self.assertTrue(need <= set(r["新增候选"][0]))
        self.assertTrue(need <= set(local[0]))

    def test_picks_must_come_from_candidates(self):
        """模型编造的编码要被丢弃，不能进精排结果"""
        ai.get_provider = lambda: FakeProvider(
            keywords=["battery"],
            picks=[{"code": "99999999", "confidence": 0.99, "reason": "编的"}])
        r = ai.assist_search(self.db, "battery")
        self.assertEqual(r["精排"], [])

    def test_pick_carries_local_criteria(self):
        """判定条件与证据来自本地税则，不经模型"""
        rows = rate.search(self.db, "battery", limit=5, sort="relevance")
        self.assertTrue(rows)
        code = rows[0]["编码"]
        ai.get_provider = lambda: FakeProvider(
            keywords=["battery"],
            picks=[{"code": code, "confidence": 0.8, "reason": "理由",
                    "need_verify": ["容量是否超过 X"]}])
        r = ai.assist_search(self.db, "battery")
        self.assertEqual(len(r["精排"]), 1)
        p = r["精排"][0]
        self.assertEqual(p["编码"], code)
        self.assertIn("判定条件", p)
        self.assertIn("证据清单", p)
        self.assertEqual(p["需确认"], ["容量是否超过 X"])

    def test_confidence_clamped(self):
        rows = rate.search(self.db, "battery", limit=5, sort="relevance")
        ai.get_provider = lambda: FakeProvider(
            keywords=["battery"],
            picks=[{"code": rows[0]["编码"], "confidence": 7.5, "reason": "x"}])
        r = ai.assist_search(self.db, "battery")
        self.assertLessEqual(r["精排"][0]["confidence"], 1.0)


class TestAssistSearchCalcParams(unittest.TestCase):
    """
    新增候选与本地行渲染在同一张表、还要一起排序，两批行的总税负必须同口径。

    回归：assist_search 收下了 origin 却从没往 rate.search 传，unit_value
    连形参都没有（靠 app.py 事后补算，且漏掉了 origin 与「总税负数值」）。
    症状是选「越南」时 AI 补进来的行照中国口径加了 301——两个数字并排摆着，
    看不出它们不是同一套算法算出来的，比报错更难发现。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig = ai.get_provider
        # 「watch」召回的一批里既有原产地敏感、也有从量税（需单位货值折算）的编码，
        # 与查询词「锂电池」的本地结果完全不重叠，因此整批都会成为新增候选
        ai.get_provider = lambda: FakeProvider(keywords=["watch"])

    def tearDown(self):
        ai.get_provider = self._orig

    def _new_rows(self, **kw):
        r = ai.assist_search(self.db, "锂电池", **kw)
        self.assertNotIn("error", r)
        self.assertTrue(r["新增候选"], "前置条件：watch 应带来本地搜不到的候选")
        return r["新增候选"]

    def test_origin_reaches_new_rows(self):
        rows = self._new_rows(origin="VN")
        differs = 0
        for row in rows:
            cn = rate.calc_total(self.db, row["编码"], origin="CN")["总税负估算"]
            vn = rate.calc_total(self.db, row["编码"], origin="VN")["总税负估算"]
            self.assertEqual(row["总税负估算"], vn,
                             f"{row['编码']} 未按越南口径计算（越南不适用中国 301）")
            differs += cn != vn
        self.assertTrue(differs, "前置条件：这批候选里应有中越口径不同的编码，否则测不出问题")

    def test_unit_value_reaches_new_rows(self):
        rows = self._new_rows(unit_value=10.0)
        differs = 0
        for row in rows:
            with_uv = rate.calc_total(self.db, row["编码"], unit_value=10.0)["总税负估算"]
            self.assertEqual(row["总税负估算"], with_uv,
                             f"{row['编码']} 的从量税未按单位货值折算")
            differs += with_uv != rate.calc_total(self.db, row["编码"])["总税负估算"]
        self.assertTrue(differs, "前置条件：这批候选里应有从量税编码，否则测不出问题")

    def test_new_rows_carry_total_num(self):
        """前端并表后要在客户端按总税负重排，缺这一列的行会被当成"折算不出"排到最后"""
        for row in self._new_rows():
            self.assertIn("总税负数值", row)


class ListProvider:
    """analyze_list 用：两轮 chat_json（出词 / 精排）+ 一次 chat（汇总报告）"""

    def __init__(self, kw_items, picks):
        self._kw = {"items": kw_items}
        self._picks = {"picks": picks}
        self.calls = 0

    def chat(self, messages):
        return "汇总报告"

    def chat_json(self, messages, fallback=None):
        self.calls += 1
        return self._kw if self.calls == 1 else self._picks


class TestAnalyzeListRecall(unittest.TestCase):
    """
    清单批量分析的召回降级。

    classify_product 里的"章号硬过滤 + 落空即报错"已修，analyze_list 漏了同一处。
    清单动辄几十行，某行报"本地库未匹配"时用户会以为库里没有，
    真实原因却是模型给这一行猜错了章。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig = ai.get_provider

    def tearDown(self):
        ai.get_provider = self._orig

    def _run(self, kw_items, picks, items):
        ai.get_provider = lambda: ListProvider(kw_items, picks)
        return ai.analyze_list(self.db, items)

    def test_wrong_chapter_still_classifies(self):
        """关键词对、章号猜错：硬过滤会滤空，加权则不受影响"""
        r = self._run(
            [{"index": 1, "keywords": ["battery"], "chapters": ["03"]}],
            [{"index": 1, "code": "85076000", "confidence": 0.8, "reason": "x"}],
            [{"name": "lithium battery"}])
        d = r["details"][0]
        self.assertNotIn("error", d, f"章号猜错不应判为无解：{d.get('error')}")
        self.assertEqual(d["编码"], "8507.60.00")

    def test_bad_keywords_degrade_to_name(self):
        """AI 检索词全落空时按品名原文再检索一次"""
        r = self._run(
            [{"index": 1, "keywords": ["zzznonexistent"], "chapters": []}],
            [{"index": 1, "code": "85076000", "confidence": 0.8, "reason": "x"}],
            [{"name": "锂电池"}])
        d = r["details"][0]
        self.assertNotIn("error", d, f"应降级为原文检索：{d.get('error')}")
        self.assertEqual(d["编码"], "8507.60.00")

    def test_genuinely_unmatched_reports_keywords(self):
        """真的搜不到时，错误信息要带上检索词，否则无从判断是谁的问题"""
        r = self._run(
            [{"index": 1, "keywords": ["zzznonexistent"], "chapters": []}],
            [],
            [{"name": "zzqqxx 不存在的商品"}])
        d = r["details"][0]
        self.assertIn("error", d)
        self.assertIn("zzznonexistent", d["error"],
                      "错误信息要带上 AI 检索词，否则分不清是模型的问题还是数据的问题")

    def test_degraded_row_is_marked(self):
        """走了降级检索的行要标注，否则用户无从判断这条为什么质量偏低"""
        r = self._run(
            [{"index": 1, "keywords": ["zzznonexistent"], "chapters": []}],
            [{"index": 1, "code": "85076000", "confidence": 0.8, "reason": "x"}],
            [{"name": "锂电池"}])
        self.assertIn("降级", r["details"][0].get("备注", ""))

    def test_row_count_preserved(self):
        """清单几十行时，结果必须逐行对齐输入，不能少行也不能错位"""
        items = [{"name": n} for n in ("锂电池", "zzqqxx", "lithium battery")]
        r = self._run(
            [{"index": 1, "keywords": ["battery"], "chapters": []},
             {"index": 2, "keywords": ["zzznonexistent"], "chapters": []},
             {"index": 3, "keywords": ["battery"], "chapters": []}],
            [{"index": 1, "code": "85076000", "confidence": 0.8, "reason": "x"},
             {"index": 3, "code": "85076000", "confidence": 0.8, "reason": "z"}],
            items)
        self.assertEqual(len(r["details"]), 3)
        self.assertEqual([d["序号"] for d in r["details"]], [1, 2, 3])
        self.assertEqual([d["品名"] for d in r["details"]],
                         ["锂电池", "zzqqxx", "lithium battery"])

    def test_hallucinated_code_not_accepted(self):
        """模型返回候选外的编码时不能退回 rows[0] 兜底"""
        r = self._run(
            [{"index": 1, "keywords": ["battery"], "chapters": []}],
            [{"index": 1, "code": "99999999", "confidence": 0.99, "reason": "编的"}],
            [{"name": "lithium battery"}])
        d = r["details"][0]
        self.assertIn("error", d)
        self.assertNotIn("编码", d)


class TestClassifyFallback(unittest.TestCase):
    """AI 关键词全落空时降级为原文检索，而不是报'税则库未找到'"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig = ai.get_provider

    def tearDown(self):
        ai.get_provider = self._orig

    def test_degrades_to_raw_description(self):
        rows = rate.search(self.db, "锂电池", limit=5, sort="relevance")
        self.assertTrue(rows, "前置条件：锂电池经同义词表应能搜到")
        ai.get_provider = lambda: FakeProvider(
            keywords=["zzzznonexistentword"],
            picks=[{"code": rows[0]["编码"], "confidence": 0.7, "reason": "x"}])
        r = ai.classify_product(self.db, "锂电池")
        self.assertNotIn("error", r, f"应降级而非报错：{r.get('error')}")
        self.assertTrue(r["降级"], "降级时必须告知用户候选质量下降")

    def test_wrong_chapter_no_longer_kills_result(self):
        """模型猜错章：旧实现硬过滤后 rows 为空，直接报'未找到'"""
        rows = rate.search(self.db, "battery", limit=5, sort="relevance")
        ai.get_provider = lambda: FakeProvider(
            keywords=["battery"], chapters=["03"],   # 故意猜成水产章
            picks=[{"code": rows[0]["编码"], "confidence": 0.7, "reason": "x"}])
        r = ai.classify_product(self.db, "锂电池")
        self.assertNotIn("error", r, f"章号猜错不应导致无结果：{r.get('error')}")
        self.assertTrue(r["candidates"])


if __name__ == "__main__":
    unittest.main()
