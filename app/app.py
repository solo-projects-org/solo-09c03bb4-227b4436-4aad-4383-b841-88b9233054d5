"""公交清分 Web 应用：换乘标志口径影响分析 + 复算差额补记。"""
from __future__ import annotations

import csv
import io
import json
import os

from flask import Flask, Response, jsonify, render_template, request

import posting
from lineage import AnalysisError, analyze, build_models
from posting import PostingError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(BASE_DIR, "data", "sample_clearing.json")
SAMPLE_SETTLEMENT_PATH = os.path.join(BASE_DIR, "data", "sample_settlement.json")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB
app.config["POSTING_DB_PATH"] = os.environ.get(
    "POSTING_DB_PATH", os.path.join(BASE_DIR, "data", "posting.db")
)


@app.errorhandler(AnalysisError)
def handle_analysis_error(exc: AnalysisError):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(PostingError)
def handle_posting_error(exc: PostingError):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(413)
def handle_too_large(_exc):
    return jsonify({"ok": False, "error": "上传文件超过 8 MB 限制。"}), 400


def _parse_payload(raw: bytes) -> dict:
    if not raw.strip():
        raise AnalysisError("上传内容为空，请选择包含 source_tables 和 models 的 JSON 文件。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AnalysisError("文件不是 UTF-8 编码，请另存为 UTF-8 后重试。") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AnalysisError("JSON 顶层必须是对象，包含 source_tables 和 models 两个数组。")
    return payload


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/sample")
def sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.post("/api/validate")
def validate():
    """校验上传的 JSON 并返回字段清单，供前端选择待改字段/结算模型。"""
    payload = _parse_payload(request.get_data())
    source_schemas, infos, _ = build_models(payload)
    return jsonify({
        "ok": True,
        "source_tables": [
            {"table": t, "columns": cols} for t, cols in source_schemas.items()
        ],
        "models": [
            {
                "name": name,
                "upstream": sorted(info.upstream),
                "output_columns": list(info.nodes[-1].output_columns),
            }
            for name, info in infos.items()
        ],
    })


@app.post("/api/analyze")
def run_analyze():
    payload = _parse_payload(request.get_data())
    target_table = request.args.get("table", "").strip()
    target_column = request.args.get("column", "").strip()
    if not target_table or not target_column:
        raise AnalysisError("请先选择待调整的源表字段。")
    result = analyze(payload, target_table, target_column)
    return jsonify({"ok": True, "result": result})


# ---------------------------------------------------------------------------
# 复算差额补记
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return app.config["POSTING_DB_PATH"]


def _form_payload() -> dict:
    """补记请求随表单携带的模型 JSON，沿用现有校验（不执行 SQL）。"""
    return _parse_payload(request.form.get("payload", "").encode("utf-8"))


def _check_settlement_model(payload: dict, model_name: str) -> None:
    """沿用现有模型校验，并确认所选模型可导出交易级结算 CSV。"""
    if not model_name:
        raise PostingError("请选择结算模型。")
    try:
        _, infos, _ = build_models(payload)
    except AnalysisError as exc:
        raise PostingError(f"模型 JSON 校验未通过：{exc}") from exc
    info = infos.get(model_name)
    if info is None:
        raise PostingError(f"模型「{model_name}」不在已载入的模型 JSON 中。")
    output_columns = set(info.nodes[-1].output_columns)
    missing = [c for c in posting.REQUIRED_COLUMNS if c not in output_columns]
    if missing:
        raise PostingError(
            f"模型「{model_name}」的输出列缺少 {', '.join(missing)}，"
            "结算模型必须输出 transaction_id、company_id、settlement_amount 三列。"
        )


def _read_upload() -> bytes:
    fh = request.files.get("file")
    if fh is None:
        raise PostingError("请上传模型导出的整日 CSV 文件。")
    return fh.read()


@app.get("/posting")
def posting_page():
    return render_template("posting.html")


@app.get("/api/posting/sample")
def posting_sample():
    with open(SAMPLE_SETTLEMENT_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.get("/api/posting/sample/<name>.csv")
def posting_sample_csv(name: str):
    if name not in ("initial", "recalc"):
        return jsonify({"ok": False, "error": "未知样例文件。"}), 404
    path = os.path.join(BASE_DIR, "data", f"sample_{name}.csv")
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sample_{name}.csv"},
    )


@app.post("/api/posting/initial")
def posting_initial():
    """登记初始入账批：每个（模型, 业务日）仅一次，不出分录。"""
    model_name = request.form.get("model_name", "").strip()
    business_date = posting.validate_business_date(request.form.get("business_date", ""))
    batch_id = posting.validate_batch_id(request.form.get("batch_id", ""), "批号")
    _check_settlement_model(_form_payload(), model_name)
    raw = _read_upload()
    lines = posting.parse_settlement_csv(raw)
    posting.register_initial(_db_path(), model_name, business_date,
                             batch_id, lines, posting.file_hash(raw))
    return jsonify({"ok": True, "batch_id": batch_id, "row_count": len(lines)})


@app.post("/api/posting/recalc")
def posting_recalc():
    """提交复算结果：对比依据批与新批，生成差额分录并推进当前依据批。"""
    model_name = request.form.get("model_name", "").strip()
    business_date = posting.validate_business_date(request.form.get("business_date", ""))
    basis_batch_id = posting.validate_batch_id(request.form.get("basis_batch_id", ""), "依据批号")
    new_batch_id = posting.validate_batch_id(request.form.get("new_batch_id", ""), "新批号")
    if new_batch_id == basis_batch_id:
        raise PostingError("新批号不能与依据批号相同。")
    _check_settlement_model(_form_payload(), model_name)
    raw = _read_upload()
    lines = posting.parse_settlement_csv(raw)
    result = posting.submit_recalc(_db_path(), model_name, business_date,
                                   basis_batch_id, new_batch_id, lines,
                                   posting.file_hash(raw))
    return jsonify({
        "ok": True,
        "batch_id": result["batch_id"],
        "replayed": result["replayed"],
        "entry_count": len(result["entries"]),
        "entries": [
            {"transaction_id": t, "company_id": c, "amount": a}
            for t, c, a in result["entries"]
        ],
        "download_url": f"/api/posting/batches/{result['batch_id']}/entries.csv",
    })


@app.get("/api/posting/state")
def posting_state():
    model_name = request.args.get("model_name", "").strip()
    business_date = request.args.get("business_date", "").strip()
    if not model_name or not business_date:
        raise PostingError("请提供结算模型与业务日。")
    return jsonify({"ok": True, **posting.get_state(_db_path(), model_name, business_date)})


@app.get("/api/posting/batches/<batch_id>/entries.csv")
def posting_entries_csv(batch_id: str):
    entries = posting.get_entries(_db_path(), batch_id)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["transaction_id", "company_id", "amount"])
    writer.writerows(entries)
    return Response(
        out.getvalue().encode("utf-8-sig"),  # 带 BOM，Excel 打开不乱码
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=entries_{batch_id}.csv"},
    )


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8000)
