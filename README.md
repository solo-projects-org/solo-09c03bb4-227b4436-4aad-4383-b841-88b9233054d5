# 公交清分工具集 · 血缘影响分析 + 复算差额补记

本仓库提供两个网页工具，共用一个服务（Flask + Waitress，端口 8000）：

1. **换乘标志口径调整影响分析**（`/`）：用 [SQLGlot](https://github.com/tobymao/sqlglot) 解析
   DuckDB SQL，从选中的源表字段出发沿模型血缘传播，给出受影响模型、输出列与逐跳依赖路径。
2. **复算差额补记工作台**（`/posting`）：刷卡流水补传后，对已入账业务日复算，
   按交易对比新旧两批（结算模型导出的整日 CSV），生成差额分录交财务补记，
   分录与批次一并落库 SQLite。

纯本地运行，**不需要连接数仓，也不执行模型 SQL**；两个页面均可一键载入清分样例。

## 复算差额补记（/posting）

### 流程

1. **载入模型 JSON**：沿用与血缘分析相同的 JSON / SQL 校验（不执行 SQL），
   输出列含 `transaction_id`、`company_id`、`settlement_amount` 的模型才能选作结算模型；
2. **登记初始入账批**：选结算模型和业务日，上传该模型导出的整日 CSV
   （列为 `transaction_id,company_id,settlement_amount`，整数分，每笔交易一行）。
   每个模型、业务日只能登记一次初始入账批，初始批**不出分录**；
3. **提交复算结果**：填**依据批号**（须为当前依据批）和**新批号**，上传复算后的整日 CSV。
   按交易比较两批生成差额分录，新批随即成为下次补记的依据批；
4. **下载分录 CSV**：列出交易号、公司和正负金额（正=补记，负=冲回）。

### 分录规则

| 情形 | 分录 |
| --- | --- |
| 同公司、金额变化 | 只补记金额差（新 − 旧） |
| 换公司 | 冲回旧公司全额、给新公司记全额（两笔） |
| 新增交易 | 记入全额 |
| 消失交易 | 冲回全额 |
| 零额 | 不出分录 |

### 退回与幂等

- **依据批已被替换**（不再是当前依据批）或 **CSV 文件有误**：整单退回，当前批不变；
- **并发**：写路径是单个 `BEGIN IMMEDIATE` 事务，同一依据批并发提交时串行，
  最多生成一份补记单，其余以「依据批已被替换」退回；
- **幂等重传**：同一新批号 + 同一依据批 + 同一文件（SHA-256）重传，返回原补记单，
  不重复落库；同一新批号但依据批或文件不同：拒收。

### 样例（一笔交易换公司）

「载入结算样例」载入 `sample_settlement.json`（含交易级结算模型
`ads_transaction_settlement`），再用「使用样例初始 CSV / 使用样例复算 CSV」走一遍流程：

- 初始批 `INIT-20250601`（业务日 2025-06-01）：4 笔交易；
- 复算批 `RECALC-20250601-1`：`TX20250601001` 由 `C0101` 换到 `C0202`（冲 -1250、记 +1250），
  `TX20250601002` 同公司 800→900（补 +100），`TX20250601003` 消失（冲 -300），
  `TX20250601005` 新增（记 +450），`TX20250601004` 不变不出分录。

### HTTP API（补记）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/posting` | 补记工作台页面 |
| GET | `/api/posting/sample` | 结算样例模型 JSON |
| GET | `/api/posting/sample/initial.csv`、`/api/posting/sample/recalc.csv` | 样例 CSV |
| POST | `/api/posting/initial` | 登记初始入账批（multipart：`payload`、`model_name`、`business_date`、`batch_id`、`file`） |
| POST | `/api/posting/recalc` | 提交复算（multipart 增加 `basis_batch_id`、`new_batch_id`） |
| GET | `/api/posting/state?model_name=&business_date=` | 当前依据批与批次历史 |
| GET | `/api/posting/batches/<批号>/entries.csv` | 下载该复算批的分录 CSV |

`payload` 为页面上已载入的模型 JSON 原文，服务端沿用现有校验重新验证并确认结算模型。
错误统一返回 HTTP 400 与 `{"ok": false, "error": "中文原因"}`。

## 血缘影响分析（/）

### 分析口径

- 普通 `SELECT`、**非递归** CTE（`WITH`）、`INNER JOIN`、`WHERE`；
- 禁止星号（`SELECT *` / `t.*`），所有列引用必须带表名或别名；
- 支持 `WHERE ... IN (SELECT ...)` / `WHERE EXISTS (SELECT ...)` 形式的非关联谓词子查询、
  FROM 派生表（需别名）；投影中允许任意函数、运算、`CASE`、类型转换等表达式；
- 不支持递归 CTE、关联子查询、SELECT 投影中的标量子查询、`UNION` 等集合运算、
  `GROUP BY`、窗口函数、`LEFT/RIGHT/FULL/CROSS JOIN`、逗号隐式连接、DML 等，
  遇到即报错指出所在模型并停止分析。

### 传播规则

1. **取值依赖按列引用传递**：输出表达式引用了某列，该列的污染传到这个输出列。
2. **条件污染整表扩散**：字段一旦进入某查询的 `JOIN ON`、`WHERE` 或
   `IN (SELECT ...)` 谓词（含谓词子查询的污染结果集），该查询的**全部输出列**都标记受影响。
3. **跨模型只走真实读取**：下游模型实际 `FROM/JOIN` 读取了上游模型的哪些输出列，
   才接收对应污染；模型依赖必须是有向无环图。
4. 任一列引用无法解析（未知表/别名、源表或上游模型上不存在的列、未限定列名等）时，
   指出所在模型与具体位置并**停止分析**。

### HTTP API（血缘）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 换乘标志影响分析样例 JSON |
| POST | `/api/validate` | 校验 JSON 与全部 SQL，返回源表字段清单、模型读取关系与输出列 |
| POST | `/api/analyze?table=<源表>&column=<字段>` | 返回受影响模型、输出列与依赖路径 |

## 目录结构

```
.
├── docker-compose.yml            # 一键启动（含 SQLite 持久化卷）
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py                    # Flask 路由（血缘分析 + 复算补记）
    ├── lineage.py                # SQLGlot 解析、血缘构建、污染传播与路径构造
    ├── posting.py                # 批次登记、两批对比、差额分录、SQLite 事务与幂等
    ├── data/
    │   ├── sample_clearing.json    # 血缘分析样例（3 源表 + 3 模型）
    │   ├── sample_settlement.json  # 结算补记样例（交易级结算模型）
    │   ├── sample_initial.csv      # 样例初始批 CSV
    │   └── sample_recalc.csv       # 样例复算批 CSV（一笔交易换公司）
    ├── templates/
    │   ├── index.html              # 血缘分析页
    │   └── posting.html            # 复算差额补记工作台
    └── tests/
        ├── test_lineage.py         # 传播规则与错误处理测试
        ├── test_api.py             # 血缘 API 测试
        └── test_posting.py         # 补记规则、幂等与并发测试
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（血缘分析）或 <http://localhost:8000/posting>（复算补记）。

- 差额分录保存在 SQLite（容器内 `/data/posting.db`，命名卷 `clearing-data` 持久化）；
- 停止：`docker compose down`；**清空全部批次数据**：`docker compose down -v`。

## 输入 JSON 格式

```json
{
  "source_tables": [
    { "table": "ods_card_transaction",
      "columns": ["transaction_id", "card_id", "transfer_flag"] }
  ],
  "models": [
    { "name": "dwd_card_transaction_detail",
      "description": "可选说明",
      "sql": "SELECT t.transaction_id AS transaction_id, t.transfer_flag AS transfer_flag FROM ods_card_transaction t WHERE t.transfer_flag = 0" }
  ]
}
```

- `source_tables[].table` 为源表名，`columns` 为该表全部字段；
- `models[].name` 为模型名，模型 SQL 中 `FROM/JOIN` 同名关系即视为读取该上游模型；
- 其余名字按源表处理，必须在 `source_tables` 中声明。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin
pip install -r app/requirements.txt
python app/app.py                    # 监听 8000，SQLite 默认落 app/data/posting.db
python app/tests/test_lineage.py     # 血缘规则测试
python app/tests/test_api.py         # 血缘 API 测试
python app/tests/test_posting.py     # 补记规则、幂等与并发测试
```

环境变量 `POSTING_DB_PATH` 可覆盖 SQLite 落库路径。
