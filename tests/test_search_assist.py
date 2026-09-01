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

    def test_malformed_pick_index_does_not_crash(self):
        """
        回归：index 由模型返回，null/字符串/缺失都出现过。int(None) 会
        TypeError 炸掉整个清单分析——几十行商品陪一个坏 index 一起死。
        坏 index 的 pick 跳过，字符串数字要认，正常行不受牵连。
        """
        r = self._run(
            [{"index": 1, "keywords": ["battery"], "chapters": []},
             {"index": 2, "keywords": ["battery"], "chapters": []},
             {"index": 3, "keywords": ["battery"], "chapters": []}],
            [{"index": None, "code": "85076000", "confidence": 0.8, "reason": "坏"},
             {"index": "2", "code": "85076000", "confidence": 0.8, "reason": "字符串数字"},
             {"code": "85076000", "confidence": 0.8, "reason": "缺 index"}],
            [{"name": "锂电池"}, {"name": "lithium battery"}, {"name": "电池"}])
        self.assertEqual(len(r["details"]), 3, "报告必须完整返回，不能 500")
        d2 = r["details"][1]
        self.assertEqual(d2.get("编码"), "8507.60.00", "字符串 '2' 是合法 index，该行要正常出结论")
        self.assertIn("error", r["details"][0], "index=null 的 pick 跳过后该行报需人工")

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


class DeepreadProvider:
    """深读用假 provider：返回预设 picks（含逐字/篡改两种 quote）"""

    def __init__(self, picks=None, summary="综述", raise_it=False):
        self._picks = picks or []
        self._summary = summary
        self._raise = raise_it

    def chat_json(self, messages, fallback=None):
        if self._raise:
            raise ai.AIProviderError("模拟不可用")
        return {"picks": self._picks, "summary": self._summary}


# 一段够长、含结论句与编码的假正文，供原文校验用
_FAKE_DOC = ("This ruling concerns two battery packs. The applicable subheading "
             "for the non-rechargeable lithium battery packs will be 8506.50.0000, "
             "HTSUS. The rechargeable lithium-ion packs are classified under "
             "8507.60.0020, HTSUS.")


class TestDeepread(unittest.TestCase):
    """
    三期正文深读：AI 只做定位 + 逐字摘录，不生成归类意见。
    整个功能的安全底线是"原文校验"——AI 引用的句子必须逐字出现在正文里，
    否则拿去跟海关讲会翻车。不联网：fetch 与 provider 都注入假的。
    """

    def _fetch(self, mapping):
        return lambda num, coll, date: mapping.get(num)

    def _rulings(self, *nums):
        return [{"裁定号": n, "来源": "NY", "日期": "2017-06-08",
                 "编码": ["8506.50.0000"], "链接": f"x/{n}"} for n in nums]

    def test_verbatim_quote_passes_verification(self):
        prov = DeepreadProvider(picks=[{
            "index": 0, "relevance": "high", "note": "不可充电锂电池",
            "quote": "The applicable subheading for the non-rechargeable lithium "
                     "battery packs will be 8506.50.0000, HTSUS."}])
        r = ai.deepread_precedents(
            "不可充电锂原电池", self._rulings("N286124"),
            provider=prov, fetch=self._fetch({"N286124": _FAKE_DOC}))
        self.assertNotIn("error", r)
        pick = r["精读"][0]
        self.assertEqual(pick["相似度"], "high")
        self.assertTrue(pick["摘录已核对"], "逐字引用应通过原文校验")

    def test_hallucinated_quote_flagged(self):
        """AI 编造/改写的引用必须被标记——这是防止把假原话拿去报关的关键闸门"""
        prov = DeepreadProvider(picks=[{
            "index": 0, "relevance": "high", "note": "x",
            "quote": "The battery is classified under 9999.99.9999 per this ruling."}])
        r = ai.deepread_precedents(
            "锂电池", self._rulings("N286124"),
            provider=prov, fetch=self._fetch({"N286124": _FAKE_DOC}))
        self.assertFalse(r["精读"][0]["摘录已核对"], "正文里没有的句子必须标存疑")

    def test_whitespace_normalized_before_match(self):
        """AI 常把换行/多空格改成单空格，不该因此判为存疑"""
        prov = DeepreadProvider(picks=[{
            "index": 0, "relevance": "medium",
            "quote": "The rechargeable lithium-ion packs are classified under 8507.60.0020, HTSUS."}])
        r = ai.deepread_precedents(
            "可充电锂电池", self._rulings("N286124"),
            provider=prov,
            fetch=self._fetch({"N286124": _FAKE_DOC.replace(". ", ".\n  ")}))
        self.assertTrue(r["精读"][0]["摘录已核对"])

    def test_unreadable_ruling_goes_to_unread_not_faked(self):
        """正文拉不到的裁定归入'未读'并保留链接，绝不假装读过"""
        prov = DeepreadProvider(picks=[])
        r = ai.deepread_precedents(
            "锂电池", self._rulings("N286124", "N999999"),
            provider=prov,
            fetch=self._fetch({"N286124": _FAKE_DOC}))  # N999999 拉不到
        self.assertEqual([x["裁定号"] for x in r["精读"]], ["N286124"])
        self.assertEqual([x["裁定号"] for x in r["未读"]], ["N999999"])

    def test_all_unreadable_degrades(self):
        prov = DeepreadProvider(picks=[])
        r = ai.deepread_precedents(
            "锂电池", self._rulings("N1", "N2"),
            provider=prov, fetch=self._fetch({}))
        self.assertIn("error", r)
        self.assertEqual([x["裁定号"] for x in r["未读"]], ["N1", "N2"])

    def test_no_provider_degrades(self):
        r = ai.deepread_precedents("锂电池", self._rulings("N1"),
                                   provider=None, fetch=self._fetch({"N1": _FAKE_DOC}))
        # get_provider 未配置时返回 None → error
        self.assertTrue("error" in r or "精读" in r)

    def test_provider_failure_degrades(self):
        r = ai.deepread_precedents(
            "锂电池", self._rulings("N286124"),
            provider=DeepreadProvider(raise_it=True),
            fetch=self._fetch({"N286124": _FAKE_DOC}))
        self.assertIn("error", r)

    def test_bad_pick_index_skipped(self):
        """模型返回坏 index 不炸，对应行按'无摘录'处理"""
        prov = DeepreadProvider(picks=[{"index": None, "quote": "x"},
                                       {"index": 0, "relevance": "low",
                                        "quote": "8507.60.0020"}])
        r = ai.deepread_precedents(
            "锂电池", self._rulings("N286124"),
            provider=prov, fetch=self._fetch({"N286124": _FAKE_DOC}))
        self.assertNotIn("error", r)
        self.assertEqual(len(r["精读"]), 1)

    def test_relevance_sorted_high_first(self):
        docs = {"A": _FAKE_DOC, "B": _FAKE_DOC, "C": _FAKE_DOC}
        prov = DeepreadProvider(picks=[
            {"index": 0, "relevance": "low", "quote": ""},
            {"index": 1, "relevance": "high", "quote": ""},
            {"index": 2, "relevance": "medium", "quote": ""}])
        r = ai.deepread_precedents(
            "x", self._rulings("A", "B", "C"), provider=prov, fetch=self._fetch(docs))
        self.assertEqual([x["相似度"] for x in r["精读"]], ["high", "medium", "low"])


class TestParseDoc(unittest.TestCase):
    """正文解析：PDF / OLE2 魔数分派 + 质量断言"""

    def test_ole2_extracts_printable_runs(self):
        import cross
        # 伪 OLE2：魔数 + 二进制噪声夹着明文正文
        data = (b"\xd0\xcf\x11\xe0" + b"\x00\x01\x02" * 50
                + b"The applicable subheading will be 8506.50.0000, HTSUS. "
                + b"This is a lithium battery classification ruling. " * 8
                + b"\x00\xff" * 30)
        txt = cross._parse_doc_bytes(data)
        self.assertIsNotNone(txt)
        self.assertIn("8506.50.0000", txt)

    def test_rejects_short_or_codeless(self):
        import cross
        self.assertIsNone(cross._parse_doc_bytes(b"\xd0\xcf\x11\xe0short"))
        self.assertIsNone(cross._parse_doc_bytes(
            b"\xd0\xcf\x11\xe0" + b"lots of text but no hts code at all here " * 20))

    def test_unknown_magic_returns_none(self):
        import cross
        self.assertIsNone(cross._parse_doc_bytes(b"GIF89a whatever"))
        self.assertIsNone(cross._parse_doc_bytes(b""))


if __name__ == "__main__":
    unittest.main()
