# -*- coding: utf-8 -*-
"""
app.py —— HTS 301 关税查询 Web 应用（FastAPI）

启动：python app.py  → 浏览器打开 http://127.0.0.1:5000
  或：uvicorn app:app --host 127.0.0.1 --port 5000

功能：
  - 文本粘贴 HTS 编码批量查询
  - 上传 Excel/CSV 文件批量查询
  - 结果表格展示 + 导出 CSV/Excel
  - 与命令行工具共用同一套查询逻辑（scripts/core.py）
"""
import io
import os
import re
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_HTML = os.path.join(BASE_DIR, "templates", "index.html")
MAX_UPLOAD = 20 * 1024 * 1024  # 上传限制 20MB

app = FastAPI(title="HTS 301 关税查询工具", version="1.1.0")

_db = None


def get_db():
    """懒加载数据库（带缓存），数据文件更新后重启服务即可"""
    global _db
    if _db is None:
        _db = core.load_db()
    return _db


class QueryRequest(BaseModel):
    text: str = Field(default="", description="包含 HTS 编码的文本")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）/ 其他国家代码")


class ExportRequest(BaseModel):
    results: list = Field(default_factory=list, description="查询结果列表")
    fmt: str = Field(default="csv", description="导出格式：csv 或 xlsx")


@app.get("/")
def index():
    """前端页面"""
    return FileResponse(TEMPLATE_HTML)


@app.get("/api/info")
def api_info():
    """数据源信息（页面头部展示）"""
    meta = get_db()["meta"]
    return {
        "sec301_mapping_count": meta.get("sec301_mapping_count", 0),
        "rates_8_count": meta.get("rates_8_count", 0),
        "hts_csv": meta.get("hts_csv", ""),
        "ustr_pdf": meta.get("ustr_pdf", ""),
        "flip_frn_pdf": meta.get("flip_frn_pdf", ""),
        "built_at": meta.get("built_at", ""),
    }


# ============================================================
# 来源追溯：官方源文件清单 / PDF 定位 / CSV 行上下文
# ============================================================

SOURCE_FILES = {
    "htsdata": {
        "path": "htsdata.csv",
        "label": "USITC 全量税率表",
        "mime": "text/csv",
        "desc": "一般/特殊/第二栏税率与商品描述的基础数据源",
    },
    "ustr_pdf": {
        "path": "China Tariffs_2026HTSRev15.pdf",
        "label": "USTR 301 中国清单",
        "mime": "application/pdf",
        "desc": "中国 Section 301 加征：8 位子目 → Chapter 99 子目归属",
    },
    "flip_frn": {
        "path": "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf",
        "label": "FLIP 301 Final Action FRN",
        "mime": "application/pdf",
        "desc": "FLIP 301 强迫劳动关税：60 经济体税率表 + ANNEX II 豁免清单（含范围限制）",
    },
}


def _source_file_info(key):
    """源文件路径存在性检查，返回 (path, info) 或抛 404"""
    info = SOURCE_FILES.get(key)
    if not info:
        raise HTTPException(status_code=404, detail=f"未知源文件 key：{key}")
    path = os.path.join(BASE_DIR, info["path"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"源文件缺失：{info['path']}（请确认数据文件存在）")
    return path, info


@app.get("/api/source/list")
def api_source_list():
    """源文件清单（含本地 meta 版本信息），供页面"数据来源"展示"""
    files = []
    for key, info in SOURCE_FILES.items():
        path = os.path.join(BASE_DIR, info["path"])
        files.append({
            "key": key,
            "label": info["label"],
            "path": info["path"],
            "desc": info["desc"],
            "mime": info["mime"],
            "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
        })
    meta = get_db()["meta"]
    return {
        "files": files,
        "meta": {
            "hts_csv": meta.get("hts_csv", ""),
            "ustr_pdf": meta.get("ustr_pdf", ""),
            "flip_frn_pdf": meta.get("flip_frn_pdf", ""),
            "built_at": meta.get("built_at", ""),
        },
    }


@app.get("/api/source/pdf/{key}")
def api_source_pdf(key: str):
    """内联返回官方 PDF。前端打开 /api/source/pdf/{key}#page=N 可定位到指定物理页"""
    path, info = _source_file_info(key)
    if info["mime"] != "application/pdf":
        raise HTTPException(status_code=400, detail=f"{info['path']} 不是 PDF 文件")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
        content_disposition_type="inline",
    )


@app.get("/api/source/csv")
def api_source_csv(key: str = "htsdata", line: int = None, around: int = 3):
    """返回 CSV 源文件指定行（1 起，含表头行）及其上下文的文本，供弹窗展示原文"""
    path, info = _source_file_info(key)
    if info["mime"] != "text/csv":
        raise HTTPException(status_code=400, detail=f"{info['path']} 不是 CSV 文件")
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    total = len(lines)
    ctx = []
    if line and 1 <= line <= total:
        around = max(0, min(int(around), 5))
        start = max(1, line - around)
        end = min(total, line + around)
        ctx = [{"行号": i, "内容": lines[i - 1].rstrip("\r\n")} for i in range(start, end + 1)]
    return {"file": os.path.basename(path), "total_lines": total, "target": line if ctx else None, "context": ctx}


@app.post("/api/query")
def api_query(req: QueryRequest):
    """文本查询：POST JSON {"text": "6204.69.45, 8703.80.00", "origin": "CN"}"""
    codes = core.extract_codes(req.text)
    if not codes:
        raise HTTPException(status_code=400, detail="未解析到任何 HTS 编码，请检查输入格式（8位或10位，如 6204.69.45）")
    results, stats = core.batch_query(get_db(), codes, origin=req.origin)
    return {"results": results, "stats": stats, "origin": req.origin}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), origin: Optional[str] = Form("CN")):
    """文件上传查询：multipart 文件字段 file（xlsx/csv/txt）+ 可选原产地 origin"""
    filename = file.filename or "未命名文件"
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail=f"文件超过 {MAX_UPLOAD // 1048576}MB 限制")
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str)
            codes = core.extract_codes(df.to_csv(index=False))
        elif ext == ".csv":
            df = pd.read_csv(io.StringIO(content.decode("utf-8-sig", errors="replace")), dtype=str)
            codes = core.extract_codes(df.to_csv(index=False))
        else:
            codes = core.extract_codes(content.decode("utf-8-sig", errors="replace"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")
    if not codes:
        raise HTTPException(status_code=400, detail=f"文件 {filename} 中未解析到 HTS 编码")
    results, stats = core.batch_query(get_db(), codes, origin=origin)
    return {"results": results, "stats": stats, "source": filename, "origin": origin}


# Excel / Sheets 会把以 = + - @ 开头（含前导 TAB/CR）的单元格当公式执行。
# 导出结果由前端回传、可含任意文本，且报关场景下导出文件常被转发他人打开，
# 因此写文件前统一加前导单引号中和（仅影响显示，不改变数值判定）。
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _defuse(value):
    """中和单元格公式注入；非字符串原样返回"""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value


def _defuse_rows(rows):
    """对导出行的每个值做公式中和；非 dict 行原样保留"""
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: _defuse(v) for k, v in row.items()})
        else:
            out.append(row)
    return out


@app.post("/api/export")
def api_export(req: ExportRequest):
    """导出结果：POST JSON {"results": [...], "fmt": "xlsx"|"csv"}，返回文件下载"""
    if not req.results:
        raise HTTPException(status_code=400, detail="无数据可导出")
    if not isinstance(req.results[0], dict):
        raise HTTPException(status_code=400, detail="导出数据格式错误：results 应为对象列表")
    rows = _defuse_rows(req.results)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if req.fmt == "xlsx":
        buf = io.BytesIO()
        pd.DataFrame(rows).to_excel(buf, index=False)
        buf.seek(0)
        fname = f"hts_301_结果_{ts}.xlsx"
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        sio = io.StringIO()
        import csv as _csv

        fieldnames = list(rows[0].keys())
        writer = _csv.DictWriter(sio, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        buf = io.BytesIO(sio.getvalue().encode("utf-8-sig"))
        fname = f"hts_301_结果_{ts}.csv"
        media = "text/csv; charset=utf-8"
    # 中文文件名需 RFC 5987 编码，保证下载时显示正确
    disposition = f"attachment; filename*=UTF-8''{quote(fname)}"
    return Response(content=buf.getvalue(), media_type=media, headers={"Content-Disposition": disposition})


# ============================================================
# 以下为 1.2 新增功能：税率搜索 / 成本估算 / 数据变动 / AI 助手
# ============================================================

class SearchRequest(BaseModel):
    keyword: str = Field(default="", description="关键词（英文品名 / 编码）")
    sort: str = Field(default="tax_asc", description="排序：tax_asc / tax_desc / relevance / code_asc")
    limit: int = Field(default=100, ge=1, le=500)
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，用于折算从量税（可选）")


class EstimateRequest(BaseModel):
    text: str = Field(default="", description="包含 HTS 编码的文本（与 codes 二选一）")
    codes: list = Field(default_factory=list, description="HTS 编码列表（与 text 二选一）")
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，折算从量税（可选）")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）/ 其他国家代码")


class AIAskRequest(BaseModel):
    question: str = Field(default="", description="自然语言问题")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）")


class AIClassifyRequest(BaseModel):
    description: str = Field(default="", description="商品描述")


class AIInterpretRequest(BaseModel):
    results: list = Field(default_factory=list, description="查询结果列表")


class AIAnalyzeRequest(BaseModel):
    items: list = Field(default_factory=list, description="商品清单：[{'name': 品名, 'unit_value': 可选}]")


class AIConfigRequest(BaseModel):
    provider: str = Field(default=None, description="provider：openai_compat / ollama / 空(null)")
    base_url: str = Field(default=None)
    model: str = Field(default=None)
    api_key: str = Field(default=None, description="传 '__KEEP__' 表示保留原值")
    temperature: float = Field(default=None)
    timeout: float = Field(default=None)


class MeasuresConfigRequest(BaseModel):
    measures: dict = Field(default_factory=dict, description="加征开关，如 {'cn301': true, 'flip301': false}")


@app.get("/api/measures/config")
def api_measures_get_config():
    """读取加征措施开关配置（cn301 / flip301）"""
    return {"measures": core.load_measures_config()}


@app.post("/api/measures/config")
def api_measures_save_config(req: MeasuresConfigRequest):
    """保存加征措施开关配置（原子写入，立即生效，无需重启）"""
    cfg = core.save_measures_config(req.measures or {})
    return {"measures": cfg}


@app.get("/api/ai/status")
def api_ai_status():
    """AI 服务配置状态"""
    try:
        import ai
        return ai.ai_status()
    except Exception as e:
        return {"enabled": False, "message": f"AI 模块加载失败：{e}"}


@app.get("/api/ai/config")
def api_ai_get_config():
    """读取 AI 配置（api_key 仅返回掩码，绝不返回明文）"""
    import ai

    return ai.mask_config(ai.load_config())


@app.post("/api/ai/config")
def api_ai_save_config(req: AIConfigRequest):
    """保存 AI 配置（Web 端手动配置），保存后立即生效"""
    import ai

    updates = {}
    for field in ("provider", "base_url", "model", "api_key", "temperature", "timeout"):
        v = getattr(req, field)
        if v is not None:
            updates[field] = v
    cfg = ai.save_config(updates)
    status = ai.ai_status()
    return {"config": cfg, "status": status}


@app.post("/api/ai/test")
def api_ai_test():
    """用当前配置测试 AI 服务连通性"""
    import ai

    return ai.test_connection()


@app.post("/api/search")
def api_search(req: SearchRequest):
    """关键词搜索：返回匹配子目，可按等效从价税率排序（最低税率）"""
    import traceback
    try:
        import rate

        db = get_db()
        rows = rate.search(db, req.keyword, limit=req.limit, sort=req.sort)
        # 可选：给定单位货值时计算总税负
        if req.unit_value:
            for r in rows:
                total = rate.calc_total(db, re.sub(r"\D", "", r["编码"]), unit_value=req.unit_value)
                r["总税负估算"] = total["总税负估算"]
                r["301加征数值"] = total["301加征数值"]
        return {"results": rows, "count": len(rows), "keyword": req.keyword}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"搜索服务端异常：{e}")


@app.post("/api/estimate")
def api_estimate(req: EstimateRequest):
    """成本估算：按编码批量计算总税负（基础 + 301 + 附加税）"""
    import traceback
    try:
        import core
        import rate

        db = get_db()
        codes = req.codes or []
        if req.text and not codes:
            codes = core.extract_codes(req.text)
        if not codes:
            raise HTTPException(status_code=400, detail="未解析到任何 HTS 编码")
        results = [rate.calc_total(db, c, unit_value=req.unit_value, origin=req.origin) for c in codes]
        return {"results": results, "count": len(results), "unit_value": req.unit_value, "origin": req.origin}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"估算服务端异常：{e}")


@app.get("/api/changes")
def api_changes():
    """最近一次数据更新后的变动清单（由 build_db.py 生成）"""
    import db_diff

    data = db_diff.load_changes()
    if data is None:
        return {"first_build": True, "message": "暂无变动记录（请先运行 python scripts/build_db.py 生成基准）"}
    return data


@app.post("/api/ai/classify")
def api_ai_classify(req: AIClassifyRequest):
    """AI 商品归类：描述 → 推荐 HTS 编码"""
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="请提供商品描述")
    import ai

    return ai.classify_product(get_db(), req.description.strip())


@app.post("/api/ai/ask")
def api_ai_ask(req: AIAskRequest):
    """AI 自然语言问税"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="请提供问题")
    import ai

    return ai.ask_tax_question(get_db(), req.question.strip(), origin=req.origin)


@app.post("/api/ai/interpret")
def api_ai_interpret(req: AIInterpretRequest):
    """AI 解读查询结果"""
    if not req.results:
        raise HTTPException(status_code=400, detail="无结果可解读")
    import ai

    return ai.interpret_results(req.results)


@app.post("/api/ai/analyze")
def api_ai_analyze(req: AIAnalyzeRequest):
    """AI 批量清单分析"""
    if not req.items:
        raise HTTPException(status_code=400, detail="清单为空")
    import ai

    return ai.analyze_list(get_db(), req.items)


if __name__ == "__main__":
    import uvicorn

    print("HTS 301 关税查询工具已启动：http://127.0.0.1:5000")
    uvicorn.run(app, host="127.0.0.1", port=5000)
