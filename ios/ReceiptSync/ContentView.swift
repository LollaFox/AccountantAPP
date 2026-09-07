import SwiftUI

struct ContentView: View {
    @State private var editingManual: PendingManual?
    @State private var discardingManual: PendingManual?
    @State private var queueError = ""
    @EnvironmentObject private var store: ReceiptStore
    @State private var showingCamera = false
    @State private var showingSettings = false
    @State private var showingManual = false

    var body: some View {
        NavigationStack {
            List {
                if !store.configured {
                    Section {
                        Button("连接电脑") { showingSettings = true }
                            .frame(maxWidth: .infinity, alignment: .center)
                    }
                }

                Section {
                    monthSelector
                    summaryGrid
                    if store.connectionState.isFailure && !store.summary.month.isEmpty {
                        Text("当前显示上次取得的汇总，连接恢复后更新。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                if !store.pendingManuals.isEmpty {
                    Section("待发送收支（\(store.pendingManuals.count)）") {
                        ForEach(store.pendingManuals) { entry in
                            VStack(alignment: .leading) {
                                HStack {
                                    Text(entry.transaction.content)
                                    Spacer()
                                    Text(CurrencyFormatter.string(cents: entry.transaction.amountCents))
                                }
                                Text(entry.serverID != store.settings.serverID ? "属于其他服务器，已保留在本机" : (entry.errorMessage ?? "等待同步到电脑"))
                                    .font(.caption).foregroundStyle(.secondary)
                                if entry.serverID == store.settings.serverID, let reason = entry.blockedReason {
                                    HStack {
                                        if reason == "invalid" {
                                            Button("修改后重试") { editingManual = entry }
                                        }
                                        Button("移除本机待发送项", role: .destructive) { discardingManual = entry }
                                    }.buttonStyle(.borderless)
                                }
                            }
                        }
                    }
                }

                if !store.summary.expenseByCategory.isEmpty {
                    Section("支出大类") {
                        ForEach(store.summary.expenseByCategory) { row in
                            CategoryRow(total: row)
                        }
                    }
                }

                if !store.summary.incomeByCategory.isEmpty {
                    Section("收入大类") {
                        ForEach(store.summary.incomeByCategory) { row in
                            CategoryRow(total: row)
                        }
                    }
                }

                Section("小票") {
                    if store.receipts.isEmpty {
                        ContentUnavailableView("没有待同步小票", systemImage: "doc.text.viewfinder")
                    } else {
                        if store.heldReceiptCount > 0 {
                            Text("\(store.heldReceiptCount) 张小票属于其他服务器，将保留到重新连接原服务器。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        ForEach(store.receipts) { receipt in
                            ReceiptRow(receipt: receipt)
                            if receipt.blockedReason != nil {
                                Button("核对后重试此小票") {
                                    Task {
                                        do { try await store.retryReceipt(receipt.id) }
                                        catch { queueError = NetworkSyncMessage.display(error) }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("收支")
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { showingManual = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("新增收支")
                        .disabled(!store.canWrite)
                    Button { showingCamera = true } label: { Image(systemName: "camera.fill") }
                        .accessibilityLabel("拍摄小票")
                        .disabled(!store.canWrite)
                    Button { showingSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("设置")
                }
            }
            .refreshable { await store.syncAll() }
            .safeAreaInset(edge: .bottom) {
                if !store.syncMessage.isEmpty {
                    Text(store.syncMessage)
                        .font(.caption)
                        .foregroundStyle(syncMessageColor)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 4)
                }
            }
        }
        .tint(Color(red: 0.06, green: 0.46, blue: 0.43))
        .sheet(isPresented: $showingCamera) {
            CameraPicker { image in Task { await store.enqueue(image: image) } }
                .ignoresSafeArea()
        }
        .sheet(isPresented: $showingSettings) {
            SettingsView(
                serverURL: store.settings.serverURL,
                token: store.settings.syncToken,
                certificateSHA256: store.settings.certificateSHA256
            )
        }
        .sheet(isPresented: $showingManual) { ManualTransactionView() }
        .sheet(item: $editingManual) { entry in ManualTransactionView(entry: entry) }
        .confirmationDialog("只移除本机待发送项，不会删除电脑上的账目。若有编号冲突，请先在电脑核对。", isPresented: Binding(
            get: { discardingManual != nil }, set: { if !$0 { discardingManual = nil } }
        ), titleVisibility: .visible) {
            if let entry = discardingManual {
                Button("移除本机待发送项", role: .destructive) {
                    Task {
                        do { try await store.discardRejectedManual(entry.id) }
                        catch { queueError = NetworkSyncMessage.display(error) }
                    }
                }
            }
        }
        .alert("队列操作失败", isPresented: Binding(get: { !queueError.isEmpty }, set: { if !$0 { queueError = "" } })) {
            Button("确定") { queueError = "" }
        } message: { Text(queueError) }
    }

    private var syncMessageColor: Color {
        store.connectionState.isFailure ? .red : .secondary
    }

    private var monthSelector: some View {
        HStack {
            Button { store.moveMonth(-1) } label: { Image(systemName: "chevron.left") }
                .buttonStyle(.borderless)
                .accessibilityLabel("上个月")
            Spacer()
            Text(HongKongDate.monthTitle(from: store.selectedMonth))
                .font(.headline)
                .accessibilityIdentifier("selectedMonth")
            Spacer()
            Button { store.moveMonth(1) } label: { Image(systemName: "chevron.right") }
                .buttonStyle(.borderless)
                .accessibilityLabel("下个月")
        }
        .accessibilityElement(children: .contain)
    }

    private var summaryGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
            SummaryMetric(title: "总收入", cents: store.summary.incomeCents, color: .green)
            SummaryMetric(title: "总支出", cents: store.summary.expenseCents, color: .red)
            SummaryMetric(title: "结余", cents: store.summary.balanceCents, color: .teal)
            SummaryMetric(title: "待处理", value: "\(store.summary.pendingReceipts)", color: .orange)
        }
        .padding(.vertical, 4)
    }
}

private struct SummaryMetric: View {
    let title: String
    let value: String
    let color: Color

    init(title: String, cents: Int, color: Color) {
        self.title = title
        value = CurrencyFormatter.string(cents: cents)
        self.color = color
    }

    init(title: String, value: String, color: Color) {
        self.title = title
        self.value = value
        self.color = color
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.headline).monospacedDigit().lineLimit(1).minimumScaleFactor(0.72)
        }
        .frame(maxWidth: .infinity, minHeight: 58, alignment: .leading)
        .padding(10)
        .background(color.opacity(0.11), in: RoundedRectangle(cornerRadius: 6))
        .overlay(alignment: .leading) { Rectangle().fill(color).frame(width: 3) }
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct CategoryRow: View {
    let total: CategoryTotal
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(total.category)
                Text("\(total.transactionCount) 笔").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text(CurrencyFormatter.string(cents: total.amountCents)).monospacedDigit()
        }
    }
}

private struct ReceiptRow: View {
    let receipt: PendingReceipt

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(receipt.merchant ?? "小票 \(receipt.capturedAt.formatted(date: .abbreviated, time: .shortened))")
                    .lineLimit(1)
                Text(receipt.errorMessage ?? receipt.status.label)
                    .font(.caption)
                    .foregroundStyle(receipt.status == .error ? Color.red : Color.secondary)
                    .lineLimit(2)
            }
            Spacer()
            if let amount = receipt.amountCents {
                Text(CurrencyFormatter.string(cents: amount)).font(.subheadline).monospacedDigit()
            }
        }
    }

    private var icon: String {
        switch receipt.status {
        case .confirmed: "checkmark.circle.fill"
        case .error: "exclamationmark.triangle.fill"
        case .review: "person.crop.circle.badge.questionmark"
        case .processing, .uploading: "arrow.triangle.2.circlepath"
        default: "clock"
        }
    }

    private var color: Color {
        switch receipt.status {
        case .confirmed: .green
        case .error: .red
        case .review: .orange
        default: .teal
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var store: ReceiptStore
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL: String
    @State private var token: String
    @State private var certificateSHA256: String
    @State private var errorMessage = ""
    @State private var connectionTask: Task<Void, Never>?
    @State private var confirmingForget = false
    @State private var showingPairingScanner = false
    @State private var invitationText = ""

    init(serverURL: String, token: String, certificateSHA256: String) {
        _serverURL = State(initialValue: serverURL)
        _token = State(initialValue: token)
        _certificateSHA256 = State(initialValue: certificateSHA256)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("一次性二维码配对") {
                    Button("扫描电脑二维码") {
                        if PairingScanner.available { showingPairingScanner = true }
                        else { errorMessage = "此设备暂时无法扫描。请复制电脑显示的邀请，粘贴在下方。" }
                    }.disabled(store.testingConnection)
                    SecureField("粘贴配对邀请", text: $invitationText)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .disabled(store.testingConnection)
                    Button("确认邀请并连接") {
                        connectionTask = Task { await redeem(invitationText) }
                    }.disabled(store.testingConnection || invitationText.isEmpty)
                    if store.pendingPairing != nil {
                        Button("重试未完成的配对") { connectionTask = Task { await redeem(nil) } }
                            .disabled(store.testingConnection)
                    }
                    Text("先在这台 Mac 的管理页验证 Passkey，再生成邀请。确认前请确保二维码来自您自己的电脑。现有连接只有在验证成功后才会替换。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("电脑") {
                    TextField("https://电脑地址:8765", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .accessibilityLabel("电脑地址")
                    SecureField("同步密钥", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityLabel("同步密钥")
                    TextField("证书 SHA-256", text: $certificateSHA256, axis: .vertical)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .lineLimit(2...4)
                        .accessibilityLabel("证书 SHA-256")
                }
                .disabled(store.testingConnection)
                Section {
                    Text("在电脑「手机配对」中为此设备创建独立密钥。多台设备可连接同一台电脑，共用账本。使用配对页当前网络的地址；多个地址可用空格分隔。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if store.testingConnection {
                        ProgressView("正在验证安全连接与设备权限…")
                            .accessibilityIdentifier("testingConnection")
                    }
                    if !errorMessage.isEmpty {
                        Text(errorMessage)
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("pairingError")
                    }
                }
                Section("设备") {
                    LabeledContent("设备编号", value: store.settings.deviceID)
                    if let role = store.settings.role {
                        LabeledContent("权限", value: role == "viewer" ? "只查看汇总" : "上传小票、记账和查看汇总")
                    }
                    Text("验证成功后才会保存。取消或关闭此页面不会保存修改。")
                        .font(.footnote).foregroundStyle(.secondary)
                    if store.configured {
                        Button("忘记电脑", role: .destructive) { confirmingForget = true }
                            .disabled(store.testingConnection)
                    }
                }
            }
            .navigationTitle("同步设置")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") {
                        connectionTask?.cancel()
                        dismiss()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("测试并保存") { connectionTask = Task { await save() } }
                        .disabled(store.testingConnection)
                }
            }
            .onDisappear { connectionTask?.cancel() }
            .sheet(isPresented: $showingPairingScanner) {
                NavigationStack {
                    PairingScanner(completion: { text in
                        invitationText = text
                        showingPairingScanner = false
                    }, failure: {
                        showingPairingScanner = false
                        errorMessage = "无法使用相机，请检查相机权限或粘贴配对邀请。"
                    })
                    .navigationTitle("扫描电脑配对二维码")
                    .toolbar { Button("取消") { showingPairingScanner = false } }
                }
            }
            .confirmationDialog("忘记电脑？本机队列和电脑账目都会保留。", isPresented: $confirmingForget, titleVisibility: .visible) {
                Button("忘记电脑", role: .destructive) {
                    connectionTask = Task {
                        do { try await store.forgetComputer(); dismiss() }
                        catch { errorMessage = NetworkSyncMessage.display(error) }
                    }
                }
            }
        }
    }

    private func redeem(_ invitation: String?) async {
        do {
            errorMessage = ""
            try await store.pairWithInvitation(invitation)
            invitationText = ""
            dismiss()
            Task { await store.syncAll() }
        } catch { errorMessage = NetworkSyncMessage.display(error) }
    }

    private func save() async {
        errorMessage = ""
        do {
            try await store.connectAndSave(serverURL: serverURL, syncToken: token, certificateSHA256: certificateSHA256)
            dismiss()
            Task { await store.syncAll() }
        } catch { errorMessage = NetworkSyncMessage.display(error) }
    }
}

struct ManualTransactionView: View {
    @EnvironmentObject private var store: ReceiptStore
    @Environment(\.dismiss) private var dismiss
    @State private var kind = "income"
    @State private var date = Date()
    @State private var category = ""
    @State private var amount = ""
    @State private var content = ""
    @State private var notes = ""
    @State private var saving = false
    @State private var errorMessage = ""
    @State private var requestID = UUID().uuidString.lowercased()

    init(entry: PendingManual? = nil) {
        if let entry {
            _requestID = State(initialValue: entry.id)
            _kind = State(initialValue: entry.transaction.kind)
            _date = State(initialValue: ISO8601DateFormatter().date(from: entry.transaction.occurredAt) ?? Date())
            _category = State(initialValue: entry.transaction.category)
            _amount = State(initialValue: String(format: "%.2f", Double(entry.transaction.amountCents) / 100))
            _content = State(initialValue: entry.transaction.content)
            _notes = State(initialValue: entry.transaction.notes)
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Picker("类型", selection: $kind) {
                    Text("收入").tag("income")
                    Text("支出").tag("expense")
                }
                .pickerStyle(.segmented)
                DatePicker("日期", selection: $date, displayedComponents: .date)
                    .environment(\.calendar, HongKongDate.calendar)
                    .environment(\.timeZone, HongKongDate.timeZone)
                TextField("大类", text: $category)
                TextField("金额（HKD）", text: $amount).keyboardType(.decimalPad)
                TextField("说明", text: $content)
                TextField("备注", text: $notes, axis: .vertical).lineLimit(2...5)
                if !errorMessage.isEmpty { Text(errorMessage).foregroundStyle(.red) }
            }
            .navigationTitle("新增收支")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }
                        .disabled(saving || category.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || (Decimal(string: amount) ?? 0) <= 0)
                }
            }
        }
    }

    private func save() async {
        guard let decimal = Decimal(string: amount), decimal > 0 else {
            errorMessage = "金额必须大于 HK$0.00"
            return
        }
        saving = true
        do {
            try await store.addManual(requestID: requestID, kind: kind, date: date, category: category, amount: decimal, content: content, notes: notes)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
        saving = false
    }
}

enum CurrencyFormatter {
    static func string(cents: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = "HKD"
        formatter.locale = Locale(identifier: "zh_HK")
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        return formatter.string(from: NSNumber(value: Double(cents) / 100)) ?? "HK$0.00"
    }
}
