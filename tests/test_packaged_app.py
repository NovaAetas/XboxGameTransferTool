import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


EXE = Path(os.environ.get(
    "XBOX_HDD_PREP_EXE",
    Path(__file__).resolve().parents[1] / "dist" / "XboxHDDPrep" / "XboxHDDPrep.exe",
))


@unittest.skipUnless(EXE.is_file(), "Portable executable has not been built")
class PackagedAppTests(unittest.TestCase):
    @staticmethod
    def package_bytes():
        data = bytearray(0x400)
        data[:4] = b"LIVE"
        data[0x344:0x348] = bytes.fromhex("000D0000")
        data[0x360:0x364] = bytes.fromhex("58410AE9")
        return data

    @staticmethod
    def artifact_path(stdout, prefix):
        matches = [line[len(prefix):].strip() for line in stdout.splitlines()
                   if line.startswith(prefix)]
        if not matches:
            raise AssertionError(f"Missing {prefix!r} in output:\n{stdout}")
        return Path(matches[-1])

    def test_interactive_directory_prompts_and_completion_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            game = source / "Prompted Game"
            game.mkdir(parents=True)
            (game / "default.xex").write_bytes(b"synthetic launcher")
            destination = root / "destination"
            destination.mkdir()
            command = [str(EXE), "--work-dir", str(root / "work"), "--idle-timeout", "30"]
            response = f"{source}\n{destination}\n1\n\n"
            result = subprocess.run(command, input=response, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Source directory [", result.stdout)
            self.assertIn("Destination directory [", result.stdout)
            self.assertIn("1/1 Games Processed (1 transferred, 0 already present, 0 failed)", result.stdout)
            self.assertIn("Finished: 1/1 games processed", result.stdout)
            self.assertIn("For troubleshooting, check that text report first", result.stdout)
            self.assertIn("Press Enter to close this window", result.stdout)
            report = self.artifact_path(result.stdout, "Run report saved to: ")
            diagnostic = self.artifact_path(result.stdout, "Detailed diagnostic log: ")
            self.assertTrue(report.is_file())
            self.assertTrue(diagnostic.is_file())
            self.assertIn("No errors were detected", report.read_text(encoding="utf-8"))

    def test_copy_verify_and_critical_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            package = source / "Synthetic XBLA"
            package.write_bytes(self.package_bytes())
            destination = root / "destination"
            (destination / "Games").mkdir(parents=True)
            (destination / "Content").mkdir()
            command = [str(EXE), "--source", str(source), "--destination", str(destination),
                       "--work-dir", str(root / "work"), "--all", "--idle-timeout", "30",
                       "--skip-stfs-integrity"]
            first = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            target = destination / "Content" / "0000000000000000" / "58410AE9" / "000D0000" / package.name
            self.assertEqual(target.read_bytes(), package.read_bytes())
            self.assertIn("SHA-256 verified", first.stdout)
            target.write_bytes(b"X" * target.stat().st_size)
            second = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("CRITICAL FAILURE", second.stderr)
            self.assertIn("Existing destination differs", second.stderr)
            self.assertEqual(package.read_bytes(), self.package_bytes())
            report = self.artifact_path(second.stdout, "Run report saved to: ")
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("PREPARATION / TRANSFER / APPLICATION PROBLEM", report_text)
            self.assertIn("not proven bad", report_text.lower())

    def test_matching_game_folder_skips_without_reading_archive_or_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            archive = source / "Happy Wars.zip"
            archive.write_bytes(b"invalid archive that must never be opened")
            destination = root / "destination"
            game = destination / "Games" / "Xbox 360" / "Happy Wars"
            game.mkdir(parents=True)
            (game / "default.xex").write_bytes(b"existing")
            receipt = destination / ".XboxHDDPrep" / "receipts" / "old.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("invalid old receipt", encoding="utf-8")
            command = [str(EXE), "--source", str(source), "--destination", str(destination),
                       "--work-dir", str(root / "work"), "--all", "--idle-timeout", "30"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Folder already exists", result.stdout)
            self.assertNotIn("Unpacking", result.stdout)
            self.assertNotIn("Checking existing destination file", result.stdout)
            self.assertNotIn("Checking source before unpacking", result.stdout)

    def test_unrecorded_existing_game_skips_before_unpacking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            archive = source / "Existing Game.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Existing Game.iso", b"invalid on purpose")
            destination = root / "destination"
            game = destination / "Games" / "Xbox 360" / "Existing Game"
            game.mkdir(parents=True)
            (game / "default.xex").write_bytes(b"existing")
            command = [str(EXE), "--source", str(source), "--destination", str(destination),
                       "--work-dir", str(root / "work"), "--all", "--idle-timeout", "30"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("skipping this game entirely", result.stdout.lower())
            self.assertNotIn("Unpacking", result.stdout)

    def test_existing_title_folder_does_not_hide_new_xbla_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            archive = source / "More XBLA.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("58410AE9/000D0000/NEWPACKAGE", self.package_bytes())
            destination = root / "destination"
            title_folder = destination / "Content" / "0000000000000000" / "58410AE9" / "000D0000"
            title_folder.mkdir(parents=True)
            (title_folder / "OLDPACKAGE").write_bytes(self.package_bytes())
            command = [str(EXE), "--source", str(source), "--destination", str(destination),
                       "--work-dir", str(root / "work"), "--all", "--idle-timeout", "30",
                       "--skip-stfs-integrity"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Unpacking", result.stdout)
            self.assertEqual((title_folder / "NEWPACKAGE").read_bytes(), self.package_bytes())

    def test_game_failure_continues_and_final_report_names_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "Bad Game.zip").write_bytes(b"invalid archive")
            (source / "Z Bad Game.zip").write_bytes(b"another invalid archive")
            good = source / "Good Game"
            good.mkdir()
            (good / "default.xex").write_bytes(b"synthetic launcher")
            destination = root / "destination"
            destination.mkdir()
            command = [str(EXE), "--source", str(source), "--destination", str(destination),
                       "--work-dir", str(root / "work"), "--all", "--idle-timeout", "30"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CRITICAL FAILURE [SOURCE FILE REJECTED]: Bad Game.zip", result.stderr)
            self.assertIn("CRITICAL FAILURE [SOURCE FILE REJECTED]: Z Bad Game.zip", result.stderr)
            self.assertIn("Continuing with the next game", result.stdout)
            self.assertEqual((destination / "Games" / "Xbox 360" / "Good Game" / "default.xex").read_bytes(),
                             b"synthetic launcher")
            self.assertIn("Finished: 3/3 games processed", result.stdout)
            self.assertIn("Transferred and SHA-256 verified: 1", result.stdout)
            self.assertIn("Failed with errors: 2", result.stdout)
            self.assertIn("Bad Game.zip:", result.stdout)
            self.assertIn("Z Bad Game.zip:", result.stdout)
            report = self.artifact_path(result.stdout, "Run report saved to: ")
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("SOURCE FILE REJECTED", report_text)
            self.assertIn("archive_corrupt_or_incomplete", report_text)

    def test_early_configuration_failure_still_writes_both_artifacts(self):
        command = [str(EXE), "--all", "--idle-timeout", "1", "--no-pause"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        report = self.artifact_path(result.stdout, "Run report saved to: ")
        diagnostic = self.artifact_path(result.stdout, "Detailed diagnostic log: ")
        self.assertTrue(report.is_file())
        self.assertTrue(diagnostic.is_file())
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("idle_timeout_invalid", report_text)
        self.assertIn("PREPARATION / TRANSFER / APPLICATION PROBLEM", report_text)


if __name__ == "__main__":
    unittest.main()
