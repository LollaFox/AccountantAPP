from __future__ import annotations

import sys
import threading
import tkinter as tk
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phone_sync import (
    HONG_KONG_TIMEZONE,
    HongKongDate,
    PairingInput,
    PairingInvitation,
    PendingManual,
    PhoneStore,
    SyncError,
    display_sync_error,
    format_hkd,
)

TEAL = "#0f766e"
TEAL_DARK = "#234e52"
INK = "#1f2933"
MUTED = "#64727d"
BAND = "#f4f7f8"
LINE = "#dce3e7"
GREEN = "#277a45"
CORAL = "#c9573f"
AMBER = "#9a6816"
WHITE = "#ffffff"
OK_STATUS = {
    "",
    "已同步",
    "已同步（只读汇总）",
    "正在同步",
    "正在连接电脑",
    "正在忘记电脑",
    "请先设置电脑地址",
    "已安全配对电脑",
    "已忘记电脑，本机队列已保留",
}


def _font(size: int = 10, weight: str = "normal") -> tuple[str, int, str] | tuple[str, int]:
    family = "Microsoft YaHei UI" if sys.platform == "win32" else "TkDefaultFont"
    if weight == "normal":
        return (family, size)
    return (family, size, weight)


class PhoneApp:
    def __init__(self, store: PhoneStore | None = None) -> None:
        self.store = store or PhoneStore()
        self.root = tk.Tk()
        self.root.title("收支")
        self.root.geometry("440x760")
        self.root.minsize(400, 640)
        self.root.configure(bg=BAND)
        self._busy = False
        self._lock = threading.Lock()
        self._build()
        self.refresh()
        self.root.after(200, self.sync_now)
        self.root.after(15 * 60 * 1000, self._periodic_sync)

    def run(self) -> None:
        self.root.mainloop()

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=TEAL_DARK, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="收支", fg=WHITE, bg=TEAL_DARK, font=_font(16, "bold")).pack(side="left", padx=16)
        button_bar = tk.Frame(header, bg=TEAL_DARK)
        button_bar.pack(side="right", padx=10)
        for text, command in (
            ("新增", self.open_manual),
            ("小票", self.choose_receipt),
            ("同步", self.sync_now),
            ("设置", self.open_settings),
        ):
            tk.Button(
                button_bar,
                text=text,
                command=command,
                bg=TEAL,
                fg=WHITE,
                relief="flat",
                padx=8,
                pady=4,
                font=_font(9),
                activebackground=TEAL,
                activeforeground=WHITE,
            ).pack(side="left", padx=3)

        self.connect_button = tk.Button(
            self.root,
            text="连接电脑",
            command=self.open_settings,
            bg=TEAL,
            fg=WHITE,
            relief="flat",
            font=_font(11, "bold"),
            pady=8,
        )
        self.pending_button = tk.Button(
            self.root,
            text="有未完成的配对，点这里重试",
            command=self.open_settings,
            bg=AMBER,
            fg=WHITE,
            relief="flat",
            font=_font(10, "bold"),
            pady=6,
        )

        self.month_frame = tk.Frame(self.root, bg=BAND, pady=10)
        self.month_frame.pack(fill="x", padx=16)
        tk.Button(self.month_frame, text="‹", command=lambda: self.move_month(-1), relief="flat", bg=BAND, font=_font(14)).pack(
            side="left"
        )
        self.month_label = tk.Label(self.month_frame, text="", bg=BAND, font=_font(12, "bold"), fg=INK)
        self.month_label.pack(side="left", expand=True)
        tk.Button(self.month_frame, text="›", command=lambda: self.move_month(1), relief="flat", bg=BAND, font=_font(14)).pack(
            side="right"
        )

        metrics = tk.Frame(self.root, bg=BAND)
        metrics.pack(fill="x", padx=16)
        self.metric_vars = {
            "income": tk.StringVar(value="HK$0.00"),
            "expense": tk.StringVar(value="HK$0.00"),
            "balance": tk.StringVar(value="HK$0.00"),
            "pending": tk.StringVar(value="0"),
        }
        self._metric(metrics, "总收入", self.metric_vars["income"], GREEN, 0, 0)
        self._metric(metrics, "总支出", self.metric_vars["expense"], CORAL, 0, 1)
        self._metric(metrics, "结余", self.metric_vars["balance"], TEAL, 1, 0)
        self._metric(metrics, "待处理", self.metric_vars["pending"], AMBER, 1, 1)
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)

        body = tk.Frame(self.root, bg=BAND)
        body.pack(fill="both", expand=True, padx=16, pady=8)
        self.canvas = tk.Canvas(body, bg=BAND, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg=BAND)
        self.list_frame.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._resize_list)

        self.status = tk.Label(self.root, text="", bg=BAND, fg=MUTED, font=_font(9), pady=8)
        self.status.pack(fill="x")

    def _metric(self, parent: tk.Frame, title: str, variable: tk.StringVar, color: str, row: int, column: int) -> None:
        card = tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
        bar = tk.Frame(card, bg=color, width=4)
        bar.pack(side="left", fill="y")
        inner = tk.Frame(card, bg=WHITE)
        inner.pack(fill="both", expand=True, padx=10, pady=8)
        tk.Label(inner, text=title, bg=WHITE, fg=MUTED, font=_font(8)).pack(anchor="w")
        tk.Label(inner, textvariable=variable, bg=WHITE, fg=INK, font=_font(12, "bold")).pack(anchor="w")

    def _resize_list(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self.canvas.itemconfigure(1, width=event.width)

    def refresh(self) -> None:
        self.connect_button.pack_forget()
        self.pending_button.pack_forget()
        if not self.store.settings.configured:
            self.connect_button.pack(fill="x", padx=16, pady=(12, 0), before=self.month_frame)
        elif self.store.pending_pairing:
            self.pending_button.pack(fill="x", padx=16, pady=(12, 0), before=self.month_frame)

        title = HongKongDate.month_title(self.store.selected_month)
        if self.store.settings.role == "viewer":
            title += " · 只读"
        self.month_label.configure(text=title)
        self.metric_vars["income"].set(format_hkd(self.store.summary.income_cents))
        self.metric_vars["expense"].set(format_hkd(self.store.summary.expense_cents))
        self.metric_vars["balance"].set(format_hkd(self.store.summary.balance_cents))
        self.metric_vars["pending"].set(str(self.store.summary.pending_receipts))
        color = MUTED if self.store.sync_message in OK_STATUS or self.store.sync_message.startswith("已同步") else CORAL
        self.status.configure(text=self.store.sync_message, fg=color)
        for child in self.list_frame.winfo_children():
            child.destroy()
        self._manual_section()
        self._section("支出大类", self.store.summary.expense_by_category)
        self._section("收入大类", self.store.summary.income_by_category)
        tk.Label(self.list_frame, text="小票", bg=BAND, fg=INK, font=_font(11, "bold")).pack(anchor="w", pady=(12, 4))
        if self.store.held_receipt_count:
            tk.Label(
                self.list_frame,
                text=f"{self.store.held_receipt_count} 张小票属于其他服务器，将保留到重新连接原服务器。",
                bg=BAND,
                fg=MUTED,
                font=_font(8),
                wraplength=360,
                justify="left",
            ).pack(anchor="w")
        if not self.store.receipts:
            tk.Label(self.list_frame, text="没有待同步小票", bg=BAND, fg=MUTED, font=_font(10)).pack(anchor="w")
            return
        for receipt in self.store.receipts:
            row = tk.Frame(self.list_frame, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
            row.pack(fill="x", pady=4)
            title_text = receipt.merchant or f"小票 {receipt.captured_at.replace('T', ' ')[:16]}"
            detail = receipt.error_message or receipt.status_label
            if receipt.server_id not in (None, self.store.settings.server_id):
                detail = "属于其他服务器，已保留在本机"
            detail_color = CORAL if receipt.status == "error" or receipt.blocked_reason else MUTED
            info = tk.Frame(row, bg=WHITE)
            info.pack(side="left", fill="both", expand=True, padx=10, pady=8)
            tk.Label(info, text=title_text, bg=WHITE, fg=INK, font=_font(10), anchor="w").pack(fill="x")
            tk.Label(info, text=detail, bg=WHITE, fg=detail_color, font=_font(8), anchor="w", wraplength=280, justify="left").pack(
                fill="x"
            )
            if receipt.blocked_reason == "invalid":
                tk.Button(
                    info,
                    text="重试这张小票",
                    command=lambda receipt_id=receipt.id: self._background(lambda: self.store.retry_receipt(receipt_id)),
                    relief="flat",
                    fg=TEAL,
                    bg=WHITE,
                    font=_font(8),
                ).pack(anchor="w")
            if receipt.amount_cents is not None:
                tk.Label(row, text=format_hkd(receipt.amount_cents), bg=WHITE, fg=INK, font=_font(10)).pack(side="right", padx=10)

    def _manual_section(self) -> None:
        if not self.store.manuals:
            return
        tk.Label(
            self.list_frame,
            text=f"待发送收支（{len(self.store.manuals)}）",
            bg=BAND,
            fg=INK,
            font=_font(11, "bold"),
        ).pack(anchor="w", pady=(12, 4))
        for entry in self.store.manuals:
            row = tk.Frame(self.list_frame, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
            row.pack(fill="x", pady=4)
            info = tk.Frame(row, bg=WHITE)
            info.pack(side="left", fill="both", expand=True, padx=10, pady=8)
            tk.Label(info, text=entry.content, bg=WHITE, fg=INK, font=_font(10), anchor="w").pack(fill="x")
            if entry.server_id != self.store.settings.server_id:
                detail = "属于其他服务器，已保留在本机"
            else:
                detail = entry.error_message or "等待同步到电脑"
            tk.Label(info, text=detail, bg=WHITE, fg=MUTED, font=_font(8), anchor="w", wraplength=280, justify="left").pack(
                fill="x"
            )
            if entry.server_id == self.store.settings.server_id and entry.blocked_reason:
                actions = tk.Frame(info, bg=WHITE)
                actions.pack(anchor="w", pady=(4, 0))
                if entry.blocked_reason == "invalid":
                    tk.Button(
                        actions,
                        text="修改后重试",
                        command=lambda item=entry: self.open_manual(item),
                        relief="flat",
                        fg=TEAL,
                        bg=WHITE,
                        font=_font(8),
                    ).pack(side="left")
                tk.Button(
                    actions,
                    text="移除本机待发送项",
                    command=lambda item=entry: self._discard_manual(item),
                    relief="flat",
                    fg=CORAL,
                    bg=WHITE,
                    font=_font(8),
                ).pack(side="left", padx=8)
            tk.Label(row, text=format_hkd(entry.amount_cents), bg=WHITE, fg=INK, font=_font(10)).pack(side="right", padx=10)

    def _discard_manual(self, entry: PendingManual) -> None:
        if not messagebox.askyesno("移除待发送项", "只从本机队列移除，不会删除电脑上的账目。确定移除？"):
            return
        try:
            self.store.discard_rejected_manual(entry.request_id)
        except OSError as error:
            messagebox.showerror("待发送收支", str(error))
            return
        self.refresh()

    def _section(self, title: str, rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        tk.Label(self.list_frame, text=title, bg=BAND, fg=INK, font=_font(11, "bold")).pack(anchor="w", pady=(12, 4))
        for item in rows:
            row = tk.Frame(self.list_frame, bg=WHITE)
            row.pack(fill="x", pady=2)
            left = tk.Frame(row, bg=WHITE)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(left, text=str(item.get("category") or ""), bg=WHITE, fg=INK, font=_font(10), anchor="w").pack(fill="x")
            tk.Label(left, text=f"{int(item.get('transaction_count') or 0)} 笔", bg=WHITE, fg=MUTED, font=_font(8), anchor="w").pack(
                fill="x"
            )
            tk.Label(row, text=format_hkd(int(item.get("amount_cents") or 0)), bg=WHITE, fg=INK, font=_font(10)).pack(side="right")

    def move_month(self, amount: int) -> None:
        self._background(lambda: self.store.move_month(amount))

    def sync_now(self) -> None:
        self._background(self.store.sync_all)

    def _periodic_sync(self) -> None:
        self.sync_now()
        self.root.after(15 * 60 * 1000, self._periodic_sync)

    def choose_receipt(self) -> None:
        if not self.store.can_write:
            messagebox.showinfo("小票", "此设备只有查看汇总的权限。")
            return
        path = filedialog.askopenfilename(
            title="选择小票图片",
            filetypes=[("小票图片", "*.jpg *.jpeg *.png"), ("JPEG", "*.jpg *.jpeg"), ("PNG", "*.png")],
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            messagebox.showerror("小票", str(error))
            return
        self._background(lambda: self.store.enqueue_image(data))

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("同步设置")
        dialog.configure(bg=WHITE)
        dialog.geometry("420x680")
        dialog.transient(self.root)
        dialog.grab_set()
        invitation = tk.StringVar()
        tk.Label(dialog, text="一次性二维码配对", bg=WHITE, fg=INK, font=_font(11, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        tk.Label(
            dialog,
            text="在电脑 http://localhost:8764 用 Passkey 解锁后生成邀请，把 receiptsync://pair?data=… 整段粘贴到这里。确认前请确认来自你自己的电脑。",
            bg=WHITE,
            fg=MUTED,
            wraplength=380,
            justify="left",
            font=_font(8),
        ).pack(anchor="w", padx=16)
        tk.Entry(dialog, textvariable=invitation, font=_font(9)).pack(fill="x", padx=16, pady=6)
        error = tk.Label(dialog, text="", bg=WHITE, fg=CORAL, font=_font(9), wraplength=380, justify="left")
        error.pack(anchor="w", padx=16)

        def confirm_invite() -> None:
            payload = invitation.get().strip()
            if payload:
                try:
                    PairingInvitation.parse(payload)
                except SyncError as parse_error:
                    error.configure(text=parse_error.message)
                    return
            elif not self.store.pending_pairing:
                error.configure(text="请粘贴电脑生成的邀请")
                return
            dialog.destroy()
            self._background(lambda: self.store.pair_with_invitation(payload or None), busy_message="正在连接电脑")

        def retry_pending() -> None:
            dialog.destroy()
            self._background(lambda: self.store.pair_with_invitation(None), busy_message="正在连接电脑")

        tk.Button(dialog, text="确认邀请并连接", command=confirm_invite, bg=TEAL, fg=WHITE, relief="flat").pack(
            fill="x", padx=16, pady=4
        )
        if self.store.pending_pairing:
            tk.Button(dialog, text="重试未完成的配对", command=retry_pending, relief="flat").pack(fill="x", padx=16)

        tk.Label(dialog, text="备用：手动填写", bg=WHITE, fg=INK, font=_font(11, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        fields: dict[str, tk.StringVar] = {}
        for key, label, initial, secret in (
            ("url", "电脑地址", self.store.settings.server_url, False),
            ("token", "同步密钥", self.store.settings.sync_token, True),
            ("cert", "证书 SHA-256", self.store.settings.certificate_sha256, False),
        ):
            tk.Label(dialog, text=label, bg=WHITE, fg=MUTED, font=_font(9)).pack(anchor="w", padx=16, pady=(8, 2))
            variable = tk.StringVar(value=initial)
            tk.Entry(dialog, textvariable=variable, show="•" if secret else "", font=_font(10)).pack(fill="x", padx=16)
            fields[key] = variable
        tk.Label(
            dialog,
            text="手动方式用于电脑管理页创建的独立设备密钥。不要填写审核页地址 8764。",
            bg=WHITE,
            fg=MUTED,
            wraplength=380,
            justify="left",
            font=_font(8),
        ).pack(anchor="w", padx=16, pady=8)
        role = (
            "只查看汇总"
            if self.store.settings.role == "viewer"
            else ("上传小票、记账和查看汇总" if self.store.settings.role else "尚未配对")
        )
        tk.Label(dialog, text=f"设备编号  {self.store.settings.device_id}", bg=WHITE, fg=MUTED, font=_font(8)).pack(
            anchor="w", padx=16
        )
        tk.Label(dialog, text=f"权限  {role}", bg=WHITE, fg=MUTED, font=_font(8)).pack(anchor="w", padx=16)

        def save_manual() -> None:
            message = PairingInput.validation_message(fields["url"].get(), fields["token"].get(), fields["cert"].get())
            if message:
                error.configure(text=message)
                return
            dialog.destroy()
            self._background(
                lambda: self.store.test_and_save(fields["url"].get(), fields["token"].get(), fields["cert"].get()),
                busy_message="正在连接电脑",
            )

        def forget() -> None:
            if not messagebox.askyesno("忘记电脑", "本机队列和电脑账目都会保留。确定忘记这台电脑？"):
                return
            dialog.destroy()
            self._background(self.store.forget_computer, busy_message="正在忘记电脑")

        actions = tk.Frame(dialog, bg=WHITE)
        actions.pack(fill="x", padx=16, pady=16)
        tk.Button(actions, text="取消", command=dialog.destroy, relief="flat").pack(side="right", padx=6)
        tk.Button(actions, text="测试并保存", command=save_manual, bg=TEAL, fg=WHITE, relief="flat", padx=12).pack(side="right")
        if self.store.settings.configured:
            tk.Button(actions, text="忘记电脑", command=forget, fg=CORAL, relief="flat").pack(side="left")

    def open_manual(self, existing: PendingManual | None = None) -> None:
        if not self.store.can_write:
            messagebox.showinfo("新增收支", "此设备只有查看汇总的权限。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("修改收支" if existing else "新增收支")
        dialog.configure(bg=WHITE)
        dialog.transient(self.root)
        dialog.grab_set()
        kind = tk.StringVar(value=existing.kind if existing else "income")
        picker = tk.Frame(dialog, bg=WHITE)
        picker.pack(fill="x", padx=16, pady=12)
        tk.Radiobutton(picker, text="收入", variable=kind, value="income", bg=WHITE).pack(side="left")
        tk.Radiobutton(picker, text="支出", variable=kind, value="expense", bg=WHITE).pack(side="left")
        values: dict[str, tk.StringVar] = {}
        today = datetime.now(HONG_KONG_TIMEZONE).strftime("%Y-%m-%d")
        amount_text = f"{existing.amount_cents / 100:.2f}" if existing else ""
        date_text = (existing.occurred_at[:10] if existing and existing.occurred_at else today)
        for key, label, initial in (
            ("date", "日期（香港）", date_text),
            ("category", "大类", existing.category if existing else ""),
            ("amount", "金额（HKD）", amount_text),
            ("content", "说明", existing.content if existing else ""),
            ("notes", "备注", existing.notes if existing else ""),
        ):
            tk.Label(dialog, text=label, bg=WHITE, fg=MUTED, font=_font(9)).pack(anchor="w", padx=16, pady=(8, 2))
            variable = tk.StringVar(value=initial)
            tk.Entry(dialog, textvariable=variable, font=_font(10)).pack(fill="x", padx=16)
            values[key] = variable
        error = tk.Label(dialog, text="", bg=WHITE, fg=CORAL, font=_font(9))
        error.pack(anchor="w", padx=16, pady=8)

        def save() -> None:
            category = values["category"].get().strip()
            try:
                amount = Decimal(values["amount"].get().strip())
            except (InvalidOperation, ValueError):
                error.configure(text="金额必须大于 HK$0.00")
                return
            if not category or amount <= 0:
                error.configure(text="请填写大类，且金额必须大于 HK$0.00")
                return
            try:
                day = datetime.strptime(values["date"].get().strip(), "%Y-%m-%d").replace(tzinfo=HONG_KONG_TIMEZONE)
            except ValueError:
                error.configure(text="日期须为 YYYY-MM-DD")
                return
            dialog.destroy()

            def work() -> None:
                self.store.add_manual(
                    kind.get(),
                    day,
                    category,
                    amount,
                    values["content"].get(),
                    values["notes"].get(),
                    request_id=existing.request_id if existing else None,
                )

            self._background(work)

        actions = tk.Frame(dialog, bg=WHITE)
        actions.pack(fill="x", padx=16, pady=12)
        tk.Button(actions, text="取消", command=dialog.destroy, relief="flat").pack(side="right", padx=6)
        tk.Button(actions, text="保存", command=save, bg=TEAL, fg=WHITE, relief="flat", padx=12).pack(side="right")

    def _background(self, work, *, busy_message: str = "正在同步") -> None:  # type: ignore[no-untyped-def]
        if self._busy:
            return
        self._busy = True
        self.store.sync_message = busy_message
        self.refresh()

        def task() -> None:
            try:
                with self._lock:
                    work()
            except Exception as error:
                with self._lock:
                    if isinstance(error, (SyncError,)):
                        self.store.sync_message = display_sync_error(error)
                    else:
                        self.store.sync_message = str(error)
            finally:
                self.root.after(0, self._finish)

        threading.Thread(target=task, daemon=True).start()

    def _finish(self) -> None:
        self._busy = False
        self.refresh()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    PhoneApp().run()


if __name__ == "__main__":
    main()
