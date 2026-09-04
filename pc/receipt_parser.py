from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Sequence


MONEY = re.compile(r"(?<![\d:])([-+]?)\s*(?:HK\$|HKD|\$)?\s*(\d{1,7}(?:[.,]\d{1,2})?)(?![\d:])", re.IGNORECASE)
DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
SKU = re.compile(r"^\d{6,}\s*[A-Z]{0,4}(?:\s+\d+(?:\.\d+)?)?$", re.IGNORECASE)

TOTAL_LABELS = ("grand total", "amount due", "net total", "total", "合計", "總計", "总计", "應付", "应付", "實付", "实付")
SUBTOTAL_LABELS = ("sub total", "subtotal", "小計", "小计")
SERVICE_LABELS = ("service", "服務費", "服务费", "茶芥", "附加費", "附加费")
DISCOUNT_LABELS = ("discount", "disc.", "markdown", "折扣", "優惠", "优惠")
ZERO_ITEM_LABELS = ("welcome", "不用加配", "不用加酒配", "no add-on", "complimentary", "free item")
DINING_HINTS = SERVICE_LABELS + ("restaurant", "cafe", "餐廳", "餐厅", "拉麵", "拉面", "飯", "饭", "麵", "面", "飲品", "饮品")
META_LABELS = TOTAL_LABELS + SUBTOTAL_LABELS + SERVICE_LABELS + DISCOUNT_LABELS + (
    "date", "time", "receipt", "invoice", "cashier", "visa", "mastercard", "octopus", "hkd",
    "total qty", "qty", "change", "tender", "table", "order", "交易", "收據", "收据",
)
UNKNOWN_MERCHANT = "未识别商户"
HONG_KONG_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class OCRLine:
    text: str
    score: float = 0.0
    box: Sequence[int] | None = None


def money_to_cents(value: Decimal | float | int | str) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_decimal(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"


def coerce_lines(
    texts: Iterable[str],
    scores: Iterable[float] | None = None,
    boxes: Iterable[Sequence[int]] | None = None,
) -> list[OCRLine]:
    text_list = [str(value).strip() for value in texts]
    score_list = list(scores or [])
    box_list = list(boxes or [])
    return [
        OCRLine(
            text=text,
            score=float(score_list[index]) if index < len(score_list) else 0.0,
            box=box_list[index] if index < len(box_list) else None,
        )
        for index, text in enumerate(text_list)
        if text
    ]


def _normalized(text: str) -> str:
    return (
        text.strip()
        .replace("＄", "$")
        .replace("：", ":")
        .replace("，", ",")
        .lower()
    )


def _contains(text: str, labels: Sequence[str]) -> bool:
    normalized = _normalized(text)
    return any(label in normalized for label in labels)


def _amounts(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    cleaned = text.replace(",", "")
    for match in MONEY.finditer(cleaned):
        if cleaned[match.end(2):].lstrip().startswith("%"):
            continue
        try:
            values.append(Decimal(f"{match.group(1)}{match.group(2)}"))
        except InvalidOperation:
            continue
    return values


def _amount_only(text: str) -> Decimal | None:
    cleaned = _normalized(text).replace("hkd", "").replace("hk$", "").replace("$", "").strip()
    if not re.fullmatch(r"[-+]?\d{1,7}(?:\.\d{1,2})?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _label_amount(lines: Sequence[OCRLine], labels: Sequence[str], *, exclude: Sequence[str] = ()) -> tuple[Decimal | None, int | None]:
    best: tuple[Decimal, int] | None = None
    for index, line in enumerate(lines):
        if not _contains(line.text, labels) or (exclude and _contains(line.text, exclude)):
            continue
        values = _amounts(line.text)
        candidate = values[-1] if values else None
        if candidate is None:
            for lookahead in range(index + 1, min(index + 4, len(lines))):
                candidate = _amount_only(lines[lookahead].text)
                if candidate is not None:
                    break
        if candidate is None:
            for lookbehind in range(index - 1, max(index - 3, -1), -1):
                candidate = _amount_only(lines[lookbehind].text)
                if candidate is not None:
                    break
        if candidate is not None and candidate >= 0:
            best = (candidate, index)
    return best if best else (None, None)


def _receipt_date(lines: Sequence[OCRLine], fallback: str | None) -> str:
    for line in lines:
        match = DATE.search(line.text)
        if not match:
            continue
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=HONG_KONG_TIMEZONE
            ).isoformat()
        except ValueError:
            pass
    if fallback:
        try:
            return datetime.fromisoformat(fallback.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _clean_product_name(text: str) -> str:
    value = TIME.sub("", text).strip(" -:·")
    value = re.sub(r"^\d{1,3}\s+(?=[A-Za-z\u3400-\u9fff])", "", value)
    return re.sub(r"\s{2,}", " ", value).strip()


def _is_product_candidate(text: str) -> bool:
    normalized = _normalized(text)
    if not normalized or SKU.fullmatch(normalized) or re.match(r"^\d{6,}\s", normalized) or DATE.search(normalized):
        return False
    if _contains(normalized, META_LABELS) or _contains(normalized, ZERO_ITEM_LABELS):
        return False
    if _amount_only(normalized) is not None:
        return False
    name = _clean_product_name(text)
    meaningful = re.findall(r"[A-Za-z\u3400-\u9fff]", name)
    return len(meaningful) >= 2


def _next_price(lines: Sequence[OCRLine], start: int, stop: int) -> Decimal | None:
    candidates: list[Decimal] = []
    for index in range(start + 1, min(start + 6, stop)):
        if index > start + 1 and _is_product_candidate(lines[index].text):
            break
        value = _amount_only(lines[index].text)
        if value is not None and value >= 0:
            candidates.append(value)
    if not candidates:
        return None
    non_quantity = [value for value in candidates if value > 5 or value % 1 != 0]
    return max(non_quantity or candidates)


def _product_candidates(lines: Sequence[OCRLine], stop: int) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, line in enumerate(lines[:stop]):
        if not _is_product_candidate(line.text):
            continue
        amount = _next_price(lines, index, stop)
        if amount is None or amount <= 0:
            continue
        name = _clean_product_name(line.text)
        key = (_normalized(name), money_to_cents(amount))
        if key in seen:
            continue
        seen.add(key)
        products.append({"name": name, "amount_cents": key[1], "ocr_index": index})
    return products


def _visual_rows(lines: Sequence[OCRLine], stop: int) -> list[list[tuple[int, OCRLine]]]:
    positioned: list[tuple[float, float, float, int, OCRLine]] = []
    heights: list[float] = []
    for index, line in enumerate(lines[:stop]):
        if line.box is None or len(line.box) < 4:
            continue
        x1, y1, x2, y2 = (float(value) for value in line.box[:4])
        if x2 <= x1 or y2 <= y1:
            continue
        height = y2 - y1
        heights.append(height)
        positioned.append(((y1 + y2) / 2, (x1 + x2) / 2, height, index, line))
    if not positioned:
        return []

    tolerance = max(7.0, min(26.0, statistics.median(heights) * 0.42))
    rows: list[list[tuple[int, OCRLine]]] = []
    row_centers: list[float] = []
    for center_y, center_x, _, index, line in sorted(positioned):
        if not rows or abs(center_y - row_centers[-1]) > tolerance:
            rows.append([(index, line)])
            row_centers.append(center_y)
        else:
            rows[-1].append((index, line))
            count = len(rows[-1])
            row_centers[-1] = ((row_centers[-1] * (count - 1)) + center_y) / count

    for row in rows:
        row.sort(key=lambda entry: (float(entry[1].box[0]), entry[0]))
    return rows


def _row_text(row: Sequence[tuple[int, OCRLine]]) -> str:
    return " ".join(line.text.strip() for _, line in row if line.text.strip())


def _row_rightmost_amount(row: Sequence[tuple[int, OCRLine]]) -> Decimal | None:
    candidates: list[tuple[float, int, Decimal]] = []
    for index, line in row:
        value = _amount_only(line.text)
        if value is None:
            values = _amounts(line.text)
            value = values[-1] if values else None
        if value is None:
            continue
        x = float(line.box[2]) if line.box is not None and len(line.box) >= 3 else float(index)
        candidates.append((x, index, value))
    return max(candidates, default=(0.0, 0, None), key=lambda candidate: (candidate[0], candidate[1]))[2]


def _visual_product_candidates(lines: Sequence[OCRLine], stop: int) -> list[dict[str, Any]]:
    rows = _visual_rows(lines, stop)
    products: list[dict[str, Any]] = []
    pending_price: Decimal | None = None

    for row in rows:
        text = _row_text(row)
        has_sku = any(re.match(r"^\s*\d{6,}\b", line.text) for _, line in row)
        amount = _row_rightmost_amount(row)

        if has_sku and amount is not None and amount > 0:
            pending_price = amount
            continue

        if _contains(text, DISCOUNT_LABELS):
            if products and amount is not None and amount < 0:
                gross_cents = products[-1]["amount_cents"]
                discount_cents = money_to_cents(amount)
                products[-1]["amount_cents"] = max(0, gross_cents + discount_cents)
                products[-1]["notes"] = (
                    f"原价 HK${cents_to_decimal(gross_cents)}；"
                    f"折扣 HK${cents_to_decimal(abs(discount_cents))}"
                )
            continue

        if pending_price is None or not _is_product_candidate(text):
            continue
        products.append({
            "name": _clean_product_name(text),
            "amount_cents": money_to_cents(pending_price),
            "ocr_index": row[0][0],
            "notes": "",
        })
        pending_price = None

    return products


def _visual_dining_candidates(lines: Sequence[OCRLine], stop: int) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for row in _visual_rows(lines, stop):
        text = _row_text(row)
        if _contains(text, META_LABELS) or _contains(text, ZERO_ITEM_LABELS):
            continue
        amount = _row_rightmost_amount(row)
        if amount is None or amount <= 0:
            continue
        name_parts = [
            _clean_product_name(line.text)
            for _, line in row
            if _is_product_candidate(line.text)
        ]
        name = " ".join(part for part in name_parts if part).strip()
        if not name:
            continue
        products.append({
            "name": name,
            "amount_cents": money_to_cents(amount),
            "ocr_index": min(index for index, _ in row),
            "notes": "",
        })
    return products


def _merchant(lines: Sequence[OCRLine]) -> str:
    for line in lines[:5]:
        if DATE.search(line.text) or TIME.search(line.text) or _contains(line.text, META_LABELS):
            continue
        if _amount_only(line.text) is not None or SKU.fullmatch(line.text.strip()):
            continue
        if re.match(r"^\s*\d{1,3}\s+", line.text):
            continue
        if len(re.findall(r"[A-Za-z\u3400-\u9fff]", line.text)) >= 3:
            return line.text.strip()
    return UNKNOWN_MERCHANT


def parse_receipt(
    lines: Sequence[OCRLine],
    *,
    captured_at: str | None = None,
) -> dict[str, Any]:
    if not lines:
        return {
            "transaction": None,
            "line_items": [],
            "warnings": ["PaddleOCR 未识别到文字"],
            "confidence": 0.0,
        }

    total, total_index = _label_amount(lines, TOTAL_LABELS, exclude=SUBTOTAL_LABELS + ("total qty",))
    subtotal, subtotal_index = _label_amount(lines, SUBTOTAL_LABELS)
    service, _ = _label_amount(lines, SERVICE_LABELS)
    dining = any(_contains(line.text, DINING_HINTS) for line in lines)
    stop = subtotal_index if subtotal_index is not None else total_index if total_index is not None else len(lines)
    if dining:
        products = _visual_dining_candidates(lines, max(stop, 0)) or _product_candidates(lines, max(stop, 0))
    else:
        products = _visual_product_candidates(lines, max(stop, 0)) or _product_candidates(lines, max(stop, 0))
    warnings: list[str] = []

    if total is None:
        fallback_amounts = [value for line in lines[-8:] if (value := _amount_only(line.text)) is not None and value > 0]
        total = fallback_amounts[-1] if fallback_amounts else None
        warnings.append("未可靠识别“总计”标签，请人工核对金额")

    if total is None:
        return {
            "transaction": None,
            "line_items": [],
            "warnings": warnings + ["未识别到可用的实付金额"],
            "confidence": 0.25,
        }

    total_cents = money_to_cents(total)
    line_items: list[dict[str, Any]] = []

    if dining:
        subtotal_cents = money_to_cents(subtotal) if subtotal is not None else None
        dish_items = [item for item in products if item["amount_cents"] <= total_cents]
        candidate_sum = sum(item["amount_cents"] for item in dish_items)
        if subtotal_cents is not None and len(dish_items) == 1:
            dish_items = [{**dish_items[0], "amount_cents": subtotal_cents}]
        elif subtotal_cents is not None and dish_items and candidate_sum != subtotal_cents:
            warnings.append("部分菜品金额与小计未完全对齐，已保留具体菜品，请逐项审核")
        elif subtotal_cents is not None and not dish_items:
            warnings.append("未识别到具体菜品，请根据小票补录菜品明细")

        for position, item in enumerate(dish_items, start=1):
            line_items.append({
                "position": position,
                "name": item["name"],
                "item_type": "餐饮菜品",
                "quantity": 1.0,
                "unit_price_cents": item["amount_cents"],
                "amount_cents": item["amount_cents"],
                "category": "餐饮",
                "notes": item.get("notes", ""),
            })

        dish_total = subtotal_cents if subtotal_cents is not None else sum(item["amount_cents"] for item in line_items)
        non_dish_cents = total_cents - dish_total
        if non_dish_cents > 0:
            note_parts = ["服务费、茶芥、附加费及收款舍入均并入本项，不单独核算"]
            if service is not None:
                note_parts.append(f"小票识别到服务费 HK${service:.2f}")
            line_items.append({
                "position": len(line_items) + 1,
                "name": "餐饮非菜品（合并）",
                "item_type": "餐饮非菜品（合并）",
                "quantity": 1.0,
                "unit_price_cents": non_dish_cents,
                "amount_cents": non_dish_cents,
                "category": "餐饮",
                "notes": "；".join(note_parts),
            })
        category = "餐饮"
        scene = "吃饭"
        content = "餐饮小票"
    else:
        for position, item in enumerate(products, start=1):
            line_items.append({
                "position": position,
                "name": item["name"],
                "item_type": "购物商品",
                "quantity": 1.0,
                "unit_price_cents": item["amount_cents"],
                "amount_cents": item["amount_cents"],
                "category": "购物",
                "notes": item.get("notes", ""),
            })
        category = "购物" if products else "其他"
        scene = "日常购物" if products else "其他"
        content = "购物小票" if products else "消费小票"

    item_sum = sum(item["amount_cents"] for item in line_items)
    if line_items and item_sum != total_cents:
        warnings.append(f"分项合计 HK${cents_to_decimal(item_sum)} 与实付 HK${cents_to_decimal(total_cents)} 不一致，请审核")

    scores = [line.score for line in lines if line.score > 0]
    average_score = sum(scores) / len(scores) if scores else 0.65
    confidence = min(0.99, max(0.1, average_score * (0.95 if warnings else 1.0)))
    return {
        "transaction": {
            "occurred_at": _receipt_date(lines, captured_at),
            "kind": "expense",
            "category": category,
            "scene": scene,
            "merchant": _merchant(lines),
            "content": content,
            "amount_cents": total_cents,
            "currency": "HKD",
            "payment_account": "其他",
            "necessary": True,
            "notes": "PaddleOCR 自动识别，确认后才计入汇总",
        },
        "line_items": line_items,
        "warnings": warnings,
        "confidence": round(confidence, 4),
    }
