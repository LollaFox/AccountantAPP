from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from receipt_parser import OCRLine, UNKNOWN_MERCHANT, coerce_lines, parse_receipt


HONG_KONG_TIMEZONE = timezone(timedelta(hours=8))
LEGACY_UNKNOWN_MERCHANTS = ("小票商户（待确认）", "小票商戶（待確認）")
MAX_AMOUNT_CENTS = 1_000_000_000_000
MAX_LINE_ITEMS = 500
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def month_or_current(value: str | None) -> str:
    if value is None:
        return datetime.now(HONG_KONG_TIMEZONE).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must use YYYY-MM")
    return value


def month_bounds_utc(month: str) -> tuple[str, str]:
    start = datetime.strptime(month, "%Y-%m").replace(day=1, tzinfo=HONG_KONG_TIMEZONE)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def canonical_receipt_id(value: Any) -> str:
    candidate = str(value).strip().lower()
    if not UUID_PATTERN.fullmatch(candidate):
        raise ValueError("receipt id must be a UUID")
    try:
        return str(uuid.UUID(candidate))
    except ValueError as error:
        raise ValueError("receipt id must be a UUID") from error


def _bounded_text(value: Any, field: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def _iso_datetime(value: Any, field: str = "occurred_at") -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{field} must be an ISO 8601 date with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 date with a timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def validate_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    required = ["occurred_at", "kind", "category", "amount_cents"]
    missing = [key for key in required if key not in transaction]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    result = dict(transaction)
    result["occurred_at"] = _iso_datetime(result["occurred_at"])
    if result["kind"] not in ("income", "expense"):
        raise ValueError("kind must be income or expense")
    if isinstance(result["amount_cents"], bool) or not isinstance(result["amount_cents"], int):
        raise ValueError("amount_cents must be a whole number")
    if not 0 <= result["amount_cents"] <= MAX_AMOUNT_CENTS:
        raise ValueError("amount_cents is outside the supported range")
    result["category"] = _bounded_text(result["category"], "category", 100, allow_empty=False)
    result["scene"] = _bounded_text(result.get("scene", "其他"), "scene", 100, allow_empty=False)
    result["merchant"] = _bounded_text(result.get("merchant", "手工录入"), "merchant", 200, allow_empty=False)
    result["content"] = _bounded_text(
        result.get("content", result["category"]), "content", 500, allow_empty=False
    )
    result["payment_account"] = _bounded_text(
        result.get("payment_account", "其他"), "payment_account", 100, allow_empty=False
    )
    result["notes"] = _bounded_text(result.get("notes", ""), "notes", 2_000)
    result["currency"] = _bounded_text(result.get("currency", "HKD"), "currency", 3, allow_empty=False).upper()
    if result["currency"] != "HKD":
        raise ValueError("only HKD is supported")
    result["necessary"] = bool(result.get("necessary", True))
    return result


def validate_line_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > MAX_LINE_ITEMS:
        raise ValueError(f"line_items must contain at most {MAX_LINE_ITEMS} entries")
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("each line item must be an object")
        result = dict(item)
        result["name"] = _bounded_text(result.get("name"), "item name", 300, allow_empty=False)
        result["item_type"] = _bounded_text(result.get("item_type"), "item type", 100, allow_empty=False)
        result["category"] = _bounded_text(result.get("category", "其他"), "item category", 100, allow_empty=False)
        result["notes"] = _bounded_text(result.get("notes", ""), "item notes", 1_000)
        result["position"] = int(result.get("position", index + 1))
        quantity = result.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not 0 < float(quantity) <= 1_000_000:
            raise ValueError("item quantity is outside the supported range")
        result["quantity"] = float(quantity)
        for field in ("unit_price_cents", "amount_cents"):
            value = result.get(field, result.get("amount_cents"))
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_AMOUNT_CENTS:
                raise ValueError(f"item {field} is outside the supported range")
            result[field] = value
        validated.append(result)
    return validated


def local_addresses() -> list[str]:
    addresses: set[str] = set()

    def add(address: str) -> None:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return
        if (
            parsed.version == 4
            and not parsed.is_loopback
            and not parsed.is_link_local
            and not parsed.is_unspecified
            and not parsed.is_multicast
        ):
            addresses.add(address)

    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(
            socket.gethostname(), None, family=socket.AF_INET, type=socket.SOCK_DGRAM
        ):
            if family == socket.AF_INET:
                add(sockaddr[0])
    except OSError:
        pass

    # UDP connect selects the active local interface without sending a packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(addresses)


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    host: str
    port: int
    sync_token: str
    model_cache: Path

    @classmethod
    def load(
        cls,
        data_dir: Path,
        host: str | None,
        port: int | None,
        model_cache: Path | None = None,
    ) -> "AppConfig":
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "config.json"
        stored: dict[str, Any] = {}
        if config_path.exists():
            stored = json.loads(config_path.read_text(encoding="utf-8"))
        if model_cache is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            shared_cache = (
                Path(local_app_data) / "ReceiptSync" / "paddle_models"
                if local_app_data
                else Path.home() / ".cache" / "ReceiptSync" / "paddle_models"
            )
            model_cache = Path(stored.get("model_cache") or shared_cache)
        configured_host = host or stored.get("host", "0.0.0.0")
        configured_port = int(port or stored.get("port", 8765))
        sync_token = stored.get("sync_token") or secrets.token_urlsafe(24)
        resolved_model_cache = model_cache.resolve()
        persisted = {
            "host": stored.get("host", configured_host),
            "port": int(stored.get("port", configured_port)),
            "sync_token": sync_token,
            "model_cache": str(resolved_model_cache),
        }
        if any(stored.get(key) != value for key, value in persisted.items()):
            temporary_path = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary_path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary_path, config_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return cls(
            data_dir=data_dir,
            host=configured_host,
            port=configured_port,
            sync_token=sync_token,
            model_cache=resolved_model_cache,
        )


class Database:
    def __init__(self, path: Path, export_dir: Path):
        self.path = path
        self.export_dir = export_dir
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ocr_json TEXT,
                    parsed_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    receipt_id TEXT UNIQUE REFERENCES receipts(id) ON DELETE SET NULL,
                    occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('income', 'expense')),
                    category TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    content TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
                    currency TEXT NOT NULL DEFAULT 'HKD',
                    payment_account TEXT NOT NULL,
                    necessary INTEGER NOT NULL DEFAULT 1,
                    notes TEXT NOT NULL DEFAULT '',
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS line_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 1,
                    unit_price_cents INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_transactions_month
                    ON transactions(occurred_at, confirmed, kind, category);
                CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status, created_at);
                """
            )
            db.execute(
                "UPDATE transactions SET merchant=? WHERE merchant IN (?, ?)",
                (UNKNOWN_MERCHANT, *LEGACY_UNKNOWN_MERCHANTS),
            )

    def create_receipt(self, receipt_id: str, device_id: str, captured_at: str, image_path: str) -> bool:
        now = utc_now()
        with self.session() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO receipts
                (id, device_id, captured_at, image_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                (receipt_id, device_id, captured_at, image_path, now, now),
            )
            return cursor.rowcount > 0

    def set_receipt_status(self, receipt_id: str, status: str, *, error: str | None = None) -> None:
        with self.session() as db:
            db.execute(
                "UPDATE receipts SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, utc_now(), receipt_id),
            )

    def finish_ocr(self, receipt_id: str, ocr_payload: dict[str, Any], parsed: dict[str, Any]) -> None:
        now = utc_now()
        with self.session() as db:
            cursor = db.execute(
                "UPDATE receipts SET status='review', ocr_json=?, parsed_json=?, error=NULL, updated_at=? WHERE id=?",
                (json.dumps(ocr_payload, ensure_ascii=False), json.dumps(parsed, ensure_ascii=False), now, receipt_id),
            )
            if cursor.rowcount == 0:
                return
            transaction = parsed.get("transaction")
            if not transaction:
                return
            transaction_id = f"receipt-{receipt_id}"
            db.execute(
                """INSERT INTO transactions
                (id, receipt_id, occurred_at, kind, category, scene, merchant, content, amount_cents,
                 currency, payment_account, necessary, notes, confirmed, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    occurred_at=excluded.occurred_at, kind=excluded.kind, category=excluded.category,
                    scene=excluded.scene, merchant=excluded.merchant, content=excluded.content,
                    amount_cents=excluded.amount_cents, currency=excluded.currency,
                    payment_account=excluded.payment_account, necessary=excluded.necessary,
                    notes=excluded.notes, confirmed=0, updated_at=excluded.updated_at""",
                (
                    transaction_id, receipt_id, transaction["occurred_at"], transaction["kind"],
                    transaction["category"], transaction["scene"], transaction["merchant"],
                    transaction["content"], transaction["amount_cents"], transaction.get("currency", "HKD"),
                    transaction.get("payment_account", "其他"), int(bool(transaction.get("necessary", True))),
                    transaction.get("notes", ""), now, now,
                ),
            )
            db.execute("DELETE FROM line_items WHERE transaction_id=?", (transaction_id,))
            self._insert_items(db, transaction_id, parsed.get("line_items", []))

    @staticmethod
    def _insert_items(db: sqlite3.Connection, transaction_id: str, items: list[dict[str, Any]]) -> None:
        db.executemany(
            """INSERT INTO line_items
            (transaction_id, position, name, item_type, quantity, unit_price_cents, amount_cents, category, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    transaction_id, int(item.get("position", index + 1)), item["name"], item["item_type"],
                    float(item.get("quantity", 1)), int(item.get("unit_price_cents", item["amount_cents"])),
                    int(item["amount_cents"]), item.get("category", "其他"), item.get("notes", ""),
                )
                for index, item in enumerate(items)
            ],
        )

    def get_receipt(self, receipt_id: str, include_raw: bool = False) -> dict[str, Any] | None:
        with self.session() as db:
            row = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if not row:
                return None
            transaction = db.execute("SELECT * FROM transactions WHERE receipt_id=?", (receipt_id,)).fetchone()
            items = []
            if transaction:
                items = [dict(item) for item in db.execute(
                    "SELECT * FROM line_items WHERE transaction_id=? ORDER BY position", (transaction["id"],)
                ).fetchall()]
            result = dict(row)
            result["parsed"] = json.loads(result.pop("parsed_json")) if result.get("parsed_json") else None
            raw = result.pop("ocr_json")
            if include_raw:
                result["ocr"] = json.loads(raw) if raw else None
            result["transaction"] = dict(transaction) if transaction else None
            result["line_items"] = items
            return result

    def list_receipts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.session() as db:
            rows = db.execute(
                """SELECT r.id, r.device_id, r.captured_at, r.status, r.error, r.updated_at,
                    t.occurred_at, t.merchant, t.amount_cents, t.currency, t.category, t.confirmed
                FROM receipts r LEFT JOIN transactions t ON t.receipt_id=r.id
                ORDER BY r.created_at DESC LIMIT ?""",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def confirm_receipt(self, receipt_id: str, body: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_receipt(receipt_id)
        if not existing or not existing.get("transaction"):
            raise ValueError("receipt has no draft transaction")
        transaction_id = existing["transaction"]["id"]
        transaction = validate_transaction({**existing["transaction"], **body.get("transaction", {})})
        items = validate_line_items(body.get("line_items", existing.get("line_items", [])))
        now = utc_now()
        with self.session() as db:
            db.execute(
                """UPDATE transactions SET occurred_at=?, kind=?, category=?, scene=?, merchant=?, content=?,
                amount_cents=?, currency=?, payment_account=?, necessary=?, notes=?, confirmed=1, updated_at=?
                WHERE id=?""",
                (
                    transaction["occurred_at"], transaction["kind"], transaction["category"], transaction["scene"],
                    transaction["merchant"], transaction["content"], int(transaction["amount_cents"]),
                    transaction.get("currency", "HKD"), transaction.get("payment_account", "其他"),
                    int(bool(transaction.get("necessary", True))), transaction.get("notes", ""), now, transaction_id,
                ),
            )
            db.execute("DELETE FROM line_items WHERE transaction_id=?", (transaction_id,))
            self._insert_items(db, transaction_id, items)
            db.execute("UPDATE receipts SET status='confirmed', updated_at=? WHERE id=?", (now, receipt_id))
        self.export_csv()
        return self.get_receipt(receipt_id) or {}

    def add_transaction(self, body: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(body.get("id") or f"manual-{uuid.uuid4()}")
        now = utc_now()
        transaction = validate_transaction(body)
        with self.session() as db:
            existing = db.execute("SELECT receipt_id FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if existing and existing["receipt_id"] is not None:
                raise ValueError("manual transaction id conflicts with a receipt transaction")
            db.execute(
                """INSERT INTO transactions
                (id, receipt_id, occurred_at, kind, category, scene, merchant, content, amount_cents,
                 currency, payment_account, necessary, notes, confirmed, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET occurred_at=excluded.occurred_at, kind=excluded.kind,
                category=excluded.category, scene=excluded.scene, merchant=excluded.merchant,
                content=excluded.content, amount_cents=excluded.amount_cents, currency=excluded.currency,
                payment_account=excluded.payment_account, necessary=excluded.necessary,
                notes=excluded.notes, confirmed=1, updated_at=excluded.updated_at""",
                (
                    transaction_id, transaction["occurred_at"], transaction["kind"], transaction["category"],
                    transaction["scene"], transaction["merchant"], transaction["content"],
                    transaction["amount_cents"], transaction["currency"], transaction["payment_account"],
                    int(transaction["necessary"]), transaction["notes"], now, now,
                ),
            )
            row = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        self.export_csv()
        return dict(row)

    def list_transactions(self, month: str) -> list[dict[str, Any]]:
        start_utc, end_utc = month_bounds_utc(month)
        with self.session() as db:
            rows = db.execute(
                """SELECT id, receipt_id, occurred_at, kind, category, scene, merchant, content,
                    amount_cents, currency, payment_account, notes,
                    CASE WHEN receipt_id IS NULL THEN 'manual' ELSE 'receipt' END AS source
                FROM transactions WHERE confirmed=1
                AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?)
                ORDER BY julianday(occurred_at) DESC, created_at DESC""",
                (start_utc, end_utc),
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_manual_transaction(self, transaction_id: str) -> bool:
        with self.session() as db:
            row = db.execute(
                "SELECT receipt_id FROM transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if not row:
                return False
            if row["receipt_id"] is not None:
                raise ValueError("receipt transactions must be deleted with their receipt")
            db.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
        self.export_csv()
        return True

    def delete_receipt(self, receipt_id: str) -> bool:
        with self.session() as db:
            receipt = db.execute(
                "SELECT image_path FROM receipts WHERE id=?", (receipt_id,)
            ).fetchone()
            if not receipt:
                return False
            image_path = Path(receipt["image_path"]).resolve()
            images_dir = (self.path.parent / "images").resolve()
            if image_path.parent != images_dir:
                raise ValueError("receipt image path is outside the managed image directory")
            db.execute("DELETE FROM transactions WHERE receipt_id=?", (receipt_id,))
            db.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
            image_path.unlink(missing_ok=True)
        self.export_csv()
        return True

    def summary(self, month: str) -> dict[str, Any]:
        start_utc, end_utc = month_bounds_utc(month)
        with self.session() as db:
            totals = db.execute(
                """SELECT kind, COALESCE(SUM(amount_cents), 0) AS amount_cents
                FROM transactions WHERE confirmed=1
                AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?)
                GROUP BY kind""",
                (start_utc, end_utc),
            ).fetchall()
            categories = db.execute(
                """SELECT kind, category, SUM(amount_cents) AS amount_cents, COUNT(*) AS transaction_count
                FROM transactions WHERE confirmed=1
                AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?)
                GROUP BY kind, category ORDER BY kind, amount_cents DESC""",
                (start_utc, end_utc),
            ).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM receipts WHERE status IN ('queued', 'processing', 'review', 'error')").fetchone()[0]
            latest = db.execute(
                """SELECT MAX(updated_at) FROM transactions WHERE confirmed=1
                AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?)""",
                (start_utc, end_utc),
            ).fetchone()[0]
        totals_map = {row["kind"]: int(row["amount_cents"]) for row in totals}
        income = totals_map.get("income", 0)
        expense = totals_map.get("expense", 0)
        return {
            "month": month,
            "currency": "HKD",
            "income_cents": income,
            "expense_cents": expense,
            "balance_cents": income - expense,
            "income_by_category": [dict(row) for row in categories if row["kind"] == "income"],
            "expense_by_category": [dict(row) for row in categories if row["kind"] == "expense"],
            "pending_receipts": int(pending),
            "updated_at": latest,
        }

    def export_csv(self) -> None:
        with self.session() as db:
            transactions = db.execute(
                "SELECT * FROM transactions WHERE confirmed=1 ORDER BY occurred_at, created_at"
            ).fetchall()
            items = db.execute(
                """SELECT li.* FROM line_items li JOIN transactions t ON t.id=li.transaction_id
                WHERE t.confirmed=1 ORDER BY t.occurred_at, li.position"""
            ).fetchall()
        self._write_csv(self.export_dir / "transactions.csv", transactions)
        self._write_csv(self.export_dir / "line_items.csv", items)

    @staticmethod
    def _write_csv(path: Path, rows: list[sqlite3.Row]) -> None:
        temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary_path.open("w", newline="", encoding="utf-8-sig") as handle:
                if rows:
                    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(dict(row) for row in rows)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


class PaddleEngine:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.engine = None

    def _load(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.cache_dir)
        os.environ["HF_HOME"] = str(self.cache_dir / "huggingface")
        os.environ["HF_HUB_CACHE"] = str(self.cache_dir / "huggingface" / "hub")
        os.environ["HF_XET_CACHE"] = str(self.cache_dir / "huggingface" / "xet")
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        self.engine = PaddleOCR(
            text_detection_model_name="PP-OCRv6_medium_det",
            text_recognition_model_name="PP-OCRv6_medium_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
        )

    def recognize(self, image_path: Path) -> tuple[list[OCRLine], dict[str, Any]]:
        if self.engine is None:
            self._load()
        result = next(iter(self.engine.predict(input=str(image_path))))
        payload = result.json.get("res", result.json)
        texts = payload.get("rec_texts", [])
        scores = payload.get("rec_scores", [])
        boxes = payload.get("rec_boxes", [])
        compact = {
            "texts": list(texts),
            "scores": [float(value) for value in scores],
            "boxes": [list(value) for value in boxes],
        }
        return coerce_lines(texts, scores, boxes), compact


class ReceiptWorker:
    def __init__(self, database: Database, images_dir: Path, model_cache: Path):
        self.database = database
        self.images_dir = images_dir
        self.engine = PaddleEngine(model_cache)
        self.jobs: queue.Queue[str] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="receipt-ocr", daemon=True)
        self.thread.start()

    def submit(self, receipt_id: str) -> None:
        self.jobs.put(receipt_id)

    def _run(self) -> None:
        while True:
            receipt_id = self.jobs.get()
            try:
                receipt = self.database.get_receipt(receipt_id)
                if not receipt:
                    continue
                self.database.set_receipt_status(receipt_id, "processing")
                lines, ocr_payload = self.engine.recognize(Path(receipt["image_path"]))
                parsed = parse_receipt(lines, captured_at=receipt["captured_at"])
                self.database.finish_ocr(receipt_id, ocr_payload, parsed)
            except Exception as error:
                self.database.set_receipt_status(
                    receipt_id,
                    "error",
                    error=f"{type(error).__name__}: {error}",
                )
                traceback.print_exc()
            finally:
                self.jobs.task_done()


class ReceiptHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        config: AppConfig,
        *,
        database: Database | None = None,
        worker: ReceiptWorker | None = None,
    ):
        super().__init__(address, handler)
        self.config = config
        self.images_dir = config.data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.database = database or Database(config.data_dir / "receipts.sqlite3", config.data_dir / "exports")
        self.worker = worker or ReceiptWorker(self.database, self.images_dir, config.model_cache)
        self.scheme = "http"
        self.pairing_scheme = "http"
        self.pairing_port = config.port
        self.certificate_sha256 = ""
        self.auth_failures: dict[str, list[float]] = {}
        self.auth_lock = threading.Lock()

    def auth_rate_limited(self, address: str) -> bool:
        now = time.monotonic()
        with self.auth_lock:
            recent = [value for value in self.auth_failures.get(address, []) if now - value < 60]
            self.auth_failures[address] = recent
            return len(recent) >= 10

    def record_auth_failure(self, address: str) -> None:
        with self.auth_lock:
            self.auth_failures.setdefault(address, []).append(time.monotonic())


class Handler(BaseHTTPRequestHandler):
    server: ReceiptHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path) -> None:
        if not path.exists():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _valid_loopback_host(self) -> bool:
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname
        if not host:
            return False
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @staticmethod
    def remote_endpoint_allowed(method: str, path: str, query: dict[str, list[str]]) -> bool:
        if method == "GET":
            if path == "/api/v1/summary":
                return True
            receipt_match = re.fullmatch(rf"/api/v1/receipts/({UUID_PATTERN.pattern})", path.lower())
            if receipt_match and query.get("raw", ["0"])[0] != "1":
                return True
            return False
        return method == "POST" and path in ("/api/v1/receipts", "/api/v1/transactions")

    def _require_auth(self, path: str, query: dict[str, list[str]] | None = None) -> bool:
        if self._is_loopback():
            if self._valid_loopback_host():
                return True
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid local host"})
            return False
        if self.server.auth_rate_limited(self.client_address[0]):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many failed authentication attempts"})
            return False
        if not secrets.compare_digest(self.headers.get("X-Sync-Token", ""), self.server.config.sync_token):
            self.server.record_auth_failure(self.client_address[0])
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid sync token"})
            return False
        if not self.remote_endpoint_allowed(self.command, path, query or {}):
            self._json(HTTPStatus.FORBIDDEN, {"error": "this endpoint is only available on the computer"})
            return False
        return True

    def _read_json(self, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            self.close_connection = True
            raise ValueError("invalid request size") from error
        if length <= 0 or length > max_bytes:
            if length > max_bytes:
                self.close_connection = True
            raise ValueError("invalid request size")
        raw_body = self.rfile.read(length)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        body = json.loads(raw_body.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        return body

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if path == "/":
            if not self._is_loopback() or not self._valid_loopback_host():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._html(Path(__file__).parent / "web" / "index.html")
            return
        if path == "/api/v1/health":
            self._json(HTTPStatus.OK, {"status": "ok", "service": "receipt-sync", "time": utc_now()})
            return
        if not path.startswith("/api/v1/") or not self._require_auth(path, query):
            if not path.startswith("/api/v1/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            if path == "/api/v1/pairing":
                self._json(HTTPStatus.OK, {
                    "computer_name": socket.gethostname(),
                    "port": self.server.pairing_port,
                    "scheme": self.server.pairing_scheme,
                    "sync_token": self.server.config.sync_token,
                    "certificate_sha256": self.server.certificate_sha256,
                    "addresses": local_addresses(),
                })
            elif path == "/api/v1/summary":
                self._json(HTTPStatus.OK, self.server.database.summary(month_or_current(query.get("month", [None])[0])))
            elif path == "/api/v1/transactions":
                month = month_or_current(query.get("month", [None])[0])
                self._json(HTTPStatus.OK, {"month": month, "transactions": self.server.database.list_transactions(month)})
            elif path == "/api/v1/receipts":
                limit = int(query.get("limit", [50])[0])
                self._json(HTTPStatus.OK, {"receipts": self.server.database.list_receipts(limit)})
            elif path.startswith("/api/v1/receipts/"):
                receipt_id = path.rsplit("/", 1)[-1]
                receipt = self.server.database.get_receipt(receipt_id, include_raw=query.get("raw", ["0"])[0] == "1")
                self._json(HTTPStatus.OK if receipt else HTTPStatus.NOT_FOUND, receipt or {"error": "receipt not found"})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, KeyError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/v1/") or not self._require_auth(path):
            self.close_connection = True
            if not path.startswith("/api/v1/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = self._read_json()
            if path == "/api/v1/receipts":
                self._upload_receipt(body)
            elif path.startswith("/api/v1/receipts/") and path.endswith("/confirm"):
                receipt_id = path.split("/")[-2]
                self._json(HTTPStatus.OK, self.server.database.confirm_receipt(receipt_id, body))
            elif path.startswith("/api/v1/receipts/") and path.endswith("/reprocess"):
                receipt_id = path.split("/")[-2]
                receipt = self.server.database.get_receipt(receipt_id)
                if not receipt:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "receipt not found"})
                else:
                    self.server.database.set_receipt_status(receipt_id, "queued")
                    self.server.worker.submit(receipt_id)
                    self._json(HTTPStatus.ACCEPTED, self.server.database.get_receipt(receipt_id))
            elif path == "/api/v1/transactions":
                self._json(HTTPStatus.CREATED, self.server.database.add_transaction(body))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/v1/") or not self._require_auth(path):
            if not path.startswith("/api/v1/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            if path.startswith("/api/v1/receipts/"):
                receipt_id = path.rsplit("/", 1)[-1]
                deleted = self.server.database.delete_receipt(receipt_id)
            elif path.startswith("/api/v1/transactions/"):
                transaction_id = path.rsplit("/", 1)[-1]
                deleted = self.server.database.delete_manual_transaction(transaction_id)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(
                HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND,
                {"deleted": deleted} if deleted else {"error": "record not found"},
            )
        except ValueError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        except Exception:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def _upload_receipt(self, body: dict[str, Any]) -> None:
        receipt_id = canonical_receipt_id(body.get("id") or uuid.uuid4())
        device_id = _bounded_text(body.get("device_id", "iphone"), "device_id", 100, allow_empty=False)
        captured_at = _iso_datetime(body.get("captured_at", utc_now()), "captured_at")
        encoded_image = body.get("image_base64")
        if not isinstance(encoded_image, str):
            raise ValueError("image_base64 must be text")
        image_data = base64.b64decode(encoded_image, validate=True)
        if len(image_data) < 32 or len(image_data) > 12 * 1024 * 1024:
            raise ValueError("image must be between 32 bytes and 12 MB")
        if image_data[:3] == b"\xff\xd8\xff":
            suffix = ".jpg"
        elif image_data[:8] == b"\x89PNG\r\n\x1a\n":
            suffix = ".png"
        else:
            raise ValueError("only JPEG and PNG receipts are supported")
        image_path = (self.server.images_dir / f"{receipt_id}{suffix}").resolve()
        if image_path.parent != self.server.images_dir.resolve():
            raise ValueError("invalid receipt image path")
        if not image_path.exists():
            image_path.write_bytes(image_data)
        created = self.server.database.create_receipt(receipt_id, device_id, captured_at, str(image_path))
        if created:
            self.server.worker.submit(receipt_id)
        else:
            existing = self.server.database.get_receipt(receipt_id)
            if existing and existing["status"] == "error":
                self.server.database.set_receipt_status(receipt_id, "queued")
                self.server.worker.submit(receipt_id)
        receipt = self.server.database.get_receipt(receipt_id)
        self._json(HTTPStatus.ACCEPTED, receipt)


def main() -> None:
    parser = argparse.ArgumentParser(description="PaddleOCR receipt sync server")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--pairing-port", type=int)
    parser.add_argument("--pairing-scheme", choices=("http", "https"))
    parser.add_argument("--pairing-fingerprint", default="")
    parser.add_argument("--review-port", type=int)
    parser.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--allow-insecure-http", action="store_true", help="Only for localhost development tests")
    args = parser.parse_args()
    config = AppConfig.load(args.data_dir.resolve(), args.host, args.port, args.model_cache)
    if args.initialize_only:
        print(f"Receipt Sync configuration initialized: {config.data_dir}")
        return
    if not args.allow_insecure_http and (not args.cert or not args.key):
        parser.error("--cert and --key are required; use --allow-insecure-http only for localhost tests")
    if args.allow_insecure_http and config.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("insecure HTTP may only bind to localhost")
    server = ReceiptHTTPServer((config.host, config.port), Handler, config)
    server.pairing_port = args.pairing_port or config.port
    server.pairing_scheme = args.pairing_scheme or "http"
    server.certificate_sha256 = args.pairing_fingerprint.replace(":", "").strip().upper()
    if args.cert and args.key:
        cert_path = args.cert.resolve()
        key_path = args.key.resolve()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        certificate_pem = cert_path.read_text(encoding="ascii")
        certificate_der = ssl.PEM_cert_to_DER_cert(certificate_pem)
        server.certificate_sha256 = hashlib.sha256(certificate_der).hexdigest().upper()
        server.scheme = "https"
        server.pairing_scheme = "https"
    review_server: ReceiptHTTPServer | None = None
    review_thread: threading.Thread | None = None
    if args.review_port:
        review_config = AppConfig(
            data_dir=config.data_dir,
            host="127.0.0.1",
            port=args.review_port,
            sync_token=config.sync_token,
            model_cache=config.model_cache,
        )
        review_server = ReceiptHTTPServer(
            (review_config.host, review_config.port),
            Handler,
            review_config,
            database=server.database,
            worker=server.worker,
        )
        review_server.pairing_port = server.pairing_port
        review_server.pairing_scheme = server.pairing_scheme
        review_server.certificate_sha256 = server.certificate_sha256
        review_thread = threading.Thread(
            target=review_server.serve_forever,
            name="receipt-review-http",
            daemon=True,
        )
        review_thread.start()
    print("Receipt Sync is running")
    review_url = f"http://127.0.0.1:{args.review_port}" if args.review_port else f"{server.scheme}://127.0.0.1:{config.port}"
    print(f"Computer review: {review_url}")
    print(f"iPhone sync token: {config.sync_token}")
    if server.certificate_sha256 and server.scheme == "https":
        print(f"Certificate SHA-256: {server.certificate_sha256}")
    print(f"Data: {config.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if review_server:
            review_server.shutdown()
            review_server.server_close()
        if review_thread:
            review_thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
