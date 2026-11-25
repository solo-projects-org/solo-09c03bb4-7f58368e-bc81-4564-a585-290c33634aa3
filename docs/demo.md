# 在 Swagger 里试查缺口、更正返修单与反查返修时刻

启动后打开 <http://localhost:8000/docs>。以下顺序可以直接在 Swagger 的
“Try it out” 里逐条执行。

## 1. 建档（探伤单 + 合格证）

`POST /ndt-orders`

```json
{
  "order_no": "NDT-1001",
  "weld_no": "W-100",
  "completed_at": "2026-03-01T08:00:00Z",
  "start_mm": 0,
  "end_mm": 1200
}
```

`POST /certificates`（核准段 0–1000 mm）

```json
{
  "cert_no": "CERT-500",
  "weld_no": "W-100",
  "issued_at": "2026-03-10T09:00:00Z",
  "start_mm": 0,
  "end_mm": 1000,
  "ndt_order_nos": ["NDT-1001"]
}
```

## 2. 查缺口

`GET /certificates/CERT-500/gaps`

此时 `gaps` 为空：探伤单被引用、同缝、签发前完成，整段覆盖。

## 3. 补录一张漏登的返修单

`POST /repairs`

```json
{
  "order_no": "REP-1",
  "weld_no": "W-100",
  "occurred_at": "2026-03-05T10:00:00Z",
  "start_mm": 400,
  "end_mm": 600
}
```

回包里 `changed_certificates[0]` 显示 `CERT-500` 的缺口由 `[]` 变为
`400–600`，并给出前后缺口总长。再查一次 `GET /certificates/CERT-500/gaps`，
`gaps` 就是 `[{"start_mm": 400, "end_mm": 600}]`，`evidence[0]` 里还能看到
是哪一张返修单截掉了哪一段（`invalidating_repairs[].overlap`）。

## 4. 同号更正返修单

`PUT /repairs/REP-1`（把返修范围改小到 450–500 mm）

```json
{
  "weld_no": "W-100",
  "occurred_at": "2026-03-05T10:00:00Z",
  "start_mm": 450,
  "end_mm": 500
}
```

回包 `changed_certificates[0]` 给出 `gaps_before = 400–600`、
`gaps_after = 450–500`。更正只复核原返修/新返修可能截断的证书，
`candidates_checked` 反映实际复核数量。

## 5. 验证“只失效相交部分”

把返修起点挪到核准段之外（例如 `start_mm: 0, end_mm: 100`），或把
`occurred_at` 改到签发之后（`2026-03-10T09:00:00Z`），再 `PUT` 一次，
返回的 `gaps_after` 会恢复为 `[]`：签发后的返修不倒推，交集外的部分不失效。

## 6. 反查返修时刻：只有单点吻合

沿用第 1–2 步的档案（`NDT-1001` 于 03-01T08:00 完成，`CERT-500` 于
03-10T09:00 签发，核准段 0–1000），另建一张待反查的返修单（区间 400–600）：

`POST /repairs`

```json
{
  "order_no": "REP-2",
  "weld_no": "W-100",
  "occurred_at": "2026-03-05T10:00:00Z",
  "start_mm": 400,
  "end_mm": 600
}
```

先确认此刻缺口：

`GET /certificates/CERT-500/gaps` → `gaps: [{"start_mm": 400, "end_mm": 600}]`

现在把返修时刻的检索窗口**收紧到签发这一瞬间**，并声明“当时没有缺口”：

`POST /repairs/REP-2/time-lookup`

```json
{
  "window_start": "2026-03-10T09:00:00Z",
  "window_end": "2026-03-10T09:00:00Z",
  "certificates": [
    {"cert_no": "CERT-500", "gaps": []}
  ]
}
```

回包里 `status: "found"`，`common.intervals` 为空，`common.instants` 是单个时刻
`2026-03-10T09:00:00.000000Z`——这是窗口的唯一一点，而且返修恰好落在签发时刻，
按规则不倒推，所以“无缺口”成立。**单个时刻单列在 `instants`**，不与区间混排。

再把窗口放开到 `2026-03-01T00:00:00Z` → `2026-03-10T09:00:00Z`，期望无缺口：

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-03-10T09:00:00Z",
  "certificates": [
    {"cert_no": "CERT-500", "gaps": []}
  ]
}
```

此时 `common` 有两块：`[2026-03-01T00:00:00Z, 2026-03-01T08:00:00Z]`（含两端，
探伤完成及之前返修无效）和孤立时刻 `2026-03-10T09:00:00.000000Z`（签发瞬间）。
中间整段都会切出 400–600 的缺口，所以两块解是分开的列表项：区间进 `intervals`，
单个时刻进 `instants`。想看到真正的区间，改用“当时缺口为 400–600”并取窗口
`2026-03-02T00:00:00Z` → `2026-03-09T00:00:00Z`，会得到
`intervals: [{start, end, start_inclusive: true, end_inclusive: true}]`。

## 7. 反查返修时刻：各证有解但没有共同时刻

再建一张同缝、签发更晚的证书，它引用一张 03-20 才完成的探伤单：

`POST /ndt-orders`

```json
{
  "order_no": "NDT-1002",
  "weld_no": "W-100",
  "completed_at": "2026-03-20T08:00:00Z",
  "start_mm": 0,
  "end_mm": 1200
}
```

`POST /certificates`

```json
{
  "cert_no": "CERT-501",
  "weld_no": "W-100",
  "issued_at": "2026-04-01T09:00:00Z",
  "start_mm": 0,
  "end_mm": 1000,
  "ndt_order_nos": ["NDT-1002"]
}
```

注意：第 6 步建好的 `REP-2` 只有 400–600 这一个区间，用它反查即可。
`CERT-500` 只有在返修早于 03-10T09:00 时才会出现 400–600 的缺口；
`CERT-501` 只有在返修晚于 03-20T08:00 时才会出现同样的缺口。两者不可能同时成立：

`POST /repairs/REP-2/time-lookup`

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-04-01T09:00:00Z",
  "certificates": [
    {"cert_no": "CERT-500", "gaps": [{"start_mm": 400, "end_mm": 600}]},
    {"cert_no": "CERT-501", "gaps": [{"start_mm": 400, "end_mm": 600}]}
  ]
}
```

回包 `status: "no_common_time"`，`common` 为空；但 `certificates` 里两张证各自
`has_solution: true`，并分别给出自己的吻合区间：

- `CERT-500`：`2026-03-01T08:00:00Z`（不含）→ `2026-03-10T09:00:00Z`（不含）
- `CERT-501`：`2026-03-20T08:00:00Z`（不含）→ `2026-04-01T09:00:00Z`（不含）

这正是“各证分别有解却找不到共同时间”的情形。若把某张证的清单改成不可能是它的
缺口（例如 100–200），`status` 会变成 `unsatisfiable`。

两次反查都不会写库：调用前后 `GET /repairs` 不变，`repair_ingest_audit` 也不会
新增记录，其间其他请求照常补录或勘误。
