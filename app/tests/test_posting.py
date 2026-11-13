"""复算差额补记测试：CSV 校验、分录规则、批次流转、幂等与并发。"""
import io
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import posting  # noqa: E402
from app import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


TMP = tempfile.mkdtemp(prefix="posting_test_")
_db_seq = 0


def fresh_db():
    """每个场景一个全新 SQLite 文件，互不影响。"""
    global _db_seq
    _db_seq += 1
    app.config["POSTING_DB_PATH"] = os.path.join(TMP, f"t{_db_seq}.db")


PAYLOAD = {
    "source_tables": [
        {"table": "t_txn", "columns": ["transaction_id", "line_id", "amount"]},
        {"table": "t_line", "columns": ["line_id", "company_id"]},
    ],
    "models": [
        {"name": "settle", "sql":
         "SELECT t.transaction_id AS transaction_id, l.company_id AS company_id,"
         " t.amount AS settlement_amount"
         " FROM t_txn t INNER JOIN t_line l ON t.line_id = l.line_id"},
        {"name": "not_settle", "sql":
         "SELECT t.line_id AS line_id, t.amount AS amount FROM t_txn t"},
    ],
}


def csv_bytes(rows, header="transaction_id,company_id,settlement_amount"):
    lines = [header] + [",".join(map(str, r)) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def post_initial(client, batch="B1", model="settle", date="2025-06-01",
                 rows=None, raw=None, payload=None):
    if raw is None:
        raw = csv_bytes(rows if rows is not None else [("T1", "C1", 100)])
    return client.post("/api/posting/initial", data={
        "payload": json.dumps(payload if payload is not None else PAYLOAD),
        "model_name": model,
        "business_date": date,
        "batch_id": batch,
        "file": (io.BytesIO(raw), "init.csv"),
    }, content_type="multipart/form-data")


def post_recalc(client, basis="B1", new="B2", model="settle", date="2025-06-01",
                rows=None, raw=None, payload=None):
    if raw is None:
        raw = csv_bytes(rows if rows is not None else [("T1", "C1", 100)])
    return client.post("/api/posting/recalc", data={
        "payload": json.dumps(payload if payload is not None else PAYLOAD),
        "model_name": model,
        "business_date": date,
        "basis_batch_id": basis,
        "new_batch_id": new,
        "file": (io.BytesIO(raw), "recalc.csv"),
    }, content_type="multipart/form-data")


def entry_tuples(body):
    return [(e["transaction_id"], e["company_id"], e["amount"]) for e in body["entries"]]


# ---------------------------------------------------------------------------
# 1. CSV 解析与 compute_entries 规则（纯函数）
# ---------------------------------------------------------------------------
lines = posting.parse_settlement_csv(csv_bytes([("T1", "C1", 100), ("T2", "C2", -50)]))
check("CSV 正常解析", lines == {"T1": ("C1", 100), "T2": ("C2", -50)})

for name, raw, kw in [
    ("CSV 表头缺列", csv_bytes([("T1", "C1", 100)], "transaction_id,company_id"), "表头"),
    ("CSV 金额非整数", csv_bytes([("T1", "C1", "12.5")]), "整数"),
    ("CSV 交易重复", csv_bytes([("T1", "C1", 1), ("T1", "C2", 2)]), "重复"),
    ("CSV 空文件", b"", "空"),
    ("CSV 只有表头", csv_bytes([]), "交易行"),
    ("CSV 列数不符", b"transaction_id,company_id,settlement_amount\nT1,C1\n", "3 列"),
    ("CSV 公司为空", csv_bytes([("T1", "", 100)]), "company_id"),
]:
    try:
        posting.parse_settlement_csv(raw)
        check(name, False, "应抛 PostingError")
    except posting.PostingError as exc:
        check(name, kw in str(exc), str(exc))

# 同公司补差、换公司全额冲/记、新增、消失、零额不出分录
entries = posting.compute_entries(
    {"A": ("C1", 100), "B": ("C1", 200), "C": ("C1", 300), "D": ("C1", 0), "E": ("C1", 50)},
    {"A": ("C1", 130), "B": ("C2", 200), "C": ("C1", 300), "D": ("C2", 0), "F": ("C2", 0)},
)
check("分录规则全集", entries == [
    ("A", "C1", 30),          # 同公司补差额
    ("B", "C1", -200),        # 换公司：冲回旧公司全额
    ("B", "C2", 200),         # 换公司：给新公司记全额
    ("E", "C1", -50),         # 消失：冲回
], str(entries))
check("零额不出分录（同额/双零换公司/零额新增）",
      all(t not in {e[0] for e in entries} for t in ("C", "D", "F")), str(entries))

# ---------------------------------------------------------------------------
# 2. 初始入账批登记
# ---------------------------------------------------------------------------
fresh_db()
client = app.test_client()

r = post_initial(client, rows=[("T1", "C1", 100), ("T2", "C2", 200)])
check("初始登记成功", r.status_code == 200 and r.get_json()["row_count"] == 2, r.get_json())

r = post_initial(client, batch="B9")
check("同模型同日重复登记拒绝", r.status_code == 400 and "只能登记一次" in r.get_json()["error"],
      r.get_json())

r = post_initial(client, batch="B1", date="2025-06-02")
check("批号全局唯一", r.status_code == 400 and "已存在" in r.get_json()["error"], r.get_json())

r = post_initial(client, batch="B3", model="ghost")
check("模型不在 JSON 拒绝", r.status_code == 400 and "不在已载入" in r.get_json()["error"], r.get_json())

r = post_initial(client, batch="B3", model="not_settle")
check("模型缺输出列拒绝", r.status_code == 400 and "缺少" in r.get_json()["error"], r.get_json())

r = post_initial(client, batch="B3", date="2025-6-1")
check("业务日格式校验", r.status_code == 400 and "YYYY-MM-DD" in r.get_json()["error"], r.get_json())

r = post_initial(client, batch="坏 批号")
check("批号字符校验", r.status_code == 400 and "不合法" in r.get_json()["error"], r.get_json())

r = post_initial(client, batch="B3", raw=csv_bytes([("T1", "C1", "abc")]))
check("文件有误退回", r.status_code == 400 and "整数" in r.get_json()["error"], r.get_json())

r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
state = r.get_json()
check("状态查询-当前批", state["current_batch_id"] == "B1", state)
check("状态查询-批次列表", [b["batch_id"] for b in state["batches"]] == ["B1"], state)

# ---------------------------------------------------------------------------
# 3. 复算：分录正确、当前批推进、链式复算
# ---------------------------------------------------------------------------
r = post_recalc(client, rows=[
    ("T1", "C2", 100),   # 换公司
    ("T2", "C2", 250),   # 同公司 +50
    ("T3", "C1", 70),    # 新增
])
body = r.get_json()
check("复算成功", r.status_code == 200 and body["ok"] and not body["replayed"], body)
check("复算分录正确", entry_tuples(body) == [
    ("T1", "C1", -100), ("T1", "C2", 100), ("T2", "C2", 50), ("T3", "C1", 70),
], body)

r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
check("新批成为当前依据", r.get_json()["current_batch_id"] == "B2", r.get_json())

# 链式：以 B2 为依据再复算
r = post_recalc(client, basis="B2", new="B4", rows=[
    ("T1", "C2", 100), ("T2", "C2", 250), ("T3", "C1", 70),  # 无变化
])
check("链式复算-无差额零分录", r.status_code == 200 and r.get_json()["entry_count"] == 0, r.get_json())
r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
check("链式复算-当前批推进", r.get_json()["current_batch_id"] == "B4", r.get_json())

# ---------------------------------------------------------------------------
# 4. 依据批问题：不存在 / 已被替换 / 模型业务日不符，均不改当前批
# ---------------------------------------------------------------------------
r = post_recalc(client, basis="GHOST", new="B5")
check("依据批不存在拒绝", r.status_code == 400 and "不存在" in r.get_json()["error"], r.get_json())

r = post_recalc(client, basis="B2", new="B5")  # B2 已被 B4 替换
check("依据批已被替换拒绝", r.status_code == 400 and "已被替换" in r.get_json()["error"]
      and "B4" in r.get_json()["error"], r.get_json())

r = post_recalc(client, basis="B4", new="B5", date="2025-06-02")
check("依据批与业务日不符拒绝", r.status_code == 400 and "不符" in r.get_json()["error"], r.get_json())

r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
check("拒绝后当前批不变", r.get_json()["current_batch_id"] == "B4", r.get_json())

# ---------------------------------------------------------------------------
# 5. 幂等：同批号按原依据批和原文件重传返回原单；改依据批或文件拒收
# ---------------------------------------------------------------------------
fresh_db()
client = app.test_client()
init_raw = csv_bytes([("T1", "C1", 100), ("T2", "C2", 200)])
recalc_raw = csv_bytes([("T1", "C1", 130), ("T2", "C2", 200)])
post_initial(client, raw=init_raw)
r = post_recalc(client, raw=recalc_raw)
check("幂等-首次复算", r.status_code == 200 and not r.get_json()["replayed"], r.get_json())

r = post_recalc(client, raw=recalc_raw)  # 同批号 B2、同依据 B1、同文件
body = r.get_json()
check("幂等-原样重传返回原单",
      r.status_code == 200 and body["replayed"] and body["entry_count"] == 1, body)

r = post_recalc(client, raw=csv_bytes([("T1", "C1", 999), ("T2", "C2", 200)]))
check("同批号改文件拒收", r.status_code == 400 and "不一致" in r.get_json()["error"], r.get_json())

r = post_recalc(client, basis="B2", new="B2", raw=recalc_raw)
check("同批号改依据拒收", r.status_code == 400, r.get_json())

r = post_recalc(client, basis="B2", new="B1")  # 新批号撞初始批号
check("新批号撞初始批拒绝", r.status_code == 400 and "已被使用" in r.get_json()["error"], r.get_json())

r = post_recalc(client, basis="B2", new="B2")
check("新批号等于依据批号拒绝", r.status_code == 400 and "相同" in r.get_json()["error"], r.get_json())

r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
check("幂等后当前批不变", r.get_json()["current_batch_id"] == "B2", r.get_json())

# ---------------------------------------------------------------------------
# 6. 分录 CSV 下载
# ---------------------------------------------------------------------------
r = client.get("/api/posting/batches/B2/entries.csv")
text = r.data.decode("utf-8-sig")
check("下载分录 CSV", r.status_code == 200
      and text.splitlines() == ["transaction_id,company_id,amount", "T1,C1,30"], text)
check("下载带正负号", post_recalc(client, basis="B2", new="B3",
      rows=[("T1", "C9", 130), ("T2", "C2", 150)]).status_code == 200)
r = client.get("/api/posting/batches/B3/entries.csv")
check("换公司分录 CSV 内容", r.data.decode("utf-8-sig").splitlines() == [
    "transaction_id,company_id,amount",
    "T1,C1,-130", "T1,C9,130", "T2,C2,-50",
], r.data.decode("utf-8-sig"))

r = client.get("/api/posting/batches/B1/entries.csv")
check("初始批无分录可下", r.status_code == 400 and "初始" in r.get_json()["error"], r.get_json())

r = client.get("/api/posting/batches/NOPE/entries.csv")
check("未知批号 400", r.status_code == 400 and "不存在" in r.get_json()["error"], r.get_json())

# ---------------------------------------------------------------------------
# 7. 并发：同一依据批不能生成两份补记单
# ---------------------------------------------------------------------------
fresh_db()
client = app.test_client()
post_initial(client, rows=[("T1", "C1", 100)])

barrier = threading.Barrier(2)
results = {}


def worker(tag, new_batch):
    c = app.test_client()
    barrier.wait()
    r = post_recalc(c, new=new_batch, rows=[("T1", "C1", 100 + (1 if tag == "a" else 2))])
    results[tag] = r.status_code


threads = [threading.Thread(target=worker, args=("a", "RA")),
           threading.Thread(target=worker, args=("b", "RB"))]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("并发仅一单成功", sorted(results.values()) == [200, 400], results)
r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
recalc_batches = [b for b in r.get_json()["batches"] if b["kind"] == "recalc"]
check("并发后只有一份补记单", len(recalc_batches) == 1, r.get_json())
check("并发后当前批唯一", r.get_json()["current_batch_id"] in ("RA", "RB"), r.get_json())

# 并发下同批号同文件重传：两请求都成功，但仍只有一份补记单
fresh_db()
client = app.test_client()
post_initial(client, rows=[("T1", "C1", 100)])
same_raw = csv_bytes([("T1", "C1", 150)])
barrier2 = threading.Barrier(2)
results2 = {}


def worker2(tag):
    c = app.test_client()
    barrier2.wait()
    r = post_recalc(c, new="SAME", raw=same_raw)
    results2[tag] = (r.status_code, r.get_json().get("replayed"))


threads = [threading.Thread(target=worker2, args=("a",)),
           threading.Thread(target=worker2, args=("b",))]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("并发幂等重传均成功", all(v[0] == 200 for v in results2.values()), results2)
check("并发幂等恰好一次落库", sorted(v[1] for v in results2.values()) == [False, True], results2)
r = client.get("/api/posting/state?model_name=settle&business_date=2025-06-01")
check("并发幂等后仍一份补记单",
      len([b for b in r.get_json()["batches"] if b["kind"] == "recalc"]) == 1, r.get_json())

# ---------------------------------------------------------------------------
# 8. 页面与样例端到端（含一笔交易换公司）
# ---------------------------------------------------------------------------
fresh_db()
client = app.test_client()

r = client.get("/posting")
check("补记页面 200", r.status_code == 200 and "复算差额补记".encode() in r.data)

r = client.get("/api/posting/sample")
sample = r.get_json()
check("结算样例接口", r.status_code == 200 and
      any(m["name"] == "ads_transaction_settlement" for m in sample["models"]), sample)

r = client.post("/api/validate", json=sample)
models = {m["name"]: m for m in r.get_json()["models"]}
check("validate 返回输出列",
      models["ads_transaction_settlement"]["output_columns"]
      == ["transaction_id", "company_id", "settlement_amount"], models)

init_csv = client.get("/api/posting/sample/initial.csv").data
recalc_csv = client.get("/api/posting/sample/recalc.csv").data
r = post_initial(client, batch="INIT-20250601", model="ads_transaction_settlement",
                 raw=init_csv, payload=sample)
check("样例初始登记", r.status_code == 200, r.get_json())
r = post_recalc(client, basis="INIT-20250601", new="RECALC-20250601-1",
                model="ads_transaction_settlement", raw=recalc_csv, payload=sample)
body = r.get_json()
check("样例复算成功", r.status_code == 200 and body["entry_count"] == 5, body)
check("样例含换公司分录",
      ("TX20250601001", "C0101", -1250) in entry_tuples(body)
      and ("TX20250601001", "C0202", 1250) in entry_tuples(body),
      body)

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("补记测试全部通过。")
