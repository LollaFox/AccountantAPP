import XCTest
@testable import ReceiptSync

final class HongKongDateTests: XCTestCase {
    func testMonthKeyUsesHongKongCalendarBoundary() {
        let lateAugustUTC = ISO8601DateFormatter().date(from: "2026-08-31T15:59:00Z")!
        let earlySeptemberUTC = ISO8601DateFormatter().date(from: "2026-08-31T16:30:00Z")!
        XCTAssertEqual(HongKongDate.monthKey(from: lateAugustUTC), "2026-08")
        XCTAssertEqual(HongKongDate.monthKey(from: earlySeptemberUTC), "2026-09")
    }

    func testIsoStringIncludesColonSeparatedOffset() {
        let date = ISO8601DateFormatter().date(from: "2026-09-04T04:00:00Z")!
        let encoded = HongKongDate.isoString(from: date)
        XCTAssertTrue(encoded.hasSuffix("+08:00"), encoded)
        XCTAssertEqual(encoded, "2026-09-04T12:00:00+08:00")
    }

    func testAddMonthsStaysOnHongKongCalendar() {
        let september = ISO8601DateFormatter().date(from: "2026-09-04T04:00:00Z")!
        XCTAssertEqual(HongKongDate.monthKey(from: HongKongDate.addMonths(-1, to: september)), "2026-08")
        XCTAssertEqual(HongKongDate.monthKey(from: HongKongDate.addMonths(1, to: september)), "2026-10")
    }

    func testCurrencyFormatsHKD() {
        XCTAssertEqual(CurrencyFormatter.string(cents: 0), "HK$0.00")
        XCTAssertEqual(CurrencyFormatter.string(cents: 250), "HK$2.50")
    }
}
