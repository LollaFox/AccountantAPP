from __future__ import annotations

import base64
import calendar
import hashlib
import json
import math
import os
import re
import secrets
import socket
import ssl
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

HONG_KONG_TIMEZONE = timezone(timedelta(hours=8))
MIN_IMAGE_BYTES = 32
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_AMOUNT_CENTS = 1_000_000_000_000
PHONE_TIMEOUT_SECONDS = 20
PROTOCOL_VERSION = 2
STATUS_LABELS = {
    "waiting": "待同步",
    "uploading": "上传中",
    "queued": "等待识别",
    "processing": "识别中",
    "review": "待电脑审核",
    "confirmed": "已计入汇总",
    "error": "同步失败",
}
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class SyncError(Exception):
    invalid_server = "配对信息无效，请检查电脑地址、设备密钥和证书指纹。"
    bad_response = "电脑返回了无法读取的数据，请更新电脑服务后重试。"
    invalid_entry = "此记录未被电脑接受，请检查内容和长度后修改重试。"
    not_found = "电脑上已没有这张小票"
    deleted = "这张小票已在电脑上删除"
    unauthorized = "设备密钥无效或已被撤销。请在电脑上为此设备创建新密钥。"
    forbidden = "此设备没有执行该操作的权限。"
    unreachable = "无法连接电脑。请确认服务正在运行、双方网络可互访，并使用配对页当前地址。"
    timed_out = "连接电脑超时。请检查当前地址和网络，稍后重试。"
    certificate_rejected = "证书不匹配，已拒绝连接。请核对当前电脑配对页的证书指纹。"
    tls_failed = "无法建立安全连接。请确认使用的是电脑的 HTTPS 同步端口。"
    incompatible_server = "电脑服务版本不兼容，请先更新电脑端。"
    server_changed = "此地址属于另一台服务器，已停止发送。请重新核对配对信息。"
    conflict = "请求编号与电脑上的记录冲突，请在电脑上核对。"
    rate_limited = "连接尝试过于频繁，请稍后重试。"
    server_unavailable = "电脑服务暂时不可用，请稍后重试。"
    invitation_expired = "邀请已过期，请在电脑生成新的二维码。"
    pairing_required = "请先设置电脑地址"
    oversized_image = "小票图片超过 12 MB，请重新选择。"
    invalid_manual = "请填写有效大类（最多 100 字）；说明最多 500 字，备注最多 2000 字。"
    invalid_amount = "金额必须至少为 HK$0.01，且在支持范围内。"
    conflict_locked = "此记录可能已发送；请先在电脑核对，不要修改后重复提交。"

    def __init__(self, message: str, *, retry_next: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retry_next = retry_next

    @classmethod
    def from_status(cls, status: int, payload: dict[str, Any] | None = None) -> "SyncError":
        if status == 401:
            return cls(cls.unauthorized)
        if status in (400, 413, 422):
            return cls(cls.invalid_entry)
        if status == 403:
            return cls(cls.forbidden)
        if status == 404:
            return cls(cls.not_found)
        if status == 410:
            return cls(cls.deleted)
        if status == 409:
            if (payload or {}).get("code") == "server_changed":
                return cls(cls.server_changed)
            return cls(cls.conflict)
        if status == 429:
            return cls(cls.rate_limited)
        if 500 <= status < 600:
            return cls(cls.server_unavailable)
        if 300 <= status < 400:
            return cls(cls.bad_response)
        return cls(f"电脑请求失败（{status}）")


class CertificateRejected(OSError):
    pass


class SecretStore(Protocol):
    def read(self, account: str) -> str | None: ...
    def write(self, account: str, value: str) -> None: ...


class MemorySecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def read(self, account: str) -> str | None:
        return self._values.get(account)

    def write(self, account: str, value: str) -> None:
        if value:
            self._values[account] = value
        else:
            self._values.pop(account, None)


class WindowsCredentialStore:
    service = "ReceiptSyncPhone"

    def _target(self, account: str) -> str:
        return f"{self.service}/{account}"

    def read(self, account: str) -> str | None:
        if sys.platform != "win32":
            return None
        import ctypes

        advapi32 = ctypes.windll.advapi32
        cred_ptr = ctypes.c_void_p()
        if not advapi32.CredReadW(self._target(account), 1, 0, ctypes.byref(cred_ptr)):
            return None
        try:
            cred = ctypes.cast(cred_ptr, ctypes.POINTER(_CREDENTIAL)).contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode("utf-16-le").rstrip("\x00")
        finally:
            advapi32.CredFree(cred_ptr)

    def write(self, account: str, value: str) -> None:
        if sys.platform != "win32":
            return
        import ctypes

        advapi32 = ctypes.windll.advapi32
        target = self._target(account)
        if not value:
            advapi32.CredDeleteW(target, 1, 0)
            return
        payload = value.encode("utf-16-le")
        cred = _CREDENTIAL()
        cred.Type = 1
        cred.TargetName = target
        cred.CredentialBlobSize = len(payload)
        cred.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(payload, len(payload)), ctypes.c_void_p)
        cred.Persist = 2
        cred.UserName = account
        if not advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise OSError("无法写入 Windows 凭据管理器")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]
else:
    _CREDENTIAL = None  # type: ignore[misc, assignment]


class PairingInput:
    @staticmethod
    def candidate_urls(raw: str) -> list[str]:
        tokens = raw.split()
        if not tokens or len(tokens) > 6:
            return []
        found: list[str] = []
        for token in tokens:
            value = token if "://" in token else "https://" + token
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            if parsed.scheme.lower() != "https" or not host or "%" in host:
                return []
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return []
            if parsed.path not in ("", "/"):
                return []
            port = parsed.port or 8765
            if not 1 <= port <= 65535:
                return []
            url = f"https://{host}:{port}"
            if url not in found:
                found.append(url)
        return found

    @staticmethod
    def preferred_sync_urls(raw: str) -> list[str]:
        return sorted(PairingInput.candidate_urls(raw), key=PairingInput._hotspot_rank)

    @staticmethod
    def normalized_url(raw: str) -> str:
        return " ".join(PairingInput.preferred_sync_urls(raw))

    @staticmethod
    def _hotspot_rank(url: str) -> int:
        host = urlparse(url).hostname or ""
        if host.startswith("172.20.10.") or host.startswith("172.20.11."):
            return 0
        return 1

    @staticmethod
    def normalized_token(raw: str) -> str:
        return raw.strip()

    @staticmethod
    def valid_token(token: str) -> bool:
        return bool(token) and len(token.encode("utf-8")) <= 512 and all(33 <= ord(char) <= 126 for char in token)

    @staticmethod
    def normalized_fingerprint(raw: str) -> str:
        value = raw.strip().upper()
        for prefix in ("SHA256 FINGERPRINT=", "SHA-256", "SHA256"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break
        hex_digits = "".join(char for char in value if char not in " \t\r\n:")
        if len(hex_digits) != 64 or any(char not in "0123456789ABCDEF" for char in hex_digits):
            return ""
        return hex_digits

    @staticmethod
    def is_ready_to_sync(server_url: str, sync_token: str, certificate_sha256: str) -> bool:
        token = PairingInput.normalized_token(sync_token)
        fingerprint = PairingInput.normalized_fingerprint(certificate_sha256)
        return bool(PairingInput.candidate_urls(server_url) and PairingInput.valid_token(token) and len(fingerprint) == 64)

    @staticmethod
    def validation_message(server_url: str, sync_token: str, certificate_sha256: str) -> str | None:
        if PairingInput.is_ready_to_sync(server_url, sync_token, certificate_sha256):
            return None
        if not PairingInput.candidate_urls(server_url):
            return "请填写完整 HTTPS 地址（端口默认 8765），不要包含网页路径。多个地址用空格分隔。"
        token = PairingInput.normalized_token(sync_token)
        if not token:
            return "请填写同步密钥"
        if not PairingInput.valid_token(token):
            return "同步密钥格式无效，请重新复制该设备的密钥"
        if len(PairingInput.normalized_fingerprint(certificate_sha256)) != 64:
            return "证书指纹应为 64 位十六进制"
        return "配对信息不完整，暂不同步"


class HongKongDate:
    @staticmethod
    def month_key(when: datetime) -> str:
        return when.astimezone(HONG_KONG_TIMEZONE).strftime("%Y-%m")

    @staticmethod
    def month_title(when: datetime) -> str:
        local = when.astimezone(HONG_KONG_TIMEZONE)
        return f"{local.year}年{local.month}月"

    @staticmethod
    def add_months(amount: int, when: datetime) -> datetime:
        local = when.astimezone(HONG_KONG_TIMEZONE)
        month_index = local.year * 12 + (local.month - 1) + amount
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        day = min(local.day, calendar.monthrange(year, month)[1])
        return local.replace(year=year, month=month, day=day)

    @staticmethod
    def iso_string(when: datetime) -> str:
        return when.astimezone(HONG_KONG_TIMEZONE).replace(microsecond=0).isoformat()


def format_hkd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}HK${abs(cents) / 100:.2f}"


def amount_to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def default_phone_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ReceiptSyncPhone"
    return Path.home() / ".local" / "share" / "ReceiptSyncPhone"


def default_secret_store() -> SecretStore:
    if sys.platform == "win32":
        return WindowsCredentialStore()
    return MemorySecretStore()


def sniff_image_suffix(data: bytes) -> str:
    if len(data) < MIN_IMAGE_BYTES:
        raise ValueError("image must be between 32 bytes and 12 MB")
    if len(data) > MAX_IMAGE_BYTES:
        raise SyncError(SyncError.oversized_image)
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    raise ValueError("only JPEG and PNG receipts are supported")


def certificate_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


class PairingInvitation:
    def __init__(
        self,
        version: int,
        server_id: str,
        addresses: list[str],
        certificate_sha256: str,
        invitation_token: str,
        expires_at: float,
    ) -> None:
        self.version = version
        self.server_id = server_id
        self.addresses = addresses
        self.certificate_sha256 = certificate_sha256
        self.invitation_token = invitation_token
        self.expires_at = expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "server_id": self.server_id,
            "addresses": self.addresses,
            "certificate_sha256": self.certificate_sha256,
            "invitation_token": self.invitation_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PairingInvitation":
        return cls(
            version=int(payload["version"]),
            server_id=str(payload["server_id"]),
            addresses=list(payload["addresses"]),
            certificate_sha256=str(payload["certificate_sha256"]),
            invitation_token=str(payload["invitation_token"]),
            expires_at=float(payload["expires_at"]),
        )

    @classmethod
    def parse(cls, text: str) -> "PairingInvitation":
        raw = text.strip()
        if len(raw.encode("utf-8")) > 4096:
            raise SyncError(SyncError.invalid_server)
        parsed = urlparse(raw)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != "receiptsync"
            or parsed.netloc != "pair"
            or parsed.path not in ("", "/")
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.fragment
            or list(query.keys()) != ["data"]
            or len(query["data"]) != 1
        ):
            raise SyncError(SyncError.invalid_server)
        encoded = query["data"][0]
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except (ValueError, json.JSONDecodeError) as error:
            raise SyncError(SyncError.invalid_server) from error
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise SyncError(SyncError.invalid_server)
        invitation = cls.from_dict(payload)
        if not UUID_PATTERN.fullmatch(invitation.server_id.lower()):
            raise SyncError(SyncError.invalid_server)
        invitation.server_id = str(uuid.UUID(invitation.server_id))
        if not invitation.addresses or len(invitation.addresses) > 6:
            raise SyncError(SyncError.invalid_server)
        if any(
            PairingInput.candidate_urls(address) != [address] or not address.startswith("https://")
            for address in invitation.addresses
        ):
            raise SyncError(SyncError.invalid_server)
        fingerprint = PairingInput.normalized_fingerprint(invitation.certificate_sha256)
        if len(fingerprint) != 64 or not TOKEN_PATTERN.fullmatch(invitation.invitation_token):
            raise SyncError(SyncError.invalid_server)
        invitation.certificate_sha256 = fingerprint
        if not math.isfinite(invitation.expires_at):
            raise SyncError(SyncError.invalid_server)
        return invitation

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PairingInvitation):
            return NotImplemented
        return (
            self.version == other.version
            and self.server_id == other.server_id
            and self.addresses == other.addresses
            and self.certificate_sha256 == other.certificate_sha256
            and self.invitation_token == other.invitation_token
            and abs(self.expires_at - other.expires_at) < 1e-6
        )


class PendingPairing:
    def __init__(self, invitation: PairingInvitation, claim_id: str, device_token: str) -> None:
        self.invitation = invitation
        self.claim_id = claim_id
        self.device_token = device_token

    @classmethod
    def create(cls, invitation: PairingInvitation) -> "PendingPairing":
        return cls(invitation=invitation, claim_id=str(uuid.uuid4()), device_token=new_device_token())

    def to_dict(self) -> dict[str, Any]:
        return {"invitation": self.invitation.to_dict(), "claim_id": self.claim_id, "device_token": self.device_token}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingPairing":
        return cls(
            invitation=PairingInvitation.from_dict(payload["invitation"]),
            claim_id=str(payload["claim_id"]),
            device_token=str(payload["device_token"]),
        )


class PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, fingerprint: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.fingerprint = PairingInput.normalized_fingerprint(fingerprint)

    def connect(self) -> None:
        raw_sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.sock = context.wrap_socket(raw_sock, server_hostname=self.host)
        der = self.sock.getpeercert(binary_form=True)
        if not der or certificate_sha256(der) != self.fingerprint:
            self.sock.close()
            raise CertificateRejected(SyncError.certificate_rejected)


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, fingerprint: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context)
        self.fingerprint = fingerprint

    def https_open(self, req: Request):  # type: ignore[no-untyped-def]
        def connection(host: str, **kwargs: Any) -> PinnedHTTPSConnection:
            return PinnedHTTPSConnection(host, fingerprint=self.fingerprint, **kwargs)

        return self.do_open(connection, req)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


Transport = Callable[[Request, str], tuple[int, bytes]]


def default_transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
    opener = build_opener(PinnedHTTPSHandler(fingerprint), NoRedirect)
    try:
        with opener.open(request, timeout=PHONE_TIMEOUT_SECONDS) as response:
            return int(response.getcode() or 0), response.read()
    except CertificateRejected as error:
        raise SyncError(SyncError.certificate_rejected) from error
    except HTTPError as error:
        return int(error.code), error.read() or b""
    except (TimeoutError, socket.timeout) as error:
        raise SyncError(SyncError.timed_out, retry_next=True) from error
    except URLError as error:
        reason = error.reason
        if isinstance(reason, CertificateRejected):
            raise SyncError(SyncError.certificate_rejected) from error
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise SyncError(SyncError.timed_out, retry_next=True) from error
        message = str(reason).lower()
        if "timed out" in message:
            raise SyncError(SyncError.timed_out, retry_next=True) from error
        raise SyncError(SyncError.unreachable, retry_next=True) from error
    except ssl.SSLError as error:
        raise SyncError(SyncError.tls_failed) from error
    except (ConnectionError, OSError) as error:
        if isinstance(error, CertificateRejected):
            raise SyncError(SyncError.certificate_rejected) from error
        raise SyncError(SyncError.unreachable, retry_next=True) from error


def display_sync_error(error: BaseException) -> str:
    if isinstance(error, SyncError):
        return error.message
    if isinstance(error, CertificateRejected):
        return SyncError.certificate_rejected
    return SyncError.unreachable


@dataclass
class AppSettings:
    server_url: str = ""
    sync_token: str = ""
    certificate_sha256: str = ""
    device_id: str = ""
    server_id: str | None = None
    last_successful_url: str | None = None
    role: str | None = None

    @property
    def configured(self) -> bool:
        return PairingInput.is_ready_to_sync(self.server_url, self.sync_token, self.certificate_sha256)

    @property
    def can_write(self) -> bool:
        return self.role != "viewer"


@dataclass
class PendingReceipt:
    id: str
    captured_at: str
    image_file_name: str
    status: str = "waiting"
    merchant: str | None = None
    amount_cents: int | None = None
    error_message: str | None = None
    server_id: str | None = None
    uploaded: bool = False
    blocked_reason: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass
class PendingManual:
    request_id: str
    occurred_at: str
    kind: str
    category: str
    amount_cents: int
    content: str
    notes: str
    server_id: str
    error_message: str | None = None
    blocked_reason: str | None = None


@dataclass
class MonthlySummary:
    month: str = ""
    currency: str = "HKD"
    income_cents: int = 0
    expense_cents: int = 0
    balance_cents: int = 0
    income_by_category: list[dict[str, Any]] = field(default_factory=list)
    expense_by_category: list[dict[str, Any]] = field(default_factory=list)
    pending_receipts: int = 0
    updated_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MonthlySummary":
        return cls(
            month=str(payload.get("month") or ""),
            currency=str(payload.get("currency") or "HKD"),
            income_cents=int(payload.get("income_cents") or 0),
            expense_cents=int(payload.get("expense_cents") or 0),
            balance_cents=int(payload.get("balance_cents") or 0),
            income_by_category=list(payload.get("income_by_category") or []),
            expense_by_category=list(payload.get("expense_by_category") or []),
            pending_receipts=int(payload.get("pending_receipts") or 0),
            updated_at=payload.get("updated_at"),
        )


@dataclass
class VerifiedConnection:
    server_id: str
    device_id: str
    role: str
    base_url: str


class SyncClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or default_transport

    def redeem_invitation(self, draft: PendingPairing, settings: AppSettings) -> dict[str, Any]:
        payload = {
            "invitation_token": draft.invitation.invitation_token,
            "device_token": draft.device_token,
            "claim_id": draft.claim_id,
            "server_id": draft.invitation.server_id,
        }
        result = self._json(settings, "POST", "/api/v1/pairing/redeem", payload)
        if result.get("server_id") != draft.invitation.server_id or not result.get("device_id"):
            raise SyncError(SyncError.server_changed)
        return result

    def connect(self, settings: AppSettings) -> VerifiedConnection:
        try:
            payload, base = self._json_with_base(settings, "GET", "/api/v1/connection")
        except SyncError as error:
            if error.message == SyncError.not_found:
                raise SyncError(SyncError.incompatible_server) from error
            raise
        if (
            payload.get("service") != "receipt-sync"
            or payload.get("protocol_version") != PROTOCOL_VERSION
            or not UUID_PATTERN.fullmatch(str(payload.get("server_id") or "").lower())
        ):
            raise SyncError(SyncError.incompatible_server)
        device = payload.get("device") or {}
        role = str(device.get("role") or "")
        if role not in ("contributor", "viewer"):
            raise SyncError(SyncError.incompatible_server)
        server_id = str(uuid.UUID(str(payload["server_id"])))
        if settings.server_id and settings.server_id != server_id:
            raise SyncError(SyncError.server_changed)
        return VerifiedConnection(
            server_id=server_id,
            device_id=str(device.get("id") or ""),
            role=role,
            base_url=base,
        )

    def upload(self, receipt: PendingReceipt, image_data: bytes, settings: AppSettings) -> dict[str, Any]:
        payload = {
            "id": receipt.id,
            "device_id": settings.device_id,
            "captured_at": receipt.captured_at,
            "image_base64": base64.b64encode(image_data).decode("ascii"),
        }
        return self._json(settings, "POST", "/api/v1/receipts", payload)

    def receipt(self, receipt_id: str, settings: AppSettings) -> dict[str, Any]:
        return self._json(settings, "GET", f"/api/v1/receipts/{receipt_id.lower()}")

    def summary(self, month: str, settings: AppSettings) -> MonthlySummary:
        if not MONTH_PATTERN.fullmatch(month):
            raise SyncError(SyncError.bad_response)
        payload = self._json(settings, "GET", f"/api/v1/summary?month={month}")
        summary = MonthlySummary.from_payload(payload)
        if summary.month != month or summary.currency != "HKD":
            raise SyncError(SyncError.bad_response)
        return summary

    def add_transaction(self, payload: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
        result = self._json(settings, "POST", "/api/v1/transactions", payload)
        expected = f"manual-{payload['request_id']}"
        if result.get("id") != expected:
            raise SyncError(SyncError.bad_response)
        return result

    def _json(
        self,
        settings: AppSettings,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        require_token: bool = True,
    ) -> dict[str, Any]:
        payload, _base = self._json_with_base(settings, method, path, body, require_token=require_token)
        return payload

    def _json_with_base(
        self,
        settings: AppSettings,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        require_token: bool = True,
    ) -> tuple[dict[str, Any], str]:
        self._assert_phone_path(method, path)
        if require_token and not settings.configured:
            raise SyncError(SyncError.invalid_server)
        if not require_token and not PairingInput.candidate_urls(settings.server_url):
            raise SyncError(SyncError.invalid_server)
        if not PairingInput.normalized_fingerprint(settings.certificate_sha256):
            raise SyncError(SyncError.invalid_server)
        bases = PairingInput.preferred_sync_urls(settings.server_url)
        if settings.last_successful_url in bases:
            bases.remove(settings.last_successful_url)
            bases.insert(0, settings.last_successful_url)
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        last_error = SyncError(SyncError.unreachable, retry_next=True)
        for base in bases:
            try:
                payload = self._request_once(settings, method, base, path, encoded)
                return payload, base
            except SyncError as error:
                if error.retry_next:
                    last_error = error
                    continue
                raise
        raise last_error

    def _request_once(
        self,
        settings: AppSettings,
        method: str,
        base: str,
        path: str,
        encoded: bytes | None,
    ) -> dict[str, Any]:
        url = base.rstrip("/") + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SyncError(SyncError.invalid_server)
        request = Request(url, data=encoded, method=method)
        # Native HTTPS client only: never send Origin (browser pairing is rejected).
        if settings.sync_token:
            request.add_header("X-Sync-Token", PairingInput.normalized_token(settings.sync_token))
        if settings.server_id:
            request.add_header("X-Server-ID", settings.server_id)
        request.add_header("Accept", "application/json")
        if encoded is not None:
            request.add_header("Content-Type", "application/json")
        status, raw = self.transport(request, settings.certificate_sha256)
        payload = _decode_object(raw) if raw else {}
        if status < 200 or status >= 300:
            raise SyncError.from_status(status, payload)
        if not isinstance(payload, dict):
            raise SyncError(SyncError.bad_response)
        return payload

    @staticmethod
    def _assert_phone_path(method: str, path: str) -> None:
        clean, _, query = path.partition("?")
        if "raw=" in query.lower():
            raise SyncError(SyncError.invalid_server)
        if method == "GET" and clean in ("/api/v1/summary", "/api/v1/connection"):
            return
        if method == "GET" and clean.startswith("/api/v1/receipts/") and clean.count("/") == 4:
            return
        if method == "POST" and clean in ("/api/v1/receipts", "/api/v1/transactions", "/api/v1/pairing/redeem"):
            return
        raise SyncError(SyncError.invalid_server)


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class PhoneStore:
    def __init__(
        self,
        data_dir: Path | None = None,
        secrets: SecretStore | None = None,
        client: SyncClient | None = None,
    ) -> None:
        self.data_dir = (data_dir or default_phone_data_dir()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file = self.data_dir / "queue.json"
        self.manual_file = self.data_dir / "manual_queue.json"
        self.settings_file = self.data_dir / "settings.json"
        self.secrets = secrets or default_secret_store()
        self.client = client or SyncClient()
        self.settings = self._load_settings()
        self.pending_pairing = self._load_pending_pairing()
        self.receipts: list[PendingReceipt] = self._load_queue()
        self.manuals: list[PendingManual] = self._load_manuals()
        self.summary = MonthlySummary()
        self.selected_month = datetime.now(HONG_KONG_TIMEZONE)
        self.sync_message = "" if self.settings.configured else SyncError.pairing_required

    @property
    def selected_month_key(self) -> str:
        return HongKongDate.month_key(self.selected_month)

    @property
    def can_write(self) -> bool:
        return self.settings.can_write

    @property
    def held_receipt_count(self) -> int:
        current = self.settings.server_id
        return sum(1 for item in self.receipts if item.server_id not in (None, current))

    @property
    def held_manual_count(self) -> int:
        current = self.settings.server_id
        return sum(1 for item in self.manuals if item.server_id != current)

    def forget_computer(self) -> None:
        self.pending_pairing = None
        self._save_pending_pairing(None)
        kept_id = self.settings.device_id
        self.settings = AppSettings(device_id=kept_id)
        self._save_settings()
        self.summary = MonthlySummary()
        self.sync_message = "已忘记电脑，本机队列已保留"

    def pair_with_invitation(self, text: str | None = None) -> None:
        if text:
            invitation = PairingInvitation.parse(text)
            if self.settings.server_id and self.settings.server_id != invitation.server_id:
                raise SyncError(SyncError.server_changed)
            if self.pending_pairing and self.pending_pairing.invitation == invitation:
                draft = self.pending_pairing
            else:
                if invitation.expires_at <= time.time():
                    raise SyncError(SyncError.invitation_expired)
                draft = PendingPairing.create(invitation)
        else:
            if not self.pending_pairing:
                raise SyncError(SyncError.invalid_server)
            draft = self.pending_pairing
        if self.settings.server_id and self.settings.server_id != draft.invitation.server_id:
            raise SyncError(SyncError.server_changed)
        self._save_pending_pairing(draft)
        self.pending_pairing = draft
        candidate = AppSettings(
            server_url=" ".join(draft.invitation.addresses),
            sync_token=draft.device_token,
            certificate_sha256=draft.invitation.certificate_sha256,
            device_id=self.settings.device_id,
            server_id=draft.invitation.server_id,
        )
        self.client.redeem_invitation(draft, candidate)
        connection = self.client.connect(candidate)
        candidate.last_successful_url = connection.base_url
        candidate.role = connection.role
        if connection.device_id:
            candidate.device_id = connection.device_id
        previous = self.settings
        self.settings = candidate
        try:
            self._save_settings()
        except OSError:
            self.settings = previous
            raise
        self.summary = MonthlySummary()
        try:
            self._save_pending_pairing(None)
            self.pending_pairing = None
        except OSError:
            self.pending_pairing = draft
        self.sync_message = "已安全配对电脑"
        self.sync_all()

    def test_and_save(self, server_url: str, sync_token: str, certificate_sha256: str) -> None:
        candidate = AppSettings(
            server_url=PairingInput.normalized_url(server_url) or server_url,
            sync_token=PairingInput.normalized_token(sync_token),
            certificate_sha256=PairingInput.normalized_fingerprint(certificate_sha256),
            device_id=self.settings.device_id,
            server_id=self.settings.server_id,
        )
        message = PairingInput.validation_message(candidate.server_url, candidate.sync_token, candidate.certificate_sha256)
        if message:
            self.sync_message = message
            raise SyncError(message)
        connection = self.client.connect(candidate)
        candidate.server_id = connection.server_id
        candidate.last_successful_url = connection.base_url
        candidate.role = connection.role
        if connection.device_id:
            candidate.device_id = connection.device_id
        previous = self.settings
        self.settings = candidate
        try:
            self._save_settings()
        except OSError:
            self.settings = previous
            raise
        self.sync_message = "已同步"
        self.sync_all()

    def move_month(self, amount: int) -> None:
        self.selected_month = HongKongDate.add_months(amount, self.selected_month)
        self.sync_all()

    def enqueue_image(self, image_data: bytes, captured_at: datetime | None = None) -> PendingReceipt:
        if not self.can_write:
            raise SyncError(SyncError.forbidden)
        suffix = sniff_image_suffix(image_data)
        receipt_id = str(uuid.uuid4())
        file_name = f"{receipt_id}{suffix}"
        path = self.data_dir / file_name
        path.write_bytes(image_data)
        receipt = PendingReceipt(
            id=receipt_id,
            captured_at=HongKongDate.iso_string(captured_at or datetime.now(timezone.utc)),
            image_file_name=file_name,
            status="waiting",
            server_id=self.settings.server_id,
        )
        previous = list(self.receipts)
        self.receipts.insert(0, receipt)
        try:
            self._persist_queue()
        except OSError:
            self.receipts = previous
            path.unlink(missing_ok=True)
            raise
        self.sync_all()
        return receipt

    def retry_receipt(self, receipt_id: str) -> None:
        current = next((item for item in self.receipts if item.id == receipt_id), None)
        if current is None:
            return
        current.blocked_reason = None
        current.error_message = None
        self._persist_queue()
        self.sync_all()

    def add_manual(
        self,
        kind: str,
        when: datetime,
        category: str,
        amount: Decimal,
        content: str,
        notes: str,
        request_id: str | None = None,
    ) -> None:
        if not self.can_write:
            raise SyncError(SyncError.forbidden)
        if not self.settings.configured or not self.settings.server_id:
            raise SyncError(SyncError.pairing_required)
        trimmed_category = category.strip()
        trimmed_content = content.strip()
        trimmed_notes = notes.strip()
        if kind not in ("income", "expense"):
            raise SyncError(SyncError.invalid_manual)
        if not trimmed_category or len(trimmed_category) > 100 or len(trimmed_content) > 500 or len(trimmed_notes) > 2000:
            raise SyncError(SyncError.invalid_manual)
        cents = amount_to_cents(amount)
        if cents <= 0 or cents > MAX_AMOUNT_CENTS:
            raise SyncError(SyncError.invalid_amount)
        identifier = request_id or str(uuid.uuid4())
        if request_id and not UUID_PATTERN.fullmatch(identifier.lower()):
            raise SyncError(SyncError.invalid_manual)
        identifier = str(uuid.UUID(identifier))
        entry = PendingManual(
            request_id=identifier,
            occurred_at=HongKongDate.iso_string(when),
            kind=kind,
            category=trimmed_category,
            amount_cents=cents,
            content=trimmed_content or trimmed_category,
            notes=trimmed_notes,
            server_id=self.settings.server_id,
        )
        previous = list(self.manuals)
        existing = next((item for item in self.manuals if item.request_id == identifier), None)
        if existing is not None:
            if existing.server_id != self.settings.server_id or existing.blocked_reason != "invalid":
                raise SyncError(SyncError.conflict_locked)
            self.manuals = [entry if item.request_id == identifier else item for item in self.manuals]
        else:
            self.manuals.append(entry)
        try:
            self._persist_manuals()
        except OSError:
            self.manuals = previous
            raise
        self.selected_month = when.astimezone(HONG_KONG_TIMEZONE)
        self.sync_all()

    def discard_rejected_manual(self, request_id: str) -> None:
        current = next((item for item in self.manuals if item.request_id == request_id), None)
        if current is None or not current.blocked_reason:
            return
        previous = list(self.manuals)
        self.manuals = [item for item in self.manuals if item.request_id != request_id]
        try:
            self._persist_manuals()
        except OSError:
            self.manuals = previous
            raise

    def sync_all(self) -> None:
        if not self.settings.configured:
            self.sync_message = SyncError.pairing_required
            return
        self.sync_message = "正在同步"
        try:
            connection = self.client.connect(self.settings)
            if self.settings.server_id and self.settings.server_id != connection.server_id:
                raise SyncError(SyncError.server_changed)
            self.settings.server_id = connection.server_id
            self.settings.role = connection.role
            self.settings.last_successful_url = connection.base_url
            self._save_settings()
            if self.can_write:
                for receipt in list(self.receipts):
                    if receipt.server_id in (None, self.settings.server_id) and not receipt.blocked_reason:
                        self.sync_receipt(receipt.id)
                kept: list[PendingManual] = []
                for manual in self.manuals:
                    if manual.server_id != self.settings.server_id or manual.blocked_reason:
                        kept.append(manual)
                        continue
                    try:
                        self.client.add_transaction(
                            {
                                "request_id": manual.request_id,
                                "occurred_at": manual.occurred_at,
                                "kind": manual.kind,
                                "category": manual.category,
                                "amount_cents": manual.amount_cents,
                                "content": manual.content,
                                "notes": manual.notes,
                            },
                            self.settings,
                        )
                    except SyncError as error:
                        if error.message in (SyncError.invalid_entry, SyncError.conflict):
                            manual.blocked_reason = "invalid" if error.message == SyncError.invalid_entry else "conflict"
                            manual.error_message = error.message
                            kept.append(manual)
                            continue
                        raise
                self.manuals = kept
                self._persist_manuals()
            summary_failed = self._load_summary()
            blocked_manual = any(
                item.blocked_reason and item.server_id == self.settings.server_id for item in self.manuals
            )
            error_receipt = next((item for item in self.receipts if item.error_message), None)
            if summary_failed:
                self.sync_message = summary_failed
                return
            if self.held_receipt_count or self.held_manual_count:
                self.sync_message = f"已同步；{self.held_receipt_count + self.held_manual_count} 条其他服务器的本机记录已保留"
                return
            if self.can_write and blocked_manual:
                self.sync_message = "汇总已更新；有收支记录需要修改或在电脑核对"
                return
            if self.can_write and error_receipt and error_receipt.error_message:
                self.sync_message = "已连接；" + error_receipt.error_message
                return
            self.sync_message = "已同步" if self.can_write else "已同步（只读汇总）"
        except SyncError as error:
            self.sync_message = display_sync_error(error)
            raise

    def sync_receipt(self, receipt_id: str) -> None:
        current = next((item for item in self.receipts if item.id == receipt_id), None)
        if current is None or current.blocked_reason:
            return
        previous_status = current.status
        try:
            if current.status in ("waiting", "error") and not current.uploaded:
                remote = self._upload_receipt(current)
            else:
                try:
                    remote = self.client.receipt(current.id, self.settings)
                except SyncError as error:
                    if error.message == SyncError.deleted:
                        self._forget_receipt(receipt_id)
                        return
                    if error.message == SyncError.not_found:
                        if current.uploaded:
                            self._forget_receipt(receipt_id)
                            return
                        remote = self._upload_receipt(current)
                    else:
                        raise
            current = next((item for item in self.receipts if item.id == receipt_id), None)
            if current is None:
                return
            current.uploaded = True
            current.status = str(remote.get("status") or "queued")
            transaction = remote.get("transaction") or {}
            if isinstance(transaction, dict):
                current.merchant = transaction.get("merchant")
                amount = transaction.get("amount_cents")
                current.amount_cents = int(amount) if amount is not None else None
            current.error_message = remote.get("error")
            current.blocked_reason = None
            if remote.get("status") == "confirmed":
                (self.data_dir / current.image_file_name).unlink(missing_ok=True)
            self._persist_queue()
        except SyncError as error:
            if error.message in (SyncError.not_found, SyncError.deleted):
                self._forget_receipt(receipt_id)
                return
            current = next((item for item in self.receipts if item.id == receipt_id), None)
            if current is None:
                return
            current.status = previous_status
            current.error_message = display_sync_error(error)
            if error.message == SyncError.invalid_entry:
                current.blocked_reason = "invalid"
            elif error.message == SyncError.conflict:
                current.blocked_reason = "conflict"
            elif current.status != "confirmed":
                current.status = "error"
            self._persist_queue()
            if current.blocked_reason:
                return
            raise

    def _upload_receipt(self, current: PendingReceipt) -> dict[str, Any]:
        current.status = "uploading"
        current.error_message = None
        self._persist_queue()
        image_data = (self.data_dir / current.image_file_name).read_bytes()
        return self.client.upload(current, image_data, self.settings)

    def _load_summary(self) -> str | None:
        if not self.settings.configured:
            return None
        try:
            self.summary = self.client.summary(self.selected_month_key, self.settings)
            return None
        except Exception as error:
            return display_sync_error(error)

    def _forget_receipt(self, receipt_id: str) -> None:
        remaining: list[PendingReceipt] = []
        for item in self.receipts:
            if item.id == receipt_id:
                (self.data_dir / item.image_file_name).unlink(missing_ok=True)
                continue
            remaining.append(item)
        self.receipts = remaining
        self._persist_queue()

    def _load_settings(self) -> AppSettings:
        blob = self.secrets.read("pairing")
        if blob:
            try:
                stored = json.loads(blob)
                if isinstance(stored, dict):
                    return AppSettings(
                        server_url=str(stored.get("server_url") or ""),
                        sync_token=str(stored.get("sync_token") or ""),
                        certificate_sha256=str(stored.get("certificate_sha256") or ""),
                        device_id=str(stored.get("device_id") or f"windows-{uuid.uuid4()}"),
                        server_id=stored.get("server_id"),
                        last_successful_url=stored.get("last_successful_url"),
                        role=stored.get("role"),
                    )
            except json.JSONDecodeError:
                pass
        device_id = f"windows-{uuid.uuid4()}"
        if self.settings_file.exists():
            try:
                stored = json.loads(self.settings_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict) and stored.get("device_id"):
                device_id = str(stored["device_id"])
        settings = AppSettings(device_id=device_id)
        self.settings = settings
        self._save_settings()
        return settings

    def _save_settings(self) -> None:
        payload = {
            "server_url": self.settings.server_url,
            "sync_token": self.settings.sync_token,
            "certificate_sha256": self.settings.certificate_sha256,
            "device_id": self.settings.device_id,
            "server_id": self.settings.server_id,
            "last_successful_url": self.settings.last_successful_url,
            "role": self.settings.role,
        }
        self.secrets.write("pairing", json.dumps(payload, ensure_ascii=False))
        public = {"device_id": self.settings.device_id, "server_id": self.settings.server_id, "role": self.settings.role}
        self._atomic_write(self.settings_file, json.dumps(public, ensure_ascii=False, indent=2))

    def _load_pending_pairing(self) -> PendingPairing | None:
        blob = self.secrets.read("pendingPairing")
        if not blob:
            return None
        try:
            payload = json.loads(blob)
            return PendingPairing.from_dict(payload) if isinstance(payload, dict) else None
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_pending_pairing(self, pending: PendingPairing | None) -> None:
        self.secrets.write("pendingPairing", json.dumps(pending.to_dict(), ensure_ascii=False) if pending else "")

    def _load_queue(self) -> list[PendingReceipt]:
        raw = self._read_json_list(self.queue_file)
        receipts: list[PendingReceipt] = []
        for item in raw:
            if "id" not in item:
                continue
            receipts.append(
                PendingReceipt(
                    id=str(item["id"]),
                    captured_at=str(item.get("captured_at") or HongKongDate.iso_string(datetime.now(timezone.utc))),
                    image_file_name=str(item.get("image_file_name") or ""),
                    status=str(item.get("status") or "waiting"),
                    merchant=item.get("merchant"),
                    amount_cents=item.get("amount_cents"),
                    error_message=item.get("error_message"),
                    server_id=item.get("server_id"),
                    uploaded=bool(item.get("uploaded")),
                    blocked_reason=item.get("blocked_reason"),
                )
            )
        return receipts

    def _load_manuals(self) -> list[PendingManual]:
        raw = self._read_json_list(self.manual_file)
        manuals: list[PendingManual] = []
        for item in raw:
            if "request_id" not in item:
                continue
            manuals.append(
                PendingManual(
                    request_id=str(item["request_id"]),
                    occurred_at=str(item["occurred_at"]),
                    kind=str(item["kind"]),
                    category=str(item["category"]),
                    amount_cents=int(item["amount_cents"]),
                    content=str(item.get("content") or item["category"]),
                    notes=str(item.get("notes") or ""),
                    server_id=str(item.get("server_id") or ""),
                    error_message=item.get("error_message"),
                    blocked_reason=item.get("blocked_reason"),
                )
            )
        return manuals

    def _persist_queue(self) -> None:
        self._atomic_write(self.queue_file, json.dumps([asdict(item) for item in self.receipts], ensure_ascii=False, indent=2))

    def _persist_manuals(self) -> None:
        self._atomic_write(self.manual_file, json.dumps([asdict(item) for item in self.manuals], ensure_ascii=False, indent=2))

    @staticmethod
    def _read_json_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
