import SwiftUI

struct ContentView: View {
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
                        ForEach(store.receipts) { receipt in
                            ReceiptRow(receipt: receipt)
                        }
                    }
                }
            }
            .navigationTitle("收支")
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { showingManual = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("新增收支")
                    Button { showingCamera = true } label: { Image(systemName: "camera.fill") }
                        .accessibilityLabel("拍摄小票")
                    Button { showingSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("设置")
                }
            }
            .refreshable { await store.syncAll() }
            .safeAreaInset(edge: .bottom) {
                if !store.syncMessage.isEmpty {
                    Text(store.syncMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
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
        .sheet(isPresented: $showingSettings) { SettingsView() }
        .sheet(isPresented: $showingManual) { ManualTransactionView() }
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
    @State private var serverURL = ""
    @State private var token = ""
    @State private var certificateSHA256 = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("电脑") {
                    TextField("https://192.168.1.10:8765", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .accessibilityLabel("电脑地址")
                    SecureField("同步密钥", text: $token)
                    TextField("证书 SHA-256", text: $certificateSHA256, axis: .vertical)
                        .textInputAutocapitalization(.characters)
                        .lineLimit(2...4)
                        .accessibilityLabel("证书 SHA-256")
                }
                Section("设备") {
                    LabeledContent("设备编号", value: store.settings.deviceID)
                }
            }
            .navigationTitle("同步设置")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        store.updateSettings(serverURL: serverURL, syncToken: token, certificateSHA256: certificateSHA256)
                        dismiss()
                    }
                    .disabled(!serverURL.lowercased().hasPrefix("https://") || token.isEmpty || certificateSHA256.replacingOccurrences(of: ":", with: "").count != 64)
                }
            }
            .onAppear {
                serverURL = store.settings.serverURL
                token = store.settings.syncToken
                certificateSHA256 = store.settings.certificateSHA256
            }
        }
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
            try await store.addManual(kind: kind, date: date, category: category, amount: decimal, content: content, notes: notes)
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
