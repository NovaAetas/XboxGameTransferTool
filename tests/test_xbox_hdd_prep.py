import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xbox_hdd_prep as app


def package_bytes(title_id="58410AE9", content_type="000D0000"):
    data = bytearray(0x400)
    data[:4] = b"LIVE"
    data[0x344:0x348] = bytes.fromhex(content_type)
    data[0x360:0x364] = bytes.fromhex(title_id)
    return data


def write_package(path, title_id="58410AE9", content_type="000D0000", payload=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(package_bytes(title_id, content_type) + payload)
    return path


def stfschk_output(*, signature="valid LIVE signature", metadata="valid",
                   hash_tables="0/1", data_blocks="0/1",
                   directory_entries="0/1", missing_blocks="0/1",
                   package_size="0xC000", extra_lines=()):
    lines = [
        "Summary (invalid/total):",
        f"  Header signature: {signature}",
        f"  Metadata hash: {metadata}",
        *[f"  {line}" for line in extra_lines],
        f"  Hash tables: {hash_tables}",
        f"  Data blocks: {data_blocks}",
        f"  Directory entries: {directory_entries}",
        f"  Missing blocks: {missing_blocks}",
        f"  Package size: {package_size}",
        ("  HDD path: Content\\0000000000000000\\58410AE9\\000D0000\\PACKAGE"),
    ]
    return "\n".join(lines)


class PrepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # package_bytes deliberately creates only the header fields needed by
        # routing tests. It is not a complete STFS filesystem, so keep the
        # external integrity verifier mocked except in its focused tests.
        self.integrity_patcher = patch.object(
            app, "verify_stfs_integrity", return_value={"valid": True}, create=True,
        )
        self.verify_stfs_integrity = self.integrity_patcher.start()
        self.addCleanup(self.integrity_patcher.stop)

    def test_directory_prompt_accepts_default_and_custom_path(self):
        custom = self.root / "custom"
        custom.mkdir()
        with patch("builtins.input", side_effect=["", str(custom)]):
            self.assertEqual(app.prompt_directory("Source", self.root), self.root)
            self.assertEqual(app.prompt_directory("Destination", self.root), custom)

    def test_inventory_ignores_xboxhddready_and_prefers_loose_iso(self):
        source = self.root / "source"
        source.mkdir()
        (source / "XboxHDDReady").mkdir()
        (source / "Xbox-Aurora-Transfer-v1.1.0").mkdir()
        (source / "Juiced.7z").write_bytes(b"archive placeholder")
        (source / "Juiced.xiso.iso").write_bytes(b"iso placeholder")
        (source / "Happy Wars.rar").write_bytes(b"archive placeholder")
        with contextlib.redirect_stdout(io.StringIO()):
            inputs = app.list_inputs(source)
        self.assertEqual([p.name for p, _ in inputs], ["Happy Wars.rar", "Juiced.xiso.iso"])

    def test_archive_with_nested_xbla_package_gets_content_path(self):
        archive = self.root / "Happy Wars.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Happy Wars/58410AE9/000D0000/PACKAGE", package_bytes())
        work = self.root / "work"
        work.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            items = app.prepared_items(archive, archive.name, work, 30)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].relative_destination,
                         Path("Content/0000000000000000/58410AE9/000D0000/PACKAGE"))

    def test_archive_inside_archive_is_unwrapped(self):
        inner = self.root / "inner.zip"
        with zipfile.ZipFile(inner, "w") as output:
            output.writestr("58410AE9/000D0000/PACKAGE", package_bytes())
        outer = self.root / "outer.zip"
        with zipfile.ZipFile(outer, "w") as output:
            output.write(inner, "wrapper/inner.zip")
        work = self.root / "work"
        work.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            items = app.prepared_items(outer, outer.name, work, 30)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].relative_destination,
                         Path("Content/0000000000000000/58410AE9/000D0000/PACKAGE"))

    def test_mixed_archive_fails_instead_of_dropping_package(self):
        archive = self.root / "mixed.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Game.iso", b"placeholder")
            output.writestr("58410AE9/000D0000/PACKAGE", package_bytes())
        work = self.root / "work"
        work.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(app.PrepError, "disc images and separate"):
                app.prepared_items(archive, archive.name, work, 30)

    def test_malformed_content_tree_fails(self):
        content = self.root / "Content" / "0000000000000000" / "58410AE9" / "000D0000"
        content.mkdir(parents=True)
        (content / "bad-package").write_bytes(b"not STFS")
        with self.assertRaisesRegex(app.SourceError, "too small") as caught:
            app.content_items(self.root)
        self.assertEqual(caught.exception.code, "stfs_too_small")

    def test_content_tree_routes_by_detected_stfs_metadata_and_keeps_data_bundle(self):
        wrapper = (self.root / "Content" / "0000000000000000" /
                   "454108DD" / "000D0000")
        package = write_package(wrapper / "5841098100000001", "58410981", "000D0000")
        data_file = package.with_name(package.name + ".data") / "Data0000"
        data_file.parent.mkdir()
        data_file.write_bytes(b"companion data")

        plan = app.content_items(self.root)

        self.assertTrue(plan.found_content_root)
        self.assertFalse(plan.installer_only)
        canonical_package = Path(
            "Content/0000000000000000/58410981/000D0000/5841098100000001"
        )
        self.assertEqual(
            {item.relative_destination for item in plan.items},
            {
                canonical_package,
                Path("Content/0000000000000000/58410981/000D0000/"
                     "5841098100000001.data/Data0000"),
            },
        )
        self.assertTrue(all(item.bundle_source == package for item in plan.items))
        self.assertTrue(all(item.bundle_destination == canonical_package
                            for item in plan.items))
        self.verify_stfs_integrity.assert_called_once()

    def test_content_tree_routes_detected_dlc_instead_of_wrapper_arcade_type(self):
        package = write_package(
            self.root / "Content" / "0000000000000000" /
            "58410912" / "000D0000" / "584109120CCF0002",
            "58410912", "00000002",
        )

        plan = app.content_items(self.root)

        self.assertEqual(len(plan.items), 1)
        self.assertEqual(
            plan.items[0].relative_destination,
            Path("Content/0000000000000000/58410912/00000002") / package.name,
        )

    def test_installer_placeholder_routes_from_metadata_and_is_content_only(self):
        package = write_package(
            self.root / "Content" / "0000000000000000" /
            "FFED2000" / "FFFFFFFF" / "BORDERLANDS-DLC",
            "5454087C", "00000002",
        )

        plan = app.content_items(self.root)

        self.assertTrue(plan.found_content_root)
        self.assertTrue(plan.installer_only)
        self.assertEqual(
            [item.relative_destination for item in plan.items],
            [Path("Content/0000000000000000/5454087C/00000002") / package.name],
        )
        self.assertFalse(any("FFED2000" in str(item.relative_destination)
                             or "FFFFFFFF" in str(item.relative_destination)
                             for item in plan.items))

    def test_game_demo_content_type_is_supported_and_routed_from_metadata(self):
        self.assertIn("00080000", app.SUPPORTED_CONTENT_TYPES)
        package = write_package(
            self.root / "Content" / "0000000000000000" /
            "4D538837" / "00080000" / "ARCADE-DEMO",
            "584108AA", "00080000",
        )

        self.assertEqual(app.source_kind(self.root), "Xbox content tree")
        plan = app.content_items(self.root)

        self.assertEqual(
            [item.relative_destination for item in plan.items],
            [Path("Content/0000000000000000/584108AA/00080000") / package.name],
        )

    def test_identical_canonical_destinations_are_deduplicated_case_insensitively(self):
        first = self.root / "one" / "PACKAGE"
        second = self.root / "two" / "package"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"identical package")
        second.write_bytes(b"identical package")
        first_data = first.with_name(first.name + ".data") / "Data0000"
        second_data = second.with_name(second.name + ".data") / "Data0000"
        first_data.parent.mkdir()
        second_data.parent.mkdir()
        first_data.write_bytes(b"identical companion")
        second_data.write_bytes(b"identical companion")
        destination = Path("Content/0000000000000000/58410981/000D0000/PACKAGE")
        data_destination = destination.with_name(destination.name + ".data") / "Data0000"
        first_bundle = [
            app.CopyItem(first, destination, first, destination),
            app.CopyItem(first_data, data_destination, first, destination),
        ]

        items = app.deduplicate_plan(first_bundle + [
            app.CopyItem(second, Path(str(destination).lower()),
                         second, Path(str(destination).lower())),
            app.CopyItem(second_data, Path(str(data_destination).lower()),
                         second, Path(str(destination).lower())),
        ])

        self.assertEqual(items, first_bundle)

    def test_conflicting_canonical_destinations_are_rejected_with_hashes(self):
        first = self.root / "one" / "PACKAGE"
        second = self.root / "two" / "PACKAGE"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"identical package")
        second.write_bytes(b"identical package")
        first_data = first.with_name(first.name + ".data") / "Data0000"
        second_data = second.with_name(second.name + ".data") / "Data0000"
        first_data.parent.mkdir()
        second_data.parent.mkdir()
        first_data.write_bytes(b"first companion")
        second_data.write_bytes(b"different companion")
        destination = Path("Content/0000000000000000/58410981/000D0000/PACKAGE")
        data_destination = destination.with_name(destination.name + ".data") / "Data0000"

        with self.assertRaises(app.SourceError) as caught:
            app.deduplicate_plan([
                app.CopyItem(first, destination, first, destination),
                app.CopyItem(first_data, data_destination, first, destination),
                app.CopyItem(second, destination, second, destination),
                app.CopyItem(second_data, data_destination, second, destination),
            ])

        self.assertEqual(caught.exception.code, "duplicate_content_conflict")
        self.assertEqual(caught.exception.details["first_source"], str(first))
        self.assertEqual(caught.exception.details["second_source"], str(second))
        self.assertIn("first_bundle_sha256", caught.exception.details)
        self.assertIn("second_bundle_sha256", caught.exception.details)

    def test_stfschk_parser_accepts_clean_and_signature_only_invalid_packages(self):
        clean = app.parse_stfschk_output(stfschk_output(), actual_size=0xC000)
        self.assertTrue(clean["complete"])
        self.assertTrue(clean["valid"])
        self.assertTrue(clean["signature_valid"])
        self.assertEqual(clean["problems"], [])

        unsigned = app.parse_stfschk_output(
            stfschk_output(signature="invalid! (expected valid LIVE signature)"),
            actual_size=0xC000,
        )
        self.assertTrue(unsigned["complete"])
        self.assertTrue(unsigned["valid"])
        self.assertFalse(unsigned["signature_valid"])
        self.assertEqual(unsigned["problems"], [])
        self.assertTrue(unsigned["warnings"])

    def test_stfschk_parser_rejects_substantive_integrity_failures(self):
        cases = {
            "metadata hash": (stfschk_output(metadata="invalid"), 0xC000),
            "hash table": (stfschk_output(hash_tables="1/2"), 0xC000),
            "data block": (stfschk_output(data_blocks="1/2"), 0xC000),
            "directory entry": (stfschk_output(directory_entries="1/2"), 0xC000),
            "missing block": (stfschk_output(missing_blocks="1/2"), 0xC000),
            "truncated": (stfschk_output(
                package_size="0xB000 (expected 0xC000)",
                extra_lines=("(file truncated by 4096 bytes)",),
            ), 0xB000),
            "directory chain": (stfschk_output(
                extra_lines=("DirectoryChain.Length: 1 (expected 2)",),
            ), 0xC000),
        }
        for name, (output, actual_size) in cases.items():
            with self.subTest(name=name):
                result = app.parse_stfschk_output(output, actual_size=actual_size)
                self.assertTrue(result["complete"])
                self.assertFalse(result["valid"])
                self.assertTrue(result["problems"])

    def test_stfschk_parser_treats_trailing_bytes_and_header_values_as_advisory(self):
        cases = {
            "oversized": (stfschk_output(
                package_size="0xD000 (expected 0xC000)",
                extra_lines=("(file oversized, contains 4096 extra bytes)",),
            ), 0xD000),
            "metadata value": (stfschk_output(
                extra_lines=("Metadata.ContentSize: 0x1000 (expected 0x2000)",),
            ), 0xC000),
            "descriptor": (stfschk_output(
                extra_lines=("StfsVolumeDescriptor.NumberOfFreeBlocks: 1 (expected 0)",),
            ), 0xC000),
            "header value": (stfschk_output(
                extra_lines=("Header.SizeOfHeaders: 0x971A (expected 0xAD0E)",),
            ), 0xC000),
        }
        for name, (output, actual_size) in cases.items():
            with self.subTest(name=name):
                result = app.parse_stfschk_output(output, actual_size=actual_size)
                self.assertTrue(result["complete"])
                self.assertTrue(result["valid"])
                self.assertEqual(result["problems"], [])
                self.assertTrue(result["warnings"])

    def test_stfschk_parser_marks_protocol_and_parse_failures_incomplete(self):
        for output in (
            "unrelated tool output",
            "FileSystemParseException: volume descriptor is invalid",
            "IOException: unexpected end of file",
        ):
            with self.subTest(output=output):
                result = app.parse_stfschk_output(output, actual_size=0xC000)
                self.assertFalse(result["complete"])
                self.assertFalse(result["valid"])
                self.assertTrue(result["problems"])
                if "IOException" in output:
                    self.assertTrue(any("IOException" in problem
                                        for problem in result["problems"]))

    def test_recorder_writes_structured_log_and_human_report(self):
        recorder = app.RunRecorder(self.root)
        recorder.set_configuration(source="source", destination="destination")
        error = app.SourceError(
            "bad package", code="stfs_invalid_magic",
            details={"expected_title_id": "58410AE9", "magic_hex": "4E4F5045"},
        )
        diagnostic = app.exception_diagnostic(error, "source_preparation", "traceback fixture")
        recorder.add_input_result({"input": "bad.zip", "name": "bad.zip", "kind": "archive",
                                   "status": "failed", "diagnostic": diagnostic})
        recorder.set_summary(selected=1, handled=1, transferred_games=0,
                             skipped_existing=0, failed_games=1,
                             copied_files=0, verified_existing_files=0)
        recorder.finalize(1)

        events = [json.loads(line) for line in recorder.log_path.read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual([event["sequence"] for event in events],
                         list(range(1, len(events) + 1)))
        self.assertTrue(all(event["schema_version"] == app.LOG_SCHEMA_VERSION
                            for event in events))
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        report = recorder.report_path.read_text(encoding="utf-8")
        self.assertIn("SOURCE FILE REJECTED", report)
        self.assertIn("stfs_invalid_magic", report)
        self.assertIn("obtain a different", report.lower())
        self.assertIn(str(recorder.log_path), report)

    def test_operation_error_report_does_not_blame_source(self):
        recorder = app.RunRecorder(self.root)
        error = app.PrepError("copy failed", code="copy_hash_mismatch")
        diagnostic = app.exception_diagnostic(error, "copy_and_verification")
        recorder.add_input_result({"input": "game.zip", "name": "game.zip", "kind": "archive",
                                   "status": "failed", "diagnostic": diagnostic})
        recorder.set_summary(selected=1, handled=1, transferred_games=0,
                             skipped_existing=0, failed_games=1,
                             copied_files=0, verified_existing_files=0)
        recorder.finalize(1)
        report = recorder.report_path.read_text(encoding="utf-8")
        self.assertIn("PREPARATION / TRANSFER / APPLICATION PROBLEM", report)
        self.assertIn("source files were not proven bad", report.lower())

    def run_with_report(self, source, destination, artifact_folder):
        recorder = app.RunRecorder(artifact_folder)
        args = SimpleNamespace(source=source, destination=destination,
                               work_dir=self.root / "work", all=True, list=False,
                               idle_timeout=30, skip_stfs_integrity=False)
        previous = app._ACTIVE_RECORDER
        app._ACTIVE_RECORDER = recorder
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = app.run(args, recorder)
            recorder.finalize(result)
        finally:
            app._ACTIVE_RECORDER = previous
        return result, recorder

    def test_bad_source_run_is_classified_separately_from_transfer_failure(self):
        source = self.root / "bad-source"
        package = (source / "Bad Game" / "Content" / "0000000000000000" /
                   "58410AE9" / "000D0000" / "PACKAGE")
        package.parent.mkdir(parents=True)
        package.write_bytes(b"not an STFS package")
        destination = self.root / "bad-destination"
        destination.mkdir()

        result, recorder = self.run_with_report(source, destination,
                                                self.root / "bad-artifacts")
        self.assertEqual(result, 1)
        report = recorder.report_path.read_text(encoding="utf-8")
        self.assertIn("SOURCE FILE REJECTED", report)
        self.assertIn("stfs_too_small", report)
        events = [json.loads(line) for line in recorder.log_path.read_text(
            encoding="utf-8").splitlines()]
        failure = next(event for event in events
                       if event["event"] == "input_result" and event["status"] == "failed")
        self.assertEqual(failure["diagnostic"]["category"], "source_problem")

    def test_destination_conflict_run_says_source_was_not_proven_bad(self):
        source = self.root / "conflict-source"
        source.mkdir()
        package = source / "Synthetic XBLA"
        package.write_bytes(package_bytes())
        destination = self.root / "conflict-destination"
        target = (destination / "Content" / "0000000000000000" / "58410AE9" /
                  "000D0000" / package.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"X" * len(package_bytes()))

        result, recorder = self.run_with_report(source, destination,
                                                self.root / "conflict-artifacts")
        self.assertEqual(result, 1)
        report = recorder.report_path.read_text(encoding="utf-8")
        self.assertIn("PREPARATION / TRANSFER / APPLICATION PROBLEM", report)
        self.assertIn("not proven bad", report.lower())
        self.assertIn("destination_conflict_hash", report)

    def test_disc_image_extracts_to_game_folder(self):
        game = self.root / "synthetic_game"
        game.mkdir()
        (game / "default.xex").write_bytes(b"synthetic XEX fixture")
        (game / "$SystemUpdate").mkdir()
        (game / "$SystemUpdate" / "update.bin").write_bytes(b"skip")
        image = self.root / "Synthetic Game.iso"
        created = subprocess.run([str(app.XISO), "-c", str(game), str(image)],
                                 capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        work = self.root / "work"
        work.mkdir()
        with patch.object(app, "run_extract", wraps=app.run_extract) as extractor:
            with contextlib.redirect_stdout(io.StringIO()):
                items = app.prepared_items(image, image.name, work, 30)
        extract_command = extractor.call_args.args[0]
        self.assertIn("-x", extract_command)
        self.assertNotIn("-s", extract_command)
        self.assertEqual(
            (work / "disc-1" / "$SystemUpdate" / "update.bin").read_bytes(),
            b"skip",
        )
        self.assertIn(Path("Games/Xbox 360/Synthetic Game/default.xex"),
                      {item.relative_destination for item in items})
        self.assertFalse(any("$SystemUpdate" in str(item.relative_destination) for item in items))

    def test_missing_system_update_file_is_rejected_by_full_disc_verification(self):
        game = self.root / "synthetic_game"
        game.mkdir()
        (game / "default.xex").write_bytes(b"launcher")
        update = game / "$SystemUpdate" / "su20076000_00000000"
        update.parent.mkdir()
        update.write_bytes(b"required update fixture")
        image = self.root / "Synthetic Game.iso"
        created = subprocess.run([str(app.XISO), "-c", str(game), str(image)],
                                 capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        output = self.root / "extracted"
        real_extract = app.run_extract

        def extract_then_remove_update(command, output_folder, label, timeout):
            real_extract(command, output_folder, label, timeout)
            extracted_update = output_folder / "$SystemUpdate" / update.name
            if extracted_update.is_file():
                extracted_update.unlink()

        with patch.object(app, "run_extract", side_effect=extract_then_remove_update):
            with self.assertRaises(app.PrepError) as caught:
                app.iso_extract(image, output, 30)

        self.assertEqual(caught.exception.code, "extraction_output_mismatch")
        self.assertIn("$systemupdate/su20076000_00000000",
                      caught.exception.details["missing_sample"])

    def test_incomplete_disc_extraction_is_rejected(self):
        game = self.root / "synthetic_game"
        game.mkdir()
        (game / "default.xex").write_bytes(b"launcher")
        (game / "data.bin").write_bytes(b"required data")
        image = self.root / "Synthetic Game.iso"
        created = subprocess.run([str(app.XISO), "-c", str(game), str(image)],
                                 capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        output = self.root / "extracted"

        def partial_extract(_command, output_folder, _label, _timeout):
            (output_folder / "default.xex").write_bytes(b"launcher")

        with patch.object(app, "run_extract", side_effect=partial_extract):
            with self.assertRaisesRegex(app.PrepError, "Incomplete disc extraction"):
                app.iso_extract(image, output, 30)

    def test_forza_disc_two_routes_install_content_only(self):
        game = self.root / "disc2"
        package = game / "Content" / "0000000000000000" / "4D53084D" / "00000002" / "DLC"
        package.parent.mkdir(parents=True)
        package.write_bytes(package_bytes("4D53084D", "00000002"))
        (game / "default.xex").write_bytes(b"synthetic installer")
        image = self.root / "Forza 3 Ultimate Collection [PAL][DVD2].iso"
        created = subprocess.run([str(app.XISO), "-c", str(game), str(image)],
                                 capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        work = self.root / "work"
        work.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            items = app.prepared_items(image, image.name, work, 30)
        self.assertEqual([item.relative_destination for item in items],
                         [Path("Content/0000000000000000/4D53084D/00000002/DLC")])

    def test_copy_verifies_hash_and_refuses_different_existing_file(self):
        source = self.root / "source.bin"
        destination = self.root / "destination.bin"
        source.write_bytes(b"a" * 8192)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.worker_copy(source, destination), 0)
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        destination.write_bytes(b"b" * 8192)
        with self.assertRaisesRegex(app.PrepError, "differs"):
            app.worker_copy(source, destination)
        self.assertEqual(source.read_bytes(), b"a" * 8192)

    def test_fat32_large_file_preflight(self):
        source = self.root / "source.bin"
        with source.open("wb") as output:
            output.truncate(app.FAT32_MAX_FILE + 1)
        destination = self.root / "destination"
        destination.mkdir()
        with patch.object(app, "drive_format", return_value="FAT32"):
            with self.assertRaisesRegex(app.PrepError, "FAT32 cannot hold"):
                app.validate_plan([app.CopyItem(source, Path("Games/too-large.bin"))], destination)


if __name__ == "__main__":
    unittest.main()
