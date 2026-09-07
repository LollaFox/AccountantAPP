import SwiftUI
import VisionKit

struct PairingScanner: UIViewControllerRepresentable {
    var completion: (String) -> Void
    var failure: () -> Void
    static var available: Bool { DataScannerViewController.isSupported && DataScannerViewController.isAvailable }

    func makeCoordinator() -> Coordinator { Coordinator(completion: completion) }
    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])],
                                                qualityLevel: .balanced, recognizesMultipleItems: false,
                                                isGuidanceEnabled: true, isHighlightingEnabled: true)
        scanner.delegate = context.coordinator
        do { try scanner.startScanning() }
        catch { DispatchQueue.main.async { failure() } }
        return scanner
    }
    func updateUIViewController(_ uiViewController: DataScannerViewController, context: Context) {}
    static func dismantleUIViewController(_ uiViewController: DataScannerViewController, coordinator: Coordinator) { uiViewController.stopScanning() }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let completion: (String) -> Void
        var finished = false
        init(completion: @escaping (String) -> Void) { self.completion = completion }
        func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            guard !finished else { return }
            for item in addedItems {
                if case .barcode(let barcode) = item, let text = barcode.payloadStringValue {
                    finished = true
                    dataScanner.stopScanning()
                    completion(text)
                    return
                }
            }
        }
    }
}
