from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.coding_agent import suggest_journal_entries
from app.db import Database, new_id, utc_now
from app.reconciler import reconcile_invoice
from app.rule_engine import validate_invoice
from app.xml_parser import parse_tt78_xml


def run_eval() -> int:
    dataset = json.loads((BASE_DIR / "eval" / "golden_dataset.json").read_text(encoding="utf-8"))
    gates = dataset["acceptance_gates"]
    cases = dataset["cases"]

    header_total = 0
    header_correct = 0
    line_total = 0
    line_correct = 0
    coding_total = 0
    coding_correct = 0

    expected_err_total = 0
    expected_err_caught = 0
    valid_case_total = 0
    valid_case_false_positives = 0

    recon_total = 0
    recon_false_positives = 0
    fx_checks_passed = True
    latencies_ms: list[float] = []
    case_results: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "eval.db")

        for case in cases:
            xml_path = BASE_DIR / case["file"]
            t0 = time.perf_counter()
            inv = parse_tt78_xml(xml_path.read_bytes())
            issues = validate_invoice(inv, db=db, tenant_id="eval-tenant")
            entries, _ = suggest_journal_entries(inv, db=db, tenant_id="eval-tenant")

            # Optional reconciliation test
            recon_status = None
            if "reconciliation_ref" in case:
                ref = case["reconciliation_ref"]
                with db.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO reference_docs (
                            id, tenant_id, doc_type, ref_code, vendor_tax_code,
                            doc_date, total_amount, currency, items_json, note, created_at
                        ) VALUES (?, 'eval-tenant', ?, ?, ?, ?, ?, 'VND', '[]', ?, ?)
                        """,
                        (
                            new_id(),
                            ref["doc_type"],
                            ref["ref_code"],
                            ref.get("vendor_tax_code"),
                            ref["doc_date"],
                            ref["total_amount"],
                            ref.get("note"),
                            utc_now(),
                        ),
                    )
                # Insert dummy doc+invoice row for FK
                doc_id, inv_id = new_id(), new_id()
                with db.connect() as conn:
                    conn.execute(
                        "INSERT INTO documents (id, tenant_id, file_hash, original_filename, storage_path, source_channel, mime_type, created_at) VALUES (?, 'eval-tenant', ?, ?, ?, 'eval', 'application/xml', ?)",
                        (doc_id, inv_id, xml_path.name, str(xml_path), utc_now()),
                    )
                    conn.execute(
                        "INSERT INTO invoices (id, tenant_id, document_id, extraction_source, extracted_payload, created_at, updated_at) VALUES (?, 'eval-tenant', ?, 'xml_tt78', ?, ?, ?)",
                        (inv_id, doc_id, inv.model_dump_json(), utc_now(), utc_now()),
                    )
                matches = reconcile_invoice(inv_id, inv, db=db, tenant_id="eval-tenant")
                recon_status = matches[0]["status"] if matches else None
                recon_total += 1
                if case["expected_recon_status"] == "MATCHED" and recon_status != "MATCHED":
                    recon_false_positives += 1

            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            latencies_ms.append(elapsed_ms)

            # 1. Header Field-level Accuracy
            actual_header = {
                "series": inv.invoice_meta.series,
                "invoice_number": inv.invoice_meta.invoice_number,
                "issue_date": inv.invoice_meta.issue_date,
                "seller_tax_code": inv.seller.tax_code,
                "subtotal": inv.totals.subtotal,
                "total_vat": inv.totals.total_vat,
                "grand_total": inv.totals.grand_total,
            }
            for k, exp_v in case["expected_header"].items():
                header_total += 1
                if actual_header.get(k) == exp_v:
                    header_correct += 1

            # 2. Line-item Accuracy & Coding Top-1 Accuracy
            by_line_no = {li.line_no: li for li in inv.line_items}
            for exp_li in case["expected_lines"]:
                line_total += 1
                coding_total += 1
                act_li = by_line_no.get(exp_li["line_no"])
                if act_li and (
                    act_li.amount_before_tax == exp_li["amount_before_tax"]
                    and act_li.vat_rate == exp_li["vat_rate"]
                    and act_li.vat_amount == exp_li["vat_amount"]
                ):
                    line_correct += 1
                if act_li and act_li.debit_account == exp_li["debit_account"]:
                    coding_correct += 1

            # 3. Rule Engine Recall & False Positive Rate
            actual_errors = {i.code for i in issues if i.severity == "ERROR"}
            exp_errors = set(case.get("expected_error_codes", []))
            if exp_errors:
                expected_err_total += len(exp_errors)
                expected_err_caught += len(exp_errors & actual_errors)
            else:
                valid_case_total += 1
                if actual_errors:
                    valid_case_false_positives += 1

            # 4. FX VND conversion check
            if "expected_journal_vnd_amount" in case:
                if not entries or entries[0].amount != case["expected_journal_vnd_amount"]:
                    fx_checks_passed = False

            case_results.append(
                {
                    "id": case["id"],
                    "latency_ms": elapsed_ms,
                    "actual_errors": sorted(actual_errors),
                    "expected_errors": sorted(exp_errors),
                    "recon_status": recon_status,
                }
            )

    latencies_ms.sort()
    p95_idx = min(int(0.95 * (len(latencies_ms) - 1)), len(latencies_ms) - 1)
    metrics = {
        "header_field_accuracy": round(header_correct / max(header_total, 1), 4),
        "line_item_accuracy": round(line_correct / max(line_total, 1), 4),
        "rule_engine_recall": round(expected_err_caught / max(expected_err_total, 1), 4),
        "rule_engine_false_positive_rate": round(valid_case_false_positives / max(valid_case_total, 1), 4),
        "coding_top1_accuracy": round(coding_correct / max(coding_total, 1), 4),
        "recon_false_positive_rate": round(recon_false_positives / max(recon_total, 1), 4),
        "p95_latency_ms": latencies_ms[p95_idx],
        "fx_vnd_conversion_ok": fx_checks_passed,
    }

    checks = [
        ("Header Field Accuracy", metrics["header_field_accuracy"], f">= {gates['header_field_accuracy']}", metrics["header_field_accuracy"] >= gates["header_field_accuracy"]),
        ("Line-item Accuracy", metrics["line_item_accuracy"], f">= {gates['line_item_accuracy']}", metrics["line_item_accuracy"] >= gates["line_item_accuracy"]),
        ("Rule Engine Recall", metrics["rule_engine_recall"], f">= {gates['rule_engine_recall']}", metrics["rule_engine_recall"] >= gates["rule_engine_recall"]),
        ("Rule Engine FP Rate", metrics["rule_engine_false_positive_rate"], f"<= {gates['rule_engine_false_positive_rate']}", metrics["rule_engine_false_positive_rate"] <= gates["rule_engine_false_positive_rate"]),
        ("Coding Top-1 Accuracy", metrics["coding_top1_accuracy"], f">= {gates['coding_top1_accuracy']}", metrics["coding_top1_accuracy"] >= gates["coding_top1_accuracy"]),
        ("Recon FP Rate", metrics["recon_false_positive_rate"], f"<= {gates['recon_false_positive_rate']}", metrics["recon_false_positive_rate"] <= gates["recon_false_positive_rate"]),
        ("P95 Latency (ms)", metrics["p95_latency_ms"], f"<= {gates['p95_latency_ms']}", metrics["p95_latency_ms"] <= gates["p95_latency_ms"]),
        ("FX VND Conversion", metrics["fx_vnd_conversion_ok"], "== True", metrics["fx_vnd_conversion_ok"]),
    ]

    print("\n=== FINAGENT AI — EVALUATION REPORT ===")
    print(f"{'KPI Metric':<26} | {'Actual':<10} | {'Gate':<12} | {'Result'}")
    print("-" * 64)
    all_passed = True
    for name, val, gate_str, ok in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_passed = False
        print(f"{name:<26} | {str(val):<10} | {gate_str:<12} | {status}")
    print("-" * 64)

    report = {"passed": all_passed, "metrics": metrics, "cases": case_results}
    (BASE_DIR / "eval" / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report saved to eval/report.json | Overall: {'PASS' if all_passed else 'FAIL'}\n")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(run_eval())
