"""WebAuthn owner authentication for the loopback-only Mac management page.

The RP is localhost; its exact origin (including port) is supplied by the local
listener, never by a request. Remote devices cannot enroll or use admin sessions.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid

try:
    from webauthn import (
        base64url_to_bytes, generate_authentication_options, generate_registration_options,
        options_to_json, verify_authentication_response, verify_registration_response,
    )
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
        ResidentKeyRequirement, UserVerificationRequirement,
    )
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


class PasskeyError(ValueError):
    pass


class AdminPasskeys:
    session_seconds = 600
    challenge_seconds = 90

    def __init__(self, database):
        self.database = database
        self.lock = threading.RLock()
        self.challenges = {}
        self.sessions = {}
        with database.session() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS admin_passkeys (
                credential_id BLOB PRIMARY KEY,
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(admin_passkeys)")}
            for name, declaration in (("name", "TEXT NOT NULL DEFAULT '管理员 Passkey'"), ("revoked_at", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE admin_passkeys ADD COLUMN {name} {declaration}")
            db.execute("CREATE TABLE IF NOT EXISTS admin_recovery (id INTEGER PRIMARY KEY CHECK(id=1), token_hash BLOB NOT NULL)")

    @property
    def count(self):
        with self.database.session() as db:
            return db.execute("SELECT COUNT(*) FROM admin_passkeys WHERE revoked_at IS NULL").fetchone()[0]

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).digest()

    def _prune(self):
        now = time.monotonic()
        self.sessions = {key: entry for key, entry in self.sessions.items() if entry["expires"] > now}
        self.challenges = {key: item for key, item in self.challenges.items() if item["expires"] > now}

    def authenticated(self, token):
        if not token or len(token) > 512:
            return False
        with self.lock:
            self._prune()
            entry = self.sessions.get(self._digest(token))
            return bool(entry and not entry["recovery"])

    def recovery_authorized(self, token):
        with self.lock:
            self._prune()
            return bool(token and self.sessions.get(self._digest(token), {}).get("recovery"))

    def require_fresh(self, token):
        with self.lock:
            if not self.authenticated(token):
                raise PasskeyError("请重新验证 Passkey。")
            entry = self.sessions[self._digest(token)]
            if not entry["fresh"] or time.monotonic() - entry["verified_at"] > 60:
                raise PasskeyError("此操作需要新的 Passkey 验证，请重新解锁后重试。")
            entry["fresh"] = False

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self._digest(token), None)

    def _require_library(self):
        if not AVAILABLE:
            raise PasskeyError("请先安装 pc/requirements-server.txt 中的 Passkey 依赖。")

    def _new_session(self, credential_id=None, *, recovery=False):
        self._prune()
        if len(self.sessions) >= 128:
            raise PasskeyError("管理会话过多，请稍后重试。")
        token = secrets.token_urlsafe(32)
        self.sessions[self._digest(token)] = {
            "expires": time.monotonic() + self.session_seconds, "credential_id": credential_id,
            "verified_at": time.monotonic(), "fresh": not recovery, "recovery": recovery,
        }
        return {"admin_session": token, "expires_in": self.session_seconds, "recovery": recovery}

    def list_credentials(self, session):
        with self.lock, self.database.session() as db:
            current = self.sessions.get(self._digest(session), {}).get("credential_id")
            return [{"id": hashlib.sha256(row["credential_id"]).hexdigest(), "name": row["name"],
                     "created_at": row["created_at"], "revoked_at": row["revoked_at"],
                     "current": row["credential_id"] == current}
                    for row in db.execute("SELECT * FROM admin_passkeys ORDER BY created_at")]

    def revoke(self, identifier, session):
        with self.lock:
            self.require_fresh(session)
            with self.database.session() as db:
                db.execute("BEGIN IMMEDIATE")
                active = db.execute("SELECT credential_id FROM admin_passkeys WHERE revoked_at IS NULL").fetchall()
                target = next((row[0] for row in active if hashlib.sha256(row[0]).hexdigest() == identifier), None)
                if target is None:
                    raise PasskeyError("找不到有效的 Passkey。")
                if len(active) <= 1:
                    raise PasskeyError("不能删除最后一个 Passkey；请先添加并验证替代 Passkey。")
                db.execute("UPDATE admin_passkeys SET revoked_at=CURRENT_TIMESTAMP WHERE credential_id=?", (target,))
            self.sessions = {key: entry for key, entry in self.sessions.items() if entry["credential_id"] != target}
            return {"revoked": True, "locked": not self.authenticated(session)}

    def rotate_recovery(self, session):
        with self.lock:
            self.require_fresh(session)
            code = secrets.token_urlsafe(32)
            with self.database.session() as db:
                db.execute("INSERT OR REPLACE INTO admin_recovery VALUES (1,?)", (self._digest(code),))
            return {"recovery_code": code}

    def recover(self, code):
        if not isinstance(code, str) or not 32 <= len(code) <= 128:
            raise PasskeyError("恢复代码无效。")
        with self.lock:
            with self.database.session() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT token_hash FROM admin_recovery WHERE id=1").fetchone()
                if not row or not secrets.compare_digest(row[0], self._digest(code)):
                    raise PasskeyError("恢复代码无效或已使用。")
            self.sessions.clear()
            self.challenges.clear()
            # Can enroll a replacement, but cannot read the ledger or authorize devices.
            result = self._new_session(recovery=True)
            self.sessions[self._digest(result["admin_session"])]["recovery_hash"] = self._digest(code)
            return result

    def _challenge(self, kind, origin, session):
        self._prune()
        if len(self.challenges) >= 64:
            raise PasskeyError("验证请求过多，请稍后重试。")
        challenge_id = secrets.token_urlsafe(24)
        challenge = secrets.token_bytes(32)
        self.challenges[challenge_id] = {
            "kind": kind, "origin": origin, "challenge": challenge,
            "session": self._digest(session) if session else None,
            "expires": time.monotonic() + self.challenge_seconds,
        }
        return challenge_id, challenge

    def registration_options(self, origin, session="", name="管理员 Passkey"):
        self._require_library()
        with self.lock:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise PasskeyError("Passkey 名称需要 1 至 80 个字符。")
            if self.count and not (self.authenticated(session) or self.recovery_authorized(session)):
                raise PasskeyError("请先用已有 Passkey 解锁，再添加备用 Passkey。")
            if self.count and not self.recovery_authorized(session):
                self.require_fresh(session)
            with self.database.session() as db:
                existing = [PublicKeyCredentialDescriptor(id=row[0]) for row in db.execute("SELECT credential_id FROM admin_passkeys WHERE revoked_at IS NULL")]
            challenge_id, challenge = self._challenge("register", origin, session if existing else "")
            self.challenges[challenge_id]["name"] = name.strip()
            options = generate_registration_options(
                rp_id="localhost", rp_name="Receipt Sync — Mac 管理",
                user_id=uuid.UUID(self.database.server_id).bytes,
                user_name="receipt-sync-" + self.database.server_id[:8],
                user_display_name="小票同步管理员", challenge=challenge,
                exclude_credentials=existing,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ), timeout=60000,
            )
            return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}

    def authentication_options(self, origin):
        self._require_library()
        with self.lock:
            with self.database.session() as db:
                credentials = [PublicKeyCredentialDescriptor(id=row[0]) for row in db.execute("SELECT credential_id FROM admin_passkeys WHERE revoked_at IS NULL")]
            if not credentials:
                raise PasskeyError("请先在这台 Mac 上创建管理员 Passkey。")
            challenge_id, challenge = self._challenge("authenticate", origin, "")
            options = generate_authentication_options(
                rp_id="localhost", challenge=challenge, allow_credentials=credentials,
                user_verification=UserVerificationRequirement.REQUIRED, timeout=60000,
            )
            return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}

    def verify(self, kind, challenge_id, credential, origin, session=""):
        self._require_library()
        with self.lock:
            self._prune()
            challenge = self.challenges.pop(challenge_id, None)
            if not challenge or challenge["kind"] != kind or challenge["origin"] != origin:
                raise PasskeyError("验证请求已过期或已使用，请重新尝试。")
            try:
                client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
                if client_data.get("crossOrigin", False) is not False or "topOrigin" in client_data:
                    raise PasskeyError("Passkey 必须在本机管理页面直接验证。")
                if kind == "register":
                    # The initial ceremony may create only the first key, even if two tabs raced.
                    if challenge["session"] is None:
                        if self.count:
                            raise PasskeyError("管理员已设置，请使用已有 Passkey 解锁。")
                    elif not (self.authenticated(session) or self.recovery_authorized(session)) or challenge["session"] != self._digest(session):
                        raise PasskeyError("管理会话已过期，请重新解锁。")
                    verified = verify_registration_response(
                        credential=credential, expected_challenge=challenge["challenge"],
                        expected_rp_id="localhost", expected_origin=origin, require_user_verification=True,
                    )
                    with self.database.session() as db:
                        db.execute("BEGIN IMMEDIATE")
                        if self.recovery_authorized(session):
                            recovery_hash = self.sessions[self._digest(session)]["recovery_hash"]
                            if db.execute("DELETE FROM admin_recovery WHERE id=1 AND token_hash=?", (recovery_hash,)).rowcount != 1:
                                raise PasskeyError("恢复代码已使用，请重新验证。")
                        db.execute("INSERT INTO admin_passkeys (credential_id,public_key,sign_count,name) VALUES (?,?,?,?)",
                                   (verified.credential_id, verified.credential_public_key, verified.sign_count, challenge["name"]))
                    credential_id = verified.credential_id
                    if self.recovery_authorized(session):
                        self.logout(session)
                elif kind == "authenticate":
                    credential_id = base64url_to_bytes(credential["rawId"])
                    user_handle = credential.get("response", {}).get("userHandle")
                    if user_handle and base64url_to_bytes(user_handle) != uuid.UUID(self.database.server_id).bytes:
                        raise PasskeyError("Passkey 不属于此服务器。")
                    with self.database.session() as db:
                        row = db.execute("SELECT * FROM admin_passkeys WHERE credential_id=? AND revoked_at IS NULL", (credential_id,)).fetchone()
                        if not row:
                            raise PasskeyError("Passkey 不属于此服务器。")
                        verified = verify_authentication_response(
                            credential=credential, expected_challenge=challenge["challenge"],
                            expected_rp_id="localhost", expected_origin=origin,
                            credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"],
                            require_user_verification=True,
                        )
                        db.execute("UPDATE admin_passkeys SET sign_count=? WHERE credential_id=?",
                                   (verified.new_sign_count, credential_id))
                else:
                    raise PasskeyError("无效验证操作。")
            except PasskeyError:
                raise
            except Exception:
                # Verification details can contain credentials; never return or log them.
                raise PasskeyError("Passkey 验证失败，请重新尝试。") from None
            return self._new_session(credential_id)
