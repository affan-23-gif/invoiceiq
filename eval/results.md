# InvoiceIQ eval results

18 invoices: 3 layouts, 4 scanned images, US and EU number/date formats, 10 clean and 8 with a planted problem. Each invoice starts from a fresh database.

| Metric | `gemini:gemini-3.5-flash-lite` | `regex-baseline` |
|---|---|---|
| Header fields correct (9 per invoice) | 100% | 69% |
| …on scanned images only | 100% | 0% |
| Line items found (recall) | 100% | 0% |
| Line items correct (precision) | 100% | 0% |
| Final decision correct | 100% | 50% |
| Planted problems caught | 8/8 | 4/8 |
| Clean invoices auto-approved | 10/10 | 2/10 |
| Avg time per invoice | 6.2s | 0.0s |
| Total cost (18 invoices) | $0.0046 | $0.0000 |

## `gemini:gemini-3.5-flash-lite` per invoice

| # | Type | Fields | Lines | Decision | Issues raised | Notes |
|---|---|---|---|---|---|---|
| c01 | pdf, classic | 9/9 | 3/3 | ✅ auto_approved | – |  |
| c02 | pdf, modern | 9/9 | 3/3 | ✅ auto_approved | – |  |
| c03 | pdf, compact | 9/9 | 3/3 | ✅ auto_approved | – |  |
| c04 | pdf, compact | 9/9 | 2/2 | ✅ auto_approved | – |  |
| c05 | pdf, modern | 9/9 | 2/2 | ✅ needs_review | PO_INFERRED |  |
| c06 | pdf, classic | 9/9 | 2/2 | ✅ needs_review | PRICE_VARIANCE |  |
| c07 | pdf, compact | 9/9 | 3/3 | ✅ needs_review | QTY_OVER_PO |  |
| c08 | pdf, modern | 9/9 | 2/2 | ✅ needs_review | MATH_TOTAL |  |
| c09 | image, classic | 9/9 | 3/3 | ✅ rejected | DUPLICATE |  |
| c10 | pdf, modern | 9/9 | 2/2 | ✅ needs_review | NO_PO, UNKNOWN_VENDOR |  |
| c11 | pdf, classic | 9/9 | 1/1 | ✅ needs_review | PO_CLOSED |  |
| c12 | image, modern | 9/9 | 3/3 | ✅ auto_approved | – |  |
| c13 | image, classic | 9/9 | 2/2 | ✅ auto_approved | – |  |
| c14 | pdf, classic | 9/9 | 24/24 | ✅ auto_approved | – |  |
| c15 | pdf, modern | 9/9 | 2/2 | ✅ needs_review | MATH_LINE, TOTAL_VS_PO |  |
| c16 | pdf, modern | 9/9 | 2/2 | ✅ auto_approved | – |  |
| c17 | image, compact | 9/9 | 2/2 | ✅ auto_approved | – |  |
| c18 | pdf, classic | 9/9 | 3/3 | ✅ auto_approved | – |  |

## `regex-baseline` per invoice

| # | Type | Fields | Lines | Decision | Issues raised | Notes |
|---|---|---|---|---|---|---|
| c01 | pdf, classic | 8/9 | 0/3 | ❌ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL |
| c02 | pdf, modern | 8/9 | 0/3 | ✅ auto_approved | – | wrong: tax |
| c03 | pdf, compact | 8/9 | 0/3 | ❌ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL |
| c04 | pdf, compact | 8/9 | 0/2 | ❌ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL |
| c05 | pdf, modern | 8/9 | 0/2 | ✅ needs_review | PO_INFERRED | wrong: tax |
| c06 | pdf, classic | 8/9 | 0/2 | ✅ needs_review | MATH_TOTAL, TOTAL_VS_PO | wrong: tax; extra: MATH_TOTAL, TOTAL_VS_PO; missed: PRICE_VARIANCE |
| c07 | pdf, compact | 8/9 | 0/3 | ✅ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL; missed: QTY_OVER_PO |
| c08 | pdf, modern | 8/9 | 0/2 | ✅ needs_review | MATH_TOTAL | wrong: tax |
| c09 | image, classic | 0/9 | 0/3 | ❌ needs_review | MISSING_FIELD, NO_PO, UNKNOWN_VENDOR | wrong: vendor_name, invoice_number, invoice_date, due_date, po_number, currency, subtotal, tax, total; extra: MISSING_FIELD, NO_PO, UNKNOWN_VENDOR; missed: DUPLICATE |
| c10 | pdf, modern | 8/9 | 0/2 | ✅ needs_review | MATH_TOTAL, NO_PO, UNKNOWN_VENDOR | wrong: tax; extra: MATH_TOTAL |
| c11 | pdf, classic | 8/9 | 0/1 | ✅ needs_review | PO_CLOSED | wrong: tax |
| c12 | image, modern | 0/9 | 0/3 | ❌ needs_review | MISSING_FIELD, NO_PO, UNKNOWN_VENDOR | wrong: vendor_name, invoice_number, invoice_date, due_date, po_number, currency, subtotal, tax, total; extra: MISSING_FIELD, NO_PO, UNKNOWN_VENDOR |
| c13 | image, classic | 0/9 | 0/2 | ❌ needs_review | MISSING_FIELD, NO_PO, UNKNOWN_VENDOR | wrong: vendor_name, invoice_number, invoice_date, due_date, po_number, currency, subtotal, tax, total; extra: MISSING_FIELD, NO_PO, UNKNOWN_VENDOR |
| c14 | pdf, classic | 8/9 | 0/24 | ❌ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL |
| c15 | pdf, modern | 8/9 | 0/2 | ✅ needs_review | TOTAL_VS_PO | wrong: tax; missed: MATH_LINE |
| c16 | pdf, modern | 8/9 | 0/2 | ✅ auto_approved | – | wrong: tax |
| c17 | image, compact | 0/9 | 0/2 | ❌ needs_review | MISSING_FIELD, NO_PO, UNKNOWN_VENDOR | wrong: vendor_name, invoice_number, invoice_date, due_date, po_number, currency, subtotal, tax, total; extra: MISSING_FIELD, NO_PO, UNKNOWN_VENDOR |
| c18 | pdf, classic | 8/9 | 0/3 | ❌ needs_review | MATH_TOTAL | wrong: tax; extra: MATH_TOTAL |
