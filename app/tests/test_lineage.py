"""lineage.py 测试：传播规则、路径构造、错误处理。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage import AnalysisError, analyze, build_models  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def base_sources():
    return [
        {"table": "t_a", "columns": ["id", "flag", "val", "grp"]},
        {"table": "t_b", "columns": ["id", "name", "amt"]},
    ]


def make_payload(models):
    return {"source_tables": base_sources(), "models": models}


# 1. 纯值依赖：只影响引用列 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, a.flag AS flag, b.name AS name "
     "FROM t_a a INNER JOIN t_b b ON a.id = b.id"},
])
r = analyze(p, "t_a", "flag")
cols = {c["column"]: c for c in r["affected_models"][0]["columns"]}
check("值依赖-仅命中引用列", set(cols) == {"flag"}, str(set(cols)))
check("值依赖-未命中无关列", "name" not in cols and "id" not in cols)
check("值依赖-via=value", cols["flag"]["via"] == "value")

# 2. WHERE 条件污染：全部输出列 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, b.name AS name, a.val AS val "
     "FROM t_a a INNER JOIN t_b b ON a.id = b.id WHERE a.flag = 1"},
])
r = analyze(p, "t_a", "flag")
m = r["affected_models"][0]
check("WHERE污染-全部输出列", {c["column"] for c in m["columns"]} == {"id", "name", "val"})
check("WHERE污染-标记condition_tainted", m["condition_tainted"] is True)
check("WHERE污染-条件查询名", m["condition_queries"] == ["m1"])
check("WHERE污染-无关列via=condition",
      next(c for c in m["columns"] if c["column"] == "name")["via"] == "condition")

# 3. JOIN ON 条件污染 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, b.name AS name "
     "FROM t_a a INNER JOIN t_b b ON a.id = b.id AND a.flag = b.amt"},
])
r = analyze(p, "t_a", "flag")
m = r["affected_models"][0]
check("JOIN污染-全部输出列", {c["column"] for c in m["columns"]} == {"id", "name"})
check("JOIN污染-条件标记", m["condition_tainted"] is True)

# 4. 跨模型：下游实际读取才传播；不读取不传播 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, a.flag AS flag, a.val AS val FROM t_a a"},
    {"name": "m2", "sql":
     "SELECT x.id AS id, x.val AS val FROM m1 x"},  # 只读 id/val，不读 flag
    {"name": "m3", "sql":
     "SELECT y.flag AS flag FROM m1 y"},            # 读 flag
])
r = analyze(p, "t_a", "flag")
by_model = {m["model"]: m for m in r["affected_models"]}
check("跨模型-未读取不受影响", "m2" not in by_model)
check("跨模型-读取者受影响",
      "m3" in by_model and {c["column"] for c in by_model["m3"]["columns"]} == {"flag"})
# m3 路径应跨模型回到源表
path = by_model["m3"]["columns"][0]["path"]
check("跨模型-路径回到源表",
      path[0]["type"] == "source" and path[0]["relation"] == "t_a" and path[0]["column"] == "flag")
check("跨模型-路径含上游模型步骤",
      any(s.get("input_relation") == "m1" for s in path))

# 5. 条件污染向下游整表传播 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, a.flag AS flag, b.name AS name "
     "FROM t_a a INNER JOIN t_b b ON a.id = b.id WHERE a.flag = 1"},
    {"name": "m2", "sql":
     "SELECT x.id AS id, x.name AS name FROM m1 x WHERE x.id > 0"},
])
r = analyze(p, "t_a", "flag")
by_model = {m["model"]: m for m in r["affected_models"]}
check("条件污染下游-全部输出", set(c["column"] for c in by_model["m2"]["columns"]) == {"id", "name"})
# m2 自身 WHERE 用的是未污染的 id，但上游 m1 的全部输出（含 id）已带条件污染，
# 因此 m2 根查询的条件集合同样命中；路径中应能看到来自 m1 的 condition 步骤
check("条件污染下游-m2条件查询被标注", by_model["m2"]["condition_queries"] == ["m2"])
check("条件污染下游-m2继承condition路径",
      all(any(s.get("via") == "condition" for s in c["path"])
          for c in by_model["m2"]["columns"]))

# 6. CTE 内部条件污染 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "WITH c1 AS (SELECT a.id AS id, a.flag AS flag FROM t_a a WHERE a.flag = 1) "
     "SELECT c1.id AS id, c1.flag AS flag FROM c1"},
])
r = analyze(p, "t_a", "flag")
m = r["affected_models"][0]
check("CTE条件-全部输出", {c["column"] for c in m["columns"]} == {"id", "flag"})
check("CTE条件-条件查询标注", m["condition_queries"] == ["c1"])

# 7. 多层 CTE 值传递链 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "WITH c1 AS (SELECT a.flag AS f1, a.id AS id1 FROM t_a a), "
     "c2 AS (SELECT c1.f1 AS f2, c1.id1 AS id2 FROM c1) "
     "SELECT c2.f2 AS out_flag, c2.id2 AS out_id FROM c2"},
])
r = analyze(p, "t_a", "flag")
m = r["affected_models"][0]
cols = {c["column"]: c for c in m["columns"]}
check("多层CTE-仅值列受影响", set(cols) == {"out_flag"})
chain = [s.get("query") for s in cols["out_flag"]["path"] if s["type"] == "node"]
check("多层CTE-路径经过c1/c2/root", chain == ["c1", "c2", "m1"], str(chain))

# 8. 未限定列名 → 报错并指出模型 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql": "SELECT flag FROM t_a a"}])
try:
    build_models(p)
    check("未限定列-报错", False)
except AnalysisError as e:
    check("未限定列-报错", "m_bad" in str(e) and "未带表名" in str(e), str(e))

# 9. 未知别名 → 报错并指出模型，停止分析 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT z.flag AS flag FROM t_a a INNER JOIN t_b b ON a.id = b.id"}])
try:
    build_models(p)
    check("未知别名-报错", False)
except AnalysisError as e:
    check("未知别名-报错", "m_bad" in str(e) and "z" in str(e), str(e))

# 10. 源表不存在的列 → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql": "SELECT a.ghost AS g FROM t_a a"}])
try:
    build_models(p)
    check("幽灵列-报错", False)
except AnalysisError as e:
    check("幽灵列-报错", "m_bad" in str(e) and "ghost" in str(e), str(e))

# 11. 上游模型不存在的输出列 → 报错 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql": "SELECT a.id AS id FROM t_a a"},
    {"name": "m2", "sql": "SELECT x.ghost AS g FROM m1 x"},
])
try:
    build_models(p)
    check("上游幽灵列-报错", False)
except AnalysisError as e:
    check("上游幽灵列-报错", "m2" in str(e) and "ghost" in str(e), str(e))

# 12. 星号 → 报错 ----------------------------------------------------------------
for bad_sql in ["SELECT * FROM t_a a", "SELECT a.* FROM t_a a"]:
    p = make_payload([{"name": "m_bad", "sql": bad_sql}])
    try:
        build_models(p)
        check(f"星号报错-{bad_sql}", False)
    except AnalysisError as e:
        check(f"星号报错-{bad_sql}", "星号" in str(e), str(e))

# 13. 递归 CTE → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "WITH RECURSIVE r AS (SELECT a.id AS n FROM t_a a) "
                  "SELECT r.n AS n FROM r"}])
try:
    build_models(p)
    check("递归CTE-报错", False)
except AnalysisError as e:
    check("递归CTE-报错", "递归" in str(e), str(e))

# 14. LEFT JOIN → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT a.id AS id FROM t_a a LEFT JOIN t_b b ON a.id = b.id"}])
try:
    build_models(p)
    check("LEFT JOIN-报错", False)
except AnalysisError as e:
    check("LEFT JOIN-报错", "INNER" in str(e), str(e))

# 14b. 逗号隐式连接 → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT a.id AS id FROM t_a a, t_b b WHERE a.id = b.id"}])
try:
    build_models(p)
    check("逗号连接-报错", False)
except AnalysisError as e:
    check("逗号连接-报错", "INNER" in str(e), str(e))

# 14c. SELECT 投影中的标量子查询 → 报错并提示改写 ------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT a.id AS id, (SELECT b.amt AS amt FROM t_b b) AS m FROM t_a a"}])
try:
    build_models(p)
    check("标量子查询-报错", False)
except AnalysisError as e:
    check("标量子查询-报错", "标量子查询" in str(e) and "m_bad" in str(e), str(e))

# 15. 未知表 → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql": "SELECT x.id AS id FROM t_x x"}])
try:
    build_models(p)
    check("未知表-报错", False)
except AnalysisError as e:
    check("未知表-报错", "t_x" in str(e), str(e))

# 16. 模型循环依赖 → 报错 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql": "SELECT y.id AS id FROM m2 y"},
    {"name": "m2", "sql": "SELECT x.id AS id FROM m1 x"},
])
try:
    build_models(p)
    check("循环依赖-报错", False)
except AnalysisError as e:
    check("循环依赖-报错", "循环" in str(e), str(e))

# 17. 非目标字段不产生影响（空结果） ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql": "SELECT a.id AS id, b.name AS name "
                          "FROM t_a a INNER JOIN t_b b ON a.id = b.id"},
])
r = analyze(p, "t_a", "val")  # val 没被任何模型读取
check("未读取字段-零影响", r["model_count"] == 0 and r["column_count"] == 0)

# 18. 目标表/列不存在 ----------------------------------------------------------------
try:
    analyze(make_payload([]), "t_nope", "x")
    check("目标表不存在-报错", False)
except AnalysisError as e:
    check("目标表不存在-报错", "t_nope" in str(e), str(e))

# 19. FROM 子查询的依赖与条件污染 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT sq.id AS id, sq.f AS f FROM "
     "(SELECT a.id AS id, a.flag AS f FROM t_a a WHERE a.flag = 1) sq"},
])
r = analyze(p, "t_a", "flag")
m = r["affected_models"][0]
check("子查询条件-全部输出", {c["column"] for c in m["columns"]} == {"id", "f"})
check("子查询条件-标注子查询名", "sq" in m["condition_queries"])

# 20. 非关联 WHERE IN 子查询：子查询内部条件只污染子查询自身输出，
#     外层查询通过 IN 使用该输出，属于外层 WHERE 条件 -> 外层全部输出被污染
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, a.val AS val FROM t_a a "
     "WHERE a.id IN (SELECT b.id AS id FROM t_b b WHERE b.amt = 1)"},
])
r2 = analyze(p, "t_b", "amt")
m = r2["affected_models"][0]
check("IN子查询-amt经IN条件污染全输出",
      {c["column"] for c in m["columns"]} == {"id", "val"})
# 外层根查询经 IN 谓词命中，内层子查询自身的 WHERE 也命中
check("IN子查询-外层与子查询均标注",
      m["condition_queries"] == ["IN 子查询#1", "m1"])
# t_a.flag 与整条 SQL 无关
r = analyze(p, "t_a", "flag")
check("IN子查询-无关字段零影响", r["model_count"] == 0)

# 21. 同一模型内 CTE 输出污染向根传播 ----------------------------------------------------------------
p = make_payload([
    {"name": "m1", "sql":
     "WITH c1 AS (SELECT a.id AS id, a.flag AS flag FROM t_a a WHERE a.flag = 1) "
     "SELECT c1.id AS id FROM c1"},
])
r = analyze(p, "t_a", "flag")
check("CTE污染向根传播", r["affected_models"][0]["columns"][0]["column"] == "id")

# 22. 样例文件可端到端运行 ----------------------------------------------------------------
sample_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "sample_clearing.json")
with open(sample_path, encoding="utf-8") as fh:
    sample = json.load(fh)
r = analyze(sample, "ods_card_transaction", "transfer_flag")
check("样例-3个模型全部受影响", r["model_count"] == 3)
check("样例-19个输出列受影响", r["column_count"] == 19)
# 每个路径都能回溯到源表种子
ok = all(c["path"][0]["type"] == "source"
         for m in r["affected_models"] for c in m["columns"])
check("样例-所有路径回到源表", ok)

# 23. 关联子查询 → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT a.id AS id FROM t_a a WHERE a.id IN "
                  "(SELECT b.id AS id FROM t_b b WHERE b.amt = a.flag)"}])
try:
    build_models(p)
    check("关联子查询-报错", False)
except AnalysisError as e:
    check("关联子查询-报错", "关联" in str(e), str(e))

# 24. 非关联 EXISTS 子查询：内部条件污染经 EXISTS 扩散到外层全部输出 -----------------------
p = make_payload([
    {"name": "m1", "sql":
     "SELECT a.id AS id, a.val AS val FROM t_a a "
     "WHERE EXISTS (SELECT b.id AS id FROM t_b b WHERE b.amt = 1)"},
])
r = analyze(p, "t_b", "amt")
m = r["affected_models"][0]
check("EXISTS-条件污染全输出",
      {c["column"] for c in m["columns"]} == {"id", "val"})
check("EXISTS-谓词类型标注",
      any(step.get("predicate") == "EXISTS"
          for c in m["columns"] for step in c["path"]))
check("EXISTS-条件查询名",
      any("EXISTS" in q for q in m["condition_queries"]), str(m["condition_queries"]))

# 25. 关联 EXISTS → 报错 ----------------------------------------------------------------
p = make_payload([{"name": "m_bad", "sql":
                  "SELECT a.id AS id FROM t_a a "
                  "WHERE EXISTS (SELECT b.id AS id FROM t_b b WHERE b.id = a.flag)"}])
try:
    build_models(p)
    check("关联EXISTS-报错", False)
except AnalysisError as e:
    check("关联EXISTS-报错", "关联" in str(e), str(e))

# 26. 多上游精确隔离：纯值依赖下，未污染的第三方表列不命中 -------------------------------
p = make_payload([
    {"name": "m1", "sql": "SELECT a.id AS id, a.flag AS flag FROM t_a a"},
    {"name": "m2", "sql":
     "SELECT x.id AS id, x.flag AS flag, b.amt AS amt "
     "FROM m1 x INNER JOIN t_b b ON x.id = b.id"},
])
r = analyze(p, "t_a", "flag")
m2 = next(m for m in r["affected_models"] if m["model"] == "m2")
check("多上游隔离-仅flag命中",
      {c["column"] for c in m2["columns"]} == {"flag"})

# 27. diamond 依赖：两个分支都读同一上游，汇合模型仍能正确传播且路径回源 -------------------
p = make_payload([
    {"name": "m1", "sql": "SELECT a.id AS id, a.flag AS flag FROM t_a a WHERE a.flag = 1"},
    {"name": "m3", "sql": "SELECT x.id AS id, x.flag AS flag FROM m1 x"},
    {"name": "m4", "sql": "SELECT y.id AS id, y.flag AS flag FROM m1 y"},
    {"name": "m5", "sql":
     "SELECT u.id AS uid, v.flag AS vflag FROM m3 u INNER JOIN m4 v ON u.id = v.id"},
])
r = analyze(p, "t_a", "flag")
names = [m["model"] for m in r["affected_models"]]
check("diamond-四模型全命中", names == ["m1", "m3", "m4", "m5"])
m5 = next(m for m in r["affected_models"] if m["model"] == "m5")
check("diamond-汇合模型全列污染",
      {c["column"] for c in m5["columns"]} == {"uid", "vflag"})
check("diamond-路径均回源",
      all(c["path"][0]["type"] == "source" and c["path"][0]["column"] == "flag"
          for c in m5["columns"]))

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("全部测试通过。")
