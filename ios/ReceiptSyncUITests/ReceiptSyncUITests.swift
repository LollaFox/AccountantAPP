import XCTest

final class ReceiptSyncUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    func testHomeShowsUnconfiguredSummary() {
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["连接电脑"].exists)
        XCTAssertTrue(app.staticTexts["总收入"].exists)
        XCTAssertTrue(app.staticTexts["总支出"].exists)
        XCTAssertTrue(app.staticTexts["结余"].exists)
        XCTAssertTrue(app.staticTexts["待处理"].exists)
        XCTAssertTrue(app.staticTexts["没有待同步小票"].exists)
        XCTAssertTrue(app.staticTexts["请先设置电脑地址"].exists)
    }

    func testSettingsSheetValidatesHTTPSPairingFields() {
        app.buttons["设置"].tap()
        XCTAssertTrue(app.navigationBars["同步设置"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.secureTextFields["同步密钥"].exists)
        XCTAssertFalse(app.buttons["保存"].isEnabled)
        app.buttons["取消"].tap()
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 3))
    }

    func testManualEntryRequiresCategoryAndPositiveAmount() {
        app.buttons["新增收支"].tap()
        XCTAssertTrue(app.navigationBars["新增收支"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["收入"].exists)
        XCTAssertTrue(app.buttons["支出"].exists)
        XCTAssertFalse(app.buttons["保存"].isEnabled)
        app.buttons["取消"].tap()
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 3))
    }

    func testPhotoPickerOpensWhenCameraUnavailable() {
        app.buttons["拍摄小票"].tap()
        let cancel = app.buttons["Cancel"].firstMatch
        let chineseCancel = app.buttons["取消"].firstMatch
        let photos = app.navigationBars["Photos"].firstMatch
        let chinesePhotos = app.navigationBars["照片"].firstMatch
        let appeared = cancel.waitForExistence(timeout: 4)
            || chineseCancel.waitForExistence(timeout: 1)
            || photos.waitForExistence(timeout: 1)
            || chinesePhotos.waitForExistence(timeout: 1)
        XCTAssertTrue(appeared, "Expected the simulator photo library picker")
        if cancel.exists { cancel.tap() }
        else if chineseCancel.exists { chineseCancel.tap() }
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 3))
    }

    func testConnectComputerOpensSettings() {
        app.buttons["连接电脑"].tap()
        XCTAssertTrue(app.navigationBars["同步设置"].waitForExistence(timeout: 3))
        app.buttons["取消"].tap()
    }

    func testMonthNavigationChangesHongKongMonth() {
        let month = app.staticTexts["selectedMonth"]
        XCTAssertTrue(month.waitForExistence(timeout: 3))
        let original = month.label
        app.buttons["上个月"].tap()
        XCTAssertTrue(month.waitForExistence(timeout: 2))
        XCTAssertNotEqual(month.label, original)
        app.buttons["下个月"].tap()
        XCTAssertEqual(month.label, original)
    }

    func testSettingsSaveRequiresCompletePairingFields() {
        app.buttons["设置"].tap()
        XCTAssertTrue(app.navigationBars["同步设置"].waitForExistence(timeout: 3))
        let save = app.buttons["保存"]
        XCTAssertFalse(save.isEnabled)

        let url = app.textFields["电脑地址"]
        url.tap()
        url.typeText("https://192.0.2.10:8765")
        XCTAssertFalse(save.isEnabled)

        app.secureTextFields["同步密钥"].tap()
        app.secureTextFields["同步密钥"].typeText("device-token")
        XCTAssertFalse(save.isEnabled)

        let fingerprint = app.textFields["证书 SHA-256"]
        fingerprint.tap()
        fingerprint.typeText("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        XCTAssertTrue(save.isEnabled)
        app.buttons["取消"].tap()
    }

    func testLiveHotspotPairingAndSummary() throws {
        guard let pairing = Self.livePairing() else {
            throw XCTSkip("live pairing environment is not set")
        }
        let url = pairing.url
        let token = pairing.token
        let cert = pairing.cert

        app.buttons["设置"].tap()
        XCTAssertTrue(app.navigationBars["同步设置"].waitForExistence(timeout: 3))

        let urlField = app.textFields["电脑地址"]
        urlField.tap()
        urlField.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 1) {
            app.menuItems["Select All"].tap()
        }
        urlField.typeText(url)

        let tokenField = app.secureTextFields["同步密钥"]
        tokenField.tap()
        tokenField.typeText(token)

        let certField = app.textFields["证书 SHA-256"]
        certField.tap()
        certField.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 1) {
            app.menuItems["Select All"].tap()
        }
        certField.typeText(cert)

        XCTAssertTrue(app.buttons["保存"].isEnabled)
        app.buttons["保存"].tap()
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["已同步"].waitForExistence(timeout: 12))
        XCTAssertFalse(app.staticTexts["请先设置电脑地址"].exists)
    }

    private struct LivePairing {
        let url: String
        let token: String
        let cert: String
    }

    private static func livePairing() -> LivePairing? {
        let env = ProcessInfo.processInfo.environment
        if let url = env["RECEIPT_SYNC_URL"], let token = env["RECEIPT_SYNC_TOKEN"],
           let cert = env["RECEIPT_SYNC_CERT"], !url.isEmpty, !token.isEmpty, !cert.isEmpty {
            return LivePairing(url: url, token: token, cert: cert)
        }
        let candidates = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
            + FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
        for folder in candidates {
            let file = folder.appendingPathComponent("live-pairing.json")
            guard let data = try? Data(contentsOf: file),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                  let url = json["url"], let token = json["token"], let cert = json["cert"],
                  !url.isEmpty, !token.isEmpty, !cert.isEmpty else { continue }
            return LivePairing(url: url, token: token, cert: cert)
        }
        return nil
    }
}
