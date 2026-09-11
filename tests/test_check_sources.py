# -*- coding: utf-8 -*-
"""
test_check_sources.py —— 官方源文件更新检测（check_sources.py）

不联网：用 httpx.MockTransport 扮演 USITC / USTR 三家服务器；本地文件在临时目录里造。

锁六件事：
  1. "有没有更新"以内容哈希为准——文件名/版本号变了但内容没变 → 不算更新
  2. CSV 只是换行符 CRLF↔LF 漂了 → 不算更新（否则每次都误报）
  3. 版本号/ETag 没变且本地文件没动 → 不重新下载大文件；本地文件被人手动换过 → 必须重下比对
  4. 内容真变了 → 报更新 + 行差异样例；--apply 原地覆盖且旧文件有备份
  5. 一个源的服务器挂了只报那个源，其他源照常；状态文件里失败源保留上次记录
  6. 退出码：0 一致 / 2 出错 / 3 有更新
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import check_sources as cs

CSV_V1 = b"\xef\xbb\xbfHTS Number,Indent\n\"0101\",\"0\"\n\"0101.21.00\",\"2\"\n"
CSV_V2 = CSV_V1 + b"\"9903.03.12\",\"0\"\n"
PDF_A = b"%PDF-1.6 china tariffs A"
PDF_B = b"%PDF-1.6 china tariffs B"
CH99_A = b"%PDF ch99 A"
CH99_B = b"%PDF ch99 B"
FRN_A = b"%PDF-1.6 flip frn A"


class FakeServers:
    """三家服务器的可变状态：测试里直接改字段来模拟官方发版。"""

    def __init__(self):
        self.release = "2026HTSRev15"
        self.csv = CSV_V1
        self.pdf = PDF_A
        self.pdf_name = "China Tariffs_2026HTSRev15.pdf"
        self.frn = FRN_A
        self.frn_etag = '"etag-1"'
        self.ch99 = CH99_A
        self.down = set()            # 放进去的 host 返回 503
        self.hits = []               # (method, url) 记录，用来断言"没重新下载"

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits.append((request.method, url))
        if request.url.host in self.down:
            return httpx.Response(503, text="down")
        if url == cs.USITC_RELEASE_URL:
            return httpx.Response(200, json={"name": self.release})
        if "exportList" in url:
            return httpx.Response(200, content=self.csv,
                                  headers={"content-disposition": "attachment; filename=htsdata.csv"})
        if "filename=China+Tariffs" in url:
            return httpx.Response(200, content=self.pdf,
                                  headers={"content-disposition": f'inline; filename="{self.pdf_name}"'})
        if "filename=Chapter+99" in url:
            return httpx.Response(200, content=self.ch99,
                                  headers={"content-disposition":
                                           'inline; filename="Chapter 99_2026HTSRev18.pdf"'})
        if "FLIP" in url:
            headers = {"etag": self.frn_etag, "last-modified": "Thu, 23 Jul 2026 20:58:23 GMT",
                       "content-length": str(len(self.frn))}
            if request.method == "HEAD":
                return httpx.Response(200, headers=headers)
            return httpx.Response(200, content=self.frn, headers=headers)
        return httpx.Response(404)

    def downloads(self, needle):
        return [u for m, u in self.hits if m == "GET" and needle in u]


class CheckSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_base = cs.BASE_DIR
        cs.BASE_DIR = self.tmp
        self.state = os.path.join(self.tmp, "data", ".sources_state.json")
        self.backup = os.path.join(self.tmp, "output", "sources_backup")
        self.srv = FakeServers()
        self.transport = httpx.MockTransport(self.srv.handler)
        # 本地四份文件与"远端当前版"一致
        self.write("htsdata", CSV_V1)
        self.write("ustr_pdf", PDF_A)
        self.write("flip_frn", FRN_A)
        self.write("ch99_pdf", CH99_A)
        # PDF 首页 "Last Updated" 提取依赖 pdfplumber 读真 PDF，假字节读不了；关掉以免噪音
        self._orig_lu = cs._pdf_last_updated
        cs._pdf_last_updated = lambda data: ""

    def tearDown(self):
        cs.BASE_DIR = self._orig_base
        cs._pdf_last_updated = self._orig_lu
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, key, data):
        with open(os.path.join(self.tmp, cs.SOURCES[key]["path"]), "wb") as f:
            f.write(data)

    def read(self, key):
        with open(os.path.join(self.tmp, cs.SOURCES[key]["path"]), "rb") as f:
            return f.read()

    def run_check(self, **kw):
        return cs.check(transport=self.transport, state_path=self.state,
                        backup_dir=self.backup, **kw)

    # 1. 内容为准：改名不算更新
    def test_all_identical_reports_no_update(self):
        r = self.run_check()
        self.assertEqual(r["updated"], [])
        self.assertEqual(r["errors"], [])
        for k in cs.SOURCES:
            self.assertFalse(r["sources"][k]["updated"], k)
        self.assertTrue(os.path.exists(self.state))
        self.assertIn("全部与官方一致", cs.format_report(r))

    def test_pdf_renamed_but_same_bytes_is_not_update(self):
        self.srv.pdf_name = "China Tariffs_2026HTSRev17.pdf"
        r = self.run_check()
        self.assertFalse(r["sources"]["ustr_pdf"]["updated"])
        self.assertEqual(r["sources"]["ustr_pdf"]["remote_version"], "China Tariffs_2026HTSRev17.pdf")

    # 2. 换行符漂移不算更新
    def test_csv_crlf_drift_is_not_update(self):
        self.srv.csv = CSV_V1.replace(b"\n", b"\r\n")
        r = self.run_check()
        self.assertFalse(r["sources"]["htsdata"]["updated"])

    # 3. 版本缓存：没变不重下；本地被手动换过必须重下
    def test_unchanged_version_skips_big_download(self):
        self.run_check()
        self.srv.hits.clear()
        r = self.run_check()
        self.assertTrue(r["sources"]["htsdata"]["skipped_download"])
        self.assertTrue(r["sources"]["flip_frn"]["skipped_download"])
        self.assertEqual(self.srv.downloads("exportList"), [])
        self.assertEqual(self.srv.downloads("FLIP"), [])
        self.assertEqual(r["updated"], [])

    def test_local_file_replaced_forces_redownload(self):
        self.run_check()
        self.write("htsdata", b"someone put an old file here\n")
        self.srv.hits.clear()
        r = self.run_check()
        self.assertEqual(len(self.srv.downloads("exportList")), 1)
        self.assertTrue(r["sources"]["htsdata"]["updated"])

    # 4. 真更新 → 报差异；--apply 覆盖 + 备份
    def test_new_release_reports_update_with_diff(self):
        self.run_check()
        self.srv.release, self.srv.csv = "2026HTSRev17", CSV_V2
        r = self.run_check()
        e = r["sources"]["htsdata"]
        self.assertTrue(e["updated"])
        self.assertEqual(e["remote_version"], "2026HTSRev17")
        self.assertEqual(e["diff"]["added"], 1)
        self.assertEqual(e["diff"]["removed"], 0)
        self.assertEqual(e["diff"]["added_sample"], ["9903.03.12"])
        self.assertEqual(r["updated"], ["htsdata"])
        self.assertEqual(self.read("htsdata"), CSV_V1)          # 没 --apply 不动本地
        rep = cs.format_report(r)
        self.assertIn("有更新（未下载）", rep)
        self.assertIn("--apply --rebuild", rep)

    def test_apply_overwrites_and_backs_up(self):
        self.srv.pdf, self.srv.frn_etag, self.srv.frn = PDF_B, '"etag-2"', b"%PDF frn B"
        r = self.run_check(apply=True)
        self.assertEqual(sorted(r["applied"]), ["flip_frn", "ustr_pdf"])
        self.assertEqual(self.read("ustr_pdf"), PDF_B)
        self.assertEqual(self.read("flip_frn"), b"%PDF frn B")
        stamps = os.listdir(self.backup)
        self.assertEqual(len(stamps), 1)
        bdir = os.path.join(self.backup, stamps[0])
        with open(os.path.join(bdir, cs.SOURCES["ustr_pdf"]["path"]), "rb") as f:
            self.assertEqual(f.read(), PDF_A)
        with open(os.path.join(bdir, cs.SOURCES["flip_frn"]["path"]), "rb") as f:
            self.assertEqual(f.read(), FRN_A)
        self.assertNotIn("htsdata.csv", os.listdir(bdir))         # 没变的不备份
        # 覆盖后状态里 local == remote，再查一次应当一致且不重下
        self.srv.hits.clear()
        r2 = self.run_check()
        self.assertEqual(r2["updated"], [])
        self.assertEqual(self.srv.downloads("FLIP"), [])

    # 5. 单源故障隔离
    def test_one_server_down_isolated(self):
        self.run_check()
        self.srv.down.add("ustr.gov")
        self.srv.release, self.srv.csv = "2026HTSRev17", CSV_V2
        r = self.run_check()
        self.assertEqual(r["errors"], ["flip_frn"])  # ch99 与 flip 不同 host，不受牵连
        self.assertIn("error", r["sources"]["flip_frn"])
        self.assertEqual(r["updated"], ["htsdata"])
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["sources"]["flip_frn"]["remote_version"], '"etag-1"')  # 上次记录保留
        self.assertIn("last_error", st["sources"]["flip_frn"])
        self.assertIn("检查失败", cs.format_report(r))

    def test_missing_local_file_reports_update(self):
        os.remove(os.path.join(self.tmp, cs.SOURCES["ustr_pdf"]["path"]))
        r = self.run_check()
        e = r["sources"]["ustr_pdf"]
        self.assertFalse(e["local_exists"])
        self.assertTrue(e["updated"])
        self.assertIn("本地文件缺失", cs.format_report(r))

    # 6. 退出码
    def test_exit_codes(self):
        orig = cs._client
        cs._client = lambda transport=None: orig(self.transport)
        orig_state, orig_backup = cs.STATE_PATH, cs.BACKUP_DIR
        cs.STATE_PATH, cs.BACKUP_DIR = self.state, self.backup
        try:
            import io, contextlib
            def run(*argv):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cs.main(list(argv))
                return rc, buf.getvalue()
            rc, out = run()
            self.assertEqual(rc, cs.EXIT_OK)
            self.srv.pdf = PDF_B
            rc, out = run("--json")
            self.assertEqual(rc, cs.EXIT_UPDATED)
            self.assertEqual(json.loads(out)["updated"], ["ustr_pdf"])
            self.srv.pdf = PDF_A
            self.srv.down.add("hts.usitc.gov")
            rc, _ = run("--only", "htsdata")
            self.assertEqual(rc, cs.EXIT_ERROR)
        finally:
            cs._client = orig
            cs.STATE_PATH, cs.BACKUP_DIR = orig_state, orig_backup

    def test_only_rejects_unknown_key(self):
        with self.assertRaises(SystemExit):
            cs.main(["--only", "nope"])


if __name__ == "__main__":
    unittest.main()
