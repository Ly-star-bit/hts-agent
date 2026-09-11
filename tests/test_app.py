# -*- coding: utf-8 -*-
"""
test_app.py —— Web API 集成测试（FastAPI TestClient）

运行：python -m unittest tests.test_app -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app as app_mod
import ai as ai_mod


class TestQueryAPI(unittest.TestCase):
    """原有查询接口不回归"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_query(self):
        r = self.client.post("/api/query", json={"text": "8507.60.00, 8703.80.00"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["stats"]["total"], 2)
        self.assertEqual(len(data["results"]), 2)

    def test_query_invalid(self):
        r = self.client.post("/api/query", json={"text": "没有编码"})
        self.assertEqual(r.status_code, 400)

    def test_info(self):
        r = self.client.get("/api/info")
        self.assertEqual(r.status_code, 200)
        self.assertIn("rates_8_count", r.json())

    def test_query_origin_vn(self):
        # 越南原产地：不适用 301，输出越南措施；不含强迫劳动检查字段（已移除）
        r = self.client.post("/api/query", json={"text": "8541.43.00", "origin": "VN"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        row = data["results"][0]
        self.assertEqual(row["原产地"], "越南")
        self.assertIn("不适用", row["301判定"])
        self.assertEqual(row["301加征"], "")
        self.assertIn("MFN", row["越南措施"])
        self.assertNotIn("强迫劳动", row)
        self.assertIn("+12.5%", row["FLIP 301加征"])  # 越南在 FLIP 301 12.5% 档（85414300 非豁免）
        self.assertEqual(data["stats"]["origin"], "越南")

    def test_query_origin_cn_default(self):
        # 默认中国：既有 301 判定 + flip/FLIP 301；不含强迫劳动检查字段（已移除）
        r = self.client.post("/api/query", json={"text": "8507.60.00"})
        self.assertEqual(r.status_code, 200)
        row = r.json()["results"][0]
        self.assertEqual(row["原产地"], "中国")
        self.assertEqual(row["301判定"], "是")
        self.assertIn("+25%", row["301加征"])
        self.assertEqual(len(row["301 flip历史"]), 1)  # 锂电池有 flip 历史
        # 8507.60.00 锂电池在 ANNEX II Part A，但带 Aircraft 范围限制（FRN 页 225）：
        # 只有民用航空器用锂电池豁免，普通锂电池照加 12.5%，故不是无条件"豁免"
        self.assertEqual(row["FLIP 301加征"], "+12.5%(范围存疑)")
        self.assertNotIn("强迫劳动", row)
        self.assertNotIn("强迫劳动提示", row)
        # 非豁免编码（光伏）仍按 12.5% 加征
        r2 = self.client.post("/api/query", json={"text": "8541.43.00"})
        self.assertEqual(r2.json()["results"][0]["FLIP 301加征"], "+12.5%")

    def test_query_no_uflpa_field(self):
        # 查询输出不再包含任何强迫劳动检查维度
        r = self.client.post("/api/query", json={"text": "8507.60.00, 85414300"})
        self.assertEqual(r.status_code, 200)
        for row in r.json()["results"]:
            self.assertNotIn("强迫劳动", row)
            self.assertNotIn("强迫劳动提示", row)

    def test_query_origin_consistency(self):
        # 连续两次查询结果一致（确定性）
        body = {"text": "8507.60.00, 85414300, 94035090", "origin": "CN"}
        r1 = self.client.post("/api/query", json=body)
        r2 = self.client.post("/api/query", json=body)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), r2.json())

    def test_upload_origin(self):
        # 文件上传带原产地参数
        import io as _io
        csv_bytes = "HS_CODE\n8507.60.00\n".encode("utf-8")
        r = self.client.post(
            "/api/upload",
            files={"file": ("codes.csv", csv_bytes, "text/csv")},
            data={"origin": "VN"},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["origin"], "VN")
        self.assertIn("不适用", data["results"][0]["301判定"])

    def test_estimate_origin_vn(self):
        # 估算接口：越南无 301 加征
        r = self.client.post("/api/estimate", json={"codes": ["85076000"], "origin": "VN"})
        self.assertEqual(r.status_code, 200)
        row = r.json()["results"][0]
        self.assertEqual(row["301加征数值"], 0.0)
        self.assertIn("原产地", row)
        self.assertEqual(row["原产地"], "越南")

    def test_query_origin_other_mfn(self):
        # 其他国家：固定 MFN 轨道，不叠加 301
        r = self.client.post("/api/query", json={"text": "8507.60.00", "origin": "IN"})
        self.assertEqual(r.status_code, 200)
        row = r.json()["results"][0]
        self.assertEqual(row["原产地"], "其他国家")
        self.assertIn("不适用", row["301判定"])
        self.assertEqual(row["301加征"], "")
        self.assertIn("其他国家通用轨道", row["越南措施"])
        self.assertEqual(r.json()["stats"]["origin"], "其他国家")


class TestSearchAPI(unittest.TestCase):
    """税率搜索与排序"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_search_lithium(self):
        r = self.client.post("/api/search", json={"keyword": "lithium ion battery", "sort": "tax_asc", "limit": 20})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreater(data["count"], 0)
        self.assertTrue(any("8507.60.00" in x["编码"] for x in data["results"]))

    def test_search_sort_tax(self):
        r = self.client.post("/api/search", json={"keyword": "apparel", "sort": "tax_asc", "limit": 50})
        data = r.json()
        vals = [x["等效从价数值"] for x in data["results"] if x["等效从价数值"] is not None]
        self.assertEqual(vals, sorted(vals))

    def test_search_with_unit_value(self):
        r = self.client.post("/api/search", json={"keyword": "apparel", "unit_value": 5.0, "limit": 10})
        self.assertEqual(r.status_code, 200)
        for x in r.json()["results"]:
            self.assertIn("总税负估算", x)


class TestEstimateAPI(unittest.TestCase):
    """成本估算"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_estimate_codes(self):
        r = self.client.post("/api/estimate", json={"codes": ["85076000", "01012100"]})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 2)
        for x in data["results"]:
            self.assertIn("总税负估算", x)
            self.assertIn("税率类型", x)

    def test_estimate_text(self):
        r = self.client.post("/api/estimate", json={"text": "8507.60.00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)

    def test_estimate_empty(self):
        r = self.client.post("/api/estimate", json={"codes": []})
        self.assertEqual(r.status_code, 400)


class TestChangesAPI(unittest.TestCase):
    """数据变动清单"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_changes(self):
        r = self.client.get("/api/changes")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("first_build") or "stats" in data)


class TestAIAPI(unittest.TestCase):
    """AI 系列接口（mock provider）"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        ai_mod.reset_provider_cache()

    def test_status_unconfigured(self):
        ai_mod._PROVIDER_CACHE["loaded"] = True
        ai_mod._PROVIDER_CACHE["provider"] = None
        r = self.client.get("/api/ai/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])

    def test_classify_mocked(self):
        class FakeProvider(ai_mod.BaseProvider):
            def __init__(self):
                super().__init__(model="fake")
                self._replies = [
                    json.dumps({"keywords": ["lithium", "battery"], "chapters": ["85"]}),
                    json.dumps({"picks": [{"code": "8507.60.00", "confidence": 0.9, "reason": "锂电池"}]}),
                ]

            def chat(self, messages):
                return self._replies.pop(0) if self._replies else "{}"

        ai_mod._PROVIDER_CACHE["provider"] = FakeProvider()
        ai_mod._PROVIDER_CACHE["loaded"] = True
        r = self.client.post("/api/ai/classify", json={"description": "锂电池"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertNotIn("error", data)
        self.assertEqual(data["candidates"][0]["编码"], "8507.60.00")

    def test_classify_no_provider(self):
        ai_mod._PROVIDER_CACHE["loaded"] = True
        ai_mod._PROVIDER_CACHE["provider"] = None
        r = self.client.post("/api/ai/classify", json={"description": "锂电池"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

    def test_ask_with_codes(self):
        class FakeProvider(ai_mod.BaseProvider):
            def __init__(self):
                super().__init__(model="fake")
                self._replies = [json.dumps({"interpretation": "解读"})]

            def chat(self, messages):
                return self._replies.pop(0) if self._replies else "{}"

        ai_mod._PROVIDER_CACHE["provider"] = FakeProvider()
        ai_mod._PROVIDER_CACHE["loaded"] = True
        r = self.client.post("/api/ai/ask", json={"question": "查一下 8507.60.00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "codes")

    def test_interpret_empty(self):
        r = self.client.post("/api/ai/interpret", json={"results": []})
        self.assertEqual(r.status_code, 400)

    def test_analyze_empty(self):
        r = self.client.post("/api/ai/analyze", json={"items": []})
        self.assertEqual(r.status_code, 400)


class TestAIConfigAPI(unittest.TestCase):
    """Web 端 AI 配置接口（读写 ai_config.json，测试后恢复）"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)
        # 备份真实配置
        cls._real_config = None
        cls._real_path = ai_mod.CONFIG_FILE
        if os.path.exists(cls._real_path):
            with open(cls._real_path, encoding="utf-8") as f:
                cls._real_config = f.read()

    @classmethod
    def tearDownClass(cls):
        # 恢复真实配置
        if cls._real_config is not None:
            with open(cls._real_path, "w", encoding="utf-8") as f:
                f.write(cls._real_config)
        else:
            if os.path.exists(cls._real_path):
                os.remove(cls._real_path)
        ai_mod.reset_provider_cache()

    def test_get_config_masked(self):
        r = self.client.get("/api/ai/config")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("provider", data)
        self.assertNotIn("api_key", data)  # 明文绝不返回

    def test_save_config(self):
        r = self.client.post("/api/ai/config", json={
            "provider": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-savetest1234567890",
        })
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["config"]["model"], "gpt-4o-mini")
        self.assertTrue(data["config"]["api_key_set"])
        # 配置完整（provider/base_url/model/api_key 齐全）→ 应显示已启用
        self.assertTrue(data["status"]["enabled"])
        self.assertEqual(data["config"]["base_url"], "https://api.openai.com/v1")

    def test_test_no_provider(self):
        # 关闭 provider 后测试连接应返回失败提示
        self.client.post("/api/ai/config", json={"provider": None})
        r = self.client.post("/api/ai/test")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])


class TestMeasuresConfigAPI(unittest.TestCase):
    """Web 端加征开关配置接口（读写 measures_config.json，测试后恢复）"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)
        import core
        cls._real_path = core.MEASURES_CONFIG
        with open(cls._real_path, encoding="utf-8") as f:
            cls._real_cfg = f.read()

    @classmethod
    def tearDownClass(cls):
        with open(cls._real_path, "w", encoding="utf-8") as f:
            f.write(cls._real_cfg)

    def test_get_config(self):
        r = self.client.get("/api/measures/config")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("cn301", data["measures"])
        self.assertIn("flip301", data["measures"])

    def test_save_config_and_effect(self):
        # 保存禁用 flip301 → 查询立即裁剪；保存启用 → 恢复
        r = self.client.post("/api/measures/config", json={"measures": {"cn301": True, "flip301": False}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["measures"]["flip301"])
        q = self.client.post("/api/query", json={"text": "8507.60.00", "origin": "CN"})
        self.assertNotIn("FLIP 301加征", q.json()["results"][0])
        # 恢复
        r2 = self.client.post("/api/measures/config", json={"measures": {"cn301": True, "flip301": True}})
        self.assertTrue(r2.json()["measures"]["flip301"])
        q2 = self.client.post("/api/query", json={"text": "8541.43.00", "origin": "CN"})
        self.assertEqual(q2.json()["results"][0]["FLIP 301加征"], "+12.5%")


class TestSourceAPI(unittest.TestCase):
    """来源追溯：源文件清单 / PDF 内联 / CSV 行上下文 / 查询结果来源字段"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_source_list(self):
        r = self.client.get("/api/source/list")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        keys = {f["key"] for f in data["files"]}
        # ch99_pdf 是 301 排除判定的来源，也要列进"数据来源"弹窗——
        # 参与判定的官方文件全部在册，是这个接口存在的意义
        self.assertEqual(keys, {"htsdata", "ustr_pdf", "flip_frn", "ch99_pdf"})
        for f in data["files"]:
            # Chapter 99 PDF 有 13MB，按 .gitignore 不入库，新克隆的仓库里没有这份，
            # 所以只要求接口如实报告存在与否，不要求文件一定在
            if f["key"] != "ch99_pdf":
                self.assertTrue(f["exists"], f"{f['key']} 源文件应存在")
            self.assertIn("exists", f)
        self.assertIn("built_at", data["meta"])

    def test_source_pdf_inline(self):
        r = self.client.get("/api/source/pdf/ustr_pdf")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/pdf", r.headers["content-type"])
        self.assertIn("inline", r.headers.get("content-disposition", ""))
        r2 = self.client.get("/api/source/pdf/flip_frn")
        self.assertEqual(r2.status_code, 200)

    def test_source_pdf_unknown(self):
        r = self.client.get("/api/source/pdf/nonexistent")
        self.assertEqual(r.status_code, 404)

    def test_source_csv_line(self):
        r = self.client.get("/api/source/csv?line=30187")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["target"], 30187)
        self.assertGreater(d["total_lines"], 30000)
        self.assertTrue(any("9025.19.80.85" in c["内容"] for c in d["context"]))

    def test_source_csv_bad_line(self):
        r = self.client.get("/api/source/csv?line=99999999")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["target"])

    def test_query_result_has_source_field(self):
        r = self.client.post("/api/query", json={"text": "9025.19.8085, 8507.60.00"})
        self.assertEqual(r.status_code, 200)
        for row in r.json()["results"]:
            self.assertIn("来源", row)
            self.assertIsInstance(row["来源"], list)
            self.assertGreaterEqual(len(row["来源"]), 2)  # 基础税率 + 至少一项措施
        row = r.json()["results"][0]
        keys = {s["key"] for s in row["来源"]}
        self.assertIn("htsdata", keys)
        base = next(s for s in row["来源"] if s["key"] == "htsdata")
        self.assertIn("行", base["位置"])  # 应带 CSV 行号
        self.assertIn("USITC", base["文件"])
        # 9025.19.80 在 ANNEX II Part A（Aircraft 范围限制）→ FLIP 来源应带范围限制与页码
        flip = next(s for s in row["来源"] if s["类型"] == "FLIP 301")
        self.assertEqual(flip["范围限制"], "Aircraft")
        self.assertIn("页", flip["位置"])
        self.assertIn("FLIP 301", flip["文件"])


class TestCompareAPI(unittest.TestCase):
    """归类对比接口：候选并列 + 跨章分歧提示"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_cross_chapter_compare(self):
        r = self.client.post("/api/compare",
                             json={"codes": ["3926.20.60", "6201.40.35"]})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["跨章"])
        self.assertEqual(d["章列表"], ["39", "62"])
        self.assertIn("预裁定", d["分歧提示"])
        self.assertEqual(len(d["候选"]), 2)
        for it in d["候选"]:
            for k in ("完整品名", "一般税率", "判定条件", "证据清单",
                      "等效从价", "总税负估算", "301判定"):
                self.assertIn(k, it)

    def test_base_rate_low_but_total_higher(self):
        # 3926.20.60 基础 Free 却因 301 加征使总税负高于 6201.40.35——
        # 正是"按最低税率挑编码"会踩的坑，接口必须把总税负一并给出
        d = self.client.post("/api/compare",
                             json={"codes": ["3926.20.60", "6201.40.35"]}).json()
        by = {i["编码"]: i for i in d["候选"]}
        self.assertEqual(by["39262060"]["等效从价"], "0%")
        self.assertEqual(by["62014035"]["等效从价"], "14.9%")
        pct = lambda s: float(s.split("%")[0])
        self.assertGreater(pct(by["39262060"]["总税负估算"]),
                           pct(by["62014035"]["总税负估算"]))

    def test_same_chapter_no_dispute_hint(self):
        d = self.client.post("/api/compare",
                             json={"codes": ["6201.40.35", "6201.40.40"]}).json()
        self.assertFalse(d["跨章"])
        self.assertEqual(d["分歧提示"], "")

    def test_requires_two_codes(self):
        self.assertEqual(
            self.client.post("/api/compare", json={"codes": ["6201.40.35"]}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/compare", json={"codes": []}).status_code, 400)


class TestCrossPrecedentsAPI(unittest.TestCase):
    """
    CBP 先例接口。网络层（cross._get）整体替换，不联网——
    联网测的是 CBP 的可用性，不是本接口的行为。
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        import tempfile

        import cross
        self._cross = cross
        self._get = cross._get
        self._dir = cross.CACHE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        cross.CACHE_DIR = self._tmp.name  # 别让测试写进真实缓存目录

    def tearDown(self):
        self._cross._get = self._get
        self._cross.CACHE_DIR = self._dir
        self._tmp.cleanup()

    def _stub(self, rulings):
        self._cross._get = lambda path, params: {
            "rulings": rulings, "totalHits": len(rulings)}

    @staticmethod
    def _raw(number, tariffs, **kw):
        return {"rulingNumber": number, "subject": kw.get("subject", "x"),
                "categories": "Classification",
                "rulingDate": kw.get("date", "2023-05-01") + "T00:00:00",
                "collection": kw.get("collection", "ny"),
                "relatedRulings": [], "modifiedBy": [], "modifies": [],
                "revokedBy": kw.get("revoked_by", []), "revokes": [],
                "tariffs": tariffs, "operationallyRevoked": False,
                "commodityGrouping": ""}

    def test_precedents_shape(self):
        self._stub([self._raw("N305619", "8507.60.0020"),
                    self._raw("X1", "3926.90.9989")])
        r = self.client.post("/api/cross/precedents",
                             json={"term": "lithium battery",
                                   "codes": ["8507.60.00", "8506.50.00"]})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual([x["裁定号"] for x in d["先例"]], ["N305619"])
        it = d["先例"][0]
        for k in ("日期", "来源", "主题", "编码", "命中候选", "状态",
                  "状态说明", "版本提示", "链接"):
            self.assertIn(k, it)
        self.assertEqual(it["命中候选"], ["8507.60.00"])
        self.assertTrue(d["候选外编码"])

    def test_empty_term_rejected(self):
        r = self.client.post("/api/cross/precedents", json={"term": "  "})
        self.assertEqual(r.status_code, 400)

    def test_upstream_failure_degrades_not_500(self):
        """与 /api/search/ai 同约定：外部失败返回 {error}，不打断前端"""
        def _boom(path, params):
            raise RuntimeError("upstream down")
        self._cross._get = _boom
        r = self.client.post("/api/cross/precedents",
                             json={"term": "battery", "codes": ["8507.60.00"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

    def test_revoked_flag_surfaces(self):
        """撤销标注必须穿透到 API 响应——这是引用先例前唯一不能省的检查"""
        self._stub([self._raw("N232914", "8507.60.0020",
                              revoked_by=["H249299"])])
        d = self.client.post("/api/cross/precedents",
                             json={"term": "battery",
                                   "codes": ["8507.60.00"]}).json()
        it = d["先例"][0]
        self.assertEqual(it["状态"], "已撤销")
        self.assertIn("H249299", it["状态说明"])


class TestCrossLocalAPI(unittest.TestCase):
    """本地镜像反查接口：镜像未构建时降级为 {error}，前端静默跳过"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        import tempfile

        import cross
        import cross_sync
        self._cross = cross
        self._orig_db = cross.DB_PATH
        self._get = cross._get
        self._tmp = tempfile.TemporaryDirectory()
        cross.DB_PATH = os.path.join(self._tmp.name, "cross.db")
        # 用同步器真实建库（假服务端），测的是端到端而不是拼出来的行
        cross._get = lambda path, params: (
            {"totalSearchableRulingsCount": 1} if path.endswith("lastupdate")
            else {"rulings": [{
                "rulingNumber": "N286124", "subject": "two battery packs",
                "categories": "Classification",
                "rulingDate": "2017-06-08T00:00:00", "collection": "ny",
                "relatedRulings": [], "modifiedBy": [], "modifies": [],
                "revokedBy": [], "revokes": [],
                "tariffs": "8506.50.0000, 8507.60.0020",
                "operationallyRevoked": False, "commodityGrouping": ""}],
                "totalHits": 1})
        cross_sync.sync(db_path=cross.DB_PATH, start_year=2017, end_year=2017,
                        sleep=0, log=lambda *a: None)

    def tearDown(self):
        self._cross.DB_PATH = self._orig_db
        self._cross._get = self._get
        self._tmp.cleanup()

    def test_local_lookup(self):
        d = self.client.post("/api/cross/local",
                             json={"codes": ["8507.60.00", "8506.50.00"]}).json()
        self.assertEqual([x["裁定号"] for x in d["先例"]], ["N286124"])
        self.assertEqual(sorted(d["先例"][0]["命中候选"]),
                         ["8506.50.00", "8507.60.00"])
        self.assertEqual(d["每码先例数"],
                         {"8507.60.00": 1, "8506.50.00": 1})
        self.assertTrue(d["数据截至"])
        # 现行税则对账：两个编码都活着，失效列表应为空（用真实 rates_8 核）
        self.assertEqual(d["先例"][0]["失效编码"], [])

    def test_search_rows_carry_precedent_counts(self):
        """搜索结果每行带「先例数」；AI 补充行同表展示，同样要有这一列"""
        d = self.client.post("/api/search",
                             json={"keyword": "lithium battery", "limit": 5}).json()
        by = {r["编码"]: r for r in d["results"]}
        self.assertIn("8507.60.00", by)
        # fixture 镜像里 N286124 判给了 8507.60/8506.50 各一条
        self.assertEqual(by["8507.60.00"]["先例数"], 1)
        self.assertEqual(by["8506.50.00"]["先例数"], 1)

    def test_search_counts_none_when_mirror_absent(self):
        """镜像没建 → 先例数为 None（"没查"），不是 0（"查过了没有"）"""
        self._cross.DB_PATH = os.path.join(self._tmp.name, "nope.db")
        d = self.client.post("/api/search",
                             json={"keyword": "lithium battery", "limit": 3}).json()
        self.assertTrue(d["results"])
        for r in d["results"]:
            self.assertIn("先例数", r)
            self.assertIsNone(r["先例数"])

    def test_dead_code_annotated_against_real_hts(self):
        """回归：8471.92.10 是被 HS 修订删掉的真实编码，必须标出——
        90 年代裁定 42% 中招，而 CROSS 对此零标记"""
        import cross_sync
        self._cross._get = lambda path, params: (
            {"totalSearchableRulingsCount": 1} if path.endswith("lastupdate")
            else {"rulings": [{
                "rulingNumber": "OLD1", "subject": "input unit",
                "categories": "Classification",
                "rulingDate": "1996-03-01T00:00:00", "collection": "ny",
                "relatedRulings": [], "modifiedBy": [], "modifies": [],
                "revokedBy": [], "revokes": [],
                "tariffs": "8471.92.1000, 8507.60.0020",
                "operationallyRevoked": False, "commodityGrouping": ""}],
                "totalHits": 1})
        cross_sync.sync(db_path=self._cross.DB_PATH, start_year=1996,
                        end_year=1996, sleep=0, log=lambda *a: None)
        d = self.client.post("/api/cross/local",
                             json={"codes": ["8471.92.10"]}).json()
        it = d["先例"][0]
        self.assertEqual(it["失效编码"], ["8471.92.1000"])
        self.assertIn("已不在现行税则", d["提示"])

    def test_missing_db_degrades(self):
        self._cross.DB_PATH = os.path.join(self._tmp.name, "nope.db")
        r = self.client.post("/api/cross/local", json={"codes": ["8507.60.00"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

    def test_empty_codes_rejected(self):
        self.assertEqual(
            self.client.post("/api/cross/local", json={"codes": []}).status_code, 400)

    def test_semantic_endpoint_degrades_without_index(self):
        """语义索引未建 → {error} 而非 5xx；空描述 → 400"""
        r = self.client.post("/api/cross/semantic",
                             json={"query": "锂电池", "codes": []})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())
        self.assertEqual(
            self.client.post("/api/cross/semantic",
                             json={"query": "  "}).status_code, 400)


class TestSearchSpecialChapters(unittest.TestCase):
    """搜索默认剔除 98/99 章：它们不是可归类的进口编码"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_excluded_by_default(self):
        d = self.client.post("/api/search",
                             json={"keyword": "wool coat", "limit": 30}).json()
        self.assertTrue(d["results"])
        self.assertFalse([r for r in d["results"] if r["编码"][:2] in ("98", "99")])

    def test_included_on_request(self):
        d = self.client.post("/api/search",
                             json={"keyword": "wool coat", "limit": 30,
                                   "include_special": True}).json()
        self.assertTrue(d["results"])

    def test_results_carry_full_desc(self):
        d = self.client.post("/api/search",
                             json={"keyword": "dress patterns", "limit": 5}).json()
        self.assertTrue(d["results"])
        for r in d["results"]:
            self.assertIn("完整品名", r)
            self.assertIn("归类路径", r)


class TestExportInjection(unittest.TestCase):
    """导出防公式注入：Excel/Sheets 会执行以 = + - @ 开头的单元格"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_mod.app)

    def test_defuse_helper(self):
        for danger in ("=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd"):
            self.assertTrue(app_mod._defuse(danger).startswith("'"))
        # 正常值不受影响（含负数数值、普通文本）
        self.assertEqual(app_mod._defuse("8507.60.00"), "8507.60.00")
        self.assertEqual(app_mod._defuse("+25%"), "'+25%")  # 加征文本也会被中和，属预期
        self.assertEqual(app_mod._defuse(25.0), 25.0)
        self.assertIsNone(app_mod._defuse(None))

    def test_csv_export_defuses_formula(self):
        payload = {"results": [{"输入编码": "=cmd|'/c calc'!A1", "备注": "ok"}], "fmt": "csv"}
        r = self.client.post("/api/export", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode("utf-8-sig")
        self.assertIn("'=cmd", body)
        # 不得存在未被中和的行首公式
        for line in body.splitlines()[1:]:
            self.assertFalse(line.startswith("="))

    def test_xlsx_export_defuses_formula(self):
        payload = {"results": [{"输入编码": "=1+1"}], "fmt": "xlsx"}
        r = self.client.post("/api/export", json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(len(r.content) > 0)

    def test_export_rejects_non_dict_rows(self):
        r = self.client.post("/api/export", json={"results": ["not-a-dict"], "fmt": "csv"})
        self.assertEqual(r.status_code, 400)

    def test_export_empty_rejected(self):
        r = self.client.post("/api/export", json={"results": [], "fmt": "csv"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TestCrossTextViewer(unittest.TestCase):
    """站内看裁定正文：正文抓取整体替换，不联网。境内点官网链接打不开，这条路必须能独立工作"""

    def setUp(self):
        import cross
        from fastapi.testclient import TestClient
        self.client = TestClient(app_mod.app)
        self._cross = cross
        self._fetch, self._meta = cross.fetch_ruling_text, cross.ruling_meta

    def tearDown(self):
        self._cross.fetch_ruling_text, self._cross.ruling_meta = self._fetch, self._meta

    def test_text_with_params(self):
        self._cross.fetch_ruling_text = lambda n, c, d, use_cache=True: (
            f"NY {n} March 9, 2026 Dear Sir: The battery. HOLDING: 8507.60.00 applies. Sincerely, X"
            if (n, c, d[:4]) == ("N359156", "ny", "2026") else None)
        r = self.client.get("/api/cross/text/N359156", params={"collection": "NY", "date": "2026-03-09"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["裁定号"], "N359156")
        self.assertEqual(d["库别"], "NY")
        self.assertTrue(d["链接"].endswith("/ruling/N359156"))
        self.assertIn("/api/getdoc/ny/2026/N359156.doc", d["全文链接"])
        self.assertIn("HOLDING", [p["标题"] for p in d["段落"]])

    def test_meta_from_mirror_when_params_missing(self):
        self._cross.ruling_meta = lambda n, db_path=None: ("hq", "2016-12-27") if n == "H192478" else None
        self._cross.fetch_ruling_text = lambda n, c, d, use_cache=True: f"HQ {n} text HOLDING: ok"
        d = self.client.get("/api/cross/text/H192478").json()
        self.assertEqual(d["库别"], "HQ")
        self.assertEqual(d["日期"], "2016-12-27")

    def test_unknown_ruling_degrades_with_link(self):
        self._cross.ruling_meta = lambda n, db_path=None: None
        d = self.client.get("/api/cross/text/X000000").json()
        self.assertIn("error", d)
        self.assertTrue(d["链接"].endswith("/ruling/X000000"))

    def test_fetch_failure_degrades_with_link(self):
        self._cross.fetch_ruling_text = lambda n, c, d, use_cache=True: None
        d = self.client.get("/api/cross/text/N1", params={"collection": "ny", "date": "2026-01-01"}).json()
        self.assertIn("error", d)
        self.assertIn("链接", d)

    def test_bad_number_rejected(self):
        r = self.client.get("/api/cross/text/%2E%2E", params={"collection": "ny", "date": "2026"})
        self.assertEqual(r.status_code, 400)
