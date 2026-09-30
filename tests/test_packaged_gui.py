"""Smoke checks for the portable GUI executable and its copy helper."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


_exe_path = os.environ.get("XBOX_GAME_PREP_EXE")
EXE = Path(_exe_path) if _exe_path else None


@unittest.skipUnless(EXE is not None and EXE.is_file(),
                     "Set XBOX_GAME_PREP_EXE to test a packaged GUI build")
class PackagedGuiTests(unittest.TestCase):
    def test_startup_check(self):
        result = subprocess.run([str(EXE), "--startup-check"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GUI startup check passed", result.stdout)

    def test_batch_worker_is_available_in_gui_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            entries = []
            for name, content in (("first.bin", b"first"), ("second.bin", b"second")):
                path = source / name
                path.write_bytes(content)
                entries.append({
                    "source": str(path),
                    "relative_destination": str(Path("Games") / name),
                    "size_bytes": len(content),
                })
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "batch_id": uuid.uuid4().hex,
                "destination": str(destination),
                "items": entries,
            }), encoding="utf-8")
            result = subprocess.run([str(EXE), "--copy-batch-worker", str(manifest)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            phases = [event["phase"] for event in events]
            self.assertLess(max(index for index, phase in enumerate(phases)
                                if phase == "copying"), phases.index("verification_phase_started"))
            self.assertLess(max(index for index, phase in enumerate(phases)
                                if phase == "verifying"), phases.index("commit_phase_started"))
            self.assertEqual(events[-1]["phase"], "done")
            for entry in entries:
                target = destination / entry["relative_destination"]
                self.assertEqual(target.read_bytes(), Path(entry["source"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
