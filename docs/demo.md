# 在 Swagger 里试查缺口与更正返修单

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
