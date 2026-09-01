# -*- coding: utf-8 -*-
"""
test_cross.py —— CBP 裁定先例检索（cross.py）

**不联网**：整个 _get 被替换掉，样本取自 rulings.cbp.gov 的真实响应（已裁剪字段值）。
测试联网就成了在测 CBP 的可用性而不是本模块的逻辑，而且会让 CI 随对方抖动而红。

锁四件事：
  1. 撤销/修改状态必须被识别并显式说明——引用一条已撤销的裁定比不引用更糟
  2. 10 位统计编码与本地 8 位候选的前缀比对
  3. 现行的排在失效的前面，且现行组内部保持 CROSS 的相关度顺序
  4. 接口失败一律降级为 {"error": ...}，绝不抛异常打断本地查询
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import cross


def raw(number, tariffs, date="2019-08-29", subject="x", collection="ny",
        revoked_by=None, modified_by=None, op_revoked=False):
    """构造一条 CROSS 原始记录（字段名与真实响应一致）"""
    return {
        "id": 0, "rulingNumber": number, "subject": subject,
        "categories": "Classification", "rulingDate": date + "T00:00:00",
        "isUsmca": False, "isNafta": False, "collection": collection,
        "relatedRulings": [], "modifiedBy": modified_by or [], "modifies": [],
        "revokedBy": revoked_by or [], "revokes": [],
        "tariffs": tariffs, "operationallyRevoked": op_revoked,
        "commodityGrouping": "",
    }


class CrossTestCase(unittest.TestCase):
    """把网络层与缓存目录都换掉，测试之间互不影响"""

    def setUp(self):
        self._get = cross._get
        self._dir = cross.CACHE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        cross.CACHE_DIR = self._tmp.name
        self.calls = []

        # 默认就把网络堵死：忘了调 stub() 的用例会立刻炸，而不是悄悄发真请求
        # 把测试变成对 CBP 可用性的测试
        def _blocked(path, params):
            raise AssertionError(f"测试试图访问网络：{path} {params}")
        cross._get = _blocked

    def tearDown(self):
        cross._get = self._get
        cross.CACHE_DIR = self._dir
        self._tmp.cleanup()

    def stub(self, rulings, total=None):
        def _fake(path, params):
            self.calls.append((path, dict(params)))
            return {"rulings": rulings, "totalHits": total if total is not None else len(rulings)}
        cross._get = _fake

    def fail_with(self, exc):
        def _fake(path, params):
            self.calls.append((path, dict(params)))
            raise exc
        cross._get = _fake


class TestStatus(CrossTestCase):
    """撤销/修改状态：这是引用先例前唯一不能省的检查"""

    def test_current(self):
        self.stub([raw("N305619", "8507.60.0020")])
        r = cross.search("battery")
        self.assertEqual(r["裁定"][0]["状态"], "现行")
        self.assertEqual(r["裁定"][0]["状态说明"], "")

    def test_revoked_names_the_revoking_ruling(self):
        """光说"已撤销"不够，要说出被谁撤销——用户得去读那一条"""
        self.stub([raw("N232914", "8507.60.0020", revoked_by=["H249299"])])
        it = cross.search("battery")["裁定"][0]
        self.assertEqual(it["状态"], "已撤销")
        self.assertIn("H249299", it["状态说明"])
        self.assertIn("不可作为归类依据", it["状态说明"])

    def test_multiple_revoking_rulings_all_listed(self):
        self.stub([raw("J81308", "8507.60.0020", revoked_by=["966328", "966329"])])
        note = cross.search("battery")["裁定"][0]["状态说明"]
        self.assertIn("966328", note)
        self.assertIn("966329", note)

    def test_operationally_revoked(self):
        self.stub([raw("X1", "8507.60.0020", op_revoked=True)])
        self.assertEqual(cross.search("b")["裁定"][0]["状态"], "已撤销")

    def test_modified_is_not_revoked(self):
        """已修改的结论可能仍有效，不能和撤销混为一谈，但必须提示连同修改件读"""
        self.stub([raw("H326262", "8507.60.0020", modified_by=["H341220"])])
        it = cross.search("b")["裁定"][0]
        self.assertEqual(it["状态"], "已修改")
        self.assertIn("H341220", it["状态说明"])

    def test_revoked_wins_over_modified(self):
        """既撤销又修改时按撤销展示——撤销是更强的结论，直接不可引用"""
        self.stub([raw("X", "8507.60.0020", revoked_by=["A"], modified_by=["B"])])
        self.assertEqual(cross.search("b")["裁定"][0]["状态"], "已撤销")


class TestCodeMatching(CrossTestCase):
    """CBP 写 10 位统计编码，本地候选是 8 位"""

    def test_ten_digit_matches_eight_digit_candidate(self):
        r = cross._norm_ruling(raw("N", "8507.60.0020"))
        self.assertEqual(cross.match_codes(r, ["8507.60.00"]), ["8507.60.00"])

    def test_eight_digit_ruling_matches(self):
        """老裁定只写到 8 位，反向前缀也要能比上"""
        r = cross._norm_ruling(raw("N", "8507.90.40"))
        self.assertEqual(cross.match_codes(r, ["8507.90.40"]), ["8507.90.40"])

    def test_sibling_subheading_does_not_match(self):
        """8507.80.80 与 8507.60.00 同品目不同子目，不能算命中"""
        r = cross._norm_ruling(raw("N", "8507.80.8000"))
        self.assertEqual(cross.match_codes(r, ["8507.60.00"]), [])

    def test_multi_tariff_ruling_matches_several_candidates(self):
        """N286124 的真实形态：一条裁定同时判了 8506 与 8507，正是分界线所在"""
        r = cross._norm_ruling(raw("N286124", "8506.50.0000, 8507.60.0020"))
        self.assertEqual(sorted(cross.match_codes(r, ["8507.60.00", "8506.50.00"])),
                         ["8506.50.00", "8507.60.00"])

    def test_short_candidate_ignored(self):
        """不足 8 位的候选不参与比对，否则 '85' 会命中整章"""
        r = cross._norm_ruling(raw("N", "8507.60.0020"))
        self.assertEqual(cross.match_codes(r, ["8507"]), [])


class TestPrecedents(CrossTestCase):

    def test_split_matched_and_others(self):
        self.stub([
            raw("A", "8507.60.0020"),
            raw("B", "8507.80.8000"),
            raw("C", "3926.90.9989"),
        ])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]], ["A"])
        codes = {g["编码"]: g for g in r["候选外编码"]}
        self.assertIn("8507.80.80", codes)
        self.assertIn("3926.90.99", codes)

    def test_same_heading_flagged_and_ranked_first(self):
        """
        回归：早先按 4 位品目归组，8507.80.80 会显示成"候选外品目 8507"——
        而 8507 正是候选所在的品目，读起来自相矛盾。改按 8 位归组并标同品目。
        """
        self.stub([raw("A", "8507.60.0020")]
                  + [raw(f"X{i}", "3926.90.9989") for i in range(5)]
                  + [raw("B", "8507.80.8000")])
        r = cross.precedents("battery", ["8507.60.00"])
        first = r["候选外编码"][0]
        self.assertEqual(first["编码"], "8507.80.80")
        self.assertTrue(first["同品目"], "同品目的应排在前面，它最可能是漏掉的那个子目")
        self.assertFalse(
            [g for g in r["候选外编码"] if g["编码"] == "3926.90.99"][0]["同品目"])

    def test_chapter_99_excluded_from_others(self):
        """9903 是加征条款不是归类结论，混进"候选外编码"会淹没真信号"""
        self.stub([raw("A", "9903.88.15")])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual(r["候选外编码"], [])

    def test_rulings_without_tariffs_skipped(self):
        """无编码的多是原产地/退税裁定，与归类无关"""
        self.stub([raw("A", ""), raw("B", "8507.60.0020")])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]], ["B"])

    def test_revoked_ranked_last_but_kept(self):
        """
        失效的不隐藏——直接藏掉会让用户以为"无先例"。但要排在现行的后面。
        """
        self.stub([
            raw("OLD", "8507.60.0020", revoked_by=["H1"]),
            raw("NEW", "8507.60.0020"),
        ])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]], ["NEW", "OLD"])
        self.assertIn("撤销", r["提示"])

    def test_relevance_order_preserved_within_current(self):
        """
        回归：早先多写了一次按日期排序，把 CROSS 的相关度顺序整个冲掉，
        最贴题的那条反而掉出前列。现行组内部必须保持对方返回的顺序。
        """
        self.stub([
            raw("FIRST", "8507.60.0020", date="2019-01-01"),
            raw("SECOND", "8507.60.0020", date="2012-01-01"),
            raw("THIRD", "8507.60.0020", date="2024-01-01"),
        ])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]],
                         ["FIRST", "SECOND", "THIRD"])

    def test_no_codes_returns_all(self):
        """不给候选编码时退化为普通检索，不做过滤"""
        self.stub([raw("A", "8507.60.0020"), raw("B", "3926.90.9989")])
        r = cross.precedents("battery")
        self.assertEqual(len(r["先例"]), 2)

    def test_limit_applied(self):
        self.stub([raw(f"N{i}", "8507.60.0020") for i in range(30)])
        self.assertEqual(len(cross.precedents("b", ["8507.60.00"], limit=5)["先例"]), 5)

    def test_no_match_says_why(self):
        self.stub([raw("A", "3926.90.9989")])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual(r["先例"], [])
        self.assertIn("未找到", r["提示"])

    def test_disclaimer_always_present(self):
        """裁定不是保护伞，这句话任何情况下都要在"""
        self.stub([raw("A", "8507.60.0020")])
        self.assertIn("不构成保护伞", cross.precedents("b", ["8507.60.00"])["提示"])


class TestRanking(CrossTestCase):
    """
    多条裁定结论冲突时该信哪条，层级说了算：HQ（总部）可以撤销/修改 NY 的裁定，
    反之不行——2399 条真实样本里 25 次撤销全部由 HQ 发起，NY 发起 0 次。
    """

    def test_hq_ranked_before_ny(self):
        self.stub([
            raw("N1", "8507.60.0020", collection="ny"),
            raw("H1", "8507.60.0020", collection="hq"),
            raw("N2", "8507.60.0020", collection="ny"),
        ])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]], ["H1", "N1", "N2"])

    def test_revoked_hq_still_after_current_ny(self):
        """失效压过层级：被撤销的 HQ 裁定也不能排在现行 NY 前面"""
        self.stub([
            raw("H_OLD", "8507.60.0020", collection="hq", revoked_by=["H_NEW"]),
            raw("N1", "8507.60.0020", collection="ny"),
        ])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]], ["N1", "H_OLD"])

    def test_relevance_preserved_within_same_tier(self):
        """同层级（都是现行 NY）内部必须保持 CROSS 的相关度顺序"""
        self.stub([raw(f"N{i}", "8507.60.0020", collection="ny") for i in range(4)])
        r = cross.precedents("battery", ["8507.60.00"])
        self.assertEqual([x["裁定号"] for x in r["先例"]],
                         ["N0", "N1", "N2", "N3"])

    def test_hierarchy_tip_present_with_matches(self):
        """"HQ 高于 NY、读全文事实段"必须随先例一起出现——列表页的信息量
        天然在鼓励扫一眼就抄，提示必须与之对冲"""
        self.stub([raw("A", "8507.60.0020")])
        tip = cross.precedents("b", ["8507.60.00"])["提示"]
        self.assertIn("事实描述", tip)
        self.assertIn("HQ 层级高于 NY", tip)

    def test_no_precedent_suggests_eruling(self):
        """CROSS 空手而归的新品类要给出口（预裁定），否则用户会回去硬翻税则猜一个"""
        self.stub([raw("A", "3926.90.9989")])
        self.assertIn("预裁定", cross.precedents("b", ["8507.60.00"])["提示"])


class TestHsVersionNote(CrossTestCase):
    """
    HS 每 5 年一修，老裁定的 6 位编码可能已被 WCO 改掉——而 CROSS 不会为此
    标记撤销：revoked 只防"结论被推翻"，防不住"编码被搬家"。
    """

    def test_recent_ruling_no_note(self):
        self.stub([raw("A", "8507.60.0020", date="2023-05-01")])
        self.assertEqual(cross.search("b")["裁定"][0]["版本提示"], "")

    def test_pre_2022_flags_hs2022_only(self):
        self.stub([raw("A", "8507.60.0020", date="2019-08-29")])
        note = cross.search("b")["裁定"][0]["版本提示"]
        self.assertIn("HS 2022", note)
        self.assertNotIn("HS 2017", note)

    def test_old_ruling_flags_all_missed_revisions(self):
        self.stub([raw("A", "8507.60.0020", date="2000-07-14")])
        note = cross.search("b")["裁定"][0]["版本提示"]
        for rev in ("HS 2012", "HS 2017", "HS 2022"):
            self.assertIn(rev, note)

    def test_boundary_uses_us_implementation_date(self):
        """HS 2022 在美国经总统公告于 2022-01-27 落地，界线取实施日而非 1 月 1 日"""
        self.stub([raw("A", "8507.60.0020", date="2022-01-26"),
                   raw("B", "8507.60.0020", date="2022-01-27")])
        rows = cross.search("b")["裁定"]
        self.assertIn("HS 2022", rows[0]["版本提示"])
        self.assertEqual(rows[1]["版本提示"], "")

    def test_missing_date_treated_as_oldest(self):
        """日期缺失按最老处理——宁可多提醒，不能让坏数据变成「无风险」"""
        self.stub([raw("A", "8507.60.0020", date="")])
        self.assertIn("HS 2012", cross.search("b")["裁定"][0]["版本提示"])

    def test_tip_mentions_version_risk(self):
        self.stub([raw("A", "8507.60.0020", date="2000-07-14")])
        tip = cross.precedents("b", ["8507.60.00"])["提示"]
        self.assertIn("HS 修订", tip)
        self.assertIn("不会为此标记撤销", tip)


class TestDeadCodes(CrossTestCase):
    """
    现行税则对账。全库实测（2026-09）：90 年代裁定引用的编码 42% 已不在
    现行税则，而 CROSS 对此零标记——撤销有红标，编码搬家什么标都没有。
    """

    ALIVE = {"85076000", "85065000"}

    def test_dead_code_flagged_alive_kept(self):
        self.stub([raw("N", "8507.60.0020, 8471.92.1000")])
        r = cross.precedents("battery", ["8507.60.00"], alive_codes=self.ALIVE)
        it = r["先例"][0]
        self.assertEqual(it["失效编码"], ["8471.92.1000"])
        self.assertIn("已不在现行税则", r["提示"])

    def test_all_alive_no_tip(self):
        self.stub([raw("N", "8507.60.0020")])
        r = cross.precedents("battery", ["8507.60.00"], alive_codes=self.ALIVE)
        self.assertEqual(r["先例"][0]["失效编码"], [])
        self.assertNotIn("已不在现行税则", r["提示"])

    def test_ch99_not_flagged(self):
        """9903 加征条款不在 rates_8 是正常的，标失效就是误报"""
        self.stub([raw("N", "8507.60.0020, 9903.88.15")])
        r = cross.precedents("battery", ["8507.60.00"], alive_codes=self.ALIVE)
        self.assertEqual(r["先例"][0]["失效编码"], [])

    def test_short_code_not_flagged(self):
        """6 位短码没法与 8 位表对账——宁可漏标不误标，误标会教用户不信这个警告"""
        self.stub([raw("N", "8507.60.0020, 850799")])
        r = cross.precedents("battery", ["8507.60.00"], alive_codes=self.ALIVE)
        self.assertEqual(r["先例"][0]["失效编码"], [])

    def test_no_alive_set_no_annotation(self):
        """没给现行码集合（本地税则库未建）时不装作核对过"""
        self.stub([raw("N", "8471.92.1000")])
        r = cross.precedents("battery", ["8471.92.10"])
        self.assertNotIn("失效编码", r["先例"][0])


class TestNormalize(CrossTestCase):

    def test_fields(self):
        self.stub([raw("N286124", "8506.50.0000, 8507.60.0020",
                       date="2017-06-08", subject="two battery packs")])
        it = cross.search("b")["裁定"][0]
        self.assertEqual(it["裁定号"], "N286124")
        self.assertEqual(it["日期"], "2017-06-08")
        self.assertEqual(it["来源"], "NY")
        self.assertEqual(it["编码"], ["8506.50.0000", "8507.60.0020"])
        self.assertEqual(it["链接"], "https://rulings.cbp.gov/ruling/N286124")
        self.assertIn("/api/getdoc/ny/2017/N286124.doc", it["全文链接"])

    def test_hq_collection(self):
        self.stub([raw("H316545", "8507.60.0020", collection="hq")])
        self.assertEqual(cross.search("b")["裁定"][0]["来源"], "HQ")


class TestDegradation(CrossTestCase):
    """AI 层的约定同样适用：本地税则查询是主链路，外部服务失败不能打断它"""

    def test_http_error_returns_error_dict(self):
        import httpx
        self.fail_with(httpx.ConnectError("boom"))
        r = cross.search("battery")
        self.assertIn("error", r)
        self.assertNotIn("裁定", r)

    def test_precedents_propagates_error(self):
        import httpx
        self.fail_with(httpx.ConnectError("boom"))
        self.assertIn("error", cross.precedents("battery", ["8507.60.00"]))

    def test_empty_term_rejected_without_network(self):
        self.stub([])
        self.assertIn("error", cross.search("   "))
        self.assertEqual(self.calls, [], "空检索词不该发出请求")

    def test_failure_is_cached_briefly(self):
        """离线时别让用户每点一次都干等一个超时"""
        import httpx
        self.fail_with(httpx.ConnectError("boom"))
        cross.search("battery")
        cross.search("battery")
        self.assertEqual(len(self.calls), 1, "失败结果应短暂缓存，不重复请求")


class TestCache(CrossTestCase):

    def test_second_call_hits_cache(self):
        self.stub([raw("A", "8507.60.0020")])
        a = cross.search("battery")
        b = cross.search("battery")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(a, b)

    def test_different_terms_not_confused(self):
        self.stub([raw("A", "8507.60.0020")])
        cross.search("battery")
        cross.search("glove")
        self.assertEqual(len(self.calls), 2)

    def test_no_cache_flag(self):
        self.stub([raw("A", "8507.60.0020")])
        cross.search("battery", use_cache=False)
        cross.search("battery", use_cache=False)
        self.assertEqual(len(self.calls), 2)

    def test_corrupt_cache_falls_back_to_request(self):
        """坏缓存不能变成坏答案"""
        self.stub([raw("A", "8507.60.0020")])
        cross.search("battery")
        for name in os.listdir(cross.CACHE_DIR):
            with open(os.path.join(cross.CACHE_DIR, name), "w") as f:
                f.write("{ not json")
        r = cross.search("battery")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(r["裁定"][0]["裁定号"], "A")


if __name__ == "__main__":
    unittest.main()
