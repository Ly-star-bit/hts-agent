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
        # 8507.60.00 锂电池在 ANNEX II 通用豁免 → FLIP 301 豁免（官方 FRN 核实）
        self.assertEqual(row["FLIP 301加征"], "豁免")
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
        self.assertEqual(keys, {"htsdata", "ustr_pdf", "flip_frn"})
        for f in data["files"]:
            self.assertTrue(f["exists"], f"{f['key']} 源文件应存在")
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
