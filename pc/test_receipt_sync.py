from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from receipt_parser import OCRLine, parse_receipt
from receipt_sync_server import (
    AppConfig,
    Database,
    Handler,
    canonical_receipt_id,
    default_data_dir,
    default_model_cache,
    local_addresses,
    month_or_current,
    validate_transaction,
)


class ParserTests(unittest.TestCase):
    def test_restaurant_rounding_is_folded_into_non_dish(self) -> None:
        texts = [
            "Date: 2026-08-31", "Time: 11:44", "烟熏黑椒豚骨拉面", "1", "115.0",
            "不用加配", "0.0", "Welcome", "0.0", "Sub Total", "115.0",
            "10% Service", "11.50", "$127.0", "Total",
        ]
        parsed = parse_receipt([OCRLine(text, 0.99) for text in texts])
        self.assertEqual(parsed["transaction"]["amount_cents"], 12700)
        self.assertEqual([item["amount_cents"] for item in parsed["line_items"]], [11500, 1200])
        self.assertEqual(parsed["line_items"][1]["item_type"], "餐饮非菜品（合并）")
        self.assertIn("HK$11.50", parsed["line_items"][1]["notes"])
        self.assertFalse(any("总计" in warning for warning in parsed["warnings"]))
        self.assertNotIn("Welcome", [item["name"] for item in parsed["line_items"]])
        self.assertNotIn("不用加配", [item["name"] for item in parsed["line_items"]])

    def test_restaurant_keeps_concrete_dishes_when_amounts_need_review(self) -> None:
        texts = [
            "叉烧饭", "48.0", "鲜虾云吞面", "42.0", "Sub Total", "100.0",
            "10% Service", "10.0", "Total", "110.0",
        ]
        parsed = parse_receipt([OCRLine(text, 0.99) for text in texts])
        self.assertEqual(
            [item["name"] for item in parsed["line_items"]],
            ["叉烧饭", "鲜虾云吞面", "餐饮非菜品（合并）"],
        )
        self.assertEqual(
            [item["amount_cents"] for item in parsed["line_items"]],
            [4800, 4200, 1000],
        )
        self.assertTrue(all(
            item["item_type"] == "餐饮菜品"
            for item in parsed["line_items"][:2]
        ))
        self.assertEqual(parsed["line_items"][2]["item_type"], "餐饮非菜品（合并）")
        self.assertTrue(any("保留具体菜品" in warning for warning in parsed["warnings"]))
        self.assertFalse(any(item["name"] == "餐饮菜品（待核对）" for item in parsed["line_items"]))

    def test_restaurant_uses_rightmost_price_on_same_visual_row(self) -> None:
        lines = [
            OCRLine("Time: 11:44", 0.99, [1004, 0, 1499, 94]),
            OCRLine("Date: 2026-08-31", 0.99, [68, 8, 754, 121]),
            OCRLine("115.0", 0.99, [1668, 185, 1905, 295]),
            OCRLine("1", 0.99, [1405, 204, 1446, 294]),
            OCRLine("11:32 烟熏黑椒豚骨拉面", 0.99, [81, 206, 1030, 334]),
            OCRLine("(黑椒油另上).", 0.99, [327, 312, 885, 441]),
            OCRLine("0.0", 0.99, [1755, 402, 1915, 512]),
            OCRLine("1", 0.99, [1407, 417, 1451, 512]),
            OCRLine("11:32 不用加配", 0.99, [87, 423, 691, 542]),
            OCRLine("0.0", 0.99, [1758, 511, 1919, 620]),
            OCRLine("1", 0.99, [1411, 523, 1456, 618]),
            OCRLine("11:28 Welcome", 0.99, [96, 537, 642, 637]),
            OCRLine("Sub Total :", 0.99, [103, 737, 565, 834]),
            OCRLine("115.0", 0.99, [1685, 733, 1927, 841]),
            OCRLine("10% Service:", 0.99, [118, 837, 601, 934]),
            OCRLine("11.50", 0.99, [1685, 841, 1928, 952]),
            OCRLine("$127.0", 0.99, [1335, 923, 1968, 1173]),
            OCRLine("Total", 0.99, [96, 1005, 308, 1114]),
        ]
        parsed = parse_receipt(lines)
        self.assertEqual(
            [item["name"] for item in parsed["line_items"]],
            ["烟熏黑椒豚骨拉面", "餐饮非菜品（合并）"],
        )
        self.assertEqual(
            [item["amount_cents"] for item in parsed["line_items"]],
            [11500, 1200],
        )
        self.assertNotIn("黑椒油另上", [item["name"] for item in parsed["line_items"]])
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(parsed["transaction"]["merchant"], "未识别商户")

    def test_supermarket_total_is_detected(self) -> None:
        texts = [
            "1 CS 30 Paper Bag M", "34.0", "34.0", "TEMPO Wet Tis Aloe", "28.0", "28.0",
            "Markdown disc.", "-14.5", "Total", "149.5", "Total Qty", "9", "Visa", "HKD", "149.5",
        ]
        parsed = parse_receipt([OCRLine(text, 0.98) for text in texts])
        self.assertEqual(parsed["transaction"]["amount_cents"], 14950)
        self.assertEqual(parsed["transaction"]["category"], "购物")

    def test_supermarket_price_rows_precede_names_and_discount_is_applied(self) -> None:
        def line(text: str, x1: int, y1: int, x2: int, y2: int) -> OCRLine:
            return OCRLine(text, 0.99, [x1, y1, x2, y2])

        lines: list[OCRLine] = []
        products = [
            ("750004302", "1 CS 30 Paper Bag M", "2.0", "2.0"),
            ("301451836", "2 TEMPO Wet Tis Aloe", "34.0", "34.0"),
            ("500001266", "3 LM H. Milk Bread", "28.0", "28.0"),
            ("301016154", "4 BONNE Blueberry Pr", "7.0", "7.0"),
            ("301016155", "5 BONNE Strawberry P", "7.0", "7.0"),
            ("301112979", "6 ORIHIRO Grap.Jelly", "11.0", "11.0"),
            ("301112978", "7 ORIHIRO Kon. Jelly", "11.0", "11.0"),
            ("301111634", "8 HOEI Jelly Candy", "14.5", "29.0"),
            ("301166783", "9 UFC Coconut Water", "35.0", "35.0"),
        ]
        y = 20
        for sku, name, unit_price, row_total in products:
            lines.extend([
                line(f"{sku} ST", 20, y, 330, y + 40),
                line("1", 440, y, 480, y + 40),
                line("x", 550, y, 585, y + 40),
                line(unit_price, 715, y, 840, y + 40),
                line(row_total, 995, y, 1125, y + 40),
                line(name, 90, y + 52, 670, y + 94),
            ])
            y += 105
            if name.startswith("8 "):
                lines.extend([
                    line("Markdown disc.", 170, y - 5, 555, y + 35),
                    line("-14.5", 970, y - 5, 1125, y + 35),
                ])
                y += 52
        lines.extend([
            line("Total", 20, y + 20, 155, y + 65),
            line("149.5", 970, y + 20, 1125, y + 65),
        ])

        parsed = parse_receipt(lines)
        self.assertEqual(parsed["transaction"]["amount_cents"], 14950)
        self.assertEqual(
            [item["amount_cents"] for item in parsed["line_items"]],
            [200, 3400, 2800, 700, 700, 1100, 1100, 1450, 3500],
        )
        self.assertEqual(parsed["line_items"][0]["name"], "CS 30 Paper Bag M")
        self.assertIn("折扣 HK$14.50", parsed["line_items"][7]["notes"])
        self.assertEqual(parsed["warnings"], [])


class SummaryTests(unittest.TestCase):
    def test_computer_returns_income_expense_and_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.add_transaction({
                "id": "income-1", "occurred_at": "2026-08-05T08:00:00+08:00", "kind": "income",
                "category": "工资", "amount_cents": 3000000,
            })
            database.add_transaction({
                "id": "expense-1", "occurred_at": "2026-08-31T11:44:00+08:00", "kind": "expense",
                "category": "餐饮", "amount_cents": 12700,
            })
            summary = database.summary("2026-08")
            self.assertEqual(summary["income_cents"], 3000000)
            self.assertEqual(summary["expense_cents"], 12700)
            self.assertEqual(summary["balance_cents"], 2987300)
            self.assertEqual(summary["income_by_category"][0]["category"], "工资")
            self.assertEqual(summary["expense_by_category"][0]["category"], "餐饮")

    def test_receipt_list_exposes_transaction_date_separately_from_import_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.create_receipt(
                "receipt-date-test", "pc", "2026-09-02T12:46:10Z", str(root / "receipt.jpg")
            )
            database.finish_ocr("receipt-date-test", {}, {
                "transaction": {
                    "occurred_at": "2026-08-31T00:00:00+08:00", "kind": "expense",
                    "category": "餐饮", "scene": "吃饭", "merchant": "未识别商户",
                    "content": "餐饮小票", "amount_cents": 12700,
                },
                "line_items": [],
            })
            receipt = database.list_receipts()[0]
            self.assertEqual(receipt["captured_at"], "2026-09-02T12:46:10Z")
            self.assertEqual(receipt["occurred_at"], "2026-08-31T00:00:00+08:00")

    def test_summary_uses_hong_kong_calendar_month(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.add_transaction({
                "id": "sep-boundary", "occurred_at": "2026-08-31T16:30:00Z", "kind": "expense",
                "category": "交通", "amount_cents": 1200,
            })
            self.assertEqual(database.summary("2026-08")["expense_cents"], 0)
            self.assertEqual(database.summary("2026-09")["expense_cents"], 1200)

    def test_transaction_list_uses_hong_kong_calendar_month(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.add_transaction({
                "id": "sep-boundary", "occurred_at": "2026-08-31T16:30:00Z", "kind": "expense",
                "category": "交通", "amount_cents": 1200,
            })
            self.assertEqual(database.list_transactions("2026-08"), [])
            september = database.list_transactions("2026-09")
            self.assertEqual([row["id"] for row in september], ["sep-boundary"])
            self.assertEqual(september[0]["source"], "manual")

    def test_deleting_manual_transaction_updates_summary_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.add_transaction({
                "id": "manual-income", "occurred_at": "2026-09-02T12:00:00+08:00", "kind": "income",
                "category": "工资", "amount_cents": 900000,
            })
            self.assertTrue(database.delete_manual_transaction("manual-income"))
            self.assertEqual(database.summary("2026-09")["income_cents"], 0)
            self.assertEqual((root / "exports" / "transactions.csv").read_text(encoding="utf-8-sig"), "")
            self.assertFalse(database.delete_manual_transaction("manual-income"))

    def test_deleting_receipt_removes_transaction_items_image_and_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            image_path = root / "images" / "receipt.jpg"
            image_path.parent.mkdir()
            image_path.write_bytes(b"test image")
            database.create_receipt("delete-me", "pc", "2026-09-02T12:00:00+08:00", str(image_path))
            database.finish_ocr("delete-me", {}, {
                "transaction": {
                    "occurred_at": "2026-09-02T12:00:00+08:00", "kind": "expense",
                    "category": "餐饮", "scene": "吃饭", "merchant": "面店",
                    "content": "餐饮小票", "amount_cents": 12700,
                },
                "line_items": [{
                    "position": 1, "name": "拉面", "item_type": "餐饮菜品", "quantity": 1,
                    "unit_price_cents": 11500, "amount_cents": 11500, "category": "餐饮",
                }],
            })
            database.confirm_receipt("delete-me", {})
            self.assertTrue(database.delete_receipt("delete-me"))
            self.assertFalse(image_path.exists())
            with database.session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM line_items").fetchone()[0], 0)
            self.assertEqual((root / "exports" / "transactions.csv").read_text(encoding="utf-8-sig"), "")
            self.assertEqual((root / "exports" / "line_items.csv").read_text(encoding="utf-8-sig"), "")

    def test_deleted_receipt_is_not_recreated_when_ocr_finishes_late(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            image_path = root / "images" / "receipt.jpg"
            image_path.parent.mkdir()
            image_path.write_bytes(b"test image")
            database.create_receipt("late-ocr", "pc", "2026-09-02T12:00:00+08:00", str(image_path))
            self.assertTrue(database.delete_receipt("late-ocr"))
            database.finish_ocr("late-ocr", {}, {
                "transaction": {
                    "occurred_at": "2026-09-02T12:00:00+08:00", "kind": "expense",
                    "category": "购物", "scene": "购物", "merchant": "商店",
                    "content": "购物小票", "amount_cents": 200,
                },
                "line_items": [],
            })
            with database.session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_receipt_transaction_cannot_be_deleted_as_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            image_path = root / "receipt.jpg"
            image_path.write_bytes(b"test image")
            database.create_receipt("protected", "pc", "2026-09-02T12:00:00+08:00", str(image_path))
            database.finish_ocr("protected", {}, {
                "transaction": {
                    "occurred_at": "2026-09-02T12:00:00+08:00", "kind": "expense",
                    "category": "购物", "scene": "购物", "merchant": "商店",
                    "content": "购物小票", "amount_cents": 200,
                },
                "line_items": [],
            })
            with self.assertRaisesRegex(ValueError, "deleted with their receipt"):
                database.delete_manual_transaction("receipt-protected")
            self.assertIsNotNone(database.get_receipt("protected"))

    def test_manual_transaction_cannot_overwrite_receipt_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.create_receipt("protected", "pc", "2026-09-02T12:00:00+08:00", str(root / "receipt.jpg"))
            database.finish_ocr("protected", {}, {
                "transaction": {
                    "occurred_at": "2026-09-02T12:00:00+08:00", "kind": "expense",
                    "category": "购物", "scene": "购物", "merchant": "商店",
                    "content": "购物小票", "amount_cents": 200,
                },
                "line_items": [],
            })
            with self.assertRaisesRegex(ValueError, "conflicts with a receipt"):
                database.add_transaction({
                    "id": "receipt-protected", "occurred_at": "2026-09-03T12:00:00+08:00",
                    "kind": "income", "category": "工资", "amount_cents": 999999,
                })
            receipt = database.get_receipt("protected")
            self.assertEqual(receipt["transaction"]["amount_cents"], 200)

    def test_receipt_delete_refuses_unmanaged_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            outside = root / "outside.jpg"
            outside.write_bytes(b"must remain")
            database.create_receipt("unsafe-path", "pc", "2026-09-02T12:00:00+08:00", str(outside))
            with self.assertRaisesRegex(ValueError, "outside the managed image directory"):
                database.delete_receipt("unsafe-path")
            self.assertTrue(outside.exists())
            self.assertIsNotNone(database.get_receipt("unsafe-path"))

    def test_legacy_unknown_merchant_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.sqlite3", root / "exports")
            database.add_transaction({
                "id": "legacy-merchant", "occurred_at": "2026-09-02T08:00:00+08:00",
                "kind": "expense", "category": "购物", "amount_cents": 200,
                "merchant": "小票商户（待确认）",
            })
            migrated = Database(root / "test.sqlite3", root / "exports")
            with migrated.session() as db:
                merchant = db.execute(
                    "SELECT merchant FROM transactions WHERE id='legacy-merchant'"
                ).fetchone()[0]
            self.assertEqual(merchant, "未识别商户")


class ConfigTests(unittest.TestCase):
    def test_temporary_data_uses_persistent_shared_model_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_app_data = root / "local-app-data"
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
                config = AppConfig.load(root / "test-data", None, None)
            self.assertEqual(
                config.model_cache,
                (local_app_data / "ReceiptSync" / "paddle_models").resolve(),
            )
            self.assertFalse(str(config.model_cache).startswith(str(root / "test-data")))

    def test_missing_localappdata_uses_macos_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {key: value for key, value in os.environ.items() if key != "LOCALAPPDATA"}
            with patch.dict("os.environ", env, clear=True), patch("receipt_sync_server.sys.platform", "darwin"):
                cache = default_model_cache()
                data = default_data_dir()
                config = AppConfig.load(root / "test-data", None, None)
            expected_cache = (
                Path.home() / "Library" / "Application Support" / "ReceiptSync" / "paddle_models"
            ).resolve()
            expected_data = Path.home() / "Library" / "Application Support" / "ReceiptSync"
            self.assertEqual(cache.resolve(), expected_cache)
            self.assertEqual(data.resolve(), expected_data.resolve())
            self.assertEqual(config.model_cache, expected_cache)
            self.assertFalse(str(config.model_cache).startswith(str(root / "test-data")))

    def test_runtime_port_override_keeps_shared_persisted_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = AppConfig.load(root / "data", "0.0.0.0", 8765, root / "models")
            second = AppConfig.load(root / "data", "127.0.0.1", 8764, root / "models")
            stored = json.loads((root / "data" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(first.sync_token, second.sync_token)
            self.assertEqual(second.host, "127.0.0.1")
            self.assertEqual(second.port, 8764)
            self.assertEqual(stored["host"], "0.0.0.0")
            self.assertEqual(stored["port"], 8765)

    def test_lan_address_falls_back_to_active_route(self) -> None:
        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.getsockname.return_value = ("10.71.6.34", 54321)
        with (
            patch("receipt_sync_server.socket.getaddrinfo", return_value=[]),
            patch("receipt_sync_server.socket.socket", return_value=probe),
        ):
            self.assertEqual(local_addresses(), ["10.71.6.34"])


class SecurityBoundaryTests(unittest.TestCase):
    def test_receipt_id_must_be_canonical_uuid(self) -> None:
        value = "2bfc30f1-f7cb-40a3-80db-9417f11b4072"
        self.assertEqual(canonical_receipt_id(value), value)
        for unsafe in ("../../outside", "not-a-uuid", "{2bfc30f1-f7cb-40a3-80db-9417f11b4072}"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                canonical_receipt_id(unsafe)

    def test_invalid_month_and_transaction_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            month_or_current("2026-13")
        with self.assertRaisesRegex(ValueError, "timezone"):
            validate_transaction({
                "occurred_at": "2026-09-03T12:00:00", "kind": "expense",
                "category": "交通", "amount_cents": 1200,
            })
        with self.assertRaisesRegex(ValueError, "supported range"):
            validate_transaction({
                "occurred_at": "2026-09-03T12:00:00+08:00", "kind": "expense",
                "category": "交通", "amount_cents": -1,
            })

    def test_phone_token_has_only_required_endpoints(self) -> None:
        receipt_id = "2bfc30f1-f7cb-40a3-80db-9417f11b4072"
        allowed = [
            ("GET", "/api/v1/summary", {}),
            ("GET", f"/api/v1/receipts/{receipt_id}", {}),
            ("POST", "/api/v1/receipts", {}),
            ("POST", "/api/v1/transactions", {}),
        ]
        denied = [
            ("GET", "/api/v1/pairing", {}),
            ("GET", "/api/v1/receipts", {}),
            ("GET", "/api/v1/transactions", {}),
            ("GET", f"/api/v1/receipts/{receipt_id}", {"raw": ["1"]}),
            ("POST", f"/api/v1/receipts/{receipt_id}/confirm", {}),
            ("POST", f"/api/v1/receipts/{receipt_id}/reprocess", {}),
            ("DELETE", f"/api/v1/receipts/{receipt_id}", {}),
        ]
        for method, path, query in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(Handler.remote_endpoint_allowed(method, path, query))
        for method, path, query in denied:
            with self.subTest(method=method, path=path):
                self.assertFalse(Handler.remote_endpoint_allowed(method, path, query))


class MacOSCertificateTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform != "win32", "OpenSSL helper is for macOS/Linux")
    def test_generated_certificate_fingerprint_matches_python_tls(self) -> None:
        script = Path(__file__).resolve().parent / "generate_certificate.sh"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            subprocess.run(["bash", str(script), str(output)], check=True, cwd=output)
            cert_path = output / "receipt-sync-cert.pem"
            key_path = output / "receipt-sync-key.pem"
            fingerprint_path = output / "receipt-sync-sha256.txt"
            self.assertTrue(cert_path.exists())
            self.assertTrue(key_path.exists())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            certificate_der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
            digest = hashlib.sha256(certificate_der).hexdigest().upper()
            stored = fingerprint_path.read_text(encoding="ascii").strip().upper()
            self.assertEqual(stored, digest)
            self.assertRegex(stored, r"^[0-9A-F]{64}$")
            self.assertIn("BEGIN RSA PRIVATE KEY", key_path.read_text(encoding="ascii"))


if __name__ == "__main__":
    unittest.main()
