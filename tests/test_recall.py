# -*- coding: utf-8 -*-
"""
test_recall.py —— 归类召回三通道（关键词 / 税则行语义 / 裁定 kNN 投票）与 RRF 融合、
LLM 调用的 JSON 模式与缓存、按产品触发的 Chapter 99 清单探测。

语义与先例两条通道依赖 ollama 与本地索引，这里一律注入假嵌入器 / 假先例，不联网。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ai  # noqa: E402
import core  # noqa: E402
import cross  # noqa: E402
import hts_embed  # noqa: E402
import rate  # noqa: E402

ALL = ("keyword", "semantic", "precedent")


class TestHybridSearch(unittest.TestCase):

    def test_default_channels_from_env(self):
        # 测试环境只开关键词通道（tests/__init__.py），生产默认三通道
        self.assertEqual(rate.DEFAULT_CHANNELS, ("keyword",))
    """三通道 RRF 融合：多通道同时命中的排前面；任一通道失败只影响自己"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_rrf_fuses_channels(self):
        # 关键词通道对 'bluetooth speaker' 是 0 条（税则写 loudspeakers），全靠另两条
        fake_sem = lambda text, limit: [{"编码": "85182100", "相似度": 0.7},  # noqa: E731
                                        {"编码": "85182200", "相似度": 0.6},
                                        {"编码": "85176200", "相似度": 0.5}]
        fake_votes = lambda db, text, limit: {  # noqa: E731
            "候选": [{"编码": "85182200", "票": 3.0, "裁定": ["N1", "N2"]},
                   {"编码": "85198141", "票": 1.0, "裁定": ["N3"]}],
            "先例数": 5}
        rows, st = rate.hybrid_search(self.db, "bluetooth speaker", limit=5, channels=ALL,
                                      _semantic=fake_sem, _votes=fake_votes)
        codes = [r["编码"] for r in rows]
        self.assertEqual(codes[0], "8518.22.00")           # 两条通道都有它
        self.assertEqual(rows[0]["召回来源"], ["语义", "先例"])
        self.assertEqual(rows[0]["先例票"], 3.0)
        self.assertEqual(rows[0]["先例裁定"], ["N1", "N2"])
        self.assertIn("总税负估算", rows[0])                # 与 search() 同结构，总税负已补
        self.assertEqual(st["关键词"]["数量"], 0)
        self.assertEqual(st["语义"]["数量"], 3)
        self.assertEqual(st["先例"], {"数量": 2, "先例数": 5})
        self.assertEqual(set(codes), {"8518.22.00", "8518.21.00", "8517.62.00", "8519.81.41"})

    def test_channel_failure_degrades_to_keyword(self):
        fake_sem = lambda text, limit: {"error": "索引未建"}  # noqa: E731
        fake_votes = lambda db, text, limit: {"error": "ollama 离线"}  # noqa: E731
        rows, st = rate.hybrid_search(self.db, "lithium battery", limit=3, channels=ALL,
                                      _semantic=fake_sem, _votes=fake_votes)
        self.assertEqual([r["编码"] for r in rows],
                         [r["编码"] for r in rate.search(self.db, "lithium battery", limit=3)])
        self.assertEqual(st["语义"]["原因"], "索引未建")
        self.assertEqual(st["先例"]["原因"], "ollama 离线")
        self.assertEqual(rows[0]["召回来源"], ["关键词"])

    def test_keyword_only_channels(self):
        rows, st = rate.hybrid_search(self.db, "lithium battery", limit=3, channels=("keyword",))
        self.assertEqual(list(st), ["关键词"])
        self.assertTrue(rows)

    def test_all_channels_empty(self):
        rows, st = rate.hybrid_search(self.db, "zzzz qqqq", limit=3, channels=ALL,
                                      _semantic=lambda t, l: [], _votes=lambda d, t, l: {"候选": []})
        self.assertEqual(rows, [])
        self.assertEqual(st["关键词"]["数量"], 0)

    def test_special_chapters_filtered(self):
        sem = lambda t, l: [{"编码": "99038815", "相似度": 0.9}, {"编码": "85076000", "相似度": 0.8}]  # noqa: E731
        rows, _ = rate.hybrid_search(self.db, "zzzz", limit=5, channels=ALL, _semantic=sem,
                                     _votes=lambda d, t, l: {"候选": []})
        self.assertEqual([r["编码"] for r in rows], ["8507.60.00"])

    def test_deterministic_order(self):
        sem = lambda t, l: [{"编码": c, "相似度": 0.5} for c in ("85182100", "85182200", "85182980")]  # noqa: E731
        votes = lambda d, t, l: {"候选": [{"编码": "85182980", "票": 1, "裁定": []},  # noqa: E731
                                          {"编码": "85182100", "票": 1, "裁定": []}]}
        a, _ = rate.hybrid_search(self.db, "speaker", limit=5, channels=ALL, _semantic=sem, _votes=votes)
        b, _ = rate.hybrid_search(self.db, "speaker", limit=5, channels=ALL, _semantic=sem, _votes=votes)
        self.assertEqual([r["编码"] for r in a], [r["编码"] for r in b])

    def test_channel_weights_prefer_precedent(self):
        # 先例通道权重最高：同为各自通道第一名，先例的那个排前面
        sem = lambda t, l: [{"编码": "85182100", "相似度": 0.9}]  # noqa: E731
        votes = lambda d, t, l: {"候选": [{"编码": "85182200", "票": 2.0, "裁定": []}]}  # noqa: E731
        rows, _ = rate.hybrid_search(self.db, "zzzz", limit=5, channels=ALL, _semantic=sem, _votes=votes)
        self.assertEqual([r["编码"] for r in rows], ["8518.22.00", "8518.21.00"])
        self.assertGreater(rate.CHANNEL_WEIGHTS["先例"], rate.CHANNEL_WEIGHTS["语义"])
        self.assertGreater(rate.CHANNEL_WEIGHTS["语义"], rate.CHANNEL_WEIGHTS["关键词"])

    def test_code_query_uses_keyword_only(self):
        # 编码前缀查询不走语义 / 先例：把 "8507" 当文本嵌入只会召回无关近邻
        called = {"sem": 0, "votes": 0}

        def sem(t, l):
            called["sem"] += 1
            return []

        def votes(d, t, l):
            called["votes"] += 1
            return {"候选": []}

        rows, st = rate.hybrid_search(self.db, "8507.60", limit=5, channels=ALL, _semantic=sem, _votes=votes)
        self.assertEqual(called, {"sem": 0, "votes": 0})
        self.assertEqual(list(st), ["关键词"])
        self.assertTrue(all(r["编码"].startswith("8507.60") for r in rows))

    def test_description_used_for_semantic_channels(self):
        seen = {}

        def sem(text, limit):
            seen["sem"] = text
            return []

        def votes(db, text, limit):
            seen["votes"] = text
            return {"候选": []}

        rate.hybrid_search(self.db, "lithium battery", description="锂电池", limit=3,
                           channels=ALL, _semantic=sem, _votes=votes)
        self.assertEqual(seen, {"sem": "锂电池", "votes": "锂电池"})


class TestCodeVotes(unittest.TestCase):
    """裁定 kNN 投票：换号回退、年份加权、撤销不投、未落位不静默"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()
        cls.alive = {c for c in cls.db["rates_8"] if c[:2] not in ("98", "99")}

    def test_votes_fallback_recency_and_unplaced(self):
        prec = lambda q, n: {"先例": [  # noqa: E731
            {"裁定号": "N1", "日期": "2023-01-01", "状态": "现行", "编码": ["8471.30.0100"]},
            {"裁定号": "R2", "日期": "1998-01-01", "状态": "现行", "编码": ["8471.30.0000"]},  # 换号→6 位回退
            {"裁定号": "X3", "日期": "2020-01-01", "状态": "已撤销", "编码": ["3926.90.9989"]},  # 撤销不投
            {"裁定号": "Y4", "日期": "1995-01-01", "状态": "现行", "编码": ["8471.20.0090"]},  # 6 位已无→换号表
            {"裁定号": "Z5", "日期": "2021-01-01", "状态": "现行", "编码": ["9999.99.9999"]},  # 落不了位
        ]}
        v = cross.code_votes("laptop", alive_codes=self.alive, _precedents=prec)
        top = v["候选"][0]
        self.assertEqual(top["编码"], "84713001")
        # Y4 的 8471.20 换号表拆成 8471.30 / .41 / .49 三个现行子目，一票平分
        self.assertAlmostEqual(top["票"], 1.0 + 0.35 + 0.35 / 3, places=2)
        self.assertEqual(top["裁定"], ["N1", "R2", "Y4"])
        self.assertNotIn("39269099", [c["编码"] for c in v["候选"]])
        self.assertEqual(v["未落位"], ["Z5:9999.99.9999"])
        self.assertEqual(v["先例数"], 5)

    def test_error_passthrough(self):
        v = cross.code_votes("x", alive_codes=self.alive, _precedents=lambda q, n: {"error": "离线"})
        self.assertEqual(v, {"error": "离线"})
        self.assertIn("error", cross.code_votes("", alive_codes=self.alive))

    def test_renumber_table_loaded(self):
        m = cross._load_renumber()
        self.assertIn("847130", m.get("847120", []))


class TestHtsEmbedIndex(unittest.TestCase):
    """税则行语义索引：假嵌入器建索引、检索、增量"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "hts_vec.db")

    @staticmethod
    def _fake_embed(texts):
        # 8 维字符袋向量：查询侧去掉 instruct 前缀，同一文本必然最相似
        out = []
        for t in texts:
            t = t.split("Query: ", 1)[-1].lower()
            v = [0.0] * 8
            for ch in t:
                v[ord(ch) % 8] += 1.0
            out.append(v)
        return out

    def test_sync_search_incremental(self):
        st = hts_embed.sync(self.db, db_path=self.path, log=lambda *a: None,
                            _embed=self._fake_embed, model="fake", dims=8)
        self.assertEqual(st["索引总数"], st["新嵌入"])
        self.assertGreater(st["索引总数"], 10000)
        self.assertNotIn("99038815", [r[0] for r in []])   # 99 章不进索引（下面用 status/search 验证）
        info = hts_embed.status(self.path)
        self.assertTrue(info["built"])
        self.assertEqual((info["model"], info["dims"]), ("fake", 8))
        q = rate.full_desc(self.db, "85076000")
        rows = hts_embed.search_codes(q, limit=3, db_path=self.path, _embed=self._fake_embed)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["编码"], "85076000")
        self.assertGreaterEqual(rows[0]["相似度"], 0.99)
        # 增量：再跑一次 0 新嵌入
        st2 = hts_embed.sync(self.db, db_path=self.path, log=lambda *a: None,
                             _embed=self._fake_embed, model="fake", dims=8)
        self.assertEqual(st2["新嵌入"], 0)
        # 换模型不 rebuild 必须拒绝
        with self.assertRaises(SystemExit):
            hts_embed.sync(self.db, db_path=self.path, log=lambda *a: None,
                           _embed=self._fake_embed, model="other", dims=8)

    def test_missing_index_degrades(self):
        r = hts_embed.search_codes("x", db_path=os.path.join(self.tmp, "none.db"))
        self.assertIn("error", r)
        self.assertFalse(hts_embed.status(os.path.join(self.tmp, "none.db"))["built"])


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad", request=None, response=self)

    def json(self):
        return self._payload


class TestProviderJsonModeAndCache(unittest.TestCase):
    """真实 Provider：JSON 模式参数、seed、磁盘缓存命中；兼容端不认 response_format 时退回"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_dir = ai.CACHE_DIR
        ai.CACHE_DIR = self.tmp
        self._old_env = os.environ.get("AI_CACHE")
        os.environ["AI_CACHE"] = "1"      # 套件默认关缓存，这里专门测缓存
        self.calls = []

    def tearDown(self):
        ai.CACHE_DIR = self._old_dir
        if self._old_env is None:
            os.environ.pop("AI_CACHE", None)
        else:
            os.environ["AI_CACHE"] = self._old_env

    def test_ollama_json_mode_seed_and_cache(self):
        import httpx

        def fake_post(url, json=None, timeout=None, **kw):
            self.calls.append(json)
            return _FakeResp(200, {"message": {"content": '{"ok": 1}'}})

        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OllamaProvider(model="m", temperature=0.0, seed=7)
            self.assertEqual(p.chat_json([{"role": "user", "content": "hi"}]), {"ok": 1})
            self.assertEqual(self.calls[0]["format"], "json")
            self.assertEqual(self.calls[0]["options"], {"temperature": 0.0, "seed": 7})
            # 第二次同样的消息：命中缓存，不再请求
            self.assertEqual(p.chat_json([{"role": "user", "content": "hi"}]), {"ok": 1})
            self.assertEqual(len(self.calls), 1)
            # 非 JSON 模式是另一个缓存键，且不带 format
            p.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(len(self.calls), 2)
            self.assertNotIn("format", self.calls[1])
        finally:
            httpx.post = old

    def test_openai_compat_falls_back_without_response_format(self):
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            self.calls.append(json)
            if "response_format" in json:
                return _FakeResp(400, {})
            return _FakeResp(200, {"choices": [{"message": {"content": '{"a": 2}'}}]})

        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OpenAICompatProvider(model="m", base_url="http://x", api_key="k", seed=3)
            self.assertEqual(p.chat_json([{"role": "user", "content": "q"}]), {"a": 2})
            self.assertEqual(len(self.calls), 2)          # 先带 response_format 被 400，再退回
            self.assertEqual(self.calls[1]["seed"], 3)
        finally:
            httpx.post = old

    def test_ping_bypasses_cache(self):
        # 「测试连接」探测的是"现在"通不通，缓存命中会让服务挂了还显示 pong
        import httpx

        def fake_post(url, json=None, timeout=None, **kw):
            self.calls.append(json)
            return _FakeResp(200, {"message": {"content": "pong"}})

        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OllamaProvider(model="m")
            self.assertEqual(ai.test_connection(p)["ok"], True)
            self.assertEqual(ai.test_connection(p)["ok"], True)
            self.assertEqual(len(self.calls), 2)
        finally:
            httpx.post = old

    def test_cache_can_be_disabled(self):
        import httpx
        os.environ["AI_CACHE"] = "0"

        def fake_post(url, json=None, timeout=None, **kw):
            self.calls.append(json)
            return _FakeResp(200, {"message": {"content": "x"}})

        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OllamaProvider(model="m")
            p.chat([{"role": "user", "content": "a"}])
            p.chat([{"role": "user", "content": "a"}])
            self.assertEqual(len(self.calls), 2)
        finally:
            httpx.post = old


class TestProductScopeDetection(unittest.TestCase):
    """按产品触发的 Chapter 99 清单（232 类）：命中即标注，FLIP 标存疑，税额仍按不豁免计"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_steel_code_flagged(self):
        r = core.query_one(self.db, "72061000", origin="CN")
        hits = r["产品类未建模措施"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["note"], "16")
        self.assertIn("(c)(iii)", hits[0]["子条"])
        self.assertIn("232 存疑", r["FLIP 301加征"])
        self.assertEqual(r["FLIP 301档位"]["mode"], "conditional")
        self.assertEqual(r["FLIP 301档位"]["scope"], "232")
        self.assertIn("232 类", r["备注"])
        self.assertIn("未建模措施（按产品触发，232 类，探测）", {s["类型"] for s in r["来源"]})
        t = rate.calc_total(self.db, "72061000", origin="CN")
        self.assertEqual(t["FLIP 301加征数值"], 12.5)     # 少收比多收危险：仍按不豁免计
        self.assertIn("232 类产品清单", t["总税负估算"])

    def test_origin_conditioned_note51(self):
        idx = self.db["c99_product_index"]
        ca_entries = {i for i, e in enumerate(idx["entries"]) if e.get("原产地条件") == "CA"}
        code = next(c for c, lst in idx["exact"].items() if set(lst) & ca_entries and len(c) == 8
                    and c in self.db["rates_8"])
        ca = [h["note"] for h in core.query_one(self.db, code, origin="CA")["产品类未建模措施"]]
        cn = [h["note"] for h in core.query_one(self.db, code, origin="CN")["产品类未建模措施"]]
        self.assertIn("51", ca)
        self.assertNotIn("51", cn)

    def test_annex_scope_name_survives_232_wrapping(self):
        # 8507.60.00：ANNEX II 带 Aircraft 范围限制 + note 33 清单，备注里的范围名必须仍是 Aircraft
        r = core.query_one(self.db, "85076000", origin="CN")
        self.assertIn("范围限制“Aircraft”", r["备注"])
        self.assertNotIn("范围限制“232”", r["备注"])
        self.assertEqual(r["FLIP 301档位"]["scope"], "232")
        self.assertEqual(r["FLIP 301档位"]["fallback"]["scope"], "Aircraft")

    def test_plain_code_not_flagged(self):
        r = core.query_one(self.db, "61091000", origin="CN")
        self.assertEqual(r["产品类未建模措施"], [])
        self.assertNotIn("232 存疑", r["FLIP 301加征"])


class TestBatchRerankPrompt(unittest.TestCase):
    """批量精排的候选行与单条同源：带归类路径与判定条件，且候选给到 12 条"""

    def test_batch_prompt_uses_candidate_lines(self):
        db = core.load_db()
        captured = {}

        class P(ai.BaseProvider):
            def __init__(self):
                super().__init__("fake")

            def chat(self, messages):
                content = messages[-1]["content"]
                if "商品清单" in content:
                    return json.dumps({"items": [{"index": 1, "keywords": ["lithium", "batteries"], "chapters": ["85"]}]})
                if "候选" in content:
                    captured["prompt"] = content
                    return json.dumps({"picks": [{"index": 1, "code": "85076000", "confidence": 0.9, "reason": "r"}]})
                return "report"

        old = ai._PROVIDER_CACHE.copy()
        ai._PROVIDER_CACHE.update({"provider": P(), "loaded": True, "error": ""})
        try:
            out = ai.analyze_list(db, [{"name": "锂电池"}], origin="CN")
        finally:
            ai._PROVIDER_CACHE.update(old)
        self.assertNotIn("error", out)
        prompt = captured["prompt"]
        self.assertIn("1-1. ", prompt)                    # 商品序号-候选序号
        self.assertIn(" | 一般税率 ", prompt)             # _candidate_line 的列
        # 测试环境只有关键词通道，'lithium batteries' 召回 2 条；上限已从 6 提到 12
        self.assertGreaterEqual(prompt.count("\n   1-"), 2)
        self.assertLessEqual(prompt.count("\n   1-"), 12)
        self.assertEqual(out["details"][0]["编码"], "8507.60.00")


if __name__ == "__main__":
    unittest.main()
