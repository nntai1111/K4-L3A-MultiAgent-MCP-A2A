# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case chạy độc lập, tuần tự, qua một pipeline cố định. Tất cả agent chạy trong cùng
một process: "message" giữa các agent là object Python mang `case_id`, không đi qua mạng.
Chỉ MCP Evidence Gateway là dịch vụ bên ngoài.

```text
inputs/<case_id>.json
        │  case_received
        ▼
  coordinator ── task_assigned ──► order-agent ────┐
        │                          payment-agent ──┼── MCP get_* ── tool_result_consumed
        │                          shipment-agent ─┘
        │◄────────── handoff (SpecialistResult) ───┘
        │
        └── task_assigned ──► policy-agent ── MCP get_policy / get_customer_history
                                   │  policy_decided
                                   ▼  handoff (bản nháp output)
                                verifier ── verification_completed ──► coordinator
                                                                           │
                        outputs/<case_id>.json ◄── schema check (cli.py) ──┘  case_finalized
```

1. `cli.py` đọc case, emit `case_received`, gọi `solve_case`.
2. Coordinator lấy `order_id` và các định danh khác có trong input. Lời khiếu nại của khách
   chỉ dùng để tạo claim cần kiểm tra, **không bao giờ là bằng chứng**.
3. Coordinator giao việc lần lượt cho order-agent → payment-agent → shipment-agent. Cả ba
   luôn chạy, vì L3A không chấm efficiency (trọng số 0%) và thiếu evidence thì bị hard gate.
   Mỗi specialist gọi MCP với `case_id` hiện tại, emit `tool_result_consumed` cho từng evidence
   dùng để rút ra facts, rồi trả `SpecialistResult` và emit `handoff` về coordinator.
4. Coordinator gom kết quả vào `CaseState` và giao cho policy-agent. Policy-agent gọi
   `get_policy`, quyết định nghiệp vụ, emit `policy_decided`, rồi `handoff` bản nháp output sang
   verifier.
5. Verifier kiểm tra bất biến (mục 6): sửa những gì evidence chứng minh được, hạ mức những gì
   không chứng minh được, đặt `confidence`, emit `verification_completed`.
6. `cli.py` validate schema, ghi `outputs/<case_id>.json`, emit `case_finalized`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| `coordinator` | `inputs/<case_id>.json` | Lấy `order_id` và ngữ cảnh; giao việc; gom `SpecialistResult` vào `CaseState`; bọc mỗi case trong lưới an toàn (lỗi bất kỳ → fallback output, không dừng batch) | `task_assigned` cho từng agent; trả output đã verify cho `cli.py` |
| `order-agent` | `case_id`, `order_id` | Lấy order, items, seller, product; trích trạng thái order, id item/seller, giá | `SpecialistResult` → `handoff` về coordinator |
| `payment-agent` | `case_id`, `order_id` | Lấy payments, payment timeline, refund timeline; phát hiện trả nhiều lần, lệch số tiền, split payment, trạng thái hoàn tiền | `SpecialistResult` → `handoff` về coordinator |
| `shipment-agent` | `case_id`, `order_id` | Lấy ngày giao dự kiến/thực tế, hạn bàn giao của seller, sự kiện vận chuyển; xác định trễ do seller hay do logistics | `SpecialistResult` → `handoff` về coordinator |
| `policy-agent` | `CaseState` (facts của 3 specialist) | Gọi `get_policy`; quyết định `primary_issue`, `case_status`, `ranked_causes`, `responsible_parties`, `financial_resolution`, `resolution_actions`; gộp `data_conflicts` và `claim_assessments` vào bản nháp | `policy_decided`; `handoff` bản nháp output → verifier |
| `verifier` | Bản nháp output + sổ evidence của case | Kiểm tra bất biến, sửa hoặc hạ mức, đặt `confidence`; không gọi MCP | `verification_completed` → coordinator |

Quyền gọi tool. Hàm gọi MCP dùng chung nhận tên actor và từ chối mọi tool nằm ngoài danh
sách của actor đó:

| Actor | Tool MCP được gọi | Domain dự kiến |
| --- | --- | --- |
| `coordinator` | — | — |
| `order-agent` | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` | order, item, seller, product |
| `payment-agent` | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | payment, refund |
| `shipment-agent` | `get_shipment_summary` | shipment |
| `policy-agent` | `get_policy`, `get_customer_history` | policy, customer |
| `verifier` | — | — |

Tham số: 8 tool đầu cần `case_id` + `order_id`; `get_policy` cần `policy_version`;
`get_customer_history` cần `customer_unique_id`. Hai giá trị này lấy từ input hoặc từ evidence
đã nhận, không bao giờ đoán.

## 3. A2A protocol

**Envelope giao việc** (coordinator → agent):

| Field | Ý nghĩa |
| --- | --- |
| `case_id` | Case đang xử lý. Mọi message và trace event đều mang nó. |
| `sender`, `recipient` | Tên actor theo quy ước ở trên. |
| `task` | Mã việc, ví dụ `COLLECT_ORDER_EVIDENCE`, dùng làm `decision_code` của `task_assigned`. |
| `payload` | Dữ liệu agent cần: `order_id`, `policy_version`, facts đã gom… |

**Envelope kết quả** (`SpecialistResult`, agent → coordinator):

| Field | Ý nghĩa |
| --- | --- |
| `case_id` | Phải trùng case đang xử lý; coordinator loại bỏ kết quả lệch case. |
| `actor` | Agent trả kết quả. |
| `status` | `ok`, `not_found` hoặc `error`. |
| `facts` | Dữ liệu trích từ `data` của MCP response. |
| `evidence` | Danh sách `{evidence_ref, tool_name, domain}`; chỉ gồm evidence đã emit `tool_result_consumed`. |
| `warnings` | Chuỗi ngắn, gồm cả `warnings` của MCP response. |

**Correlation theo `case_id`.** `CaseState` được tạo mới cho mỗi case và bỏ đi sau
`case_finalized`. Sổ evidence nằm trong `CaseState`, nên evidence không thể dùng chéo case.
Mọi lần gọi MCP lấy `case_id` từ `CaseState`, không từ biến dùng lại hay hằng số.

**Điều kiện handoff:**

| Từ → đến | Khi nào |
| --- | --- |
| coordinator → specialist | Ngay sau `case_received`, lần lượt cả ba. |
| specialist → coordinator | Khi đã gọi xong các tool của mình, kể cả khi `not_found` hoặc `error`. |
| coordinator → policy-agent | Khi đã nhận đủ ba `SpecialistResult`, bất kể status. |
| policy-agent → verifier | Khi bản nháp đã có đủ các field bắt buộc của schema. |
| verifier → coordinator | Luôn luôn, kèm `decision_code`. |

**Timeout.** Mỗi lần gọi MCP có giới hạn thời gian của gateway (connect 30 s, read 300 s). Timeout
được thử lại tối đa 2 lần; quá giới hạn thì agent trả `status: error`, không chờ vô hạn.

**Tránh vòng lặp.** Luồng là đồ thị một chiều: mỗi agent chạy tối đa một lần mỗi case, không
agent nào giao việc ngược lên trên, và verifier sửa tại chỗ thay vì yêu cầu specialist gọi lại
MCP. Số lần gọi MCP mỗi case vì thế có trần cố định.

**Trace.** Chỉ ghi sự kiện quan sát được; `attributes` chỉ chứa mã và số đếm. Không ghi lời khách,
nội dung suy luận hay API key.

| `event_type` | `actor` | `target` | `decision_code` | Khi nào |
| --- | --- | --- | --- | --- |
| `case_received` | coordinator | — | — | `cli.py`, đầu case |
| `task_assigned` | coordinator | agent nhận việc | mã `task` | Trước khi agent chạy |
| `tool_result_consumed` | agent gọi tool | — | — | Mỗi evidence dùng để rút facts; kèm `tool_name`, `evidence_refs=[ref]` |
| `handoff` | agent gửi | agent nhận | `OK` / `NOT_FOUND` / `ERROR` | Khi agent xong việc |
| `policy_decided` | policy-agent | verifier | `primary_issue` | Sau khi quyết định |
| `verification_completed` | verifier | coordinator | `VERIFIED` / `REPAIRED` / `FALLBACK` | Sau khi kiểm tra |
| `case_finalized` | coordinator | — | — | `cli.py`, sau khi ghi output |

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.
