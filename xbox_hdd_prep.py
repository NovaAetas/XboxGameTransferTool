#!/usr/bin/env python3
"""Prepare Xbox game backups and copy them to a locally mounted Aurora drive.

The source archives are never changed. 7-Zip and extract-xiso are bundled beside
the packaged application. Only one selected input is staged at a time.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import datetime as dt
import errno
import hashlib
import json
import os
import platform
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


VERSION = "0.5.0-alpha.1"
LOG_SCHEMA_VERSION = 3
DEFAULT_SOURCE = Path(r"D:\Xbox360Staging")
DEFAULT_DESTINATION = Path("E:/")
IGNORED_TOP_LEVEL = {"xboxhddready", "xbox-aurora-transfer-v1.1.0", ".xbox-hdd-prep-work"}
ARCHIVE_SUFFIXES = {".7z", ".zip", ".rar"}
STFS_MAGICS = {b"CON ", b"LIVE", b"PIRS"}
SUPPORTED_CONTENT_TYPES = {"000D0000", "00007000", "00000002", "00080000"}
HEX8 = re.compile(r"^[0-9a-fA-F]{8}$")
FAT32_MAX_FILE = 0xFFFFFFFF
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)).resolve()
SEVEN_ZIP = RESOURCE_ROOT / "tools" / "7z.exe"
XISO = RESOURCE_ROOT / "tools" / "extract-xiso.exe"
STFSCHK = RESOURCE_ROOT / "tools" / "stfschk.exe"
STFSCHK_SHA256 = "4CE12A6D5E7F816F011B4E7BE6534B85E1A49B83B23B66C991968314951170D5"


class PrepError(RuntimeError):
    """Expected failure for which the source has not been proven defective."""

    category = "operation_problem"
    default_code = "preparation_or_transfer_error"
    default_recommendation = (
        "The source was not proven bad. Check the work and destination drives, free space, "
        "filesystem, connections, and the detailed diagnostic log before replacing the source."
    )

    def __init__(self, message: str, *, code: str | None = None,
                 details: dict[str, object] | None = None,
                 recommendation: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.details = details or {}
        self.recommendation = recommendation or self.default_recommendation


class SourceError(PrepError):
    """The input is malformed, incomplete, unsafe, inconsistent, or unsupported."""

    category = "source_problem"
    default_code = "source_rejected"
    default_recommendation = (
        "This input was rejected before a verified transfer. Obtain a different, complete dump "
        "or release unless the diagnostic details show a layout the application should support."
    )


class UnsupportedLayoutError(PrepError):
    """The source may be valid, but its layout is not safely supported yet."""

    category = "unsupported_layout"
    default_code = "unsupported_layout"
    default_recommendation = (
        "The source was not shown to be damaged. This Xbox content layout or content type is not "
        "supported safely yet; update Xbox HDD Prep or handle this input separately."
    )


class ConfigurationError(PrepError):
    category = "configuration_problem"
    default_code = "configuration_error"
    default_recommendation = "Correct the source, destination, or run settings and try again."


class ApplicationError(PrepError):
    category = "application_problem"
    default_code = "application_error"
    default_recommendation = (
        "The source was not proven bad. Repair or update Xbox HDD Prep and use the detailed log "
        "to diagnose the application failure."
    )


def path_metadata(path: Path) -> dict[str, object]:
    details: dict[str, object] = {"path": str(path)}
    try:
        details.update({
            "exists": path.exists(),
            "is_file": path.is_file(),
            "is_directory": path.is_dir(),
        })
        if path.is_file():
            stat = path.stat()
            details.update({"size_bytes": stat.st_size,
                            "modified": dt.datetime.fromtimestamp(
                                stat.st_mtime, tz=dt.timezone.utc).isoformat()})
    except OSError as exc:
        details["metadata_error"] = repr(exc)
    return details


def exception_diagnostic(exc: BaseException, stage: str,
                         traceback_text: str = "") -> dict[str, object]:
    if isinstance(exc, PrepError):
        category = exc.category
        code = exc.code
        details = dict(exc.details)
        recommendation = exc.recommendation
    elif isinstance(exc, subprocess.TimeoutExpired):
        category = "operation_problem"
        code = "external_tool_timeout"
        details = {"command": exc.cmd, "timeout_seconds": exc.timeout}
        recommendation = PrepError.default_recommendation
    elif isinstance(exc, OSError):
        category = "operation_problem"
        code = "filesystem_or_device_error"
        details = {
            "errno": exc.errno,
            "winerror": getattr(exc, "winerror", None),
            "filename": exc.filename,
            "filename2": exc.filename2,
        }
        recommendation = PrepError.default_recommendation
    else:
        category = "application_problem"
        code = "unexpected_application_error"
        details = {}
        recommendation = ApplicationError.default_recommendation
    return {
        "stage": stage,
        "category": category,
        "code": code,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "details": details,
        "recommendation": recommendation,
        "traceback": traceback_text,
    }


class RunRecorder:
    """Durable JSONL diagnostics plus a concise text report for one user run."""

    def __init__(self, base_dir: Path = ROOT) -> None:
        self.run_id = uuid.uuid4().hex
        self.started_at = dt.datetime.now().astimezone()
        self.started_monotonic = time.monotonic()
        self.configuration: dict[str, object] = {}
        self.input_results: list[dict[str, object]] = []
        self.fatal_error: dict[str, object] | None = None
        self.summary: dict[str, object] = {}
        self.mode = "prepare"
        self.current_stage = "startup"
        self.current_input: str | None = None
        self.current_notices: list[dict[str, object]] = []
        self._finalized = False
        self._sequence = 0
        stem = f"run-{self.started_at:%Y%m%d-%H%M%S-%f}-{self.run_id[:8]}"
        candidates = [base_dir]
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "XboxHDDPrep")
        candidates.append(Path(tempfile.gettempdir()) / "XboxHDDPrep")
        last_error: OSError | None = None
        for candidate in candidates:
            try:
                log_dir = candidate / "Logs"
                report_dir = candidate / "Reports"
                log_dir.mkdir(parents=True, exist_ok=True)
                report_dir.mkdir(parents=True, exist_ok=True)
                self.log_path = log_dir / f"{stem}-diagnostic.jsonl"
                self.report_path = report_dir / f"{stem}-report.txt"
                self._stream = self.log_path.open("x", encoding="utf-8", buffering=1)
                break
            except OSError as exc:
                last_error = exc
        else:
            raise ApplicationError(
                f"Could not create a diagnostic log or report: {last_error}",
                code="artifact_creation_failed",
                details={"attempted_locations": [str(path) for path in candidates],
                         "last_error": repr(last_error)},
            )
        self.record(
            "run_started",
            schema_version=LOG_SCHEMA_VERSION,
            application="Xbox HDD Prep",
            application_version=VERSION,
            command_line=sys.argv,
            executable=str(sys.executable),
            frozen=bool(getattr(sys, "frozen", False)),
            operating_system=platform.platform(),
            python_version=platform.python_version(),
            tools={"7zip": path_metadata(SEVEN_ZIP), "extract_xiso": path_metadata(XISO),
                   "stfschk": path_metadata(STFSCHK)},
            diagnostic_log=str(self.log_path),
            text_report=str(self.report_path),
        )

    def record(self, event: str, **fields: object) -> None:
        self._sequence += 1
        payload = {
            "schema_version": LOG_SCHEMA_VERSION,
            "sequence": self._sequence,
            "timestamp": dt.datetime.now().astimezone().isoformat(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        self._stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._stream.flush()

    def set_configuration(self, **fields: object) -> None:
        self.configuration.update(fields)
        self.record("configuration", **fields)

    def add_input_result(self, result: dict[str, object]) -> None:
        if self.current_notices and "notices" not in result:
            result["notices"] = list(self.current_notices)
        self.input_results.append(result)
        self.record("input_result", **result)
        self.current_notices = []

    def begin_input(self) -> None:
        self.current_notices = []

    def add_notice(self, code: str, message: str, **details: object) -> None:
        notice = {"code": code, "message": message, "details": details}
        if any(item.get("code") == code and item.get("message") == message
               for item in self.current_notices):
            return
        self.current_notices.append(notice)
        self.record("preparation_notice", input=self.current_input, **notice)

    def set_fatal_error(self, diagnostic: dict[str, object]) -> None:
        self.fatal_error = diagnostic
        self.record("fatal_error", **diagnostic)

    def set_summary(self, **fields: object) -> None:
        self.summary.update(fields)

    def _quick_summary(self) -> str:
        failures = [item for item in self.input_results if item.get("status") == "failed"]
        if self.fatal_error:
            failures.append({"diagnostic": self.fatal_error})
        source_failures = sum(
            1 for item in failures
            if isinstance(item.get("diagnostic"), dict)
            and item["diagnostic"].get("category") == "source_problem"
        )
        unsupported_failures = sum(
            1 for item in failures
            if isinstance(item.get("diagnostic"), dict)
            and item["diagnostic"].get("category") == "unsupported_layout"
        )
        operation_failures = len(failures) - source_failures - unsupported_failures
        if not failures:
            transferred = int(self.summary.get("transferred_games", 0))
            skipped = int(self.summary.get("skipped_existing", 0))
            if self.mode == "list":
                return "Inventory completed without preparing or moving files."
            if self.mode == "cancelled":
                return "The run was cancelled by the user; completed files were left in place."
            return (f"No errors were detected. {transferred} input(s) were transferred and verified; "
                    f"{skipped} existing game folder(s) were skipped without inspection.")
        if source_failures and not operation_failures and not unsupported_failures:
            return (f"{source_failures} input(s) were rejected as bad, incomplete, internally "
                    "inconsistent, unsafe, or unsupported source material. No preparation or "
                    "transfer fault was identified for those failures.")
        if unsupported_failures and not source_failures and not operation_failures:
            return (f"{unsupported_failures} input(s) used an unsupported Xbox layout or "
                    "content type. The source files were not proven bad.")
        if operation_failures and not source_failures and not unsupported_failures:
            return (f"{operation_failures} failure(s) occurred in configuration, the application, "
                    "preparation, destination writing, or verification. The affected source files "
                    "were not proven bad.")
        parts = []
        if source_failures:
            parts.append(f"{source_failures} source-file failure(s)")
        if unsupported_failures:
            parts.append(f"{unsupported_failures} unsupported-layout failure(s)")
        if operation_failures:
            parts.append(f"{operation_failures} preparation/transfer/application failure(s)")
        return "Mixed result: " + ", ".join(parts) + "."

    @staticmethod
    def _classification_label(diagnostic: dict[str, object]) -> str:
        if diagnostic.get("category") == "source_problem":
            return "SOURCE FILE REJECTED"
        if diagnostic.get("category") == "unsupported_layout":
            return "UNSUPPORTED LAYOUT"
        return "PREPARATION / TRANSFER / APPLICATION PROBLEM"

    def _build_report(self, exit_code: int) -> str:
        ended = dt.datetime.now().astimezone()
        lines = [
            "Xbox HDD Prep - End-of-Run Report",
            "=" * 36,
            f"Application version: {VERSION}",
            f"Run ID: {self.run_id}",
            f"Started: {self.started_at.isoformat()}",
            f"Ended: {ended.isoformat()}",
            f"Exit code: {exit_code}",
            "",
            "QUICK SUMMARY",
            "-------------",
            self._quick_summary(),
            "",
            "RUN SETTINGS",
            "------------",
        ]
        if self.configuration:
            for key, value in self.configuration.items():
                lines.append(f"{key.replace('_', ' ').title()}: {value}")
        else:
            lines.append("The run failed before all settings were collected.")
        lines.extend(["", "COUNTS", "------"])
        count_fields = (
            ("detected_inputs", "Detected inputs"),
            ("selected", "Selected inputs"),
            ("handled", "Processed inputs"),
            ("transferred_games", "Transferred and SHA-256 verified"),
            ("skipped_existing", "Existing game folders skipped without inspection"),
            ("failed_games", "Failed inputs"),
            ("copied_files", "New files copied"),
            ("verified_existing_files", "Existing files SHA-256 verified"),
        )
        for key, label in count_fields:
            lines.append(f"{label}: {self.summary.get(key, 0)}")
        lines.extend(["", "WHAT IT FOUND", "-------------"])
        inventory_by_kind = self.summary.get("inventory_by_kind", {})
        if isinstance(inventory_by_kind, dict) and inventory_by_kind:
            for kind, count in sorted(inventory_by_kind.items()):
                lines.append(f"{kind}: {count}")
        else:
            lines.append("No source inventory was completed.")
        lines.extend(["", "FINDINGS", "--------"])
        if not self.input_results and not self.fatal_error:
            lines.append("No input results were recorded.")
        for result in self.input_results:
            name = str(result.get("name") or result.get("input") or "Unknown input")
            status = result.get("status")
            if status == "verified":
                lines.append(f"[OK] {name} - prepared, transferred, and SHA-256 verified "
                             f"({result.get('files', 0)} files; {result.get('bytes', 0)} bytes).")
            elif status == "skipped_existing":
                lines.append(f"[SKIPPED, NOT CHECKED] {name} - matching destination folder: "
                             f"{result.get('folder')}")
            elif status == "failed":
                diagnostic = result.get("diagnostic", {})
                assert isinstance(diagnostic, dict)
                lines.extend([
                    f"[{self._classification_label(diagnostic)}] {name}",
                    f"  Stage: {diagnostic.get('stage')}",
                    f"  Code: {diagnostic.get('code')}",
                    f"  Error: {diagnostic.get('message')}",
                    f"  Assessment: {diagnostic.get('recommendation')}",
                ])
                details = diagnostic.get("details")
                if details:
                    lines.append("  Diagnostic details: " + json.dumps(
                        details, ensure_ascii=False, sort_keys=True, default=str))
            notices = result.get("notices", [])
            if isinstance(notices, list):
                for notice in notices:
                    if isinstance(notice, dict):
                        lines.append(f"  Note [{notice.get('code')}]: {notice.get('message')}")
        if self.fatal_error:
            fatal_target = (f" while processing {self.fatal_error['input']}"
                            if self.fatal_error.get("input") else "")
            lines.extend([
                f"[{self._classification_label(self.fatal_error)}] Run-level failure{fatal_target}",
                f"  Stage: {self.fatal_error.get('stage')}",
                f"  Code: {self.fatal_error.get('code')}",
                f"  Error: {self.fatal_error.get('message')}",
                f"  Assessment: {self.fatal_error.get('recommendation')}",
            ])
            if self.fatal_error.get("details"):
                lines.append("  Diagnostic details: " + json.dumps(
                    self.fatal_error["details"], ensure_ascii=False,
                    sort_keys=True, default=str))
        lines.extend([
            "",
            "HOW TO INTERPRET FAILURES",
            "-------------------------",
            "SOURCE FILE REJECTED means the input itself failed structural or consistency checks. "
            "A different complete dump/release is normally the next step.",
            "UNSUPPORTED LAYOUT means the input may be valid, but this application cannot yet "
            "place it safely. It is not evidence that the source is damaged.",
            "PREPARATION / TRANSFER / APPLICATION PROBLEM means the source was not proven bad. "
            "Check the drives, free space, filesystem, connections, application files, and the "
            "diagnostic log before replacing it.",
            "",
            f"Detailed diagnostic log: {self.log_path}",
            f"This text report: {self.report_path}",
            "Source files are never intentionally modified by Xbox HDD Prep.",
            "",
        ])
        return "\n".join(lines)

    def finalize(self, exit_code: int) -> None:
        if self._finalized:
            return
        self._finalized = True
        self.record(
            "run_finished",
            exit_code=exit_code,
            elapsed_seconds=round(time.monotonic() - self.started_monotonic, 3),
            summary=self.summary,
            quick_summary=self._quick_summary(),
            report_path=str(self.report_path),
        )
        self._stream.close()
        self.report_path.write_text(self._build_report(exit_code), encoding="utf-8")


_ACTIVE_RECORDER: RunRecorder | None = None
_CANCEL_EVENT: threading.Event | None = None


class UserCancelled(Exception):
    """Raised when the GUI requests a cooperative, cleanup-safe cancellation."""


def check_cancelled() -> None:
    if _CANCEL_EVENT is not None and _CANCEL_EVENT.is_set():
        raise UserCancelled("Run cancelled by the user")


def diagnostic_event(event: str, **fields: object) -> None:
    if _ACTIVE_RECORDER is not None:
        _ACTIVE_RECORDER.record(event, **fields)


def preparation_notice(code: str, message: str, **details: object) -> None:
    """Record an important non-fatal preparation decision in both outputs."""
    print(f"  NOTE: {message}", flush=True)
    if _ACTIVE_RECORDER is not None:
        _ACTIVE_RECORDER.add_notice(code, message, **details)


@dataclass(frozen=True)
class CopyItem:
    source: Path
    relative_destination: Path
    bundle_source: Path | None = None
    bundle_destination: Path | None = None


@dataclass
class ContentPlan:
    items: list[CopyItem]
    found_content_root: bool
    installer_only: bool = False

    def __bool__(self) -> bool:
        return bool(self.items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class CopyOutcome:
    status: str
    sha256: str


@dataclass(frozen=True)
class BatchCopyOutcome:
    copied_files: int
    verified_existing_files: int


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def safe_game_name(name: str) -> str:
    for suffix in (".xiso.iso", ".iso", ".7z", ".zip", ".rar"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = re.sub(r"[^A-Za-z0-9 ._()'&\[\]-]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        raise SourceError("Could not make a safe game folder name",
                          code="safe_name_empty", details={"original_name": name})
    return name[:60].rstrip(" .")


def archive_primary(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() in {".zip", ".7z"}:
        return True
    if path.suffix.lower() == ".rar":
        match = re.search(r"\.part0*(\d+)\.rar$", name)
        return match is None or int(match.group(1)) == 1
    return bool(re.search(r"\.(?:7z|zip)\.0*1$", name))


def input_key(path: Path) -> str:
    name = path.name.lower()
    for suffix in (".7z", ".zip", ".rar", ".xiso.iso", ".iso"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "", name)


def inspect_stfs(path: Path) -> dict[str, object]:
    """Return enough package-header evidence to diagnose every STFS rejection."""
    result: dict[str, object] = {"path": str(path)}
    if not path.is_file():
        result["status"] = "not_a_file"
        return result
    size = path.stat().st_size
    result["size_bytes"] = size
    if size < 0x364:
        with path.open("rb") as stream:
            prefix = stream.read(4)
        result.update({
            "status": "too_small",
            "minimum_header_bytes": 0x364,
            "magic_ascii": prefix.decode("ascii", errors="replace"),
            "magic_hex": prefix.hex().upper(),
        })
        return result
    with path.open("rb") as stream:
        header = stream.read(0x3AD)
    magic = header[:4]
    content_type = f"{int.from_bytes(header[0x344:0x348], 'big'):08X}"
    title_id = header[0x360:0x364].hex().upper()
    result.update({
        "magic_ascii": magic.decode("ascii", errors="replace"),
        "magic_hex": magic.hex().upper(),
        "detected_title_id": title_id,
        "detected_content_type": content_type,
        "volume_type": (int.from_bytes(header[0x3A9:0x3AD], "big")
                        if len(header) >= 0x3AD else None),
    })
    if magic not in STFS_MAGICS:
        result["status"] = "invalid_magic"
    elif title_id == "00000000":
        result["status"] = "empty_title_id"
    else:
        result["status"] = "ok"
    return result


def parse_stfschk_output(output: str, actual_size: int | None = None) -> dict[str, object]:
    """Parse stfschk's human-readable summary without trusting its exit code/footer."""
    result: dict[str, object] = {
        "complete": False,
        "valid": False,
        "signature_valid": None,
        "problems": [],
        "warnings": [],
    }
    problems = result["problems"]
    warnings = result["warnings"]
    assert isinstance(problems, list) and isinstance(warnings, list)

    if "Summary (invalid/total):" not in output:
        for pattern in (r"FileSystemParseException", r"DirectoryChain\.Length",
                        r"Unhandled exception", r"System\.IO\.(?:IOException|EndOfStreamException)",
                        r"\bIOException\b"):
            match = re.search(pattern, output, re.I)
            if match:
                problems.append(f"stfschk reported {match.group(0)}")
        problems.append("stfschk did not emit a complete summary")
        return result

    signature_match = re.search(r"^\s*Header signature:\s*(.+?)\s*$", output, re.M | re.I)
    metadata_match = re.search(r"^\s*Metadata hash:\s*(.+?)\s*$", output, re.M | re.I)
    if signature_match:
        signature_text = signature_match.group(1).strip()
        result["header_signature"] = signature_text
        if re.search(r"\binvalid\b", signature_text, re.I):
            result["signature_valid"] = False
            warnings.append("header signature is invalid, but content integrity is checked separately")
        elif re.search(r"\bvalid\b", signature_text, re.I):
            result["signature_valid"] = True
        else:
            warnings.append(f"header signature is advisory: {signature_text}")
    if metadata_match:
        metadata_text = metadata_match.group(1).strip()
        result["metadata_hash"] = metadata_text
        if not re.match(r"valid\b", metadata_text, re.I):
            problems.append(f"metadata hash is not valid: {metadata_text}")

    count_labels = {
        "hash_tables": "Hash tables",
        "data_blocks": "Data blocks",
        "directory_entries": "Directory entries",
        "missing_blocks": "Missing blocks",
    }
    count_fields_found = 0
    for key, label in count_labels.items():
        match = re.search(rf"^\s*{re.escape(label)}:\s*(\d+)\s*/\s*(\d+)(.*?)$",
                          output, re.M | re.I)
        if not match:
            continue
        count_fields_found += 1
        invalid = int(match.group(1))
        total = int(match.group(2))
        result[f"{key}_invalid"] = invalid
        result[f"{key}_total"] = total
        if invalid:
            problems.append(f"{label.lower()} invalid: {invalid}/{total}")

    size_match = re.search(r"^\s*Package size:\s*0x([0-9A-F]+)(.*?)$",
                           output, re.M | re.I)
    if size_match:
        reported_size = int(size_match.group(1), 16)
        result["reported_package_size"] = reported_size
        suffix = size_match.group(2)
        expected_match = re.search(r"expected\s+0x([0-9A-F]+)", suffix, re.I)
        expected_size = int(expected_match.group(1), 16) if expected_match else reported_size
        result["expected_package_size"] = expected_size
        measured_size = actual_size if actual_size is not None else reported_size
        result["actual_package_size"] = measured_size
        if reported_size != measured_size:
            problems.append(
                f"stfschk reported package size {reported_size}, but the file size is {measured_size}"
            )
        if measured_size < expected_size:
            problems.append(
                f"package is truncated: {measured_size} bytes, expected {expected_size}"
            )
        elif measured_size > expected_size:
            warnings.append(
                f"package has {measured_size - expected_size} trailing byte(s) beyond its STFS data"
            )

    exception_patterns = (
        r"FileSystemParseException", r"DirectoryChain\.Length", r"Unhandled exception",
        r"System\.IO\.(?:IOException|EndOfStreamException)", r"\bIOException\b",
    )
    for pattern in exception_patterns:
        match = re.search(pattern, output, re.I)
        if match:
            problems.append(f"stfschk reported {match.group(0)}")

    advisory_line_patterns = (
        r"^.*Metadata\.ContentSize.*$",
        r"^.*ContentMetadataVersion.*(?:expected|invalid).*$",
        r"^.*StfsVolumeDescriptor\..*(?:expected|invalid).*$",
        r"^.*(?:XContent)?Header\.SizeOfHeaders.*(?:expected|invalid).*$",
    )
    for pattern in advisory_line_patterns:
        for match in re.finditer(pattern, output, re.M | re.I):
            warning = match.group(0).strip()
            if warning and warning not in warnings:
                warnings.append(warning)

    complete = bool(signature_match and metadata_match and count_fields_found == 4 and size_match)
    result["complete"] = complete
    if not complete:
        problems.append("stfschk summary is incomplete")
    result["problems"] = list(dict.fromkeys(str(problem) for problem in problems))
    result["warnings"] = list(dict.fromkeys(str(warning) for warning in warnings))
    problems = result["problems"]
    assert isinstance(problems, list)
    result["valid"] = complete and not problems
    return result


def verify_stfs_integrity(package: Path, volume_type: int | None = None) -> dict[str, object]:
    """Deep-check one physical STFS package with bounded stfschk output retention."""
    if volume_type not in {None, 0}:
        result = {
            "complete": True,
            "valid": True,
            "signature_valid": None,
            "problems": [],
            "warnings": ["stfschk does not support the SVOD filesystem"],
            "skipped": "svod",
        }
        preparation_notice(
            "stfs_integrity_not_applicable_svod",
            f"Deep STFS hash checking is not available for SVOD package {package.name}; "
            "header and transfer hashes will still be checked.",
            package=str(package), volume_type=volume_type,
        )
        diagnostic_event("stfs_integrity_skipped", package=str(package), reason="svod",
                         volume_type=volume_type)
        return result
    if not STFSCHK.is_file():
        raise ApplicationError(
            f"Bundled STFS verifier is missing: {STFSCHK}",
            code="bundled_stfs_verifier_missing", details=path_metadata(STFSCHK),
        )

    size = package.stat().st_size
    gibibytes = (size + (1024 ** 3) - 1) // (1024 ** 3)
    timeout_seconds = min(6 * 60 * 60, max(15 * 60, 15 * 60 + gibibytes * 10 * 60))
    command = [str(STFSCHK), str(package)]
    diagnostic_event("external_tool_started", tool="stfschk", stage="stfs_integrity",
                     command=command, input=str(package), timeout_seconds=timeout_seconds)
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, errors="replace",
    )
    messages: queue.Queue[str] = queue.Queue()
    tail: deque[str] = deque(maxlen=500)
    signals: deque[str] = deque(maxlen=100)

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            messages.put(line.rstrip("\r\n"))

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    last_report = started
    try:
        while process.poll() is None or not messages.empty():
            check_cancelled()
            try:
                line = messages.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line:
                tail.append(line)
                if re.search(r"Summary \(|Header signature:|Metadata hash:|Hash tables:|"
                             r"Data blocks:|Directory entries:|Missing blocks:|Package size:|"
                             r"FileSystemParseException|DirectoryChain\.Length|Unhandled exception|"
                             r"System\.IO\.", line, re.I):
                    signals.append(line)
            now = time.monotonic()
            if now - last_report >= 30:
                print(f"    checking package integrity; elapsed {int(now-started)}s", flush=True)
                last_report = now
            if now - started > timeout_seconds:
                process.kill()
                raise ApplicationError(
                    f"STFS integrity checker timed out for {package.name}",
                    code="stfs_verifier_timeout",
                    details={"package": str(package), "size_bytes": size,
                             "timeout_seconds": timeout_seconds, "output_tail": list(tail)[-40:]},
                )
        process.wait()
        reader_thread.join(timeout=2)
        while not messages.empty():
            line = messages.get_nowait()
            tail.append(line)
        combined = "\n".join([*signals, *tail])
        if process.returncode:
            raise ApplicationError(
                f"STFS integrity checker crashed for {package.name} (exit {process.returncode})",
                code="stfs_verifier_failed",
                details={"package": str(package), "exit_code": process.returncode,
                         "output_tail": list(tail)[-40:]},
            )
        parsed = parse_stfschk_output(combined, actual_size=size)
        diagnostic_event(
            "external_tool_finished", tool="stfschk", stage="stfs_integrity",
            command=command, input=str(package), exit_code=process.returncode,
            duration_seconds=round(time.monotonic() - started, 3), parsed=parsed,
            output_tail=list(tail)[-40:],
        )
        if not parsed["complete"]:
            raise ApplicationError(
                f"STFS integrity checker returned an incomplete result for {package.name}",
                code="stfs_verifier_protocol_invalid",
                details={"package": str(package), "parsed": parsed,
                         "output_tail": list(tail)[-40:]},
            )
        if not parsed["valid"]:
            raise SourceError(
                f"Xbox package failed its internal integrity checks: {package.name}",
                code="stfs_integrity_failed",
                details={"package": str(package), "size_bytes": size, "integrity": parsed},
            )
        if parsed["signature_valid"] is False:
            preparation_notice(
                "stfs_signature_invalid_data_intact",
                f"{package.name} has a modified/unverifiable signature, but all internal data "
                "and filesystem hashes are intact.", package=str(package), integrity=parsed,
            )
        for warning in parsed["warnings"]:
            if parsed["signature_valid"] is False and "signature" in str(warning):
                continue
            preparation_notice("stfs_integrity_advisory", f"{package.name}: {warning}",
                               package=str(package), integrity=parsed)
        return parsed
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader_thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()


def validate_stfs_package(package: Path, *, verify_integrity: bool = True) -> tuple[str, str]:
    inspection = inspect_stfs(package)
    diagnostic_event("stfs_inspected", **inspection)
    status = inspection["status"]
    details = dict(inspection)
    if status == "too_small":
        raise SourceError(
            f"Xbox content package is too small to contain an STFS header: {package}",
            code="stfs_too_small", details=details,
        )
    if status == "invalid_magic":
        raise SourceError(
            f"Xbox content package has an invalid STFS signature: {package}",
            code="stfs_invalid_magic", details=details,
        )
    if status == "empty_title_id":
        raise SourceError(
            f"Xbox content package has an empty Title ID: {package}",
            code="stfs_empty_title_id", details=details,
        )
    if status != "ok":
        raise SourceError(
            f"Xbox content package is missing or unreadable: {package}",
            code="stfs_unreadable", details=details,
        )
    detected_title = str(inspection["detected_title_id"])
    detected_type = str(inspection["detected_content_type"])
    if detected_type not in SUPPORTED_CONTENT_TYPES:
        raise UnsupportedLayoutError(
            f"Unsupported Xbox content type {detected_type}: {package.name}",
            code="content_type_unsupported",
            details={**details, "supported_types": sorted(SUPPORTED_CONTENT_TYPES)},
        )
    if verify_integrity:
        verify_stfs_integrity(package, inspection.get("volume_type"))
    else:
        diagnostic_event("stfs_integrity_skipped", package=str(package), reason="user_option")
    return detected_title, detected_type


def stfs_info(path: Path) -> tuple[str, str] | None:
    """Read the STFS content type and Title ID from the package header."""
    inspection = inspect_stfs(path)
    if inspection["status"] in {"not_a_file", "too_small", "invalid_magic"}:
        return None
    if inspection["status"] == "empty_title_id":
        raise SourceError(
            f"STFS package has an empty Title ID: {path}",
            code="stfs_empty_title_id",
            details=inspection,
        )
    return (str(inspection["detected_title_id"]),
            str(inspection["detected_content_type"]))


def source_kind(path: Path) -> str | None:
    if path.is_file():
        if archive_primary(path):
            return "archive"
        if path.suffix.lower() == ".iso":
            return "disc image"
        if stfs_info(path):
            return "Xbox content package"
        return None
    if path.is_dir():
        if (path / "default.xex").is_file() or (path / "default.xbe").is_file():
            return "extracted game"
        if path.name.lower() == "content" or path.name == "0000000000000000":
            return "Xbox content tree"
        if any(path.rglob("default.xex")) or any(path.rglob("default.xbe")):
            return "extracted game collection"
        if any(path.rglob("*.iso")):
            return "disc image collection"
        if any(any(p.is_dir() for p in path.rglob(content_type))
               for content_type in SUPPORTED_CONTENT_TYPES):
            return "Xbox content tree"
    return None


def list_inputs(source: Path) -> list[tuple[Path, str]]:
    if not source.exists():
        raise ConfigurationError(f"Source does not exist: {source}",
                                 code="source_not_found", details=path_metadata(source))
    if source.is_file():
        kind = source_kind(source)
        if not kind:
            raise SourceError(f"Unsupported input: {source.name}",
                              code="unsupported_input", details=path_metadata(source))
        return [(source, kind)]
    # A collection root such as D:\Xbox360Staging contains other projects and
    # previously extracted files deeper down. Only treat it as one game when
    # the game/content marker is immediately at the selected root.
    if ((source / "default.xex").is_file() or (source / "default.xbe").is_file()
            or source.name.lower() == "content" or source.name == "0000000000000000"):
        return [(source, source_kind(source) or "Xbox content tree")]
    found: list[tuple[Path, str]] = []
    for child in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
        if child.name.casefold() in IGNORED_TOP_LEVEL or child.name.startswith("."):
            continue
        kind = source_kind(child)
        if kind:
            found.append((child, kind))
    unpacked = {input_key(path) for path, kind in found if kind != "archive"}
    duplicates = [path.name for path, kind in found if kind == "archive" and input_key(path) in unpacked]
    if duplicates:
        print("Packed duplicates hidden because a matching loose input exists: " + ", ".join(duplicates))
        found = [(path, kind) for path, kind in found
                 if kind != "archive" or input_key(path) not in unpacked]
    if not found:
        raise ConfigurationError(
            f"No supported game inputs found in {source}",
            code="no_supported_inputs",
            details={"source": str(source), "supported_archives": sorted(ARCHIVE_SUFFIXES)},
        )
    return found


def read_archive_inventory(archive: Path) -> tuple[list[str], int]:
    """List archive members and their uncompressed size without extracting them."""
    if not SEVEN_ZIP.is_file():
        raise ApplicationError(f"Bundled 7-Zip is missing: {SEVEN_ZIP}",
                               code="bundled_7zip_missing", details=path_metadata(SEVEN_ZIP))
    command = [str(SEVEN_ZIP), "l", "-slt", "--", str(archive)]
    started = time.monotonic()
    diagnostic_event("external_tool_started", tool="7-Zip", stage="archive_listing",
                     command=command, input=str(archive), timeout_seconds=300)
    result = subprocess.run(
        command,
        capture_output=True, text=True, errors="replace", timeout=300, check=False,
    )
    stdout_tail = result.stdout[-2000:]
    stderr_tail = result.stderr[-2000:]
    diagnostic_event("external_tool_finished", tool="7-Zip", stage="archive_listing",
                     command=command, input=str(archive), exit_code=result.returncode,
                     duration_seconds=round(time.monotonic() - started, 3),
                     stdout_tail=stdout_tail, stderr_tail=stderr_tail)
    if result.returncode:
        evidence = (result.stderr.strip() or result.stdout[-500:]).strip()
        details = {"archive": path_metadata(archive), "exit_code": result.returncode,
                   "stdout_tail": stdout_tail, "stderr_tail": stderr_tail}
        if re.search(r"crc failed|data error|unexpected end|headers error|is not archive|"
                     r"can(?:not| not) open.*archive",
                     result.stdout + result.stderr, re.I):
            raise SourceError(
                f"7-Zip reports that {archive.name} is damaged or incomplete: {evidence}",
                code="archive_corrupt_or_incomplete", details=details,
            )
        raise PrepError(
            f"7-Zip could not inspect {archive.name}: {evidence}",
            code="archiver_failed", details=details,
        )
    lines = result.stdout.splitlines()
    try:
        start = lines.index("----------") + 1
    except ValueError as exc:
        raise ApplicationError(
            f"7-Zip returned no parseable file listing for {archive.name}",
            code="archiver_protocol_invalid",
            details={"archive": path_metadata(archive), "stdout_tail": stdout_tail},
        ) from exc
    entries: list[dict[str, str]] = []
    entry: dict[str, str] = {}
    for line in (*lines[start:], ""):
        if not line:
            if "Path" in entry:
                entries.append(entry)
            entry = {}
            continue
        key, separator, value = line.partition(" = ")
        if separator and key in {"Path", "Size", "Folder", "Attributes"}:
            entry[key] = value
    paths = [entry["Path"] for entry in entries]
    if not paths:
        raise SourceError(f"Archive is empty: {archive.name}", code="archive_empty",
                          details={"archive": path_metadata(archive)})
    for raw in paths:
        normalized = raw.replace("\\", "/")
        parts = normalized.rstrip("/").split("/")
        if normalized.startswith("/") or any(p in {"", ".", ".."} for p in parts) or ":" in parts[0]:
            raise SourceError(f"Unsafe path inside {archive.name}: {raw}",
                              code="archive_unsafe_path",
                              details={"archive": str(archive), "unsafe_path": raw})
    total_bytes = 0
    for entry in entries:
        is_directory = entry.get("Folder") == "+" or "D" in entry.get("Attributes", "")
        if is_directory:
            continue
        size = entry.get("Size")
        if size is not None:
            try:
                total_bytes += int(size)
            except ValueError as exc:
                raise ApplicationError(
                    f"7-Zip returned an invalid file size for {entry['Path']} in {archive.name}",
                    code="archiver_protocol_invalid",
                    details={"archive": str(archive), "entry": entry},
                ) from exc
    return paths, total_bytes


def read_archive_paths(archive: Path) -> list[str]:
    return read_archive_inventory(archive)[0]


def tree_bytes(folder: Path) -> int:
    total = 0
    if folder.exists():
        for root, _, files in os.walk(folder):
            for filename in files:
                try:
                    total += (Path(root) / filename).stat().st_size
                except FileNotFoundError:
                    pass
    return total


def estimate_input_bytes(path: Path, kind: str) -> tuple[int, str]:
    """Estimate output size from metadata/listings without unpacking the input."""
    if kind == "archive":
        _paths, total = read_archive_inventory(path)
        return total, "7-Zip uncompressed member sizes"
    if kind == "disc image":
        sizes = read_iso_file_sizes(path)
        total = sum(
            size for name, size in sizes.items()
            if name.split("/", 1)[0].casefold() != "$systemupdate"
        )
        return total, "extract-xiso disc listing, excluding system updates"
    if kind == "Xbox content package":
        total = path.stat().st_size
        companion = path.with_name(path.name + ".data")
        if companion.is_dir():
            total += tree_bytes(companion)
        return total, "package and companion data sizes"
    if path.is_dir():
        return tree_bytes(path), "source folder file sizes"
    if path.is_file():
        return path.stat().st_size, "source file size"
    raise PrepError(f"Could not estimate output size for {path}",
                    code="size_estimate_unavailable", details=path_metadata(path))


def run_extract(command: list[str], output: Path, label: str, idle_timeout: int) -> None:
    """Run an extractor with heartbeat and fail if it stops producing data."""
    print(f"  {label} ...", flush=True)
    diagnostic_event("external_tool_started", tool=Path(command[0]).name,
                     stage="extraction", command=command, output=str(output),
                     label=label, timeout_seconds=idle_timeout)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", stdin=subprocess.DEVNULL)
    messages: queue.Queue[str] = queue.Queue()
    tail: list[str] = []

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            messages.put(line.rstrip())

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    started = last_progress = time.monotonic()
    previous_size = tree_bytes(output)
    last_report = 0.0
    try:
        while process.poll() is None:
            check_cancelled()
            time.sleep(1)
            while not messages.empty():
                line = messages.get_nowait()
                if line:
                    tail.append(line)
                    tail = tail[-12:]
                    if re.search(r"\b\d{1,3}%", line):
                        last_progress = time.monotonic()
            now = time.monotonic()
            if now - last_report >= 10:
                size = tree_bytes(output)
                if size > previous_size:
                    last_progress = now
                    previous_size = size
                print(f"    {human_size(size)} prepared; elapsed {int(now-started)}s", flush=True)
                diagnostic_event(
                    "external_tool_progress",
                    tool=Path(command[0]).name,
                    stage="extraction",
                    label=label,
                    output=str(output),
                    output_bytes=size,
                    elapsed_seconds=int(now - started),
                )
                last_report = now
            if now - last_progress > idle_timeout:
                process.kill()
                diagnostic_event("external_tool_stalled", tool=Path(command[0]).name,
                                 stage="extraction", command=command, output=str(output),
                                 label=label, timeout_seconds=idle_timeout,
                                 output_bytes=previous_size, output_tail=tail)
                raise PrepError(
                    f"{label} stalled for {idle_timeout}s with no output growth",
                    code="extraction_stalled",
                    details={"command": command, "output": str(output),
                             "output_bytes": previous_size, "output_tail": tail},
                )
        process.wait()
        reader_thread.join(timeout=2)
        while not messages.empty():
            line = messages.get_nowait()
            if line:
                tail.append(line)
                tail = tail[-12:]
        if process.returncode:
            diagnostic_event("external_tool_finished", tool=Path(command[0]).name,
                             stage="extraction", command=command, output=str(output),
                             label=label, exit_code=process.returncode,
                             duration_seconds=round(time.monotonic() - started, 3),
                             output_bytes=tree_bytes(output), output_tail=tail)
            failure_details = {"command": command, "exit_code": process.returncode,
                               "output": str(output), "output_bytes": tree_bytes(output),
                               "output_tail": tail}
            if Path(command[0]).name.casefold().startswith("7z") and re.search(
                    r"crc failed|data error|unexpected end|headers error|is not archive|"
                    r"can(?:not| not) open.*archive", "\n".join(tail), re.I):
                raise SourceError(
                    f"{label} found damaged or incomplete archive data "
                    f"(exit {process.returncode}): {' | '.join(tail[-5:])}",
                    code="archive_corrupt_or_incomplete", details=failure_details,
                )
            raise PrepError(
                f"{label} failed (exit {process.returncode}): {' | '.join(tail[-5:])}",
                code="extractor_failed",
                details=failure_details,
            )
        diagnostic_event("external_tool_finished", tool=Path(command[0]).name,
                         stage="extraction", command=command, output=str(output),
                         label=label, exit_code=process.returncode,
                         duration_seconds=round(time.monotonic() - started, 3),
                         output_bytes=tree_bytes(output), output_tail=tail)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader_thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()


def assert_no_links(folder: Path) -> None:
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            item = Path(root) / name
            if item.is_symlink() or not item.resolve().is_relative_to(folder.resolve()):
                raise SourceError(f"Archive contains a link or escaping path: {item}",
                                  code="archive_link_or_escape",
                                  details={"item": str(item), "root": str(folder)})


def archive_extract(archive: Path, output: Path, idle_timeout: int) -> None:
    read_archive_paths(archive)
    output.mkdir(parents=True)
    run_extract([str(SEVEN_ZIP), "x", str(archive), f"-o{output}", "-y", "-bsp1", "-bso0"],
                output, f"Unpacking {archive.name}", idle_timeout)
    assert_no_links(output)


def read_iso_file_sizes(image: Path) -> dict[str, int]:
    """Read an XISO directory and file sizes without extracting the disc."""
    if not XISO.is_file():
        raise ApplicationError(f"Bundled disc extractor is missing: {XISO}",
                               code="bundled_disc_extractor_missing",
                               details=path_metadata(XISO))
    list_command = [str(XISO), "-l", str(image)]
    started = time.monotonic()
    diagnostic_event("external_tool_started", tool="extract-xiso", stage="disc_listing",
                     command=list_command, input=str(image), timeout_seconds=300)
    listing = subprocess.run(list_command, capture_output=True,
                             text=True, errors="replace", timeout=300, check=False)
    diagnostic_event("external_tool_finished", tool="extract-xiso", stage="disc_listing",
                     command=list_command, input=str(image), exit_code=listing.returncode,
                     duration_seconds=round(time.monotonic() - started, 3),
                     stdout_tail=listing.stdout[-2000:], stderr_tail=listing.stderr[-2000:])
    if listing.returncode:
        raise SourceError(
            f"Disc image could not be read: {image.name}: {(listing.stdout + listing.stderr)[-500:]}",
            code="disc_image_unreadable",
            details={"image": path_metadata(image), "exit_code": listing.returncode,
                     "stdout_tail": listing.stdout[-2000:],
                     "stderr_tail": listing.stderr[-2000:]},
        )
    listing_text = listing.stdout + listing.stderr
    lowered = listing_text.lower()
    if "default.xex" not in lowered and "default.xbe" not in lowered:
        raise SourceError(f"Disc image has no default.xex or default.xbe: {image.name}",
                          code="disc_default_executable_missing",
                          details={"image": path_metadata(image),
                                   "listing_tail": listing_text[-2000:]})
    expected: dict[str, int] = {}
    for line in listing_text.splitlines():
        match = re.fullmatch(r"\\(.+?) \((\d+) bytes\)", line.strip())
        if not match:
            continue
        raw, size_text = match.groups()
        if raw.endswith("\\"):
            continue
        parts = raw.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise SourceError(f"Unsafe disc path in {image.name}: {raw}",
                              code="disc_unsafe_path",
                              details={"image": str(image), "unsafe_path": raw})
        key = "/".join(parts).casefold()
        if key in expected:
            raise SourceError(f"Duplicate case-insensitive disc path in {image.name}: {raw}",
                              code="disc_duplicate_path",
                              details={"image": str(image), "duplicate_path": raw})
        expected[key] = int(size_text)
    if not expected:
        raise SourceError(f"Disc listing has no extractable files: {image.name}",
                          code="disc_empty",
                          details={"image": path_metadata(image),
                                   "listing_tail": listing_text[-2000:]})
    return expected


def iso_extract(image: Path, output: Path, idle_timeout: int) -> None:
    expected = read_iso_file_sizes(image)
    output.mkdir(parents=True)
    # extract-xiso 2.7.1 can still emit $SystemUpdate while using -s. Extract the
    # complete image, verify the complete listing, then omit updates in game_items.
    run_extract([str(XISO), "-x", "-d", str(output), str(image)], output,
                f"Extracting {image.name}", idle_timeout)
    if not (output / "default.xex").is_file() and not (output / "default.xbe").is_file():
        raise PrepError(
            f"Disc extraction finished without a root default.xex/xbe: {image.name}",
            code="extraction_output_default_missing",
            details={"image": str(image), "output": str(output),
                     "output_bytes": tree_bytes(output)},
        )
    actual: dict[str, int] = {}
    for file in output.rglob("*"):
        if not file.is_file():
            continue
        if file.is_symlink():
            raise PrepError(f"Disc extraction produced a link: {file}",
                            code="extraction_output_link",
                            details={"image": str(image), "file": str(file)})
        key = file.relative_to(output).as_posix().casefold()
        if key in actual:
            raise PrepError(
                f"Disc extraction produced duplicate case-insensitive paths: {file}",
                code="extraction_output_duplicate_path",
                details={"image": str(image), "file": str(file)},
            )
        actual[key] = file.stat().st_size
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())[:5]
        extra = sorted(actual.keys() - expected.keys())[:5]
        wrong_size = sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])[:5]
        raise PrepError(
            f"Incomplete disc extraction: missing={missing}, extra={extra}, wrong_size={wrong_size}",
            code="extraction_output_mismatch",
            details={"image": str(image), "expected_file_count": len(expected),
                     "actual_file_count": len(actual), "missing_count": len(expected.keys() - actual.keys()),
                     "extra_count": len(actual.keys() - expected.keys()),
                     "wrong_size_count": sum(expected[k] != actual[k]
                                             for k in expected.keys() & actual.keys()),
                     "missing_sample": missing, "extra_sample": extra,
                     "wrong_size_sample": wrong_size},
        )


def require_stfs_match(package: Path, expected_title_id: str,
                       expected_content_type: str, *,
                       verify_integrity: bool = True) -> tuple[str, str]:
    """Strict compatibility helper; Content-tree routing no longer relies on wrappers."""
    detected_title, detected_type = validate_stfs_package(
        package, verify_integrity=verify_integrity
    )
    details = {
        "expected_title_id": expected_title_id.upper(),
        "expected_content_type": expected_content_type.upper(),
        **inspect_stfs(package),
    }
    if detected_title != expected_title_id.upper():
        raise SourceError(
            f"Xbox content package Title ID mismatch: expected {expected_title_id.upper()}, "
            f"detected {detected_title}: {package}",
            code="stfs_title_id_mismatch", details=details,
        )
    if detected_type != expected_content_type.upper():
        raise SourceError(
            f"Xbox content package type mismatch: expected {expected_content_type.upper()}, "
            f"detected {detected_type}: {package}",
            code="stfs_content_type_mismatch", details=details,
        )
    return detected_title, detected_type


def content_items(folder: Path, idle_timeout: int = 600, *,
                  verify_integrity: bool = True) -> ContentPlan:
    """Map packages by their internal metadata; wrapper folders are advisory only."""
    del idle_timeout  # Integrity checks use a size-scaled absolute timeout.
    anonymous_dirs = [p for p in folder.rglob("0000000000000000") if p.is_dir()]
    if folder.name == "0000000000000000":
        anonymous_dirs.insert(0, folder)
    anonymous_dirs = list(dict.fromkeys(anonymous_dirs))
    items: list[CopyItem] = []
    seen: set[Path] = set()
    wrapper_pairs: list[tuple[str, str]] = []
    for anonymous in anonymous_dirs:
        for title in anonymous.iterdir():
            if not title.is_dir():
                continue
            if not HEX8.fullmatch(title.name):
                raise SourceError(f"Unrecognized Xbox Content Title ID folder: {title}",
                                  code="content_title_folder_invalid",
                                  details={"folder": str(title)})
            for content_type in title.iterdir():
                if not content_type.is_dir():
                    continue
                if not HEX8.fullmatch(content_type.name):
                    raise SourceError(f"Unrecognized Xbox Content type folder: {content_type}",
                                      code="content_type_folder_invalid",
                                      details={"folder": str(content_type)})
                packages = {p.name: p for p in content_type.iterdir() if p.is_file()}
                if not packages:
                    raise SourceError(f"Content folder has no package file: {content_type}",
                                      code="content_package_missing",
                                      details={"folder": str(content_type)})
                wrapper_title = title.name.upper()
                wrapper_type = content_type.name.upper()
                wrapper_pairs.append((wrapper_title, wrapper_type))
                allowed_companions = {name + ".data" for name in packages}
                for child in content_type.iterdir():
                    if child.is_dir() and child.name not in allowed_companions:
                        raise SourceError(
                            f"Unrecognized extra folder in Content tree: {child}",
                            code="content_extra_file",
                            details={"folder": str(child), "content_folder": str(content_type)},
                        )
                for package in packages.values():
                    title_id, detected_type = validate_stfs_package(
                        package, verify_integrity=verify_integrity
                    )
                    base = (Path("Content") / "0000000000000000" /
                            title_id / detected_type)
                    bundle_destination = base / package.name
                    if (title_id, detected_type) != (wrapper_title, wrapper_type):
                        preparation_notice(
                            "content_wrapper_normalized",
                            f"Placed {package.name} using its internal Title ID {title_id} and "
                            f"type {detected_type}, instead of wrapper {wrapper_title}/{wrapper_type}.",
                            package=str(package), wrapper_title_id=wrapper_title,
                            wrapper_content_type=wrapper_type, detected_title_id=title_id,
                            detected_content_type=detected_type,
                        )
                    if detected_type == "00080000":
                        preparation_notice(
                            "demo_content_supported",
                            f"Recognized {package.name} as Xbox demo/trial content (00080000).",
                            package=str(package), detected_title_id=title_id,
                        )
                    if package not in seen:
                        seen.add(package)
                        items.append(CopyItem(
                            package, bundle_destination,
                            bundle_source=package, bundle_destination=bundle_destination,
                        ))
                    companion = content_type / (package.name + ".data")
                    if companion.is_dir():
                        for file in companion.rglob("*"):
                            if not file.is_file():
                                continue
                            if file in seen:
                                continue
                            seen.add(file)
                            relative = file.relative_to(companion)
                            items.append(CopyItem(
                                file, base / companion.name / relative,
                                bundle_source=package, bundle_destination=bundle_destination,
                            ))
    installer_only = bool(items) and bool(wrapper_pairs) and all(
        title == "FFED2000" and content_type == "FFFFFFFF"
        for title, content_type in wrapper_pairs
    )
    if installer_only:
        preparation_notice(
            "installer_only_content_disc",
            "This uses the standard Xbox installer-only placeholder layout; its launcher is "
            "not copied as a playable game.", folder=str(folder),
        )
    return ContentPlan(items, found_content_root=bool(anonymous_dirs),
                       installer_only=installer_only)


def standalone_content_items(package: Path, idle_timeout: int = 600, *,
                             verify_integrity: bool = True) -> list[CopyItem]:
    del idle_timeout
    title_id, content_type = validate_stfs_package(
        package, verify_integrity=verify_integrity
    )
    base = Path("Content") / "0000000000000000" / title_id / content_type
    bundle_destination = base / package.name
    items = [CopyItem(package, bundle_destination, package, bundle_destination)]
    companion = package.with_name(package.name + ".data")
    if companion.is_dir():
        items.extend(CopyItem(file, base / companion.name / file.relative_to(companion),
                              package, bundle_destination)
                     for file in companion.rglob("*") if file.is_file())
    if content_type == "00080000":
        preparation_notice(
            "demo_content_supported",
            f"Recognized {package.name} as Xbox demo/trial content (00080000).",
            package=str(package), detected_title_id=title_id,
        )
    return items


def game_items(game: Path, display_name: str, *, exclude_content: bool) -> list[CopyItem]:
    if (game / "default.xex").is_file():
        category = "Xbox 360"
    elif (game / "default.xbe").is_file():
        category = "Xbox Original"
    else:
        raise PrepError(f"Extracted folder has no root default.xex/xbe: {game}",
                        code="prepared_game_executable_missing",
                        details={"game_folder": str(game)})
    base = Path("Games") / category / safe_game_name(display_name)
    items: list[CopyItem] = []
    for file in game.rglob("*"):
        if not file.is_file():
            continue
        relative = file.relative_to(game)
        if relative.parts[0].casefold() == "$systemupdate":
            continue
        if exclude_content and relative.parts[0].casefold() == "content":
            continue
        items.append(CopyItem(file, base / relative))
    return items


def prepared_items(path: Path, display_name: str, work: Path, idle_timeout: int, *,
                   verify_integrity: bool = True) -> list[CopyItem]:
    """Convert one selected raw input into copy items without further prompts."""
    candidate = path
    for depth in range(3):
        if candidate.is_file() and archive_primary(candidate):
            extracted_archive = work / f"archive-{depth}"
            archive_extract(candidate, extracted_archive, idle_timeout)
            candidate = extracted_archive
            continue
        if candidate.is_dir():
            nested = [p for p in candidate.rglob("*") if p.is_file() and archive_primary(p)]
            if nested:
                loose = [p for p in candidate.rglob("*") if p.is_file() and
                         (p.suffix.lower() == ".iso" or p.name.lower() in {"default.xex", "default.xbe"}
                          or stfs_info(p))]
                if len(nested) == 1 and not loose:
                    candidate = nested[0]
                    continue
                raise SourceError(
                    "Mixed or multiple nested archives need to be selected separately: " +
                    ", ".join(p.name for p in nested[:5]),
                    code="nested_archive_ambiguous",
                    details={"nested_archive_count": len(nested),
                             "sample": [str(p) for p in nested[:5]]},
                )
        break
    if candidate.is_file() and archive_primary(candidate):
        raise SourceError("Archive nesting exceeds the supported three levels",
                          code="archive_nesting_too_deep",
                          details={"candidate": str(candidate), "maximum_levels": 3})
    if candidate.is_file() and candidate.suffix.lower() == ".iso":
        images = [candidate]
    elif candidate.is_dir():
        images = sorted(p for p in candidate.rglob("*.iso") if p.is_file())
    else:
        images = []
    if images:
        if candidate.is_dir():
            mixed = [p for p in candidate.rglob("*") if p.is_file() and
                     p not in images and (p.name.lower() in {"default.xex", "default.xbe"} or stfs_info(p))]
            if mixed:
                raise SourceError(
                    "Input contains disc images and separate Xbox game/content files; "
                    "select them separately so none are skipped",
                    code="mixed_input_layout",
                    details={"disc_images": [str(p) for p in images[:10]],
                             "separate_xbox_files": [str(p) for p in mixed[:10]]},
                )
        items: list[CopyItem] = []
        for index, image in enumerate(images, 1):
            extracted = work / f"disc-{index}"
            iso_extract(image, extracted, idle_timeout)
            content = content_items(extracted, idle_timeout,
                                    verify_integrity=verify_integrity)
            if content.found_content_root and not content:
                raise SourceError(f"Disc has unrecognized install content: {image.name}",
                                  code="disc_content_tree_unrecognized",
                                  details={"image": str(image)})
            is_forza_install_disc = bool(re.search(r"forza\s*(?:motorsport\s*)?3", display_name, re.I)
                                         and re.search(r"(?:dvd|disc)\s*2", display_name, re.I))
            if is_forza_install_disc and not content:
                raise SourceError(
                    "Forza 3 Disc 2 has no install content; refusing to copy it as a play disc",
                    code="forza_install_content_missing",
                    details={"image": str(image)},
                )
            items.extend(content.items)
            if not is_forza_install_disc and not content.installer_only:
                name = display_name if len(images) == 1 else f"{display_name} Disc {index}"
                items.extend(game_items(extracted, name, exclude_content=bool(content)))
        return items
    if candidate.is_file():
        return standalone_content_items(candidate, idle_timeout,
                                        verify_integrity=verify_integrity)
    if not candidate.is_dir():
        raise PrepError(f"Could not prepare {path}", code="prepared_output_missing",
                        details={"input": path_metadata(path),
                                 "candidate": path_metadata(candidate)})
    games = sorted((p for p in candidate.rglob("default.xex") if p.is_file()), key=lambda p: len(p.parts))
    games += sorted((p for p in candidate.rglob("default.xbe") if p.is_file()), key=lambda p: len(p.parts))
    game_roots = {p.parent for p in games}
    content = content_items(candidate, idle_timeout,
                            verify_integrity=verify_integrity)
    if content.found_content_root and not content:
        raise SourceError(f"Input has an unrecognized Xbox Content tree: {path.name}",
                          code="content_tree_unrecognized",
                          details={"input": str(path), "candidate": str(candidate)})
    if game_roots:
        items = list(content.items)
        if not content.installer_only:
            for root in sorted(game_roots):
                name = display_name if len(game_roots) == 1 else root.name
                items.extend(game_items(root, name, exclude_content=bool(content)))
        return items
    if content:
        return content.items
    packages = []
    for file in candidate.rglob("*"):
        if file.is_file() and stfs_info(file):
            packages.append(file)
    if packages:
        return [item for package in packages
                for item in standalone_content_items(
                    package, idle_timeout, verify_integrity=verify_integrity
                )]
    raise SourceError(f"No Xbox disc, extracted game, or content package found in {path.name}",
                      code="xbox_content_not_found",
                      details={"input": str(path), "candidate": str(candidate)})


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def deduplicate_plan(items: list[CopyItem]) -> list[CopyItem]:
    """Collapse byte-identical canonical collisions and reject ambiguous ones."""
    if not items:
        return items
    hashes: dict[Path, str] = {}

    def digest(path: Path) -> str:
        if path not in hashes:
            hashes[path] = file_sha256(path)
        return hashes[path]

    bundle_groups: dict[str, dict[str, list[tuple[int, CopyItem]]]] = {}
    for index, item in enumerate(items):
        if item.bundle_source is None or item.bundle_destination is None:
            continue
        destination_key = str(item.bundle_destination).casefold()
        source_key = str(item.bundle_source.resolve()).casefold()
        bundle_groups.setdefault(destination_key, {}).setdefault(source_key, []).append(
            (index, item)
        )

    dropped: set[int] = set()

    def bundle_manifest(candidate: list[tuple[int, CopyItem]]) -> tuple[dict[str, tuple[int, str]], str]:
        manifest: dict[str, tuple[int, str]] = {}
        fingerprint = hashlib.sha256()
        for _, item in sorted(candidate, key=lambda pair: str(pair[1].relative_destination).casefold()):
            assert item.bundle_destination is not None
            relative = item.relative_destination.relative_to(
                item.bundle_destination.parent
            ).as_posix().casefold()
            value = (item.source.stat().st_size, digest(item.source))
            if relative in manifest:
                raise SourceError(
                    f"One Xbox content bundle maps two files to {relative}",
                    code="duplicate_content_conflict",
                    details={"bundle_source": str(item.bundle_source),
                             "relative_path": relative},
                )
            manifest[relative] = value
            fingerprint.update(relative.encode("utf-8"))
            fingerprint.update(b"\0")
            fingerprint.update(str(value[0]).encode("ascii"))
            fingerprint.update(b"\0")
            fingerprint.update(value[1].encode("ascii"))
            fingerprint.update(b"\n")
        return manifest, fingerprint.hexdigest()

    for destination_key, candidates_by_source in bundle_groups.items():
        candidates = list(candidates_by_source.values())
        if len(candidates) < 2:
            continue
        first = candidates[0]
        first_manifest, first_fingerprint = bundle_manifest(first)
        first_source = first[0][1].bundle_source
        for duplicate in candidates[1:]:
            duplicate_manifest, duplicate_fingerprint = bundle_manifest(duplicate)
            duplicate_source = duplicate[0][1].bundle_source
            if duplicate_manifest != first_manifest:
                def sample(manifest: dict[str, tuple[int, str]]) -> list[dict[str, object]]:
                    return [
                        {"relative_path": path, "size_bytes": value[0], "sha256": value[1]}
                        for path, value in list(sorted(manifest.items()))[:20]
                    ]

                raise SourceError(
                    "Different Xbox content bundles map to the same canonical destination: "
                    f"{destination_key}",
                    code="duplicate_content_conflict",
                    details={
                        "destination": destination_key,
                        "first_source": str(first_source),
                        "second_source": str(duplicate_source),
                        "first_bundle_sha256": first_fingerprint,
                        "second_bundle_sha256": duplicate_fingerprint,
                        "first_file_count": len(first_manifest),
                        "second_file_count": len(duplicate_manifest),
                        "first_manifest_sample": sample(first_manifest),
                        "second_manifest_sample": sample(duplicate_manifest),
                    },
                )
            dropped.update(index for index, _ in duplicate)
            preparation_notice(
                "duplicate_content_deduplicated",
                f"Identical duplicate Xbox content was found and will be copied once: "
                f"{duplicate_source}",
                canonical_destination=destination_key, kept_source=str(first_source),
                duplicate_source=str(duplicate_source), bundle_sha256=first_fingerprint,
            )

    unbundled: dict[str, list[tuple[int, CopyItem]]] = {}
    for index, item in enumerate(items):
        if index in dropped or item.bundle_source is not None:
            continue
        unbundled.setdefault(str(item.relative_destination).casefold(), []).append((index, item))
    for destination_key, collisions in unbundled.items():
        if len(collisions) < 2:
            continue
        first_index, first_item = collisions[0]
        first_value = (first_item.source.stat().st_size, digest(first_item.source))
        for duplicate_index, duplicate_item in collisions[1:]:
            duplicate_value = (duplicate_item.source.stat().st_size,
                               digest(duplicate_item.source))
            if duplicate_value != first_value:
                raise SourceError(
                    f"Different files map to the same output: {first_item.relative_destination}",
                    code="duplicate_output_conflict",
                    details={
                        "destination": destination_key,
                        "first_source": str(first_item.source),
                        "second_source": str(duplicate_item.source),
                        "first_size_bytes": first_value[0],
                        "second_size_bytes": duplicate_value[0],
                        "first_sha256": first_value[1],
                        "second_sha256": duplicate_value[1],
                    },
                )
            dropped.add(duplicate_index)
            preparation_notice(
                "duplicate_file_deduplicated",
                f"Identical duplicate output was found and will be copied once: "
                f"{first_item.relative_destination}",
                kept_source=str(first_item.source), duplicate_source=str(duplicate_item.source),
                sha256=first_value[1],
            )
        del first_index
    return [item for index, item in enumerate(items) if index not in dropped]


def validate_plan(items: list[CopyItem], destination: Path) -> None:
    if not items:
        raise PrepError("No files were prepared", code="empty_preparation_plan")
    paths: dict[str, Path] = {}
    total = 0
    for item in items:
        if not item.source.is_file():
            raise PrepError(f"Prepared source disappeared: {item.source}",
                            code="prepared_source_disappeared",
                            details=path_metadata(item.source))
        size = item.source.stat().st_size
        if size > FAT32_MAX_FILE and drive_format(destination) == "FAT32":
            raise PrepError(
                f"FAT32 cannot hold {human_size(size)} file: {item.relative_destination}",
                code="destination_file_too_large_for_fat32",
                details={"file": str(item.relative_destination), "size_bytes": size,
                         "fat32_max_file_bytes": FAT32_MAX_FILE,
                         "destination": str(destination)},
            )
        key = str(item.relative_destination).casefold()
        if key in paths:
            raise SourceError(
                f"Two prepared files map to the same output: {item.relative_destination}",
                code="output_path_collision",
                details={"destination": str(item.relative_destination),
                         "first_source": str(paths[key]), "second_source": str(item.source)},
            )
        paths[key] = item.source
        total += size
    free = shutil.disk_usage(destination).free
    if total > free:
        raise PrepError(
            f"Not enough free space on {destination}: need {human_size(total)}, have {human_size(free)}",
            code="destination_no_space",
            details={"destination": str(destination), "required_bytes": total,
                     "free_bytes": free},
        )


def drive_format(path: Path) -> str:
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes
    root = Path(path.anchor or path).anchor
    buffer = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        wintypes.LPCWSTR(root), None, 0, None, None, None, buffer, len(buffer)
    )
    return buffer.value.upper() if ok else ""


def worker_copy(source: Path, destination: Path) -> int:
    """Isolated worker: a stalled filesystem call can be terminated by the parent."""
    expected_size = source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256()
    destination_hash = hashlib.sha256()
    count = 0
    last_report = time.monotonic()
    if destination.exists():
        if destination.stat().st_size != expected_size:
            raise PrepError(
                f"Existing destination has a different size: {destination}",
                code="destination_conflict_size",
                details={"source": str(source), "destination": str(destination),
                         "source_size_bytes": expected_size,
                         "destination_size_bytes": destination.stat().st_size},
            )
        with source.open("rb") as stream:
            while block := stream.read(8 * 1024 * 1024):
                source_hash.update(block)
                count += len(block)
                if time.monotonic() - last_report >= 2:
                    print(json.dumps({"phase": "hashing existing", "bytes": count}), flush=True)
                    last_report = time.monotonic()
        if count != expected_size:
            raise PrepError(f"Source length changed during transfer: {source}",
                            code="source_changed_during_transfer",
                            details={"source": str(source), "expected_bytes": expected_size,
                                     "read_bytes": count})
        count = 0
        with destination.open("rb") as stream:
            while block := stream.read(8 * 1024 * 1024):
                destination_hash.update(block)
                count += len(block)
                if time.monotonic() - last_report >= 2:
                    print(json.dumps({"phase": "verifying existing", "bytes": count}), flush=True)
                    last_report = time.monotonic()
        if count != expected_size or source_hash.digest() != destination_hash.digest():
            raise PrepError(
                f"Existing destination differs; refusing to overwrite: {destination}",
                code="destination_conflict_hash",
                details={"source": str(source), "destination": str(destination),
                         "size_bytes": expected_size, "source_sha256": source_hash.hexdigest(),
                         "destination_sha256": destination_hash.hexdigest()},
            )
        print(json.dumps({"phase": "done", "result": "already verified", "bytes": count,
                          "sha256": source_hash.hexdigest()}), flush=True)
        return 0
    for stale in destination.parent.glob(destination.name + ".xboxhddprep-part-*"):
        stale.unlink()
    temp = destination.with_name(destination.name + f".xboxhddprep-part-{uuid.uuid4().hex}")
    try:
        with source.open("rb") as incoming, temp.open("xb") as outgoing:
            while block := incoming.read(8 * 1024 * 1024):
                outgoing.write(block)
                source_hash.update(block)
                count += len(block)
                if time.monotonic() - last_report >= 2:
                    print(json.dumps({"phase": "copying", "bytes": count}), flush=True)
                    last_report = time.monotonic()
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if count != expected_size or temp.stat().st_size != expected_size:
            raise PrepError(f"Incomplete copy: {source}", code="copy_incomplete",
                            details={"source": str(source), "temporary_destination": str(temp),
                                     "expected_bytes": expected_size, "copied_bytes": count,
                                     "temporary_size_bytes": temp.stat().st_size})
        count = 0
        with temp.open("rb") as stream:
            while block := stream.read(8 * 1024 * 1024):
                destination_hash.update(block)
                count += len(block)
                if time.monotonic() - last_report >= 2:
                    print(json.dumps({"phase": "verifying", "bytes": count}), flush=True)
                    last_report = time.monotonic()
        if count != expected_size or source_hash.digest() != destination_hash.digest():
            raise PrepError(
                f"SHA-256 mismatch after copying: {destination}",
                code="copy_hash_mismatch",
                details={"source": str(source), "temporary_destination": str(temp),
                         "size_bytes": expected_size, "source_sha256": source_hash.hexdigest(),
                         "destination_sha256": destination_hash.hexdigest()},
            )
        if destination.exists():
            raise PrepError(f"Destination appeared during copy: {destination}",
                            code="destination_race",
                            details={"source": str(source), "destination": str(destination)})
        temp.rename(destination)
        if destination.stat().st_size != expected_size:
            raise PrepError(
                f"Final file size mismatch: {destination}",
                code="destination_final_size_mismatch",
                details={"destination": str(destination), "expected_bytes": expected_size,
                         "actual_bytes": destination.stat().st_size},
            )
        print(json.dumps({"phase": "done", "result": "copied and verified", "bytes": count,
                          "sha256": source_hash.hexdigest()}), flush=True)
        return 0
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def worker_copy_batch(manifest_path: Path) -> int:
    """Write every file in one game, verify the batch, then publish new files."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    destination_root = Path(manifest["destination"]).resolve()
    batch_id = str(manifest["batch_id"])
    raw_items = manifest["items"]
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id) or not isinstance(raw_items, list) or not raw_items:
        raise ConfigurationError("Invalid copy batch manifest", code="copy_batch_manifest_invalid")

    plan: list[dict[str, object]] = []
    for raw in raw_items:
        source = Path(raw["source"])
        relative = Path(raw["relative_destination"])
        size = int(raw["size_bytes"])
        target = destination_root / relative
        if (relative.is_absolute() or ".." in relative.parts
                or not target.resolve().is_relative_to(destination_root)):
            raise ConfigurationError(
                f"Unsafe destination in copy batch: {relative}",
                code="copy_batch_destination_unsafe",
            )
        if size < 0 or not source.is_file() or source.stat().st_size != size:
            raise PrepError(
                f"Prepared source changed before copying: {source}",
                code="prepared_source_disappeared",
                details=path_metadata(source),
            )
        plan.append({
            "source": source,
            "relative": relative,
            "target": target,
            "temp": target.with_name(target.name + f".xboxhddprep-part-{batch_id}"),
            "size": size,
            "weight": max(1, size),
        })

    file_count = len(plan)
    total_weight = sum(int(item["weight"]) for item in plan)
    created: list[Path] = []
    staged: list[dict[str, object]] = []
    active_target: Path | None = None

    def emit(phase: str, number: int = 0, count: int = 0,
             progress: float = 0.0, **extra: object) -> None:
        item = plan[number - 1] if number else None
        payload = {
            "phase": phase,
            "file_number": number,
            "file_count": file_count,
            "relative_destination": str(item["relative"]) if item else "",
            "size_bytes": int(item["size"]) if item else 0,
            "bytes": count,
            "progress": round(min(1.0, max(0.0, progress)), 6),
            **extra,
        }
        print(json.dumps(payload), flush=True)

    try:
        emit("copy_phase_started")
        copied_weight = 0
        for number, item in enumerate(plan, 1):
            source = item["source"]
            target = item["target"]
            temp = item["temp"]
            size = int(item["size"])
            weight = int(item["weight"])
            assert isinstance(source, Path) and isinstance(target, Path) and isinstance(temp, Path)
            active_target = target
            if target.exists():
                destination_size = target.stat().st_size
                if destination_size != size:
                    raise PrepError(
                        f"Existing destination has a different size: {target}",
                        code="destination_conflict_size",
                        details={"source": str(source), "destination": str(target),
                                 "source_size_bytes": size,
                                 "destination_size_bytes": destination_size},
                    )
                staged.append({**item, "existing": True, "source_sha256": ""})
                copied_weight += weight
                emit("copying", number, size, 0.5 * copied_weight / total_weight, existing=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source_hash = hashlib.sha256()
            copied = 0
            last_report = time.monotonic()
            with source.open("rb") as incoming, temp.open("xb") as outgoing:
                created.append(temp)
                emit("copying", number, 0, 0.5 * copied_weight / total_weight)
                while block := incoming.read(8 * 1024 * 1024):
                    outgoing.write(block)
                    source_hash.update(block)
                    copied += len(block)
                    now = time.monotonic()
                    if now - last_report >= 1:
                        fraction = min(1.0, copied / max(1, size))
                        emit("copying", number, copied,
                             0.5 * (copied_weight + weight * fraction) / total_weight)
                        last_report = now
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if copied != size or temp.stat().st_size != size:
                raise PrepError(
                    f"Incomplete copy: {item['relative']}",
                    code="copy_incomplete",
                    details={"source": str(source), "destination": str(target),
                             "expected_bytes": size, "copied_bytes": copied},
                )
            staged.append({**item, "existing": False,
                           "source_sha256": source_hash.hexdigest()})
            copied_weight += weight
            emit("copying", number, size, 0.5 * copied_weight / total_weight)

        emit("verification_phase_started", progress=0.5)
        verified_weight = 0
        for number, item in enumerate(staged, 1):
            source = item["source"]
            target = item["target"]
            temp = item["temp"]
            size = int(item["size"])
            weight = int(item["weight"])
            existing = bool(item["existing"])
            assert isinstance(source, Path) and isinstance(target, Path) and isinstance(temp, Path)
            active_target = target

            def hash_stream(path: Path, phase: str) -> str:
                digest = hashlib.sha256()
                counted = 0
                last_report = time.monotonic()
                emit(phase, number, 0, 0.5 + 0.45 * verified_weight / total_weight)
                with path.open("rb") as stream:
                    while block := stream.read(8 * 1024 * 1024):
                        digest.update(block)
                        counted += len(block)
                        now = time.monotonic()
                        if now - last_report >= 1:
                            fraction = min(1.0, counted / max(1, size))
                            emit(phase, number, counted,
                                 0.5 + 0.45 * (verified_weight + weight * fraction) / total_weight)
                            last_report = now
                if counted != size:
                    raise PrepError(
                        f"File size changed during verification: {path}",
                        code="copy_size_mismatch",
                        details={"path": str(path), "expected_bytes": size,
                                 "actual_bytes": counted},
                    )
                return digest.hexdigest()

            expected_hash = hash_stream(source, "hashing_existing") if existing else str(item["source_sha256"])
            actual_hash = hash_stream(target if existing else temp, "verifying")
            if expected_hash != actual_hash:
                raise PrepError(
                    (f"Existing destination differs; refusing to overwrite: {target}"
                     if existing else f"SHA-256 mismatch after copying: {target}"),
                    code="destination_conflict_hash" if existing else "copy_hash_mismatch",
                    details={"source": str(source), "destination": str(target),
                             "size_bytes": size, "source_sha256": expected_hash,
                             "destination_sha256": actual_hash},
                )
            verified_weight += weight
            emit("verifying", number, size,
                 0.5 + 0.45 * verified_weight / total_weight, existing=existing,
                 source_sha256=expected_hash, destination_sha256=actual_hash)

        emit("commit_phase_started", progress=0.95)
        copied_files = 0
        verified_existing_files = 0
        for number, item in enumerate(staged, 1):
            target = item["target"]
            temp = item["temp"]
            size = int(item["size"])
            assert isinstance(target, Path) and isinstance(temp, Path)
            active_target = target
            if item["existing"]:
                verified_existing_files += 1
            else:
                if target.exists():
                    raise PrepError(
                        f"Destination appeared during transfer: {target}",
                        code="destination_race",
                        details={"destination": str(target)},
                    )
                temp.rename(target)
                if target.stat().st_size != size:
                    raise PrepError(
                        f"Final file size mismatch: {target}",
                        code="destination_final_size_mismatch",
                        details={"destination": str(target), "expected_bytes": size},
                    )
                copied_files += 1
            emit("committing", number, size, 0.95 + 0.05 * number / file_count)
        emit("done", progress=1.0, copied_files=copied_files,
             verified_existing_files=verified_existing_files)
        return 0
    except OSError as exc:
        if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
            try:
                free_bytes = shutil.disk_usage(destination_root).free
            except OSError:
                free_bytes = None
            raise PrepError(
                f"Destination ran out of space while copying {active_target}",
                code="destination_no_space",
                details={"destination": str(active_target),
                         "free_bytes": free_bytes},
            ) from exc
        raise
    finally:
        for temp in created:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def self_command() -> list[str]:
    return [str(sys.executable)] if getattr(sys, "frozen", False) else [str(sys.executable), str(Path(__file__).resolve())]


def copy_monitored(item: CopyItem, destination_root: Path, idle_timeout: int, number: int, total: int) -> CopyOutcome:
    target = destination_root / item.relative_destination
    size = item.source.stat().st_size
    print(f"  [{number}/{total}] {item.relative_destination} ({human_size(size)})", flush=True)
    command = self_command() + ["--copy-worker", str(item.source), str(target)]
    started = time.monotonic()
    diagnostic_event("copy_worker_started", source=str(item.source), destination=str(target),
                     relative_destination=str(item.relative_destination), size_bytes=size,
                     file_number=number, file_count=total, command=command,
                     idle_timeout_seconds=idle_timeout)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", stdin=subprocess.DEVNULL)
    messages: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            messages.put(line.rstrip())

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    last_progress = last_report = time.monotonic()
    latest = "starting"
    result = ""
    digest = ""
    tail: list[str] = []
    worker_failure: dict[str, object] | None = None
    try:
        while process.poll() is None or not messages.empty():
            check_cancelled()
            try:
                line = messages.get(timeout=1)
            except queue.Empty:
                line = ""
            if line:
                try:
                    event = json.loads(line)
                    last_progress = time.monotonic()
                    diagnostic_event(
                        "copy_worker_progress",
                        source=str(item.source),
                        destination=str(target),
                        relative_destination=str(item.relative_destination),
                        size_bytes=size,
                        file_number=number,
                        file_count=total,
                        phase=event.get("phase"),
                        bytes=int(event.get("bytes", 0) or 0),
                    )
                    if event.get("phase") == "done":
                        result = event.get("result", "verified")
                        digest = event.get("sha256", "")
                    elif event.get("phase") == "error" and isinstance(event.get("diagnostic"), dict):
                        worker_failure = event["diagnostic"]
                    else:
                        latest = f"{event.get('phase')}: {human_size(event.get('bytes', 0))}"
                except (ValueError, TypeError):
                    tail.append(line)
                    tail = tail[-5:]
            now = time.monotonic()
            if now - last_report >= 10:
                print(f"    {latest}; waiting {int(now-last_progress)}s since progress", flush=True)
                last_report = now
            if process.poll() is None and now - last_progress > idle_timeout:
                process.kill()
                diagnostic_event("copy_worker_stalled", source=str(item.source),
                                 destination=str(target), size_bytes=size,
                                 timeout_seconds=idle_timeout, last_phase=latest,
                                 output_tail=tail)
                raise PrepError(
                    f"CRITICAL: copy/verification stalled for {idle_timeout}s: {target}",
                    code="transfer_stalled",
                    details={"source": str(item.source), "destination": str(target),
                             "size_bytes": size, "timeout_seconds": idle_timeout,
                             "last_phase": latest, "output_tail": tail},
                )
        process.wait()
        reader_thread.join(timeout=2)
        while not messages.empty():
            line = messages.get_nowait()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("phase") == "done":
                        result = event.get("result", "verified")
                        digest = event.get("sha256", "")
                    elif event.get("phase") == "error" and isinstance(event.get("diagnostic"), dict):
                        worker_failure = event["diagnostic"]
                except (ValueError, TypeError):
                    tail.append(line)
                    tail = tail[-5:]
        if process.returncode or not result or not re.fullmatch(r"[0-9a-f]{64}", digest):
            diagnostic_event("copy_worker_failed", source=str(item.source),
                             destination=str(target), size_bytes=size,
                             exit_code=process.returncode, worker_failure=worker_failure,
                             output_tail=tail,
                             duration_seconds=round(time.monotonic() - started, 3))
            if worker_failure:
                raise PrepError(
                    str(worker_failure.get("message") or
                        f"Failed or incomplete transfer of {target}"),
                    code=str(worker_failure.get("code") or "copy_worker_failed"),
                    details={"source": str(item.source), "destination": str(target),
                             "size_bytes": size, "worker_diagnostic": worker_failure,
                             "worker_exit_code": process.returncode, "output_tail": tail},
                    recommendation=str(worker_failure.get("recommendation") or
                                       PrepError.default_recommendation),
                )
            raise PrepError(
                f"CRITICAL: failed or incomplete transfer of {target}: {' | '.join(tail)}",
                code="copy_worker_failed",
                details={"source": str(item.source), "destination": str(target),
                         "size_bytes": size, "worker_exit_code": process.returncode,
                         "output_tail": tail},
            )
        print(f"    {result}", flush=True)
        diagnostic_event("copy_worker_finished", source=str(item.source),
                         destination=str(target), relative_destination=str(item.relative_destination),
                         size_bytes=size, result=result, sha256=digest,
                         exit_code=process.returncode,
                         duration_seconds=round(time.monotonic() - started, 3))
        return CopyOutcome(result, digest)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader_thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        for stale in target.parent.glob(target.name + ".xboxhddprep-part-*"):
            try:
                stale.unlink()
            except OSError as exc:
                diagnostic_event(
                    "temporary_copy_cleanup_failed",
                    path=str(stale),
                    error=repr(exc),
                )


def copy_batch_monitored(
    items: list[CopyItem], destination_root: Path, idle_timeout: int, work_dir: Path,
) -> BatchCopyOutcome:
    """Monitor one isolated worker that copies, verifies, and commits a game."""
    batch_id = uuid.uuid4().hex
    manifest_path = work_dir / f"copy-batch-{batch_id}.json"
    manifest = {
        "batch_id": batch_id,
        "destination": str(destination_root),
        "items": [
            {"source": str(item.source),
             "relative_destination": str(item.relative_destination),
             "size_bytes": item.source.stat().st_size}
            for item in items
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    command = self_command() + ["--copy-batch-worker", str(manifest_path)]
    current_input = _ACTIVE_RECORDER.current_input if _ACTIVE_RECORDER else None
    diagnostic_event(
        "copy_batch_started", input=current_input, file_count=len(items),
        total_bytes=sum(record["size_bytes"] for record in manifest["items"]),
        batch_id=batch_id, idle_timeout_seconds=idle_timeout,
    )
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", stdin=subprocess.DEVNULL,
    )
    messages: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            messages.put(line.rstrip())

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    last_progress = last_report = started = time.monotonic()
    last_phase = "starting"
    result: dict[str, object] | None = None
    worker_failure: dict[str, object] | None = None
    tail: deque[str] = deque(maxlen=10)
    try:
        while process.poll() is None or not messages.empty():
            check_cancelled()
            try:
                line = messages.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line:
                try:
                    event = json.loads(line)
                except ValueError:
                    tail.append(line)
                else:
                    if isinstance(event, dict):
                        phase = str(event.get("phase", ""))
                        last_phase = phase or last_phase
                        last_progress = time.monotonic()
                        if phase == "error" and isinstance(event.get("diagnostic"), dict):
                            worker_failure = event["diagnostic"]
                        elif phase == "done":
                            result = event
                        else:
                            diagnostic_event("copy_batch_progress", input=current_input, **event)
                    else:
                        tail.append(line)
            now = time.monotonic()
            if now - last_report >= 10:
                print(f"  {last_phase.replace('_', ' ').title()}…", flush=True)
                last_report = now
            if process.poll() is None and now - last_progress > idle_timeout:
                process.kill()
                diagnostic_event(
                    "copy_batch_stalled", input=current_input, batch_id=batch_id,
                    last_phase=last_phase, idle_timeout_seconds=idle_timeout,
                    output_tail=list(tail),
                )
                raise PrepError(
                    f"Transfer made no progress for {idle_timeout} seconds",
                    code="transfer_stalled",
                    details={"batch_id": batch_id, "last_phase": last_phase,
                             "timeout_seconds": idle_timeout},
                )
        process.wait()
        reader_thread.join(timeout=2)
        while not messages.empty():
            line = messages.get_nowait()
            try:
                event = json.loads(line)
            except ValueError:
                tail.append(line)
                continue
            if isinstance(event, dict) and event.get("phase") == "done":
                result = event
            elif isinstance(event, dict) and event.get("phase") == "error":
                diagnostic = event.get("diagnostic")
                if isinstance(diagnostic, dict):
                    worker_failure = diagnostic
        if process.returncode or result is None:
            diagnostic_event(
                "copy_batch_failed", input=current_input, batch_id=batch_id,
                exit_code=process.returncode, worker_failure=worker_failure,
                output_tail=list(tail),
            )
            if worker_failure:
                category = worker_failure.get("category")
                error_type = SourceError if category == "source_problem" else PrepError
                raise error_type(
                    str(worker_failure.get("message") or "The transfer worker failed"),
                    code=str(worker_failure.get("code") or "copy_batch_worker_failed"),
                    details={"batch_id": batch_id, "worker_diagnostic": worker_failure,
                             "worker_exit_code": process.returncode},
                )
            raise PrepError(
                "The transfer worker stopped without completing verification",
                code="copy_batch_worker_failed",
                details={"batch_id": batch_id, "worker_exit_code": process.returncode,
                         "output_tail": list(tail)},
            )
        copied = int(result.get("copied_files", -1))
        existing = int(result.get("verified_existing_files", -1))
        if copied < 0 or existing < 0 or copied + existing != len(items):
            raise ApplicationError(
                "The transfer worker returned inconsistent file counts",
                code="copy_batch_result_invalid",
                details={"batch_id": batch_id, "result": result},
            )
        diagnostic_event(
            "copy_batch_finished", input=current_input, batch_id=batch_id,
            copied_files=copied, verified_existing_files=existing,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return BatchCopyOutcome(copied, existing)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader_thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        for item in items:
            target = destination_root / item.relative_destination
            temp = target.with_name(target.name + f".xboxhddprep-part-{batch_id}")
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                diagnostic_event("temporary_copy_cleanup_failed", path=str(temp),
                                 error=repr(exc))


def choose_inputs(inputs: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    print("Detected inputs:")
    for index, (path, kind) in enumerate(inputs, 1):
        print(f"  {index:2}. {path.name} [{kind}]")
    print("  A. All listed inputs    0. Cancel")
    while True:
        response = input("Choose one or more numbers (comma separated), or A: ").strip().lower()
        if response == "0":
            return []
        if response in {"a", "all"}:
            return inputs
        try:
            numbers = [int(part.strip()) for part in response.split(",")]
            if numbers and all(1 <= number <= len(inputs) for number in numbers):
                return [inputs[number - 1] for number in dict.fromkeys(numbers)]
        except ValueError:
            pass
        print("Enter listed numbers, A, or 0.")


def prompt_directory(label: str, default: Path) -> Path:
    while True:
        answer = input(f"{label} directory [{default}]: ").strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z]:", answer):
            answer += "\\"
        chosen = Path(answer).expanduser() if answer else default
        if chosen.is_dir():
            return chosen
        print(f"Directory not found: {chosen}. Enter a different path, or press Enter for the default.")


def matching_destination_folder(source: Path, destination: Path) -> Path | None:
    """Find an exact game-folder name match without reading the input or its contents."""
    name = safe_game_name(source.name)
    for category in ("Xbox 360", "Xbox Original"):
        parent = destination / "Games" / category
        if not parent.is_dir() or not (parent / name).is_dir():
            continue
        # Windows path lookup ignores case; the user requested the exact name.
        for folder in parent.iterdir():
            if folder.is_dir() and folder.name == name:
                return folder
    return None


def run(args: argparse.Namespace, recorder: RunRecorder) -> int:
    recorder.current_stage = "configuration"
    source = args.source.resolve()
    destination = args.destination.resolve()
    if not destination.is_dir():
        raise ConfigurationError(
            f"Destination drive/folder is unavailable: {destination}",
            code="destination_unavailable", details=path_metadata(destination),
        )
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ConfigurationError(
            "Source and destination must be separate locations",
            code="source_destination_overlap",
            details={"source": str(source), "destination": str(destination)},
        )
    verify_integrity = not args.skip_stfs_integrity
    if verify_integrity and not args.list and not STFSCHK.is_file():
        raise ApplicationError(
            f"Bundled STFS verifier is missing: {STFSCHK}",
            code="bundled_stfs_verifier_missing", details=path_metadata(STFSCHK),
        )
    missing_tools = [str(tool) for tool in (SEVEN_ZIP, XISO) if not tool.is_file()]
    if missing_tools:
        raise ApplicationError(
            "One or more bundled preparation tools are missing from tools/",
            code="bundled_tool_missing", details={"missing_tools": missing_tools},
        )
    stfschk_sha256 = file_sha256(STFSCHK).upper() if STFSCHK.is_file() else None
    if verify_integrity and not args.list and stfschk_sha256 != STFSCHK_SHA256:
        raise ApplicationError(
            "The bundled STFS verifier does not match the tested release",
            code="bundled_stfs_verifier_checksum_mismatch",
            details={"path": str(STFSCHK), "expected_sha256": STFSCHK_SHA256,
                     "actual_sha256": stfschk_sha256},
        )
    destination_format = drive_format(destination) or "unknown"
    destination_free = shutil.disk_usage(destination).free
    work_parent = (args.work_dir.resolve() if args.work_dir else
                   (source if source.is_dir() else source.parent) / ".xbox-hdd-prep-work")
    recorder.set_configuration(
        mode="list" if args.list else "prepare",
        source=str(source),
        destination=str(destination),
        work_directory=str(work_parent),
        destination_format=destination_format,
        destination_free_bytes=destination_free,
        idle_timeout_seconds=args.idle_timeout,
        select_all=bool(args.all),
        stfs_integrity_verification=verify_integrity,
        stfschk_sha256=stfschk_sha256,
    )
    recorder.current_stage = "inventory"
    inputs = list_inputs(source)
    inventory_by_kind: dict[str, int] = {}
    for _, kind in inputs:
        inventory_by_kind[kind] = inventory_by_kind.get(kind, 0) + 1
    recorder.record(
        "inventory_completed",
        detected_count=len(inputs),
        inputs=[{"path": str(path), "kind": kind, "metadata": path_metadata(path)}
                for path, kind in inputs],
    )
    print(f"Source: {source}\nDestination: {destination}")
    print(f"Destination format: {destination_format}; free: {human_size(destination_free)}")
    if args.list:
        recorder.mode = "list"
        print("Detected inputs:")
        for index, (path, kind) in enumerate(inputs, 1):
            print(f"  {index:2}. {path.name} [{kind}]")
        recorder.set_summary(detected_inputs=len(inputs), selected=0, handled=0,
                             transferred_games=0, skipped_existing=0,
                             failed_games=0, copied_files=0,
                             verified_existing_files=0,
                             inventory_by_kind=inventory_by_kind)
        return 0
    recorder.current_stage = "selection"
    requested_inputs = getattr(args, "selected_inputs", None)
    if requested_inputs is not None:
        requested = {
            os.path.normcase(str(Path(value).resolve()))
            for value in requested_inputs
        }
        selected = [
            (path, kind) for path, kind in inputs
            if os.path.normcase(str(path.resolve())) in requested
        ]
        matched = {os.path.normcase(str(path.resolve())) for path, _kind in selected}
        missing = sorted(requested - matched)
        if missing:
            raise ConfigurationError(
                "The selected game list no longer matches the source. Rescan the source and try again.",
                code="selected_inputs_changed",
                details={"missing_selected_inputs": missing},
            )
    else:
        selected = inputs if args.all else choose_inputs(inputs)
    recorder.record("selection_completed", selected_count=len(selected),
                    selected=[{"path": str(path), "kind": kind}
                              for path, kind in selected])
    if not selected:
        recorder.mode = "cancelled"
        recorder.set_summary(detected_inputs=len(inputs), selected=0, handled=0,
                             transferred_games=0, skipped_existing=0,
                             failed_games=0, copied_files=0,
                             verified_existing_files=0,
                             inventory_by_kind=inventory_by_kind)
        print("Cancelled. No files changed.")
        return 0
    recorder.set_summary(
        detected_inputs=len(inputs), selected=len(selected), handled=0,
        transferred_games=0, skipped_existing=0, failed_games=0,
        copied_files=0, verified_existing_files=0,
        inventory_by_kind=inventory_by_kind,
    )
    recorder.current_stage = "work_directory_setup"
    work_parent.mkdir(parents=True, exist_ok=True)
    if work_parent.is_relative_to(destination):
        raise ConfigurationError(
            "Working directory must not be on the destination drive",
            code="work_directory_on_destination",
            details={"work_directory": str(work_parent), "destination": str(destination)},
        )
    handled = 0
    transferred_games = 0
    skipped_existing = 0
    failed_games: list[tuple[str, dict[str, object]]] = []
    copied_files = 0
    verified_existing_files = 0

    def update_progress_summary() -> None:
        recorder.set_summary(
            detected_inputs=len(inputs), selected=len(selected), handled=handled,
            transferred_games=transferred_games, skipped_existing=skipped_existing,
            failed_games=len(failed_games), copied_files=copied_files,
            verified_existing_files=verified_existing_files,
            inventory_by_kind=inventory_by_kind,
        )

    for index, (path, kind) in enumerate(selected, 1):
        check_cancelled()
        recorder.current_input = str(path)
        recorder.begin_input()
        recorder.current_stage = "destination_folder_precheck"
        recorder.record("input_started", input=str(path), name=path.name, kind=kind,
                        input_number=index, input_count=len(selected),
                        metadata=path_metadata(path))
        print(f"\n[{index}/{len(selected)}] Checking {path.name} ({kind}) before unpacking", flush=True)
        try:
            existing = matching_destination_folder(path, destination)
            if existing is not None:
                handled += 1
                skipped_existing += 1
                print(f"  Folder already exists: {existing}", flush=True)
                print("  Skipping this game entirely; existing files were not checked.", flush=True)
                recorder.add_input_result({
                    "input": str(path), "name": path.name, "kind": kind,
                    "status": "skipped_existing", "folder": str(existing),
                    "files_checked": 0,
                })
                update_progress_summary()
                print(f"{handled}/{len(selected)} Games Processed "
                      f"({transferred_games} transferred, {skipped_existing} already present, "
                      f"{len(failed_games)} failed)", flush=True)
                continue
            recorder.current_stage = "destination_preflight"
            recorder.record("input_stage", input=str(path), stage=recorder.current_stage)
            estimated_bytes, estimate_method = estimate_input_bytes(path, kind)
            free_bytes = shutil.disk_usage(destination).free
            fits = estimated_bytes <= free_bytes
            recorder.record(
                "destination_space_check",
                input=str(path),
                estimated_bytes=estimated_bytes,
                free_bytes=free_bytes,
                estimate_method=estimate_method,
                fits=fits,
                checked_before_extraction=True,
            )
            print(
                f"  Space check before unpacking: estimated {human_size(estimated_bytes)}, "
                f"{human_size(free_bytes)} available",
                flush=True,
            )
            if not fits:
                raise PrepError(
                    f"Not enough free space on {destination}: estimated need "
                    f"{human_size(estimated_bytes)}, have {human_size(free_bytes)} "
                    "(checked before unpacking)",
                    code="destination_no_space",
                    details={
                        "destination": str(destination),
                        "estimated_bytes": estimated_bytes,
                        "free_bytes": free_bytes,
                        "estimate_method": estimate_method,
                        "checked_before_extraction": True,
                    },
                )
            print(f"  Preparing {path.name}", flush=True)
            recorder.current_stage = "source_preparation"
            recorder.record("input_stage", input=str(path), stage=recorder.current_stage)
            with tempfile.TemporaryDirectory(prefix="job-", dir=work_parent) as temp_name:
                items = deduplicate_plan(prepared_items(
                    path, path.name, Path(temp_name), args.idle_timeout,
                    verify_integrity=verify_integrity,
                ))
                planned_bytes = sum(item.source.stat().st_size for item in items)
                recorder.current_stage = "destination_preflight"
                recorder.record("input_stage", input=str(path), stage=recorder.current_stage)
                validate_plan(items, destination)
                recorder.record(
                    "preparation_plan_validated",
                    input=str(path), file_count=len(items), total_bytes=planned_bytes,
                    destination_format=destination_format,
                    destination_free_bytes=shutil.disk_usage(destination).free,
                    destination_sample=[str(item.relative_destination) for item in items[:25]],
                    destination_sample_truncated=len(items) > 25,
                )
                print(f"  Ready: {len(items)} files, {human_size(planned_bytes)}", flush=True)
                recorder.current_stage = "copy_and_verification"
                recorder.record("input_stage", input=str(path), stage=recorder.current_stage)
                print(f"  Moving {len(items)} files; verification follows the copy phase",
                      flush=True)
                batch_result = copy_batch_monitored(
                    items, destination, args.idle_timeout, Path(temp_name)
                )
                copied_files += batch_result.copied_files
                verified_existing_files += batch_result.verified_existing_files
                update_progress_summary()
            handled += 1
            transferred_games += 1
            recorder.current_stage = "input_complete"
            recorder.add_input_result({
                "input": str(path), "name": path.name, "kind": kind,
                "status": "verified", "files": len(items), "bytes": planned_bytes,
            })
            update_progress_summary()
            print(f"  VERIFIED: {path.name}", flush=True)
            print(f"{handled}/{len(selected)} Games Processed "
                  f"({transferred_games} transferred, {skipped_existing} already present, "
                  f"{len(failed_games)} failed)", flush=True)
        except (OSError, PrepError, subprocess.TimeoutExpired) as exc:
            handled += 1
            diagnostic = exception_diagnostic(exc, recorder.current_stage,
                                              traceback.format_exc())
            diagnostic["input"] = str(path)
            failed_games.append((path.name, diagnostic))
            recorder.add_input_result({
                "input": str(path), "name": path.name, "kind": kind,
                "status": "failed", "diagnostic": diagnostic,
            })
            update_progress_summary()
            label = RunRecorder._classification_label(diagnostic)
            print(f"\nCRITICAL FAILURE [{label}]: {path.name}: {exc}",
                  file=sys.stderr, flush=True)
            print("  Continuing with the next game.", flush=True)
            print(f"{handled}/{len(selected)} Games Processed "
                  f"({transferred_games} transferred, {skipped_existing} already present, "
                  f"{len(failed_games)} failed)", flush=True)
    recorder.current_input = None
    recorder.current_stage = "run_summary"
    update_progress_summary()
    print(f"\nFinished: {handled}/{len(selected)} games processed.")
    print(f"Transferred and SHA-256 verified: {transferred_games}.")
    print(f"Already present, skipped by folder name without checking files: {skipped_existing}.")
    print(f"Failed with errors: {len(failed_games)}.")
    for name, diagnostic in failed_games:
        print(f"  - [{RunRecorder._classification_label(diagnostic)}] "
              f"{name}: {diagnostic['message']}")
    print(f"New files copied: {copied_files}; existing files verified: {verified_existing_files}.")
    print(f"Quick summary: {recorder._quick_summary()}")
    print(f"Destination: {destination}. Source files remain intact.")
    print("In Aurora, add the USB Games folder as a scan path; scan the Content folder for XBLA/GOD packages.")
    return 1 if failed_games else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Xbox 360, XBLA, and original Xbox games for a local Aurora drive")
    parser.add_argument("--source", type=Path, help=f"Source directory (default: {DEFAULT_SOURCE})")
    parser.add_argument("--destination", type=Path, help=f"Destination drive or directory (default: {DEFAULT_DESTINATION})")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--all", action="store_true", help="Select all detected inputs without prompting")
    parser.add_argument("--list", action="store_true", help="Show detected inputs without preparing or copying")
    parser.add_argument("--idle-timeout", type=int, default=600, help="Seconds without extraction/copy progress before failure")
    parser.add_argument(
        "--skip-stfs-integrity", action="store_true",
        help="Skip deep Xbox package hash/filesystem checks (troubleshooting only)",
    )
    parser.add_argument("--copy-worker", nargs=2, metavar=("SOURCE", "DESTINATION"), help=argparse.SUPPRESS)
    parser.add_argument("--copy-batch-worker", type=Path, metavar="MANIFEST", help=argparse.SUPPRESS)
    parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    interactive = not (args.all or args.list or args.copy_worker or args.copy_batch_worker)
    if args.copy_batch_worker:
        try:
            return worker_copy_batch(args.copy_batch_worker)
        except Exception as exc:
            diagnostic = exception_diagnostic(exc, "copy_and_verification",
                                              traceback.format_exc())
            print(json.dumps({"phase": "error", "diagnostic": diagnostic},
                             default=str), flush=True)
            return 1
    if args.copy_worker:
        try:
            return worker_copy(Path(args.copy_worker[0]), Path(args.copy_worker[1]))
        except Exception as exc:
            diagnostic = exception_diagnostic(exc, "copy_and_verification",
                                              traceback.format_exc())
            print(json.dumps({"phase": "error", "diagnostic": diagnostic},
                             ensure_ascii=False, default=str), flush=True)
            return 1
    try:
        recorder = RunRecorder()
    except Exception as exc:
        print(f"CRITICAL FAILURE: Xbox HDD Prep could not create its mandatory log/report: {exc}",
              file=sys.stderr, flush=True)
        return 1
    global _ACTIVE_RECORDER
    _ACTIVE_RECORDER = recorder
    exit_code = 1
    try:
        if args.idle_timeout < 30:
            raise ConfigurationError("Idle timeout must be at least 30 seconds",
                                     code="idle_timeout_invalid",
                                     details={"idle_timeout_seconds": args.idle_timeout,
                                              "minimum_seconds": 30})
        print(f"Xbox HDD Prep {VERSION}\n", flush=True)
        recorder.current_stage = "source_prompt"
        if args.source is None:
            args.source = prompt_directory("Source", DEFAULT_SOURCE) if interactive else DEFAULT_SOURCE
        recorder.current_stage = "destination_prompt"
        if args.destination is None:
            args.destination = prompt_directory("Destination", DEFAULT_DESTINATION) if interactive else DEFAULT_DESTINATION
        exit_code = run(args, recorder)
    except KeyboardInterrupt:
        exc = ConfigurationError("Run cancelled by the user", code="user_cancelled")
        diagnostic = exception_diagnostic(exc, recorder.current_stage,
                                          traceback.format_exc())
        if recorder.current_input:
            diagnostic["input"] = recorder.current_input
        recorder.set_fatal_error(diagnostic)
        print("\nRun cancelled by the user.", file=sys.stderr, flush=True)
        exit_code = 1
    except Exception as exc:
        diagnostic = exception_diagnostic(exc, recorder.current_stage,
                                          traceback.format_exc())
        if recorder.current_input:
            diagnostic["input"] = recorder.current_input
        recorder.set_fatal_error(diagnostic)
        print(f"CRITICAL FAILURE: {exc}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        report_saved = False
        try:
            recorder.finalize(exit_code)
            report_saved = True
        except Exception as exc:
            exit_code = 1
            print(f"CRITICAL FAILURE: Could not finish the mandatory text report: {exc}",
                  file=sys.stderr, flush=True)
        _ACTIVE_RECORDER = None
        print(f"\nDetailed diagnostic log: {recorder.log_path}", flush=True)
        if report_saved:
            print(f"Run report saved to: {recorder.report_path}", flush=True)
            print("For troubleshooting, check that text report first.", flush=True)
        if interactive and not args.no_pause:
            try:
                input("\nRun ended. Press Enter to close this window...")
            except EOFError:
                pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
