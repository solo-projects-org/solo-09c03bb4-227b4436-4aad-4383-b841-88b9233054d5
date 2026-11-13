"""复算差额补记：批次登记、两批对比、差额分录与并发安全。

背景：公交刷卡流水补传后，清分工程师对「已入账」的业务日复算，
把新旧两批（结算模型导出的整日 CSV）按交易对比，差额交给财务补记。

规则：
- 每个（结算模型, 业务日）只能登记一次初始入账批，初始批不出分录；
- 复算提交需填依据批号与新批号，按交易对比两批——
  · 同公司：只补记金额差（新 - 旧）；
  · 换公司：冲回旧公司全额、给新公司记全额；
  · 新增交易：记入全额；消失交易：冲回全额；
  · 零额不出分录；
- 复算成功后，新批成为该（模型, 业务日）的当前依据批；
- 依据批已被替换（不再是当前批）或文件有误：退回，不改当前批；
- 同一新批号 + 同一依据批 + 同一文件重传：幂等返回原补记单；
  同一新批号但依据批或文件不同：拒收；
- 并发下同一依据批最多生成一份补记单（BEGIN IMMEDIATE 串行化写事务）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from datetime import datetime

REQUIRED_COLUMNS = ("transaction_id", "company_id", "settlement_amount")
MAX_ROWS = 200_000
MAX_ID_LEN = 128
# 留足余量，避免写入 SQLite INTEGER（64 位）时溢出
INT_MIN, INT_MAX = -(2 ** 62), 2 ** 62 - 1

_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_AMOUNT_RE = re.compile(r"[+-]?\d+")


class PostingError(Exception):
    """补记流程被退回时抛出，message 为可直接展示给用户的中文原因。"""


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    model_name     TEXT NOT NULL,
    business_date  TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('initial', 'recalc')),
    basis_batch_id TEXT,
    file_hash      TEXT NOT NULL,
    entry_count    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_lines (
    batch_id           TEXT NOT NULL REFERENCES batches (batch_id),
    transaction_id     TEXT NOT NULL,
    company_id         TEXT NOT NULL,
    settlement_amount  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, transaction_id)
);
CREATE TABLE IF NOT EXISTS current_batches (
    model_name     TEXT NOT NULL,
    business_date  TEXT NOT NULL,
    batch_id       TEXT NOT NULL REFERENCES batches (batch_id),
    PRIMARY KEY (model_name, business_date)
);
CREATE TABLE IF NOT EXISTS entries (
    batch_id        TEXT NOT NULL REFERENCES batches (batch_id),
    seq             INTEGER NOT NULL,
    transaction_id  TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    amount          INTEGER NOT NULL CHECK (amount <> 0),
    PRIMARY KEY (batch_id, seq)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开连接并确保表结构存在（WAL、外键、忙等 30s，自动提交模式）。"""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def file_hash(raw: bytes) -> str:
    """原文件内容指纹，用于识别「同一文件重传」。"""
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def validate_batch_id(value: str, label: str = "批号") -> str:
    value = (value or "").strip()
    if not _BATCH_ID_RE.fullmatch(value):
        raise PostingError(
            f"{label}「{value}」不合法：1–64 位，仅限字母、数字、下划线和中划线。"
        )
    return value


def validate_business_date(value: str) -> str:
    value = (value or "").strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise PostingError(f"业务日「{value}」不合法，请使用 YYYY-MM-DD 格式。") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise PostingError(f"业务日「{value}」不合法，请使用 YYYY-MM-DD 格式。")
    return value


def parse_settlement_csv(raw: bytes) -> dict[str, tuple[str, int]]:
    """解析模型导出的整日 CSV，返回 {transaction_id: (company_id, settlement_amount)}。

    列固定为 transaction_id,company_id,settlement_amount（整数分），每笔交易一行；
    文件有误（编码、表头、列数、金额非整数、交易重复等）抛 PostingError。
    """
    if not raw or not raw.strip():
        raise PostingError("CSV 文件为空，请上传模型导出的整日文件。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PostingError("CSV 不是 UTF-8 编码，请另存为 UTF-8 后重试。") from exc
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise PostingError("CSV 文件为空，请上传模型导出的整日文件。")

    header = [c.strip() for c in rows[0]]
    if len(header) != len(REQUIRED_COLUMNS) or sorted(header) != sorted(REQUIRED_COLUMNS):
        raise PostingError(
            "CSV 表头必须是 transaction_id,company_id,settlement_amount 三列，"
            f"当前为：{','.join(header) or '（空）'}。"
        )
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    lines: dict[str, tuple[str, int]] = {}
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) != len(REQUIRED_COLUMNS):
            raise PostingError(f"CSV 第 {lineno} 行应有 3 列，实际 {len(row)} 列。")
        txn_id = row[idx["transaction_id"]].strip()
        company_id = row[idx["company_id"]].strip()
        amount_raw = row[idx["settlement_amount"]].strip()
        if not txn_id or len(txn_id) > MAX_ID_LEN:
            raise PostingError(f"CSV 第 {lineno} 行 transaction_id 为空或过长。")
        if not company_id or len(company_id) > MAX_ID_LEN:
            raise PostingError(f"CSV 第 {lineno} 行 company_id 为空或过长。")
        if not _AMOUNT_RE.fullmatch(amount_raw):
            raise PostingError(
                f"CSV 第 {lineno} 行 settlement_amount「{amount_raw}」不是整数（单位：分）。"
            )
        amount = int(amount_raw)
        if not (INT_MIN <= amount <= INT_MAX):
            raise PostingError(f"CSV 第 {lineno} 行 settlement_amount 超出可表示范围。")
        if txn_id in lines:
            raise PostingError(f"CSV 第 {lineno} 行交易「{txn_id}」重复，每笔交易只能一行。")
        lines[txn_id] = (company_id, amount)
        if len(lines) > MAX_ROWS:
            raise PostingError(f"CSV 行数超过 {MAX_ROWS} 上限。")
    if not lines:
        raise PostingError("CSV 没有任何交易行。")
    return lines


# ---------------------------------------------------------------------------
# 差额分录
# ---------------------------------------------------------------------------

def compute_entries(old: dict[str, tuple[str, int]],
                    new: dict[str, tuple[str, int]]) -> list[tuple[str, str, int]]:
    """按交易对比两批，返回 [(transaction_id, company_id, amount)]，正=补记，负=冲回。

    同公司只补金额差；换公司冲回旧公司全额、给新公司记全额；
    新增记入、消失冲回；零额不出分录。按交易号排序，结果确定。
    """
    entries: list[tuple[str, str, int]] = []
    for txn_id in sorted(set(old) | set(new)):
        o = old.get(txn_id)
        n = new.get(txn_id)
        if o is None:
            entries.append((txn_id, n[0], n[1]))          # 新增：记入全额
        elif n is None:
            entries.append((txn_id, o[0], -o[1]))         # 消失：冲回全额
        elif o[0] == n[0]:
            diff = n[1] - o[1]
            if diff:
                entries.append((txn_id, n[0], diff))      # 同公司：只补差额
        else:
            entries.append((txn_id, o[0], -o[1]))         # 换公司：冲回旧公司全额
            entries.append((txn_id, n[0], n[1]))          #          给新公司记全额
    return [e for e in entries if e[2] != 0]              # 零额不出分录


# ---------------------------------------------------------------------------
# 批次登记与复算提交（单写事务，并发安全）
# ---------------------------------------------------------------------------

def _insert_batch(conn: sqlite3.Connection, batch_id: str, model_name: str,
                  business_date: str, kind: str, basis_batch_id: str | None,
                  file_digest: str, entry_count: int,
                  lines: dict[str, tuple[str, int]]) -> None:
    conn.execute(
        "INSERT INTO batches (batch_id, model_name, business_date, kind,"
        " basis_batch_id, file_hash, entry_count, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (batch_id, model_name, business_date, kind, basis_batch_id,
         file_digest, entry_count, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.executemany(
        "INSERT INTO batch_lines (batch_id, transaction_id, company_id, settlement_amount)"
        " VALUES (?,?,?,?)",
        [(batch_id, txn, comp, amt) for txn, (comp, amt) in lines.items()],
    )


def _load_entries(conn: sqlite3.Connection, batch_id: str) -> list[tuple[str, str, int]]:
    return [
        (r["transaction_id"], r["company_id"], r["amount"])
        for r in conn.execute(
            "SELECT transaction_id, company_id, amount FROM entries"
            " WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        )
    ]


def register_initial(db_path: str, model_name: str, business_date: str,
                     batch_id: str, lines: dict[str, tuple[str, int]],
                     file_digest: str) -> None:
    """登记初始入账批：每个（模型, 业务日）仅一次，不出分录。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "SELECT batch_id FROM current_batches"
                " WHERE model_name=? AND business_date=?",
                (model_name, business_date),
            ).fetchone()
            if cur is not None:
                raise PostingError(
                    f"模型「{model_name}」业务日 {business_date} 已登记入账批"
                    f"「{cur['batch_id']}」，每个模型、业务日只能登记一次初始入账批。"
                )
            if conn.execute("SELECT 1 FROM batches WHERE batch_id=?",
                            (batch_id,)).fetchone():
                raise PostingError(f"批号「{batch_id}」已存在，请更换批号。")
            _insert_batch(conn, batch_id, model_name, business_date,
                          "initial", None, file_digest, 0, lines)
            conn.execute(
                "INSERT INTO current_batches (model_name, business_date, batch_id)"
                " VALUES (?,?,?)",
                (model_name, business_date, batch_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def submit_recalc(db_path: str, model_name: str, business_date: str,
                  basis_batch_id: str, new_batch_id: str,
                  lines: dict[str, tuple[str, int]],
                  file_digest: str) -> dict:
    """提交复算结果：对比依据批与新批，落库差额分录，新批成为当前依据批。

    依据批已被替换则退回（不改当前批）；同一新批号按原依据批和原文件重传时
    幂等返回原补记单（replayed=True），依据批或文件不同则拒收。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            basis = conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (basis_batch_id,)).fetchone()
            if basis is None:
                raise PostingError(f"依据批「{basis_batch_id}」不存在，请核对批号。")
            if basis["model_name"] != model_name or basis["business_date"] != business_date:
                raise PostingError(
                    f"依据批「{basis_batch_id}」属于模型「{basis['model_name']}」"
                    f"业务日 {basis['business_date']}，与当前选择的模型/业务日不符。"
                )

            existing = conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (new_batch_id,)).fetchone()
            if existing is not None:
                if (existing["kind"] == "recalc"
                        and existing["basis_batch_id"] == basis_batch_id
                        and existing["file_hash"] == file_digest):
                    # 同批号 + 原依据批 + 原文件：幂等重传，返回原补记单
                    entries = _load_entries(conn, new_batch_id)
                    conn.execute("COMMIT")
                    return {"batch_id": new_batch_id, "entries": entries, "replayed": True}
                raise PostingError(
                    f"新批号「{new_batch_id}」已被使用，且依据批或文件与本次提交不一致，已拒收。"
                )

            cur = conn.execute(
                "SELECT batch_id FROM current_batches"
                " WHERE model_name=? AND business_date=?",
                (model_name, business_date),
            ).fetchone()
            current_id = cur["batch_id"] if cur else None
            if current_id != basis_batch_id:
                detail = (f"，当前依据批为「{current_id}」" if current_id
                          else "，该模型业务日尚未登记入账批")
                raise PostingError(
                    f"依据批「{basis_batch_id}」已被替换{detail}，本次复算已退回，当前批不变。"
                )

            old_lines = {
                r["transaction_id"]: (r["company_id"], r["settlement_amount"])
                for r in conn.execute(
                    "SELECT transaction_id, company_id, settlement_amount"
                    " FROM batch_lines WHERE batch_id=?",
                    (basis_batch_id,),
                )
            }
            entries = compute_entries(old_lines, lines)
            _insert_batch(conn, new_batch_id, model_name, business_date,
                          "recalc", basis_batch_id, file_digest, len(entries), lines)
            conn.executemany(
                "INSERT INTO entries (batch_id, seq, transaction_id, company_id, amount)"
                " VALUES (?,?,?,?,?)",
                [(new_batch_id, seq, txn, comp, amt)
                 for seq, (txn, comp, amt) in enumerate(entries, start=1)],
            )
            conn.execute(
                "UPDATE current_batches SET batch_id=?"
                " WHERE model_name=? AND business_date=?",
                (new_batch_id, model_name, business_date),
            )
            conn.execute("COMMIT")
            return {"batch_id": new_batch_id, "entries": entries, "replayed": False}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_state(db_path: str, model_name: str, business_date: str) -> dict:
    """某（模型, 业务日）的当前依据批与全部批次，供页面展示与预填。"""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT batch_id FROM current_batches"
            " WHERE model_name=? AND business_date=?",
            (model_name, business_date),
        ).fetchone()
        rows = conn.execute(
            "SELECT batch_id, kind, basis_batch_id, entry_count, created_at"
            " FROM batches WHERE model_name=? AND business_date=?"
            " ORDER BY rowid",
            (model_name, business_date),
        ).fetchall()
        return {
            "current_batch_id": cur["batch_id"] if cur else None,
            "batches": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def get_entries(db_path: str, batch_id: str) -> list[tuple[str, str, int]]:
    """下载补记单分录；初始批没有分录。"""
    conn = connect(db_path)
    try:
        batch = conn.execute("SELECT kind FROM batches WHERE batch_id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PostingError(f"批号「{batch_id}」不存在。")
        if batch["kind"] != "recalc":
            raise PostingError(f"批号「{batch_id}」是初始入账批，没有分录可下载。")
        return _load_entries(conn, batch_id)
    finally:
        conn.close()
