# 焊缝合格证缺口核查 API

补录或更正返修单后，核对**早先签发的焊缝合格证**是否因探伤依据被截断而出现
“未覆盖区间”（下称缺口）。档案存 SQLite，FastAPI 提供 HTTP 接口与 Swagger UI。

## 快速启动

```bash
docker compose up --build -d
open http://localhost:8000/docs
curl http://localhost:8000/healthz
```

数据库落在命名卷 `archive-data` 的 `/data/archive.db`，容器重建不丢档案。

本地不带 Docker 运行（开发用）：

```bash
python3 -m pip install -r requirements-dev.txt
ARCHIVE_DB=./archive.db uvicorn app.main:app --reload
pytest -q
```

## 数据模型

| 表 | 含义 | 关键字段 |
| --- | --- | --- |
| `ndt_orders` | 探伤单（拍片） | 单号、焊缝号、完成时刻、覆盖区间 |
| `certificates` | 合格证 | 证号、焊缝号、签发时刻、核准区间 |
| `certificate_citations` | 证 → 探伤单的引用 | 证号、探伤单号、引用顺序 |
| `repairs` | 返修单（可补录、可同号更正） | 单号、焊缝号、发生时刻、区间 |
| `repair_ingest_audit` | 每次补录/更正的留痕 | 动作、候选数、前后缺口快照 |

所有区间都是**半开区间** `[起点, 终点)`，单位毫米，起点含、终点不含。
`[0,10)` 与 `[10,20)` 不相交；这是“拍片末端”和“返修起点”能否恰好衔接
的判断基础。空区间（起点 ≥ 终点）在写入前即被拒绝。

时刻写库统一规范化为 UTC、微秒精度、`...Z` 结尾，因此字符串比较即时间比较。

## 核查规则

一张合格证的缺口 = **核准区间** 减去 **有效探伤覆盖** 的并集。

探伤单只有同时满足以下条件才算“有效探伤”：

1. **被这张证引用**（在 `certificate_citations` 中）——未被证书记载的探伤单
   不构成该证的依据，哪怕同缝、时间更早。
2. **同一焊缝**；异缝引用不采信，并在报告里标 `weld_mismatch`。
3. **在签发前完成**（严格早于签发时刻）；与签发同一时刻完成的也不采信
   （`not_completed_before_issuance`）。

有效探伤覆盖先与核准区间取交集，探伤超出核准范围的部分不能填补缺口。

返修对某张证的失效范围：

- 只考虑 **探伤完成之后、证书签发之前** 发生的返修。拍片之前的返修拍片时
  已可见；签发时刻及之后的返修不能倒推削弱已签发的依据。
- 只失效 **返修区间与该探伤覆盖相交的部分**，不是整段返修区间，也不是整张证。
- 多条返修按并集扣减；重叠返修不会重复扣减。

## 补录 / 更正返修单

补录（`POST /repairs`）与同号更正（`PUT /repairs/{repair_no}`）都返回
**受影响的证书及其前后缺口**：

```json
{
  "repair": {"order_no": "REP-1", "weld_no": "W-100", "occurred_at": "...",
             "interval": {"start_mm": 400, "end_mm": 600}},
  "action": "created",
  "candidates_checked": 1,
  "changed_count": 1,
  "changed_certificates": [{
    "cert_no": "CERT-500", "weld_no": "W-100",
    "gaps_before": [],
    "gaps_after": [{"start_mm": 400, "end_mm": 600}],
    "gap_total_before_mm": 0, "gap_total_after_mm": 200
  }],
  "audit_id": 1
}
```

只复核 **原返修或新返修可能截断证上引证** 的证书：同缝、同一探伤覆盖（裁到
核准区间后）与之相交、且返修时刻落在“探伤完成 → 证书签发”之间。其余证书
在构造上缺口不变，既不重算也不出现在结果里。更正时同时用**旧版本**和
**新版本**两个区间求候选，因此“缩小/挪走返修”导致的缺口恢复也能被捕获。

`candidates_checked` 是实际复核的证书数，便于核对筛查范围；`changed_count`
只统计前后缺口真正不同的证书。同号更正若四个字段都没变，返回
`action: "unchanged"`，不产生虚假变更。

**原子性**：返修行写入/更新与审计留痕在同一个 `BEGIN IMMEDIATE` 事务里。
任一步失败整体回滚，原有档案不受影响、仍可查询。读接口用快照事务，不会看到
半写状态。

## 接口一览（Swagger UI 可直接试）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| POST | `/ndt-orders` | 登记探伤单 |
| POST | `/certificates` | 签发合格证（引用探伤单号） |
| GET | `/certificates` | 列出合格证 |
| GET | `/certificates/{cert_no}/gaps` | **查证书缺口**（含逐条探伤采信/失效明细） |
| GET | `/repairs` | 列出返修单 |
| POST | `/repairs` | **补录返修单**，返回缺口变化的证号与前后缺口 |
| PUT | `/repairs/{repair_no}` | **同号更正返修单**，返回缺口变化的证号与前后缺口 |

演示数据与两条 Swagger 试查步骤见 [`docs/demo.md`](docs/demo.md)。

## 测试

```bash
pytest -q          # 40 项：区间代数、HTTP 规则、事务回滚、并发、随机化一致性
ruff check app tests
```

随机化一致性测试会把增量回报与“全量重算”逐次比对：每次都断言
`changed_certificates` 集合恰好等于全库缺口真正变化的集合，且候选集合
与对规则独立重述的结果一致。

## 已知边界

- 未做身份认证/鉴权；预期部署在受信内网，鉴权由反向代理承担。
- 证书与探伤单目前只支持新建，不支持改号或改区间；只有返修单支持同号更正。
- 结果全部整数毫米；不处理亚毫米精度。
