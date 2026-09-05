from __future__ import annotations

import importlib.util
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORT_PATH = REPO_ROOT / "ios" / "install_iphone_support.py"
INSTALL_SCRIPT = REPO_ROOT / "ios" / "install-iphone.sh"
INSTALL_COMMAND = REPO_ROOT / "install-iphone.command"


def load_support():
    spec = importlib.util.spec_from_file_location("install_iphone_support", SUPPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


support = load_support()


class IphoneInstallerTests(unittest.TestCase):
    def test_scripts_are_parseable_and_document_free_apple_id(self) -> None:
        subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], check=True)
        subprocess.run(["bash", "-n", str(INSTALL_COMMAND)], check=True)
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        command = INSTALL_COMMAND.read_text(encoding="utf-8")
        self.assertIn("ios/install-iphone.sh", command)
        self.assertIn("free Apple ID", text)
        self.assertIn("not required", text.lower())
        self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def test_suggest_bundle_id_stays_valid_for_numeric_team_prefix(self) -> None:
        bundle = support.suggest_bundle_id("9ABCDE1234")
        self.assertEqual(bundle, "com.receiptsync.t9abcde1234")
        self.assertRegex(bundle, support.BUNDLE_ID_RE)

    def test_xcdevice_keeps_usb_iphone_and_drops_simulator(self) -> None:
        payload = [
            {
                "simulator": True,
                "available": True,
                "platform": "com.apple.platform.iphonesimulator",
                "identifier": "SIM-1",
                "modelName": "iPhone 17",
                "name": "iPhone 17",
            },
            {
                "simulator": False,
                "available": True,
                "platform": "com.apple.platform.iphoneos",
                "identifier": "00008120-001A1234567890AB",
                "modelName": "iPhone 15",
                "name": "Test iPhone",
                "operatingSystemVersion": "26.5",
            },
            {
                "simulator": False,
                "available": True,
                "platform": "com.apple.platform.macosx",
                "identifier": "MAC-1",
                "modelName": "MacBook Pro",
                "name": "My Mac",
            },
        ]
        phones = support.parse_xcdevice_list(payload)
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["udid"], "00008120-001A1234567890AB")
        self.assertEqual(phones[0]["name"], "Test iPhone")

    def test_xcode_team_parser_hides_account_email(self) -> None:
        data = {
            "IDEProvisioningTeams": {
                "someone@example.com": [
                    {
                        "teamID": "AB12CD34EF",
                        "teamName": "Alex (Personal Team)",
                        "teamType": "Personal Team",
                    }
                ]
            }
        }
        teams = support.parse_xcode_provisioning_teams(data)
        self.assertEqual(teams, [("AB12CD34EF", "Personal Team")])
        self.assertNotIn("someone@example.com", teams[0][1])

    def test_xcode26_reads_team_by_identifier(self) -> None:
        data = {
            "IDEProvisioningTeamByIdentifier": {
                "825E4E2B-34B4-4AFF-8140-3DB7F4955829": [
                    {
                        "teamID": "AB12CD34EF",
                        "teamName": "Example (Personal Team)",
                        "isFreeProvisioningTeam": True,
                        "teamType": "Personal Team",
                    }
                ]
            }
        }
        self.assertEqual(
            support.parse_xcode_provisioning_teams(data),
            [("AB12CD34EF", "Personal Team")],
        )

    def test_pbxproj_and_certificate_team_ids(self) -> None:
        pbx = 'DEVELOPMENT_TEAM = AB12CD34EF;\nPRODUCT_BUNDLE_IDENTIFIER = com.local.receiptsync;\n'
        self.assertEqual(support.parse_pbxproj_team_ids(pbx), ["AB12CD34EF"])
        subject = "subject= /CN=Apple Development: user@example.com (AB12CD34EF)/OU=AB12CD34EF/O=User/C=US"
        self.assertEqual(support.parse_certificate_subjects(subject), ["AB12CD34EF"])

    def test_local_env_round_trip_rejects_unsafe_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "install-local.env"
            support.write_local_env(path, "AB12CD34EF", "com.receiptsync.tab12cd34ef", "00008120-001A")
            loaded = support.read_local_env(path)
            self.assertEqual(loaded["DEVELOPMENT_TEAM"], "AB12CD34EF")
            self.assertEqual(loaded["PRODUCT_BUNDLE_IDENTIFIER"], "com.receiptsync.tab12cd34ef")
            self.assertEqual(loaded["DEVICE_UDID"], "00008120-001A")
            with self.assertRaises(ValueError):
                support.write_local_env(path, "AB12CD34EF", "not a bundle", "00008120-001A")

    def test_ios_app_icon_is_opaque_1024_png_in_asset_catalog(self) -> None:
        icon = REPO_ROOT / "ios" / "ReceiptSync" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
        catalog = icon.with_name("Contents.json")
        project = (REPO_ROOT / "ios" / "ReceiptSync.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
        info = (REPO_ROOT / "ios" / "ReceiptSync" / "Info.plist").read_text(encoding="utf-8")
        self.assertTrue(icon.is_file())
        self.assertIn("AppIcon.png", catalog.read_text(encoding="utf-8"))
        self.assertIn("ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon", project)
        self.assertIn("Assets.xcassets", project)
        self.assertIn("CFBundleIconName", info)
        data = icon.read_bytes()
        self.assertGreaterEqual(len(data), 24)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        width, height, _bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height), (1024, 1024))
        self.assertEqual(color_type, 2, "iOS app icons must be opaque RGB, not RGBA")


if __name__ == "__main__":
    unittest.main()
