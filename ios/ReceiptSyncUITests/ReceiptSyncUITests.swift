import XCTest

final class ReceiptSyncUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["-ui-testing"]
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
        XCTAssertTrue(app.buttons["测试并保存"].isEnabled)
        app.buttons["测试并保存"].tap()
        XCTAssertTrue(app.navigationBars["同步设置"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["pairingError"].waitForExistence(timeout: 2))
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

    func testQRPairingRequiresExplicitConfirmationAndValidInvitation() {
        app.buttons["设置"].tap()
        XCTAssertTrue(app.buttons["扫描电脑二维码"].waitForExistence(timeout: 3))
        let connect = app.buttons["确认邀请并连接"]
        XCTAssertFalse(connect.isEnabled)
        let invitation = app.secureTextFields["粘贴配对邀请"]
        focusPairingField(invitation)
        invitation.typeText("not-a-pairing-invitation")
        XCTAssertTrue(connect.isEnabled)
        connect.tap()
        XCTAssertTrue(app.staticTexts["pairingError"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.navigationBars["同步设置"].exists)
        app.buttons["取消"].tap()
        XCTAssertTrue(app.staticTexts["请先设置电脑地址"].waitForExistence(timeout: 3))
    }

    private func focusPairingField(_ field: XCUIElement) {
        // The QR section makes the form taller. Keep fields clear of the sticky
        // navigation bar and keyboard before tapping; automatic scrolling can hide them there.
        for _ in 0..<6 {
            let frame = field.frame
            let keyboardTop = app.keyboards.firstMatch.exists ? app.keyboards.firstMatch.frame.minY : app.frame.maxY
            let high = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.26))
            let low = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45))
            if frame.minY < 195 { high.press(forDuration: 0.1, thenDragTo: low) }
            else if frame.maxY > keyboardTop - 40 { low.press(forDuration: 0.1, thenDragTo: high) }
            else { break }
        }
        field.tap()
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
        let save = app.buttons["测试并保存"]
        XCTAssertTrue(save.isEnabled)
        save.tap()
        XCTAssertTrue(app.staticTexts["pairingError"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.navigationBars["同步设置"].exists)

        let url = app.textFields["电脑地址"]
        focusPairingField(url)
        url.typeText("https://192.0.2.10:8765")
        focusPairingField(app.secureTextFields["同步密钥"])
        app.secureTextFields["同步密钥"].typeText("device-token")
        let fingerprint = app.textFields["证书 SHA-256"]
        focusPairingField(fingerprint)
        fingerprint.typeText("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        XCTAssertTrue(save.isEnabled)
        app.buttons["取消"].tap()
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 3))
        app.buttons["设置"].tap()
        XCTAssertEqual(app.textFields["电脑地址"].value as? String, "https://电脑地址:8765")
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

        XCTAssertTrue(app.buttons["测试并保存"].isEnabled)
        app.buttons["测试并保存"].tap()
        XCTAssertTrue(app.navigationBars["收支"].waitForExistence(timeout: 20))
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
        // Never implicitly reuse a previous machine's pairing or Xcode's unresolved variables.
        // UI automation records typed values: opt in only with disposable test credentials.
        guard env["RECEIPT_SYNC_RUN_LIVE_UI"] == "1" else { return nil }
        if let url = env["RECEIPT_SYNC_URL"], let token = env["RECEIPT_SYNC_TOKEN"],
           let cert = env["RECEIPT_SYNC_CERT"], url.hasPrefix("https://"), !token.isEmpty,
           cert.count == 64, cert.allSatisfy(\.isHexDigit) {
            return LivePairing(url: url, token: token, cert: cert)
        }
        return nil
    }
}
