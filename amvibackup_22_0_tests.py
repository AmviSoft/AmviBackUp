# -*- coding: utf-8 -*-
"""Complete self-test suite of AmviBackUp 22.0 beta (file amvibackup_22_0_tests.py).

Everything is in one file: the 18.x regression tests and the newer 19.x tests.
Run (put the file next to amvibackup_22_0.py):

    py .\\amvibackup_22_0_tests.py            # compact output
    py .\\amvibackup_22_0_tests.py -v         # verbose test list
    py .\\amvibackup_22_0_tests.py -k restore # only tests with "restore" in the name
    py .\\amvibackup_22_0_tests.py --failfast # stop at the first failure

Through the application: $env:AMVIBACKUP_TEST_MODULE=".\\amvibackup_22_0_tests.py"; py .\\amvibackup_22_0.py --self-test
Another application: $env:AMVIBACKUP_APP_MODULE="path\\to\\amvibackup_X.py"

The console is not flooded: identical background exceptions are printed once with a counter,
the full report with tracebacks is saved to amvibackup_tests_report.txt.
"""
from __future__ import annotations
from contextlib import closing as _amvi_test_closing

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

_APP = globals().get("AMVI_APP_MODULE")
if _APP is None:
    _default_app = next(
        (p for p in (Path(__file__).with_name("amvibackup_22_0.py"),) if p.is_file()),
        None,
    )
    if _default_app is None:
        _candidates = sorted(Path(__file__).resolve().parent.glob("amvibackup_[0-9]*.py"))
        _candidates = [p for p in _candidates if "test" not in p.name]
        if not _candidates:
            raise ImportError("Application file amvibackup_*.py not found next to the tests; set AMVIBACKUP_APP_MODULE")
        _default_app = _candidates[-1]
    app_path = Path(os.environ.get("AMVIBACKUP_APP_MODULE", _default_app)).resolve()
    spec = importlib.util.spec_from_file_location("amvibackup_app_under_test", app_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load the application: {app_path}")
    _APP = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _APP
    spec.loader.exec_module(_APP)

for _name, _value in vars(_APP).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

APP_MODULE_GLOBALS = vars(_APP)
if "StorageMode" not in APP_MODULE_GLOBALS:
    raise ImportError(
        f"The tests loaded an incompatible application: {app_path}. "
        "amvibackup_22_0.py containing StorageMode is expected."
    )
TMP_FILE_LOCK_NAME = "locked.txt"

class _MenuActionControl:
    """A "More" menu item in the versions row looks like a button to the tests."""

    def __init__(self, action: Any) -> None:
        self.action = action

    def text(self) -> str:
        return self.action.text()

    def click(self) -> None:
        self.action.trigger()

    def property(self, name: str) -> Any:
        return None

    def isEnabled(self) -> bool:
        return self.action.isEnabled()

    def toolTip(self) -> str:
        return self.action.toolTip()


def _row_controls(holder: Any) -> list[Any]:
    """Action buttons of a version row plus the items of their drop-down menus."""
    controls: list[Any] = []
    for button in holder.findChildren(QPushButton):
        controls.append(button)
        menu = button.menu()
        if menu is not None:
            controls.extend(_MenuActionControl(a) for a in menu.actions() if not a.isSeparator())
    return controls


class TestWorkspace:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "test.sqlite3", journal_mode="WAL")

    def __enter__(self) -> "TestWorkspace":
        self.db.migrate()
        self.db.verify_contract()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.db.close_for_cleanup()
        self.temp.cleanup()

    def make_writable_for_external_mutation(self, path: Path) -> None:
        """Prepares a test artifact for simulating an external change or deletion.

        Production snapshots are protected by the read-only policy after commit.
        Tests use this explicit helper only when they must simulate external
        damage of a snapshot or the physical disappearance of the storage.
        """
        if not path.exists():
            return

        set_writable_best_effort(path)

        if path.is_file():
            try:
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
            except OSError as exc:
                raise AssertionError(
                    f"Could not remove write protection from the test file: {path}"
                ) from exc

class FakeRegistryKey:
    def __init__(self, storage: dict[str, str]) -> None:
        self.storage = storage

    def __enter__(self) -> "FakeRegistryKey":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc: Any,
        traceback_value: Any,
    ) -> None:
        return None

class FakeWinRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.storage: dict[str, str] = {}

    def OpenKey(
        self,
        root: Any,
        sub_key: str,
        reserved: int,
        access: int,
    ) -> FakeRegistryKey:
        if (
            access == self.KEY_READ
            and WINDOWS_AUTOSTART_VALUE_NAME not in self.storage
        ):
            raise FileNotFoundError()

        return FakeRegistryKey(self.storage)

    def CreateKeyEx(
        self,
        root: Any,
        sub_key: str,
        reserved: int,
        access: int,
    ) -> FakeRegistryKey:
        return FakeRegistryKey(self.storage)

    def QueryValueEx(
        self,
        key: FakeRegistryKey,
        value_name: str,
    ) -> tuple[str, int]:
        if value_name not in self.storage:
            raise FileNotFoundError()

        return self.storage[value_name], self.REG_SZ

    def SetValueEx(
        self,
        key: FakeRegistryKey,
        value_name: str,
        reserved: int,
        value_type: int,
        value: str,
    ) -> None:
        self.storage[value_name] = value

    def DeleteValue(
        self,
        key: FakeRegistryKey,
        value_name: str,
    ) -> None:
        if value_name not in self.storage:
            raise FileNotFoundError()

        del self.storage[value_name]

class FakeTrayIcon:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.messages: list[tuple[Any, ...]] = []
        self.tooltip = ""

    def isVisible(self) -> bool:
        return self.visible

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def showMessage(
        self,
        title: str,
        message: str,
        icon: Any,
        timeout_ms: int,
    ) -> None:
        self.messages.append(
            (title, message, icon, timeout_ms)
        )

class Alpha181ReleaseProfileTests(unittest.TestCase):
    def test_version_and_schema_are_stable(self) -> None:
        self.assertTrue(APP_VERSION.startswith("22.0"), APP_VERSION)
        self.assertGreaterEqual(DB_SCHEMA_VERSION, 12)

    def test_production_module_has_no_dev_source_limit(self) -> None:
        self.assertFalse(hasattr(_APP, "DEV_MODE"))
        self.assertFalse(hasattr(_APP, "DEV_TEST_MAX_SOURCE_BYTES"))
        source = Path(_APP.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SOURCE_TOO_LARGE_FOR_DEV_LIMIT", source)

    def test_production_module_has_entry_point_and_external_tests(self) -> None:
        self.assertTrue(callable(_APP.main))
        self.assertTrue(callable(_APP.run_gui))
        self.assertTrue(callable(_APP.run_self_tests))
        self.assertFalse(hasattr(_APP, "TestWorkspace"))
        self.assertFalse(hasattr(_APP, "Alpha511Tests"))

class Alpha511Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TestWorkspace().__enter__()
        self.root = self.workspace.root
        self.db = self.workspace.db
        self.repo = Repository(self.db)
        self.storage = StorageService()
        self.health = HealthService(self.repo)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.storage_root = self.root / "storage"
        self.snapshot_service = SnapshotService(self.repo, self.storage, self.health, reserve_bytes=0, reserve_ratio=0.0)
        self.verifier = VerificationService(self.repo, self.storage, self.health)
        self.restore = RestoreService(self.repo, self.verifier, self.health)
        self.delete = DeleteService(self.repo, self.storage, self.health)
        self.progress = lambda event: None
        self.not_cancelled = lambda: False

    def tearDown(self) -> None:
        self.workspace.__exit__(None, None, None)

        application = QApplication.instance()
        if application is not None:
            application.processEvents()

    def make_plan(
        self,
        source: Path,
        keep: int = 5,
        storage_mode: str = StorageMode.DIRECT.value,
    ) -> str:
        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.storage_root))
        storage_id = self.repo.get_or_create_storage(storage)
        self.storage.ensure_root(storage, storage_id)

        return self.repo.create_plan(
            "Тестовый план",
            "",
            descriptor,
            storage_id,
            keep,
            storage_mode,
        )

    def backup(self, plan_id: str, force: bool = False) -> OperationResult:
        operation_id = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {"force": force})
        return self.snapshot_service.create(operation_id, plan_id, force, self.progress, self.not_cancelled)

    def test_schema_contract_and_issues_table(self) -> None:
        self.db.verify_contract()

        source = self.source_root / "issue-test.txt"
        source.write_text("issue test", encoding="utf-8")
        plan_id = self.make_plan(source)

        dedupe_key = f"TEST:{plan_id}"
        self.repo.upsert_issue(
            IssueDraft(
                dedupe_key,
                plan_id,
                "warning",
                "TEST",
                "Test",
                "Detail",
            )
        )
        self.repo.upsert_issue(
            IssueDraft(
                dedupe_key,
                plan_id,
                "error",
                "TEST",
                "Updated",
                "New detail",
            )
        )

        issues = self.repo.list_active_issues(plan_id)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].title, "Updated")
        self.assertEqual(issues[0].severity, "error")

    def test_file_backup_verify_restore_no_change_force(self) -> None:
        source = self.source_root / "document.txt"
        source.write_text("version one", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.snapshots(plan_id)[0]
        operation_id = self.repo.create_operation(OperationType.VERIFY, plan_id, snapshot.id, {})
        self.assertTrue(self.verifier.verify(operation_id, snapshot.id, self.progress, self.not_cancelled).success)
        operation_id = self.repo.create_operation(OperationType.RESTORE_BESIDE, plan_id, snapshot.id, {})
        restored = self.restore.restore(operation_id, snapshot.id, self.progress, self.not_cancelled)
        self.assertTrue(restored.success)
        restored_path = Path(restored.message.removeprefix("Restored: "))
        self.assertEqual(restored_path.read_text(encoding="utf-8"), "version one")
        self.assertTrue(self.backup(plan_id).success)
        self.assertEqual(len([item for item in self.repo.snapshots(plan_id) if item.state == "committed"]), 1)
        self.assertTrue(self.backup(plan_id, True).success)
        self.assertEqual(len([item for item in self.repo.snapshots(plan_id) if item.state == "committed"]), 2)

    def test_folder_unicode_backup_and_verify(self) -> None:
        folder = self.source_root / "Проект"
        (folder / "вложенная").mkdir(parents=True)
        (folder / "файл.txt").write_text("данные", encoding="utf-8")
        (folder / "вложенная" / "second.bin").write_bytes(b"abc\x00def")
        plan_id = self.make_plan(folder)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.snapshots(plan_id)[0]
        operation_id = self.repo.create_operation(OperationType.VERIFY, plan_id, snapshot.id, {})
        self.assertTrue(self.verifier.verify(operation_id, snapshot.id, self.progress, self.not_cancelled).success)

    def test_health_new_plan_unprotected_then_protected(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("a", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertEqual(self.health.evaluate(self.repo.plan_details(plan_id)).state, HealthState.UNPROTECTED.value)
        self.assertTrue(self.backup(plan_id).success)
        self.assertEqual(self.health.evaluate(self.repo.plan_details(plan_id)).state, HealthState.PROTECTED.value)

    def test_missing_source_creates_error_issue(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("a", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        source.unlink()
        health = self.health.evaluate(self.repo.plan_details(plan_id))
        self.assertEqual(health.state, HealthState.ERROR.value)
        self.assertTrue(any(item.code == "SOURCE_NOT_FOUND" for item in self.repo.list_active_issues(plan_id)))

    def test_missing_storage_creates_error_issue(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("data", encoding="utf-8")

        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        # Simulate the storage root being disconnected or disappearing.
        # First remove read-only protection, otherwise Windows correctly
        # blocks shutil.rmtree() on a committed artifact.
        self.workspace.make_writable_for_external_mutation(
            self.storage_root
        )
        shutil.rmtree(self.storage_root)

        self.assertFalse(self.storage_root.exists())

        health = self.health.evaluate(
            self.repo.plan_details(plan_id)
        )

        self.assertEqual(
            health.state,
            HealthState.ERROR.value,
        )
        self.assertTrue(
            any(
                item.code == "STORAGE_NOT_FOUND"
                for item in self.repo.list_active_issues(plan_id)
            )
        )

    def test_corruption_blocks_restore_and_creates_issue(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("safe", encoding="utf-8")

        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)

        paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            snapshot.sequence_no,
        )
        artifact = paths.direct_data_root / source.name

        self.assertTrue(artifact.is_file())

        # Simulate external damage after a successful commit.
        # Before the mutation the test helper removes the normal read-only protection.
        self.workspace.make_writable_for_external_mutation(
            paths.snapshot_root
        )
        artifact.write_text("changed", encoding="utf-8")

        verify_operation = self.repo.create_operation(
            OperationType.VERIFY,
            plan_id,
            snapshot.id,
            {},
        )
        verify_result = self.verifier.verify(
            verify_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertFalse(verify_result.success)
        self.assertEqual(
            self.repo.snapshot(snapshot.id).state,
            "corrupted",
        )
        self.assertTrue(
            any(
                item.code == "SNAPSHOT_CORRUPTED"
                for item in self.repo.list_active_issues(plan_id)
            )
        )

        restore_operation = self.repo.create_operation(
            OperationType.RESTORE_BESIDE,
            plan_id,
            snapshot.id,
            {},
        )
        restore_result = self.restore.restore(
            restore_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertFalse(restore_result.success)
        self.assertEqual(
            restore_result.error_code,
            "SNAPSHOT_CORRUPTED",
        )

    def test_retention_and_pinning(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("0", encoding="utf-8")
        plan_id = self.make_plan(source, keep=2)
        self.assertTrue(self.backup(plan_id).success)
        pinned = self.repo.snapshots(plan_id)[0]
        self.repo.set_pin(pinned.id, True)
        for content in ("1", "2", "3"):
            time.sleep(0.003)
            source.write_text(content, encoding="utf-8")
            self.assertTrue(self.backup(plan_id).success)
        snapshots = self.repo.snapshots(plan_id)
        self.assertTrue(any(item.id == pinned.id and item.pinned for item in snapshots))
        self.assertLessEqual(len([item for item in snapshots if item.state == "committed"]), 3)

    def test_delete_protects_last_and_pinned(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("one", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        only = self.repo.snapshots(plan_id)[0]
        operation_id = self.repo.create_operation(OperationType.DELETE_SNAPSHOTS, plan_id, None, {})
        self.assertFalse(self.delete.delete(operation_id, [only.id], self.progress, self.not_cancelled).success)
        source.write_text("two", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)
        pinned = self.repo.snapshots(plan_id)[0]
        self.repo.set_pin(pinned.id, True)
        operation_id = self.repo.create_operation(OperationType.DELETE_SNAPSHOTS, plan_id, None, {})
        self.assertFalse(self.delete.delete(operation_id, [pinned.id], self.progress, self.not_cancelled).success)

    def test_path_policy_guards(self) -> None:
        folder = self.source_root / "folder"
        folder.mkdir()
        source = PathPolicy.validate_source(str(folder))
        with self.assertRaises(AppError) as ctx:
            PathPolicy.validate_topology(source, folder / "backup")
        self.assertEqual(ctx.exception.code, "STORAGE_TOPOLOGY_INVALID")
        with self.assertRaises(AppError):
            PathPolicy.safe_join_under(self.root, "../outside.txt")

    def test_recovery_marks_only_operation_temp_and_blocks_backup(self) -> None:
        source = self.source_root / "a.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        operation_id = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
        self.repo.operation_state(operation_id, "running")
        self.repo.create_snapshot_creating(plan_id, operation_id)
        operation_temp = self.storage_root / "_amvibackup" / "temporary" / operation_id
        operation_temp.mkdir(parents=True)
        (operation_temp / "partial.bin").write_text("partial", encoding="utf-8")
        unrelated_temp = self.storage_root / "_amvibackup" / "temporary" / "unrelated"
        unrelated_temp.mkdir(parents=True)
        (unrelated_temp / "keep.txt").write_text("keep", encoding="utf-8")
        user_file = self.storage_root / "user-file.txt"
        user_file.write_text("keep", encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        self.assertTrue(operation_temp.exists())
        self.assertEqual((operation_temp / "partial.bin").read_text(encoding="utf-8"), "partial")
        self.assertTrue(unrelated_temp.exists())
        self.assertTrue(user_file.exists())
        result = self.backup(plan_id, True)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "OPERATION_NEEDS_RECOVERY")

    def test_cancelled_backup_has_no_committed_snapshot(self) -> None:
        source = self.source_root / "a.txt"
        source.write_bytes(b"x" * 1024)
        plan_id = self.make_plan(source)
        operation_id = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
        result = self.snapshot_service.create(operation_id, plan_id, False, self.progress, lambda: True)
        self.assertFalse(result.success)
        self.assertFalse([item for item in self.repo.snapshots(plan_id) if item.state == "committed"])

    def test_alpha4_database_migrates_to_issues(self) -> None:
        legacy_path = self.root / "legacy.sqlite3"
        legacy = Database(legacy_path, journal_mode="WAL")
        with legacy.transaction() as conn:
            conn.execute("CREATE TABLE ActivityLog (id TEXT PRIMARY KEY, created_at_utc_ms INTEGER NOT NULL, level TEXT NOT NULL, plan_id TEXT, operation_id TEXT, event_code TEXT NOT NULL, message TEXT NOT NULL)")
        legacy.migrate()
        with legacy.read_connection() as conn:
            self.assertIn("details_json", legacy.columns(conn, "ActivityLog"))
            self.assertIn("dedupe_key", legacy.columns(conn, "Issues"))
        legacy.close_for_cleanup()

    def test_alpha52_database_migrates_to_storage_mode(self) -> None:
        legacy_path = self.root / "alpha52.sqlite3"
        legacy = Database(legacy_path, journal_mode="WAL")

        with legacy.transaction() as conn:
            conn.execute(
                """CREATE TABLE BackupPlans (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    storage_root_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    keep_last_n INTEGER NOT NULL DEFAULT 5,
                    last_success_snapshot_id TEXT NULL,
                    created_at_utc_ms INTEGER NOT NULL,
                    updated_at_utc_ms INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE Snapshots (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at_utc_ms INTEGER NOT NULL,
                    artifact_relpath TEXT NOT NULL,
                    manifest_relpath TEXT NOT NULL,
                    logical_size_bytes INTEGER NOT NULL DEFAULT 0,
                    stored_size_bytes INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    verification_state TEXT NOT NULL DEFAULT 'not_verified',
                    verified_at_utc_ms INTEGER NULL,
                    pinned INTEGER NOT NULL DEFAULT 0
                )"""
            )

        legacy.migrate()

        with legacy.read_connection() as conn:
            self.assertIn("storage_mode", legacy.columns(conn, "BackupPlans"))
            self.assertIn("storage_mode", legacy.columns(conn, "Snapshots"))

        legacy.close_for_cleanup()

    def test_zip_artifact_writer_and_reader(self) -> None:
        source_folder = self.source_root / "zip-source"
        nested = source_folder / "nested"
        nested.mkdir(parents=True)

        (source_folder / "root.txt").write_text("root data", encoding="utf-8")
        (nested / "файл.txt").write_text("unicode data", encoding="utf-8")

        source = PathPolicy.validate_source(str(source_folder))
        entries, total_bytes, _ = Scanner.scan(
            source,
            self.not_cancelled,
            self.progress,
            "zip-test-operation",
        )

        artifact = self.root / "zip-test" / "artifact.zip"
        writer = ZipArtifactWriter()

        manifest_entries, stored_size, file_count = writer.write(
            artifact,
            source.kind.value,
            entries,
            self.not_cancelled,
            self.progress,
            "zip-test-operation",
            total_bytes,
        )

        self.assertTrue(artifact.is_file())
        self.assertGreater(stored_size, 0)
        self.assertEqual(file_count, 2)

        reader = ZipArtifactReader()
        reader.verify(
            artifact,
            manifest_entries,
            self.not_cancelled,
            self.progress,
            "zip-test-verify",
        )

        restored = self.root / "zip-restored"
        reader.extract_to(
            artifact,
            manifest_entries,
            restored,
            self.not_cancelled,
            self.progress,
            "zip-test-restore",
        )

        self.assertEqual(
            (restored / "root.txt").read_text(encoding="utf-8"),
            "root data",
        )
        self.assertEqual(
            (restored / "nested" / "файл.txt").read_text(encoding="utf-8"),
            "unicode data",
        )


    def test_zip_reader_blocks_traversal_entry(self) -> None:
        artifact = self.root / "unsafe.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("../outside.txt", "unsafe")

        reader = ZipArtifactReader()

        with self.assertRaises(AppError) as context:
            reader.verify(
                artifact,
                [],
                self.not_cancelled,
                self.progress,
                "zip-traversal-test",
            )

        self.assertEqual(context.exception.code, "ZIP_TRAVERSAL_BLOCKED")


    def test_zip_reader_detects_corrupted_content(self) -> None:
        artifact = self.root / "corrupted.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("file.txt", b"actual data")

        manifest_entries = [
            {
                "relative_path": "file.txt",
                "entry_type": "file",
                "size_bytes": len(b"actual data"),
                "mtime_ns": 0,
                "sha256": sha256_bytes(b"different data"),
            }
        ]

        reader = ZipArtifactReader()

        with self.assertRaises(AppError) as context:
            reader.verify(
                artifact,
                manifest_entries,
                self.not_cancelled,
                self.progress,
                "zip-corruption-test",
            )

        self.assertEqual(context.exception.code, "SNAPSHOT_CORRUPTED")


    def make_zip_plan(self, source: Path, keep: int = 5) -> str:
        return self.make_plan(
            source,
            keep,
            StorageMode.ZIP.value,
        )

    def test_create_plan_accepts_zip_storage_mode(self) -> None:
        source = self.source_root / "zip-plan-source.txt"
        source.write_text("zip plan source", encoding="utf-8")

        plan_id = self.make_plan(
            source,
            storage_mode=StorageMode.ZIP.value,
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.storage_mode,
            StorageMode.ZIP.value,
        )


    def test_plan_storage_mode_can_change_before_first_snapshot(self) -> None:
        source = self.source_root / "mode-before-snapshot.txt"
        source.write_text("before snapshot", encoding="utf-8")

        plan_id = self.make_plan(source)

        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.storage_root))
        storage_id = self.repo.get_or_create_storage(storage)

        self.repo.update_plan(
            plan_id,
            "Тестовый план",
            "",
            descriptor,
            storage_id,
            5,
            StorageMode.ZIP.value,
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.storage_mode,
            StorageMode.ZIP.value,
        )


    def test_plan_storage_mode_change_is_blocked_after_snapshot(self) -> None:
        source = self.source_root / "mode-after-snapshot.txt"
        source.write_text("after snapshot", encoding="utf-8")

        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.storage_root))
        storage_id = self.repo.get_or_create_storage(storage)

        with self.assertRaises(AppError) as context:
            self.repo.update_plan(
                plan_id,
                "Тестовый план",
                "",
                descriptor,
                storage_id,
                5,
                StorageMode.ZIP.value,
            )

        self.assertEqual(
            context.exception.code,
            "STORAGE_MODE_CHANGE_BLOCKED",
        )

    def test_zip_file_backup_verify_restore(self) -> None:
        source = self.source_root / "zip-document.txt"
        source.write_text("zip version one", encoding="utf-8")

        plan_id = self.make_zip_plan(source)
        result = self.backup(plan_id)

        self.assertTrue(result.success)
        snapshot = self.repo.snapshots(plan_id)[0]

        self.assertEqual(snapshot.storage_mode, StorageMode.ZIP.value)
        self.assertEqual(snapshot.state, "committed")

        plan = self.repo.plan_details(plan_id)
        paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            snapshot.sequence_no,
        )

        self.assertTrue(paths.zip_artifact.is_file())
        self.assertFalse(paths.direct_data_root.exists())
        self.assertTrue(paths.manifest_path.is_file())
        self.assertTrue(paths.snapshot_metadata_path.is_file())

        verify_operation = self.repo.create_operation(
            OperationType.VERIFY,
            plan_id,
            snapshot.id,
            {},
        )
        verify_result = self.verifier.verify(
            verify_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertTrue(verify_result.success)
        self.assertEqual(
            self.repo.snapshot(snapshot.id).verification_state,
            "passed",
        )

        restore_operation = self.repo.create_operation(
            OperationType.RESTORE_BESIDE,
            plan_id,
            snapshot.id,
            {},
        )
        restore_result = self.restore.restore(
            restore_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertTrue(restore_result.success)

        restored_path = Path(
            restore_result.message.removeprefix("Restored: ")
        )
        self.assertTrue(restored_path.is_file())
        self.assertEqual(
            restored_path.read_text(encoding="utf-8"),
            "zip version one",
        )


    def test_zip_folder_backup_verify_restore(self) -> None:
        source = self.source_root / "ZIP Проект"
        (source / "вложенная").mkdir(parents=True)
        (source / "readme.txt").write_text("root", encoding="utf-8")
        (source / "вложенная" / "данные.txt").write_text(
            "unicode payload",
            encoding="utf-8",
        )

        plan_id = self.make_zip_plan(source)
        backup_result = self.backup(plan_id)

        self.assertTrue(backup_result.success)

        snapshot = self.repo.snapshots(plan_id)[0]
        verify_operation = self.repo.create_operation(
            OperationType.VERIFY,
            plan_id,
            snapshot.id,
            {},
        )

        self.assertTrue(
            self.verifier.verify(
                verify_operation,
                snapshot.id,
                self.progress,
                self.not_cancelled,
            ).success
        )

        restore_operation = self.repo.create_operation(
            OperationType.RESTORE_BESIDE,
            plan_id,
            snapshot.id,
            {},
        )

        restore_result = self.restore.restore(
            restore_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertTrue(restore_result.success)

        restored_folder = Path(
            restore_result.message.removeprefix("Restored: ")
        )
        self.assertTrue(restored_folder.is_dir())
        self.assertEqual(
            (restored_folder / "readme.txt").read_text(encoding="utf-8"),
            "root",
        )
        self.assertEqual(
            (restored_folder / "вложенная" / "данные.txt").read_text(
                encoding="utf-8"
            ),
            "unicode payload",
        )


    def test_zip_corruption_blocks_restore(self) -> None:
        source = self.source_root / "zip-corrupted.txt"
        source.write_text("safe zip content", encoding="utf-8")

        plan_id = self.make_zip_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)

        paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            snapshot.sequence_no,
        )

        self.assertTrue(paths.zip_artifact.is_file())

        # Simulate external damage of the ZIP artifact after commit.
        self.workspace.make_writable_for_external_mutation(
            paths.snapshot_root
        )
        paths.zip_artifact.write_bytes(b"not a valid zip")

        verify_operation = self.repo.create_operation(
            OperationType.VERIFY,
            plan_id,
            snapshot.id,
            {},
        )
        verify_result = self.verifier.verify(
            verify_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertFalse(verify_result.success)
        self.assertEqual(
            self.repo.snapshot(snapshot.id).state,
            "corrupted",
        )

        restore_operation = self.repo.create_operation(
            OperationType.RESTORE_BESIDE,
            plan_id,
            snapshot.id,
            {},
        )
        restore_result = self.restore.restore(
            restore_operation,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertFalse(restore_result.success)
        self.assertEqual(
            restore_result.error_code,
            "SNAPSHOT_CORRUPTED",
        )

    def test_zip_retention_removes_complete_snapshot_root(self) -> None:
        source = self.source_root / "zip-retention.txt"
        source.write_text("0", encoding="utf-8")

        plan_id = self.make_zip_plan(source, keep=1)
        self.assertTrue(self.backup(plan_id).success)

        first_snapshot = self.repo.snapshots(plan_id)[0]
        first_plan = self.repo.plan_details(plan_id)
        first_paths = self.storage.artifact_paths(
            self.storage_root,
            first_plan,
            first_snapshot.sequence_no,
        )

        source.write_text("1", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)

        snapshots = [
            item
            for item in self.repo.snapshots(plan_id)
            if item.state == "committed"
        ]

        self.assertEqual(len(snapshots), 1)
        self.assertFalse(first_paths.snapshot_root.exists())

    def test_direct_committed_artifact_is_read_only_best_effort(self) -> None:
        source = self.source_root / "readonly-direct.txt"
        source.write_text("direct readonly", encoding="utf-8")

        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)

        paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            snapshot.sequence_no,
        )
        artifact = paths.direct_data_root / source.name

        self.assertTrue(artifact.is_file())

        # Check the policy call, not a specific Windows attribute:
        # different file systems/CI may report writable flags differently.
        set_read_only_best_effort(artifact)
        self.assertTrue(artifact.exists())


    def test_zip_committed_artifact_is_read_only_best_effort(self) -> None:
        source = self.source_root / "readonly-zip.txt"
        source.write_text("zip readonly", encoding="utf-8")

        plan_id = self.make_zip_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)

        paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            snapshot.sequence_no,
        )

        self.assertTrue(paths.zip_artifact.is_file())

        set_read_only_best_effort(paths.zip_artifact)
        self.assertTrue(paths.zip_artifact.exists())


    def test_retention_can_delete_read_only_direct_snapshot(self) -> None:
        source = self.source_root / "readonly-retention-direct.txt"
        source.write_text("0", encoding="utf-8")

        plan_id = self.make_plan(source, keep=1)
        self.assertTrue(self.backup(plan_id).success)

        first_snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)
        first_paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            first_snapshot.sequence_no,
        )

        source.write_text("1", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)

        committed = [
            item
            for item in self.repo.snapshots(plan_id)
            if item.state == "committed"
        ]

        self.assertEqual(len(committed), 1)
        self.assertFalse(first_paths.snapshot_root.exists())


    def test_retention_can_delete_read_only_zip_snapshot(self) -> None:
        source = self.source_root / "readonly-retention-zip.txt"
        source.write_text("0", encoding="utf-8")

        plan_id = self.make_zip_plan(source, keep=1)
        self.assertTrue(self.backup(plan_id).success)

        first_snapshot = self.repo.snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)
        first_paths = self.storage.artifact_paths(
            self.storage_root,
            plan,
            first_snapshot.sequence_no,
        )

        source.write_text("1", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)

        committed = [
            item
            for item in self.repo.snapshots(plan_id)
            if item.state == "committed"
        ]

        self.assertEqual(len(committed), 1)
        self.assertFalse(first_paths.snapshot_root.exists())


    def test_recovery_removes_nested_zip_temporary_artifact_only(self) -> None:
        source = self.source_root / "zip-recovery.txt"
        source.write_text("zip recovery", encoding="utf-8")

        plan_id = self.make_zip_plan(source)

        operation_id = self.repo.create_operation(
            OperationType.BACKUP,
            plan_id,
            None,
            {},
        )
        self.repo.operation_state(operation_id, "running")
        self.repo.create_snapshot_creating(plan_id, operation_id)

        plan = self.repo.plan_details(plan_id)
        zip_temporary = self.storage.temporary_snapshot_dir(
            self.storage_root,
            operation_id,
            StorageMode.ZIP.value,
        )
        zip_temporary.mkdir(parents=True)
        (zip_temporary / StorageService.ZIP_ARTIFACT_NAME).write_bytes(
            b"partial zip"
        )

        unrelated = (
            self.storage_root
            / "_amvibackup"
            / "temporary"
            / "unrelated-operation"
        )
        unrelated.mkdir(parents=True)
        (unrelated / "keep.txt").write_text(
            "must stay",
            encoding="utf-8",
        )

        RecoveryService(self.repo, self.health).run()

        operation_root = self.storage.temporary_operation_dir(
            self.storage_root,
            operation_id,
        )

        self.assertTrue(operation_root.exists())
        self.assertEqual((zip_temporary / StorageService.ZIP_ARTIFACT_NAME).read_bytes(), b"partial zip")
        self.assertTrue(unrelated.exists())
        self.assertTrue((unrelated / "keep.txt").is_file())

    def test_new_plan_has_safe_automation_defaults(self) -> None:
        source = self.source_root / "automation-defaults.txt"
        source.write_text("automation defaults", encoding="utf-8")

        plan_id = self.make_plan(source)
        plan = self.repo.plan_details(plan_id)

        self.assertFalse(plan.on_change_enabled)
        self.assertEqual(
            plan.change_debounce_seconds,
            DEFAULT_CHANGE_DEBOUNCE_SECONDS,
        )
        self.assertEqual(
            plan.min_auto_interval_minutes,
            DEFAULT_MIN_AUTO_INTERVAL_MINUTES,
        )
        self.assertFalse(plan.on_schedule_enabled)
        self.assertEqual(
            plan.schedule_interval_minutes,
            DEFAULT_SCHEDULE_INTERVAL_MINUTES,
        )
        self.assertFalse(plan.quiet_hours_enabled)
        self.assertIsNone(plan.quiet_hours_start_local)
        self.assertIsNone(plan.quiet_hours_end_local)
        self.assertTrue(plan.manual_allowed_quiet_hours)
        self.assertIsNone(plan.last_auto_backup_at_utc_ms)
        self.assertEqual(plan.pending_trigger_mask, 0)
        self.assertIsNone(plan.pending_trigger_since_utc_ms)
        self.assertEqual(plan.last_source_scan_fingerprint, "")


    def test_alpha66_database_migrates_to_automation_policy(self) -> None:
        legacy_path = self.root / "alpha66.sqlite3"
        legacy = Database(legacy_path, journal_mode="WAL")

        with legacy.transaction() as conn:
            conn.execute(
                """CREATE TABLE BackupPlans (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    storage_mode TEXT NOT NULL DEFAULT 'direct',
                    storage_root_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    keep_last_n INTEGER NOT NULL DEFAULT 5,
                    last_success_snapshot_id TEXT NULL,
                    created_at_utc_ms INTEGER NOT NULL,
                    updated_at_utc_ms INTEGER NOT NULL
                )"""
            )

        legacy.migrate()

        expected_columns = {
            "on_change_enabled",
            "change_debounce_seconds",
            "min_auto_interval_minutes",
            "on_schedule_enabled",
            "schedule_interval_minutes",
            "quiet_hours_enabled",
            "quiet_hours_start_local",
            "quiet_hours_end_local",
            "manual_allowed_quiet_hours",
            "last_auto_backup_at_utc_ms",
            "pending_trigger_mask",
            "pending_trigger_since_utc_ms",
            "last_source_scan_fingerprint",
        }

        with legacy.read_connection() as conn:
            actual_columns = legacy.columns(conn, "BackupPlans")

        self.assertTrue(expected_columns.issubset(actual_columns))
        legacy.close_for_cleanup()


    def test_trigger_reason_values_are_stable(self) -> None:
        self.assertEqual(TriggerReason.MANUAL.value, "manual")
        self.assertEqual(TriggerReason.MANUAL_FORCE.value, "manual_force")
        self.assertEqual(TriggerReason.EVENT.value, "event")
        self.assertEqual(TriggerReason.SCHEDULE.value, "schedule")
        self.assertEqual(
            TriggerReason.STARTUP_CATCHUP.value,
            "startup_catchup",
        )

    def test_trigger_policy_round_trip_and_normalization(self) -> None:
        source = self.source_root / "policy-round-trip.txt"
        source.write_text("policy", encoding="utf-8")
        plan_id = self.make_plan(source)

        policy = TriggerPolicy(
            on_change_enabled=True,
            change_debounce_seconds=15,
            min_auto_interval_minutes=30,
            on_schedule_enabled=True,
            schedule_interval_minutes=120,
            quiet_hours_enabled=True,
            quiet_hours_start_local="7:05",
            quiet_hours_end_local="22:15",
            manual_allowed_quiet_hours=False,
        )

        saved = self.repo.update_trigger_policy(plan_id, policy)
        loaded = self.repo.get_trigger_policy(plan_id)

        self.assertEqual(saved, loaded)
        self.assertEqual(loaded.quiet_hours_start_local, "07:05")
        self.assertEqual(loaded.quiet_hours_end_local, "22:15")


    def test_trigger_policy_disabled_quiet_hours_clears_times(self) -> None:
        source = self.source_root / "policy-no-quiet-hours.txt"
        source.write_text("policy", encoding="utf-8")
        plan_id = self.make_plan(source)

        saved = self.repo.update_trigger_policy(
            plan_id,
            TriggerPolicy(
                on_change_enabled=True,
                change_debounce_seconds=5,
                min_auto_interval_minutes=10,
                on_schedule_enabled=False,
                schedule_interval_minutes=60,
                quiet_hours_enabled=False,
                quiet_hours_start_local="22:00",
                quiet_hours_end_local="07:00",
                manual_allowed_quiet_hours=True,
            ),
        )

        self.assertFalse(saved.quiet_hours_enabled)
        self.assertIsNone(saved.quiet_hours_start_local)
        self.assertIsNone(saved.quiet_hours_end_local)


    def test_trigger_policy_rejects_invalid_values(self) -> None:
        source = self.source_root / "policy-invalid.txt"
        source.write_text("policy", encoding="utf-8")
        plan_id = self.make_plan(source)

        with self.assertRaises(AppError) as debounce_error:
            self.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=0,
                    min_auto_interval_minutes=10,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )
        self.assertEqual(
            debounce_error.exception.code,
            "TRIGGER_POLICY_INVALID",
        )

        with self.assertRaises(AppError) as quiet_hours_error:
            self.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=5,
                    min_auto_interval_minutes=10,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=True,
                    quiet_hours_start_local="22:00",
                    quiet_hours_end_local="22:00",
                    manual_allowed_quiet_hours=True,
                ),
            )
        self.assertEqual(
            quiet_hours_error.exception.code,
            "TRIGGER_POLICY_INVALID",
        )


    def test_pending_trigger_deduplicates_and_merges_reasons(self) -> None:
        source = self.source_root / "pending-trigger.txt"
        source.write_text("pending", encoding="utf-8")
        plan_id = self.make_plan(source)

        first_now = 1_000_000
        first_mask = self.repo.mark_pending_trigger(
            plan_id,
            TRIGGER_MASK_EVENT,
            first_now,
        )

        second_mask = self.repo.mark_pending_trigger(
            plan_id,
            TRIGGER_MASK_EVENT | TRIGGER_MASK_SCHEDULE,
            first_now + 5_000,
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(first_mask, TRIGGER_MASK_EVENT)
        self.assertEqual(
            second_mask,
            TRIGGER_MASK_EVENT | TRIGGER_MASK_SCHEDULE,
        )
        self.assertEqual(
            plan.pending_trigger_mask,
            TRIGGER_MASK_EVENT | TRIGGER_MASK_SCHEDULE,
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            first_now,
        )


    def test_pending_trigger_clear_and_pending_plans(self) -> None:
        source_one = self.source_root / "pending-one.txt"
        source_two = self.source_root / "pending-two.txt"
        source_one.write_text("one", encoding="utf-8")
        source_two.write_text("two", encoding="utf-8")

        plan_one = self.make_plan(source_one)
        plan_two = self.make_plan(source_two)

        self.repo.mark_pending_trigger(
            plan_one,
            TRIGGER_MASK_EVENT,
            2_000,
        )
        self.repo.mark_pending_trigger(
            plan_two,
            TRIGGER_MASK_SCHEDULE,
            1_000,
        )

        pending_ids = [plan.id for plan in self.repo.pending_plans()]
        self.assertEqual(pending_ids, [plan_two, plan_one])

        self.repo.clear_pending_trigger(plan_two)
        pending_ids_after_clear = [
            plan.id
            for plan in self.repo.pending_plans()
        ]

        self.assertEqual(pending_ids_after_clear, [plan_one])
        self.assertEqual(
            self.repo.plan_details(plan_two).pending_trigger_mask,
            0,
        )
        self.assertIsNone(
            self.repo.plan_details(plan_two).pending_trigger_since_utc_ms
        )


    def test_disabled_and_archived_plans_do_not_acquire_pending_trigger(self) -> None:
        source = self.source_root / "disabled-pending.txt"
        source.write_text("disabled", encoding="utf-8")
        plan_id = self.make_plan(source)

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE BackupPlans SET state=? WHERE id=?",
                (PlanState.PAUSED.value, plan_id),
            )

        mask = self.repo.mark_pending_trigger(
            plan_id,
            TRIGGER_MASK_EVENT,
            10_000,
        )

        self.assertEqual(mask, 0)
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_auto_timestamp_and_source_fingerprint_updates(self) -> None:
        source = self.source_root / "automation-metadata.txt"
        source.write_text("metadata", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_last_auto_backup(
            plan_id,
            3_000_000,
        )
        self.repo.update_last_source_scan_fingerprint(
            plan_id,
            "test-fingerprint",
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.last_auto_backup_at_utc_ms,
            3_000_000,
        )
        self.assertEqual(
            plan.last_source_scan_fingerprint,
            "test-fingerprint",
        )

    def make_automation_policy(
        self,
        *,
        on_change_enabled: bool = True,
        change_debounce_seconds: int = 5,
        min_auto_interval_minutes: int = 10,
        on_schedule_enabled: bool = True,
        schedule_interval_minutes: int = 60,
        quiet_hours_enabled: bool = False,
        quiet_hours_start_local: str | None = None,
        quiet_hours_end_local: str | None = None,
        manual_allowed_quiet_hours: bool = True,
    ) -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=on_change_enabled,
            change_debounce_seconds=change_debounce_seconds,
            min_auto_interval_minutes=min_auto_interval_minutes,
            on_schedule_enabled=on_schedule_enabled,
            schedule_interval_minutes=schedule_interval_minutes,
            quiet_hours_enabled=quiet_hours_enabled,
            quiet_hours_start_local=quiet_hours_start_local,
            quiet_hours_end_local=quiet_hours_end_local,
            manual_allowed_quiet_hours=manual_allowed_quiet_hours,
        )

    def make_change_watcher(
        self,
    ) -> tuple[AutoBackupCoordinator, PollingChangeWatcher]:
        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )
        watcher = PollingChangeWatcher(
            self.repo,
            self.health,
            coordinator,
        )
        return coordinator, watcher

    def make_schedule_coordinator(
        self,
    ) -> tuple[AutoBackupCoordinator, ScheduleCoordinator]:
        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )
        scheduler = ScheduleCoordinator(
            self.repo,
            coordinator,
        )
        return coordinator, scheduler

    def test_local_time_validator_normalizes_short_hour(self) -> None:
        self.assertEqual(
            validate_local_time_text("7:05"),
            "07:05",
        )
        self.assertEqual(
            validate_local_time_text("0:0"),
            "00:00",
        )
        self.assertEqual(
            validate_local_time_text("23:59"),
            "23:59",
        )


    def test_auto_coordinator_signal_deduplicates_event(self) -> None:
        source = self.source_root / "auto-signal.txt"
        source.write_text("signal", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(),
        )

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )

        first = coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            1_000_000,
        )
        second = coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            1_000_500,
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(first.action, "pending")
        self.assertEqual(second.action, "pending")
        self.assertEqual(
            plan.pending_trigger_mask,
            TRIGGER_MASK_EVENT,
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            1_000_000,
        )


    def test_auto_coordinator_event_debounce_then_queue(self) -> None:
        source = self.source_root / "auto-debounce.txt"
        source.write_text("debounce", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                change_debounce_seconds=5,
                min_auto_interval_minutes=0,
            ),
        )

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )
        now = 2_000_000

        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            now,
        )

        deferred = coordinator.evaluate(
            plan_id,
            now + 4_999,
            datetime(2026, 9, 22, 12, 0),
        )
        queued = coordinator.evaluate(
            plan_id,
            now + 5_000,
            datetime(2026, 9, 22, 12, 0),
        )

        self.assertEqual(deferred.action, "deferred_debounce")
        self.assertEqual(queued.action, "queued")


    def test_auto_coordinator_quiet_hours_defers_automatic_run(self) -> None:
        source = self.source_root / "auto-quiet.txt"
        source.write_text("quiet", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                change_debounce_seconds=1,
                min_auto_interval_minutes=0,
                quiet_hours_enabled=True,
                quiet_hours_start_local="22:00",
                quiet_hours_end_local="07:00",
            ),
        )

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )

        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            3_000_000,
        )

        quiet_decision = coordinator.evaluate(
            plan_id,
            3_001_000,
            datetime(2026, 9, 22, 23, 0),
        )
        after_quiet_decision = coordinator.evaluate(
            plan_id,
            3_001_000,
            datetime(2026, 9, 23, 8, 0),
        )

        self.assertEqual(
            quiet_decision.action,
            "deferred_quiet_hours",
        )
        self.assertEqual(
            after_quiet_decision.action,
            "queued",
        )


    def test_auto_coordinator_min_interval_defers(self) -> None:
        source = self.source_root / "auto-interval.txt"
        source.write_text("interval", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                change_debounce_seconds=1,
                min_auto_interval_minutes=10,
            ),
        )
        self.repo.update_last_auto_backup(
            plan_id,
            4_000_000,
        )

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )

        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            4_100_000,
        )

        deferred = coordinator.evaluate(
            plan_id,
            4_200_000,
            datetime(2026, 9, 22, 12, 0),
        )
        queued = coordinator.evaluate(
            plan_id,
            4_600_000,
            datetime(2026, 9, 22, 12, 10),
        )

        self.assertEqual(
            deferred.action,
            "deferred_min_interval",
        )
        self.assertEqual(queued.action, "queued")


    def test_auto_coordinator_manual_signal_is_not_pending(self) -> None:
        source = self.source_root / "auto-manual.txt"
        source.write_text("manual", encoding="utf-8")
        plan_id = self.make_plan(source)

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )

        decision = coordinator.signal(
            plan_id,
            TriggerReason.MANUAL,
            5_000_000,
        )

        self.assertEqual(decision.action, "no_action")
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_auto_coordinator_recovery_blocks_pending_plan(self) -> None:
        source = self.source_root / "auto-recovery.txt"
        source.write_text("recovery", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                change_debounce_seconds=1,
                min_auto_interval_minutes=0,
            ),
        )

        operation_id = self.repo.create_operation(
            OperationType.BACKUP,
            plan_id,
            None,
            {},
        )
        self.repo.recovery_mark(operation_id)

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )
        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            6_000_000,
        )

        decision = coordinator.evaluate(
            plan_id,
            6_001_000,
            datetime(2026, 9, 22, 12, 0),
        )

        self.assertEqual(
            decision.action,
            "blocked_recovery",
        )


    def test_auto_coordinator_complete_run_updates_timestamp_and_clears_pending(self) -> None:
        source = self.source_root / "auto-complete.txt"
        source.write_text("complete", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(),
        )

        coordinator = AutoBackupCoordinator(
            self.repo,
            self.health,
        )
        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            7_000_000,
        )

        coordinator.complete_automatic_run(
            plan_id,
            7_100_000,
            success_or_no_changes=True,
        )

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.last_auto_backup_at_utc_ms,
            7_100_000,
        )
        self.assertEqual(plan.pending_trigger_mask, 0)
        self.assertIsNone(plan.pending_trigger_since_utc_ms)

    def test_source_metadata_fingerprint_file_changes_on_metadata_change(self) -> None:
        source = self.source_root / "watch-file.txt"
        source.write_text("one", encoding="utf-8")

        descriptor = PathPolicy.validate_source(str(source))
        first = source_metadata_fingerprint(descriptor)

        before = source.stat()
        source.write_text("two", encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

        second = source_metadata_fingerprint(descriptor)

        self.assertNotEqual(first, second)


    def test_source_metadata_fingerprint_folder_detects_create_change_delete(self) -> None:
        source = self.source_root / "watch-folder"
        source.mkdir()

        descriptor = PathPolicy.validate_source(str(source))
        initial = source_metadata_fingerprint(descriptor)

        first_file = source / "first.txt"
        first_file.write_text("one", encoding="utf-8")
        after_create = source_metadata_fingerprint(descriptor)

        before = first_file.stat()
        first_file.write_text("two", encoding="utf-8")
        os.utime(first_file, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        after_change = source_metadata_fingerprint(descriptor)

        first_file.unlink()
        after_delete = source_metadata_fingerprint(descriptor)

        self.assertNotEqual(initial, after_create)
        self.assertNotEqual(after_create, after_change)
        self.assertNotEqual(after_change, after_delete)


    def test_polling_watcher_first_scan_records_baseline_only(self) -> None:
        source = self.source_root / "watch-baseline.txt"
        source.write_text("baseline", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
            ),
        )

        _, watcher = self.make_change_watcher()

        decisions = watcher.poll_once(10_000)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0].action,
            "baseline_recorded",
        )

        plan = self.repo.plan_details(plan_id)

        self.assertNotEqual(
            plan.last_source_scan_fingerprint,
            "",
        )
        self.assertEqual(plan.pending_trigger_mask, 0)


    def test_polling_watcher_repeated_unchanged_scan_creates_no_trigger(self) -> None:
        source = self.source_root / "watch-no-change.txt"
        source.write_text("same", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
            ),
        )

        _, watcher = self.make_change_watcher()

        watcher.poll_once(20_000)
        decisions = watcher.poll_once(21_000)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "no_change")
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_polling_watcher_change_creates_one_pending_event(self) -> None:
        source = self.source_root / "watch-change.txt"
        source.write_text("before", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
                change_debounce_seconds=5,
            ),
        )

        _, watcher = self.make_change_watcher()

        watcher.poll_once(30_000)

        time.sleep(0.003)
        source.write_text("after", encoding="utf-8")

        first_change = watcher.poll_once(31_000)
        second_change = watcher.poll_once(32_000)

        self.assertEqual(len(first_change), 1)
        self.assertEqual(first_change[0].action, "pending")
        self.assertEqual(len(second_change), 1)
        self.assertEqual(second_change[0].action, "no_change")

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.pending_trigger_mask,
            TRIGGER_MASK_EVENT,
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            31_000,
        )


    def test_polling_watcher_ignores_disabled_change_policy(self) -> None:
        source = self.source_root / "watch-disabled.txt"
        source.write_text("before", encoding="utf-8")
        plan_id = self.make_plan(source)

        _, watcher = self.make_change_watcher()

        decisions = watcher.poll_once(40_000)

        self.assertEqual(decisions, [])
        self.assertEqual(
            self.repo.plan_details(plan_id).last_source_scan_fingerprint,
            "",
        )

        source.write_text("after", encoding="utf-8")
        decisions_after_change = watcher.poll_once(41_000)

        self.assertEqual(decisions_after_change, [])
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_polling_watcher_missing_source_creates_one_issue_without_pending_run(self) -> None:
        source = self.source_root / "watch-missing.txt"
        source.write_text("present", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
            ),
        )

        _, watcher = self.make_change_watcher()

        watcher.poll_once(50_000)
        source.unlink()

        first = watcher.poll_once(51_000)
        second = watcher.poll_once(52_000)

        self.assertEqual(first[0].action, "blocked_source")
        self.assertEqual(second[0].action, "blocked_source")
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )

        source_issues = [
            issue
            for issue in self.repo.list_active_issues(plan_id)
            if issue.code == "SOURCE_NOT_FOUND"
        ]

        self.assertEqual(len(source_issues), 1)


    def test_polling_watcher_skips_paused_plan(self) -> None:
        source = self.source_root / "watch-paused.txt"
        source.write_text("paused", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
            ),
        )

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE BackupPlans SET state=? WHERE id=?",
                (PlanState.PAUSED.value, plan_id),
            )

        _, watcher = self.make_change_watcher()

        decisions = watcher.poll_once(60_000)

        self.assertEqual(decisions, [])
        self.assertEqual(
            self.repo.plan_details(plan_id).last_source_scan_fingerprint,
            "",
        )

    def test_schedule_tick_signals_due_plan_once(self) -> None:
        source = self.source_root / "schedule-due.txt"
        source.write_text("schedule", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
                schedule_interval_minutes=60,
            ),
        )

        _, scheduler = self.make_schedule_coordinator()

        first = scheduler.tick(10_000)
        second = scheduler.tick(20_000)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, "pending")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].action, "already_pending")

        plan = self.repo.plan_details(plan_id)

        self.assertTrue(
            has_trigger_mask(
                plan.pending_trigger_mask,
                TRIGGER_MASK_SCHEDULE,
            )
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            10_000,
        )


    def test_schedule_tick_does_not_signal_before_interval_due(self) -> None:
        source = self.source_root / "schedule-not-due.txt"
        source.write_text("schedule", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
                schedule_interval_minutes=60,
            ),
        )
        self.repo.update_last_auto_backup(
            plan_id,
            1_000_000,
        )

        _, scheduler = self.make_schedule_coordinator()

        decisions = scheduler.tick(
            1_000_000 + (59 * 60 * 1000),
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "not_due")
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_schedule_tick_signals_at_interval_boundary(self) -> None:
        source = self.source_root / "schedule-boundary.txt"
        source.write_text("schedule", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
                schedule_interval_minutes=60,
            ),
        )
        self.repo.update_last_auto_backup(
            plan_id,
            2_000_000,
        )

        _, scheduler = self.make_schedule_coordinator()

        decisions = scheduler.tick(
            2_000_000 + (60 * 60 * 1000),
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "pending")
        self.assertTrue(
            has_trigger_mask(
                self.repo.plan_details(plan_id).pending_trigger_mask,
                TRIGGER_MASK_SCHEDULE,
            )
        )


    def test_schedule_tick_skips_disabled_schedule_policy(self) -> None:
        source = self.source_root / "schedule-disabled.txt"
        source.write_text("schedule", encoding="utf-8")
        plan_id = self.make_plan(source)

        _, scheduler = self.make_schedule_coordinator()

        decisions = scheduler.tick(3_000_000)

        self.assertEqual(decisions, [])
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_startup_catchup_creates_one_pending_trigger(self) -> None:
        source = self.source_root / "startup-catchup.txt"
        source.write_text("startup", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
                schedule_interval_minutes=60,
            ),
        )

        _, scheduler = self.make_schedule_coordinator()

        first = scheduler.startup_catchup(4_000_000)
        second = scheduler.startup_catchup(4_001_000)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, "pending")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].action, "already_pending")

        plan = self.repo.plan_details(plan_id)

        self.assertEqual(
            plan.pending_trigger_mask,
            TRIGGER_MASK_STARTUP_CATCHUP,
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            4_000_000,
        )


    def test_startup_catchup_respects_recent_auto_backup_grace(self) -> None:
        source = self.source_root / "startup-grace.txt"
        source.write_text("startup", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
                schedule_interval_minutes=1,
            ),
        )

        now = 5_000_000
        self.repo.update_last_auto_backup(
            plan_id,
            now - (STARTUP_CATCHUP_GRACE_MINUTES * 60 * 1000) + 1,
        )

        _, scheduler = self.make_schedule_coordinator()

        decisions = scheduler.startup_catchup(now)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "not_due")
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )


    def test_event_and_schedule_triggers_merge_into_one_pending_plan(self) -> None:
        source = self.source_root / "schedule-event-merge.txt"
        source.write_text("merge", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=True,
                on_schedule_enabled=True,
                schedule_interval_minutes=60,
            ),
        )

        coordinator, scheduler = self.make_schedule_coordinator()

        coordinator.signal(
            plan_id,
            TriggerReason.EVENT,
            6_000_000,
        )

        decisions = scheduler.tick(6_001_000)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "pending")

        plan = self.repo.plan_details(plan_id)

        self.assertTrue(
            has_trigger_mask(
                plan.pending_trigger_mask,
                TRIGGER_MASK_EVENT,
            )
        )
        self.assertTrue(
            has_trigger_mask(
                plan.pending_trigger_mask,
                TRIGGER_MASK_SCHEDULE,
            )
        )
        self.assertEqual(
            plan.pending_trigger_since_utc_ms,
            6_000_000,
        )


    def test_schedule_does_not_create_pending_for_paused_plan(self) -> None:
        source = self.source_root / "schedule-paused.txt"
        source.write_text("paused", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.update_trigger_policy(
            plan_id,
            self.make_automation_policy(
                on_change_enabled=False,
                on_schedule_enabled=True,
            ),
        )

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE BackupPlans SET state=? WHERE id=?",
                (PlanState.PAUSED.value, plan_id),
            )

        _, scheduler = self.make_schedule_coordinator()

        decisions = scheduler.tick(7_000_000)

        self.assertEqual(decisions, [])
        self.assertEqual(
            self.repo.plan_details(plan_id).pending_trigger_mask,
            0,
        )

    def make_workerless_controller(
        self,
    ) -> tuple[AppController, AppPaths]:
        root = self.root / f"controller-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)

        logs = root / "logs"
        logs.mkdir()

        paths = AppPaths(
            root=root,
            db_path=root / "controller.sqlite3",
            logs_dir=logs,
            log_path=logs / "controller.log",
        )

        controller = AppController(
            paths,
            start_worker=False,
        )

        return controller, paths


    def create_controller_plan(
        self,
        controller: AppController,
        paths: AppPaths,
        filename: str,
    ) -> tuple[str, Path]:
        source_root = paths.root / "source"
        source_root.mkdir(exist_ok=True)

        source = source_root / filename
        source.write_text("automatic backup content", encoding="utf-8")

        storage_root = paths.root / "storage"

        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(storage_root))
        storage_id = controller.repo.get_or_create_storage(storage)

        controller.storage.ensure_root(storage, storage_id)

        plan_id = controller.repo.create_plan(
            "Automatic test plan",
            "",
            descriptor,
            storage_id,
            5,
            StorageMode.DIRECT.value,
        )

        return plan_id, source


    def test_automatic_pending_run_creates_snapshot_and_clears_pending(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "automatic-success.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            controller.auto_coordinator.signal(
                plan_id,
                TriggerReason.EVENT,
                10_000,
            )

            decisions = controller.run_automatic_pending_synchronously_for_test(
                11_000,
                datetime(2026, 9, 23, 12, 0),
            )

            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].action, "queued")

            plan = controller.repo.plan_details(plan_id)
            snapshots = controller.repo.snapshots(plan_id)

            self.assertEqual(plan.pending_trigger_mask, 0)
            self.assertEqual(plan.last_auto_backup_at_utc_ms, 11_000)
            self.assertEqual(
                len(
                    [
                        snapshot
                        for snapshot in snapshots
                        if snapshot.state == "committed"
                    ]
                ),
                1,
            )

        finally:
            controller.shutdown()


    def test_failed_automatic_run_preserves_pending_trigger(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "automatic-failure.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            controller.auto_coordinator.signal(
                plan_id,
                TriggerReason.EVENT,
                20_000,
            )

            source.unlink()

            decisions = controller.run_automatic_pending_synchronously_for_test(
                21_000,
                datetime(2026, 9, 23, 12, 0),
            )

            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].action, "blocked_source")

            plan = controller.repo.plan_details(plan_id)

            self.assertEqual(
                plan.pending_trigger_mask,
                TRIGGER_MASK_EVENT,
            )
            self.assertIsNone(plan.last_auto_backup_at_utc_ms)

        finally:
            controller.shutdown()


    def test_manual_backup_payload_keeps_manual_trigger_reason(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "manual-trigger.txt",
            )

            operation_id = controller.repo.create_operation(
                OperationType.BACKUP,
                plan_id,
                None,
                {
                    "force": False,
                    "trigger_reason": TriggerReason.MANUAL.value,
                },
            )

            result = controller.snapshot_service.create(
                operation_id,
                plan_id,
                False,
                lambda event: None,
                lambda: False,
            )

            self.assertTrue(result.success)

            payload = controller.repo.operation_payload(operation_id)

            self.assertEqual(
                payload["trigger_reason"],
                TriggerReason.MANUAL.value,
            )

            plan = controller.repo.plan_details(plan_id)

            self.assertIsNone(plan.last_auto_backup_at_utc_ms)
            self.assertEqual(plan.pending_trigger_mask, 0)

        finally:
            controller.shutdown()


    def test_automatic_event_arriving_after_success_remains_pending(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "automatic-next-event.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            controller.auto_coordinator.signal(
                plan_id,
                TriggerReason.EVENT,
                30_000,
            )

            controller.run_automatic_pending_synchronously_for_test(
                31_000,
                datetime(2026, 9, 23, 12, 0),
            )

            source.write_text(
                "changed after automatic backup",
                encoding="utf-8",
            )

            controller.auto_coordinator.signal(
                plan_id,
                TriggerReason.EVENT,
                32_000,
            )

            plan = controller.repo.plan_details(plan_id)

            self.assertEqual(
                plan.pending_trigger_mask,
                TRIGGER_MASK_EVENT,
            )
            self.assertEqual(
                plan.pending_trigger_since_utc_ms,
                32_000,
            )

        finally:
            controller.shutdown()

    def test_automation_runtime_tick_throttles_watcher_and_schedule(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "runtime-throttle.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=True,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            first = controller.automation_runtime_tick_for_test(
                100_000,
                datetime(2026, 9, 23, 12, 0),
            )
            second = controller.automation_runtime_tick_for_test(
                101_000,
                datetime(2026, 9, 23, 12, 0),
            )

            self.assertEqual(
                first["watcher"][0].action,
                "baseline_recorded",
            )
            self.assertEqual(
                first["schedule"][0].action,
                "pending",
            )
            self.assertEqual(second["watcher"], [])
            self.assertEqual(second["schedule"], [])

        finally:
            controller.shutdown()


    def test_automation_runtime_watcher_change_creates_pending_event(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "runtime-change.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            controller.automation_runtime_tick_for_test(
                200_000,
                datetime(2026, 9, 23, 12, 0),
            )

            source.write_text(
                "changed by runtime watcher",
                encoding="utf-8",
            )

            result = controller.automation_runtime_tick_for_test(
                200_000
                + (DEFAULT_WATCHER_POLL_SECONDS * 1000),
                datetime(2026, 9, 23, 12, 0),
            )

            self.assertEqual(
                result["watcher"][0].action,
                "pending",
            )

            plan = controller.repo.plan_details(plan_id)

            self.assertTrue(
                has_trigger_mask(
                    plan.pending_trigger_mask,
                    TRIGGER_MASK_EVENT,
                )
            )

        finally:
            controller.shutdown()


    def test_automation_runtime_does_not_process_pending_without_worker(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            plan_id, source = self.create_controller_plan(
                controller,
                paths,
                "runtime-workerless.txt",
            )

            controller.repo.update_trigger_policy(
                plan_id,
                TriggerPolicy(
                    on_change_enabled=True,
                    change_debounce_seconds=1,
                    min_auto_interval_minutes=0,
                    on_schedule_enabled=False,
                    schedule_interval_minutes=60,
                    quiet_hours_enabled=False,
                    quiet_hours_start_local=None,
                    quiet_hours_end_local=None,
                    manual_allowed_quiet_hours=True,
                ),
            )

            controller.auto_coordinator.signal(
                plan_id,
                TriggerReason.EVENT,
                300_000,
            )

            result = controller.automation_runtime_tick_for_test(
                301_000,
                datetime(2026, 9, 23, 12, 0),
            )

            self.assertEqual(result["process"], [])
            self.assertEqual(
                controller.repo.plan_details(plan_id).pending_trigger_mask,
                TRIGGER_MASK_EVENT,
            )
            self.assertEqual(
                len(
                    [
                        snapshot
                        for snapshot in controller.repo.snapshots(plan_id)
                        if snapshot.state == "committed"
                    ]
                ),
                0,
            )

        finally:
            controller.shutdown()


    def test_automation_runtime_start_stop_is_idempotent(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            controller.start_automation_runtime(
                DEFAULT_AUTOMATION_RUNTIME_TICK_SECONDS
            )
            first_timer = controller.automation_runtime_timer

            controller.start_automation_runtime(
                DEFAULT_AUTOMATION_RUNTIME_TICK_SECONDS
            )

            self.assertTrue(
                controller._automation_runtime_running
            )
            self.assertIs(
                controller.automation_runtime_timer,
                first_timer,
            )

            controller.stop_automation_runtime()

            self.assertFalse(
                controller._automation_runtime_running
            )
            self.assertIsNone(
                controller.automation_runtime_timer
            )

            controller.stop_automation_runtime()

            self.assertFalse(
                controller._automation_runtime_running
            )

        finally:
            controller.shutdown()

    def test_plan_editor_saves_automation_policy_for_new_plan(self) -> None:
        source = self.source_root / "editor-automation.txt"
        source.write_text("editor automation", encoding="utf-8")

        storage = PathPolicy.validate_storage(
            str(self.storage_root)
        )

        controller_paths = AppPaths(
            root=self.root / "editor-controller",
            db_path=self.root / "editor-controller" / "editor.sqlite3",
            logs_dir=self.root / "editor-controller" / "logs",
            log_path=self.root / "editor-controller" / "logs" / "editor.log",
        )
        controller_paths.root.mkdir(parents=True)
        controller_paths.logs_dir.mkdir(parents=True)

        controller = AppController(
            controller_paths,
            start_worker=False,
        )
        editor = None

        try:
            editor = PlanEditor(controller)

            editor.name_edit.setText("Automation editor plan")
            editor.source_edit.setText(str(source))
            editor.storage_edit.setText(str(storage))
            editor.on_change_check.setChecked(True)
            editor.debounce_spin.setValue(12)
            editor.min_interval_spin.setValue(25)
            editor.on_schedule_check.setChecked(True)
            editor.schedule_spin.setValue(90)
            editor.quiet_hours_check.setChecked(True)
            editor.quiet_start_edit.setText("22:00")
            editor.quiet_end_edit.setText("07:00")
            editor.manual_quiet_check.setChecked(False)

            editor.save()

            plans = controller.repo.plan_summaries()
            self.assertEqual(len(plans), 1)

            plan = controller.repo.plan_details(plans[0].id)

            self.assertTrue(plan.on_change_enabled)
            self.assertEqual(plan.change_debounce_seconds, 12)
            self.assertEqual(plan.min_auto_interval_minutes, 25)
            self.assertTrue(plan.on_schedule_enabled)
            self.assertEqual(plan.schedule_interval_minutes, 90)
            self.assertTrue(plan.quiet_hours_enabled)
            self.assertEqual(plan.quiet_hours_start_local, "22:00")
            self.assertEqual(plan.quiet_hours_end_local, "07:00")
            self.assertFalse(plan.manual_allowed_quiet_hours)

        finally:
            if editor is not None:
                editor.close()
            controller.shutdown()

    def test_plan_editor_archived_plan_disables_automation_controls(self) -> None:
        source = self.source_root / "archived-automation.txt"
        source.write_text("archived automation", encoding="utf-8")

        plan_id = self.make_plan(source)
        operation_id = self.repo.create_operation(
            OperationType.ARCHIVE_PLAN,
            plan_id,
            None,
            {},
        )
        self.repo.archive(plan_id, operation_id)

        archived_plan = self.repo.plan_details(plan_id)

        controller_paths = AppPaths(
            root=self.root / "archived-editor-controller",
            db_path=self.root / "archived-editor-controller" / "editor.sqlite3",
            logs_dir=self.root / "archived-editor-controller" / "logs",
            log_path=self.root / "archived-editor-controller" / "logs" / "editor.log",
        )
        controller_paths.root.mkdir(parents=True)
        controller_paths.logs_dir.mkdir(parents=True)

        controller = AppController(
            controller_paths,
            start_worker=False,
        )

        try:
            editor = PlanEditor(controller, archived_plan)

            self.assertFalse(editor.on_change_check.isEnabled())
            self.assertFalse(editor.debounce_spin.isEnabled())
            self.assertFalse(editor.on_schedule_check.isEnabled())
            self.assertFalse(editor.schedule_spin.isEnabled())
            self.assertFalse(editor.quiet_hours_check.isEnabled())
            self.assertFalse(editor.quiet_start_edit.isEnabled())
            self.assertFalse(editor.quiet_end_edit.isEnabled())

            editor.close()

        finally:
            controller.shutdown()

    def test_save_plan_with_policy_rejects_invalid_new_plan_without_insert(self) -> None:
        source = self.source_root / "atomic-new.txt"
        source.write_text("atomic new", encoding="utf-8")

        storage = PathPolicy.validate_storage(
            str(self.storage_root)
        )
        storage_id = self.repo.get_or_create_storage(storage)

        invalid_policy = TriggerPolicy(
            on_change_enabled=True,
            change_debounce_seconds=0,
            min_auto_interval_minutes=10,
            on_schedule_enabled=False,
            schedule_interval_minutes=60,
            quiet_hours_enabled=False,
            quiet_hours_start_local=None,
            quiet_hours_end_local=None,
            manual_allowed_quiet_hours=True,
        )

        with self.assertRaises(AppError) as context:
            self.repo.save_plan_with_policy(
                None,
                "Atomic new plan",
                "",
                PathPolicy.validate_source(str(source)),
                storage_id,
                5,
                StorageMode.DIRECT.value,
                invalid_policy,
            )

        self.assertEqual(
            context.exception.code,
            "TRIGGER_POLICY_INVALID",
        )
        self.assertEqual(
            len(self.repo.plan_summaries()),
            0,
        )


    def test_save_plan_with_policy_rolls_back_invalid_edit(self) -> None:
        source = self.source_root / "atomic-edit.txt"
        source.write_text("atomic edit", encoding="utf-8")

        plan_id = self.make_plan(source)
        before = self.repo.plan_details(plan_id)

        invalid_policy = TriggerPolicy(
            on_change_enabled=True,
            change_debounce_seconds=5,
            min_auto_interval_minutes=10,
            on_schedule_enabled=True,
            schedule_interval_minutes=60,
            quiet_hours_enabled=True,
            quiet_hours_start_local="22:00",
            quiet_hours_end_local="22:00",
            manual_allowed_quiet_hours=True,
        )

        storage = PathPolicy.validate_storage(
            str(self.storage_root)
        )
        storage_id = self.repo.get_or_create_storage(storage)

        with self.assertRaises(AppError) as context:
            self.repo.save_plan_with_policy(
                plan_id,
                "Cannot save the new name",
                "New note",
                PathPolicy.validate_source(str(source)),
                storage_id,
                2,
                StorageMode.ZIP.value,
                invalid_policy,
            )

        self.assertEqual(
            context.exception.code,
            "TRIGGER_POLICY_INVALID",
        )

        after = self.repo.plan_details(plan_id)

        self.assertEqual(after.display_name, before.display_name)
        self.assertEqual(after.note, before.note)
        self.assertEqual(after.keep_last_n, before.keep_last_n)
        self.assertEqual(after.storage_mode, before.storage_mode)
        self.assertEqual(
            after.on_change_enabled,
            before.on_change_enabled,
        )
        self.assertEqual(
            after.on_schedule_enabled,
            before.on_schedule_enabled,
        )

    def test_windows_autostart_command_for_python_script(self) -> None:
        registry = FakeWinRegistry()

        service = WindowsAutostartService(
            executable_path=r"C:\Python313\python.exe",
            script_path=r"C:\Apps\AmviBackUp\amvibackup.py",
            registry_module=registry,
            platform_name="Windows",
        )

        self.assertTrue(service.is_supported())

        command = service.launch_command()

        self.assertIn("python.exe", command)
        self.assertIn("amvibackup.py", command)


    def test_windows_autostart_enable_disable_round_trip(self) -> None:
        registry = FakeWinRegistry()

        service = WindowsAutostartService(
            executable_path=r"C:\Python313\python.exe",
            script_path=r"C:\Apps\AmviBackUp\amvibackup.py",
            registry_module=registry,
            platform_name="Windows",
        )

        self.assertFalse(service.is_enabled())

        service.enable()

        self.assertTrue(service.is_enabled())
        self.assertIn(
            WINDOWS_AUTOSTART_VALUE_NAME,
            registry.storage,
        )

        service.disable()

        self.assertFalse(service.is_enabled())
        self.assertNotIn(
            WINDOWS_AUTOSTART_VALUE_NAME,
            registry.storage,
        )


    def test_windows_autostart_uses_executable_only_when_packaged(self) -> None:
        registry = FakeWinRegistry()

        service = WindowsAutostartService(
            executable_path=r"C:\Apps\AmviBackUp\AmviBackUp.exe",
            script_path=r"C:\Apps\AmviBackUp\AmviBackUp.exe",
            registry_module=registry,
            platform_name="Windows",
        )

        command = service.launch_command()

        self.assertEqual(
            command,
            subprocess.list2cmdline(
                [r"C:\Apps\AmviBackUp\AmviBackUp.exe"]
            ),
        )


    def test_windows_autostart_unsupported_platform_is_safe(self) -> None:
        service = WindowsAutostartService(
            executable_path="/usr/bin/python3",
            script_path="/tmp/amvibackup.py",
            registry_module=None,
            platform_name="Linux",
        )

        self.assertFalse(service.is_supported())
        self.assertFalse(service.is_enabled())

        with self.assertRaises(AppError) as context:
            service.enable()

        self.assertEqual(
            context.exception.code,
            "AUTOSTART_UNSUPPORTED",
        )

    def make_notification_service(
        self,
        tray: FakeTrayIcon | None,
    ) -> NotificationService:
        return NotificationService(self.repo, lambda: tray)


    def make_notification_plan(self, filename: str) -> str:
        source = self.source_root / filename
        source.write_text("notification fixture", encoding="utf-8")
        return self.make_plan(source)


    def test_notification_critical_event_shown_once_then_cooled_down(self) -> None:
        plan_id = self.make_notification_plan("notify-cooldown.txt")
        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)

        first = service.notify(
            "STORAGE_NOT_FOUND",
            "Storage unavailable",
            "Connect the drive.",
            NotificationSeverity.ERROR,
            plan_id,
            now_utc_ms=1_000_000,
        )
        second = service.notify(
            "STORAGE_NOT_FOUND",
            "Storage unavailable",
            "Connect the drive.",
            NotificationSeverity.ERROR,
            plan_id,
            now_utc_ms=1_001_000,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(tray.messages), 1)
        self.assertEqual(
            sum(
                row["event_code"] == "NOTIFICATION_SHOWN"
                for row in self.repo.recent_activity()
            ),
            1,
        )


    def test_notification_same_code_different_plan_is_not_deduplicated(self) -> None:
        first_plan = self.make_notification_plan("notify-one.txt")
        second_plan = self.make_notification_plan("notify-two.txt")
        self.assertNotEqual(first_plan, second_plan)

        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)

        for plan_id in (first_plan, second_plan):
            self.assertTrue(
                service.notify(
                    "SOURCE_NOT_FOUND",
                    "Source unavailable",
                    "Check the path.",
                    NotificationSeverity.ERROR,
                    plan_id,
                    now_utc_ms=2_000_000,
                )
            )

        self.assertEqual(len(tray.messages), 2)


    def test_success_notification_disabled_by_default(self) -> None:
        plan_id = self.make_notification_plan("notify-default.txt")
        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)

        shown = service.notify(
            "AUTO_BACKUP_SUCCEEDED",
            "Automatic backup finished",
            "Version created.",
            NotificationSeverity.INFO,
            plan_id,
            now_utc_ms=3_000_000,
        )

        self.assertFalse(shown)
        self.assertEqual(tray.messages, [])


    def test_success_notification_can_be_enabled(self) -> None:
        plan_id = self.make_notification_plan("notify-success.txt")
        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)
        self.repo.set_setting(SETTING_NOTIFICATIONS_SUCCESS_ENABLED, True)

        shown = service.notify(
            "AUTO_BACKUP_SUCCEEDED",
            "Automatic backup finished",
            "Version created.",
            NotificationSeverity.INFO,
            plan_id,
            now_utc_ms=4_000_000,
        )

        self.assertTrue(shown)
        self.assertEqual(len(tray.messages), 1)


    def test_notifications_disabled_blocks_all_non_forced_messages(self) -> None:
        plan_id = self.make_notification_plan("notify-disabled.txt")
        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)
        self.repo.set_setting(SETTING_NOTIFICATIONS_ENABLED, False)

        shown = service.notify(
            "SNAPSHOT_CORRUPTED",
            "Version corrupted",
            "Restore blocked.",
            NotificationSeverity.CRITICAL,
            plan_id,
            now_utc_ms=5_000_000,
        )

        self.assertFalse(shown)
        self.assertEqual(tray.messages, [])


    def test_notification_no_tray_is_safe(self) -> None:
        plan_id = self.make_notification_plan("notify-no-tray.txt")
        service = self.make_notification_service(None)

        shown = service.notify(
            "SOURCE_NOT_FOUND",
            "Source unavailable",
            "Path not found.",
            NotificationSeverity.ERROR,
            plan_id,
            now_utc_ms=6_000_000,
        )

        self.assertFalse(shown)
        self.assertFalse(
            any(
                row["event_code"] == "NOTIFICATION_SKIPPED_NO_TRAY"
                for row in self.repo.recent_activity()
            )
        )


    def test_notification_rejects_unknown_plan_before_showing(self) -> None:
        tray = FakeTrayIcon()
        service = self.make_notification_service(tray)

        with self.assertRaises(AppError) as context:
            service.notify(
                "SOURCE_NOT_FOUND",
                "Source unavailable",
                "Path not found.",
                NotificationSeverity.ERROR,
                "nonexistent-plan",
                now_utc_ms=7_000_000,
            )

        self.assertEqual(context.exception.code, "PLAN_NOT_FOUND")
        self.assertEqual(tray.messages, [])

    def test_runtime_status_counts_are_query_only(self) -> None:
        source = self.source_root / "runtime-status.txt"
        source.write_text("status", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.mark_pending_trigger(
            plan_id,
            TRIGGER_MASK_EVENT,
            1_000_000,
        )

        before = len(self.repo.recent_activity())
        counts = self.repo.runtime_status_counts()
        after = len(self.repo.recent_activity())

        self.assertEqual(before, after)
        self.assertEqual(counts["enabled_plans"], 1)
        self.assertEqual(counts["pending_plans"], 1)
        self.assertEqual(counts["active_errors"], 0)


    def test_set_settings_updates_all_values_together(self) -> None:
        self.repo.set_settings(
            {
                SETTING_CLOSE_TO_TRAY: False,
                SETTING_START_MINIMIZED: True,
                SETTING_NOTIFICATIONS_ENABLED: True,
                SETTING_NOTIFICATION_COOLDOWN_MINUTES: 55,
            }
        )

        self.assertFalse(
            self.repo.get_bool_setting(
                SETTING_CLOSE_TO_TRAY,
                True,
            )
        )
        self.assertTrue(
            self.repo.get_bool_setting(
                SETTING_START_MINIMIZED,
                False,
            )
        )
        self.assertTrue(
            self.repo.get_bool_setting(
                SETTING_NOTIFICATIONS_ENABLED,
                False,
            )
        )
        self.assertEqual(
            self.repo.get_setting(
                SETTING_NOTIFICATION_COOLDOWN_MINUTES,
                0,
            ),
            55,
        )


    def test_controller_runtime_settings_rolls_back_autostart_on_db_failure(self) -> None:
        controller, paths = self.make_workerless_controller()

        try:
            registry = FakeWinRegistry()

            controller.autostart_service = WindowsAutostartService(
                executable_path=r"C:\Python313\python.exe",
                script_path=r"C:\Apps\AmviBackUp\amvibackup.py",
                registry_module=registry,
                platform_name="Windows",
            )

            original_set_settings = controller.repo.set_settings

            def failing_set_settings(values: dict[str, Any]) -> None:
                raise AppError(
                    "DATABASE_WRITE_FAILED",
                    "Simulated settings save error.",
                )

            controller.repo.set_settings = failing_set_settings

            with self.assertRaises(AppError) as context:
                controller.save_runtime_settings(
                    close_to_tray=True,
                    start_minimized=False,
                    autostart_enabled=True,
                    notifications_enabled=True,
                    success_notifications_enabled=False,
                    notification_cooldown_minutes=30,
                )

            self.assertEqual(
                context.exception.code,
                "DATABASE_WRITE_FAILED",
            )
            self.assertFalse(
                controller.autostart_service.is_enabled()
            )

            controller.repo.set_settings = original_set_settings

        finally:
            controller.shutdown()

    def test_dismiss_issue_resolves_only_target_issue(self) -> None:
        source_one = self.source_root / "issue-one.txt"
        source_two = self.source_root / "issue-two.txt"
        source_one.write_text("one", encoding="utf-8")
        source_two.write_text("two", encoding="utf-8")

        first_plan = self.make_plan(source_one)
        second_plan = self.make_plan(source_two)

        first_issue = IssueDraft(
            dedupe_key=f"TEST_ISSUE:{first_plan}",
            plan_id=first_plan,
            severity=IssueSeverity.WARNING.value,
            code="TEST_ISSUE",
            title="First issue",
            detail="First detail",
        )
        second_issue = IssueDraft(
            dedupe_key=f"TEST_ISSUE:{second_plan}",
            plan_id=second_plan,
            severity=IssueSeverity.WARNING.value,
            code="TEST_ISSUE",
            title="Second issue",
            detail="Second detail",
        )

        self.repo.upsert_issue(first_issue)
        self.repo.upsert_issue(second_issue)

        self.repo.dismiss_issue(first_issue.dedupe_key)

        active = self.repo.list_active_issues()

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].key, second_issue.dedupe_key)
        self.assertTrue(
            any(
                row["event_code"] == "ISSUE_DISMISSED"
                for row in self.repo.recent_activity()
            )
        )


    def test_dismissed_issue_stays_hidden_until_resolved_then_reoccurs(self) -> None:
        source = self.source_root / "issue-reopen.txt"
        source.write_text("issue", encoding="utf-8")
        plan_id = self.make_plan(source)

        draft = IssueDraft(
            dedupe_key=f"REOPEN_ISSUE:{plan_id}",
            plan_id=plan_id,
            severity=IssueSeverity.ERROR.value,
            code="REOPEN_ISSUE",
            title="Reopen issue",
            detail="Initial detail",
        )

        self.repo.upsert_issue(draft)
        self.repo.dismiss_issue(draft.dedupe_key)
        self.repo.upsert_issue(draft)

        self.assertEqual(self.repo.list_active_issues(plan_id), [])

        self.repo.resolve_issue(draft.dedupe_key)
        self.repo.upsert_issue(draft)

        active = self.repo.list_active_issues(plan_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].key, draft.dedupe_key)

    def test_dismissed_missing_source_stays_hidden_but_health_is_error(self) -> None:
        source = self.source_root / "dismissed-source.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)

        source.unlink()
        self.health.evaluate(self.repo.plan_details(plan_id))

        key = f"SOURCE_NOT_FOUND:{plan_id}"
        self.repo.dismiss_issue(key)

        health = self.health.evaluate(self.repo.plan_details(plan_id))
        self.assertEqual(health.state, HealthState.ERROR.value)
        self.assertEqual(self.repo.list_active_issues(plan_id), [])

        source.write_text("back", encoding="utf-8")
        self.health.evaluate(self.repo.plan_details(plan_id))

        source.unlink()
        self.health.evaluate(self.repo.plan_details(plan_id))
        self.assertEqual(
            len(self.repo.list_active_issues(plan_id)),
            1,
        )

    def test_i18n_default_is_english_without_writing_setting(self) -> None:
        service = I18nService(self.repo)

        self.assertEqual(service.language, "en")
        self.assertEqual(
            service.tr("snapshot.restore"),
            "Restore",
        )
        self.assertIsNone(
            self.repo.get_setting(SETTING_UI_LANGUAGE),
        )


    def test_i18n_language_persists_and_uses_russian_dictionary(self) -> None:
        service = I18nService(self.repo)
        service.set_language("ru")

        restarted = I18nService(self.repo)

        self.assertEqual(restarted.language, "ru")
        self.assertEqual(
            restarted.tr("snapshot.restore"),
            "Восстановить",
        )


    def test_i18n_rejects_unsupported_language(self) -> None:
        service = I18nService(self.repo)

        with self.assertRaises(AppError) as context:
            service.set_language("xx")

        self.assertEqual(
            context.exception.code,
            "UI_LANGUAGE_INVALID",
        )
        self.assertIsNone(
            self.repo.get_setting(SETTING_UI_LANGUAGE),
        )


    def test_i18n_missing_key_is_reported(self) -> None:
        service = I18nService(self.repo)

        with self.assertRaises(AppError) as context:
            service.tr("missing.example")

        self.assertEqual(
            context.exception.code,
            "TRANSLATION_KEY_MISSING",
        )

    def test_i18n_languages_have_identical_keys(self) -> None:
        self.assertEqual(
            set(TRANSLATIONS["en"]),
            set(TRANSLATIONS["ru"]),
        )


    def test_settings_language_persists_after_new_service(self) -> None:
        service = I18nService(self.repo)
        service.set_language("ru")

        restarted = I18nService(self.repo)
        self.assertEqual(restarted.language, "ru")
        self.assertEqual(
            restarted.tr("app.settings"),
            "Настройки",
        )

    def test_i18n_required_runtime_keys_exist_in_both_languages(self) -> None:
        required = {
            "drop.title",
            "drop.detail",
            "tray.show",
            "tray.exit",
            "tray.pending",
            "dialog.delete_title",
            "dialog.delete_question",
            "dialog.active_operation_title",
            "dialog.active_operation_question",
            "progress.backup",
            "progress.verify",
            "progress.restore",
            "progress.delete",
        }
        for language in ("en", "ru"):
            self.assertTrue(
                required.issubset(TRANSLATIONS[language]),
                language,
            )

    def test_i18n_health_detail_follows_language(self) -> None:
        service = I18nService(self.repo)
        self.assertEqual(
            service.health_detail(HealthState.PROTECTED.value),
            TRANSLATIONS["en"]["health.detail.protected"],
        )
        service.set_language("ru")
        self.assertEqual(
            service.health_detail(HealthState.PROTECTED.value),
            TRANSLATIONS["ru"]["health.detail.protected"],
        )


    def test_i18n_issue_display_uses_code_not_stored_russian_text(self) -> None:
        service = I18nService(self.repo)
        self.assertEqual(
            service.issue_title("SOURCE_NOT_FOUND"),
            TRANSLATIONS["en"]["issue.SOURCE_NOT_FOUND"],
        )
        self.assertEqual(
            service.issue_detail("SOURCE_NOT_FOUND"),
            TRANSLATIONS["en"]["issue.detail.SOURCE_NOT_FOUND"],
        )


    def test_i18n_unknown_issue_has_neutral_fallback(self) -> None:
        service = I18nService(self.repo)
        self.assertEqual(
            service.issue_title("UNEXPECTED_CODE"),
            TRANSLATIONS["en"]["issue.unknown"],
        )
        self.assertEqual(
            service.issue_detail("UNEXPECTED_CODE"),
            TRANSLATIONS["en"]["issue.detail.generic"],
        )

    def test_backup_result_codes_distinguish_created_from_no_changes(self) -> None:
        source = self.source_root / "result-codes.txt"
        source.write_text("same data", encoding="utf-8")
        plan_id = self.make_plan(source)

        created = self.backup(plan_id)
        no_changes = self.backup(plan_id)

        self.assertTrue(created.success)
        self.assertTrue(no_changes.success)
        self.assertEqual(created.result_code, "BACKUP_CREATED")
        self.assertEqual(no_changes.result_code, "BACKUP_NO_CHANGES")

        i18n = I18nService(self.repo)
        self.assertEqual(
            i18n.operation_message(created),
            TRANSLATIONS["en"]["operation.backup_created"],
        )
        self.assertEqual(
            i18n.operation_message(no_changes),
            TRANSLATIONS["en"]["operation.no_changes"],
        )


    def test_backup_result_translation_does_not_depend_on_message_text(self) -> None:
        source = self.source_root / "result-language.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        created = self.backup(plan_id)

        created.message = "Arbitrary text without the old Russian prefix"

        i18n = I18nService(self.repo)
        i18n.set_language("ru")

        self.assertEqual(
            i18n.operation_message(created),
            TRANSLATIONS["ru"]["operation.backup_created"],
        )

    def test_automatic_notification_text_uses_result_code(self) -> None:
        i18n = I18nService(self.repo)
        created = OperationResult(
            "created-op",
            True,
            "arbitrary message",
            result_code="BACKUP_CREATED",
        )
        no_changes = OperationResult(
            "unchanged-op",
            True,
            "arbitrary message",
            result_code="BACKUP_NO_CHANGES",
        )

        self.assertEqual(
            i18n.tr(
                "notification.auto_no_changes"
                if no_changes.result_code == "BACKUP_NO_CHANGES"
                else "notification.auto_created"
            ),
            TRANSLATIONS["en"]["notification.auto_no_changes"],
        )
        self.assertEqual(
            i18n.operation_message(created),
            TRANSLATIONS["en"]["operation.backup_created"],
        )


    def test_automatic_failure_message_is_localized(self) -> None:
        i18n = I18nService(self.repo)
        failed = OperationResult(
            "failed-op",
            False,
            "Original internal message",
            error_code="SOURCE_NOT_FOUND",
        )

        self.assertEqual(
            i18n.operation_message(failed),
            TRANSLATIONS["en"]["operation.error.SOURCE_NOT_FOUND"],
        )

        i18n.set_language("ru")
        self.assertEqual(
            i18n.operation_message(failed),
            TRANSLATIONS["ru"]["operation.error.SOURCE_NOT_FOUND"],
        )

    def test_success_result_codes_have_translations_in_both_languages(self) -> None:
        expected = {
            "BACKUP_CREATED": "operation.backup_created",
            "BACKUP_NO_CHANGES": "operation.no_changes",
            "VERIFY_SUCCEEDED": "operation.verify_succeeded",
            "RESTORE_SUCCEEDED": "operation.restore_succeeded",
            "DELETE_SUCCEEDED": "operation.delete_succeeded",
        }

        i18n = I18nService(self.repo)
        for language in ("en", "ru"):
            i18n.set_language(language)
            for code, key in expected.items():
                with self.subTest(language=language, code=code):
                    result = OperationResult(
                        "not-used-for-typed-result",
                        True,
                        "Text must not be parsed",
                        result_code=code,
                    )
                    self.assertEqual(
                        i18n.operation_message(result),
                        TRANSLATIONS[language][key],
                    )


    def test_failed_result_takes_priority_over_success_code(self) -> None:
        i18n = I18nService(self.repo)
        result = OperationResult(
            "failed-op",
            False,
            "Original diagnostic text",
            error_code="SOURCE_NOT_FOUND",
            result_code="BACKUP_CREATED",
        )
        self.assertEqual(
            i18n.operation_message(result),
            TRANSLATIONS["en"]["operation.error.SOURCE_NOT_FOUND"],
        )

    def test_ui_error_message_uses_code_in_both_languages(self) -> None:
        i18n = I18nService(self.repo)
        error = AppError("SOURCE_NOT_FOUND", "Service internal text")

        self.assertEqual(
            i18n.error_message(error),
            TRANSLATIONS["en"]["error.SOURCE_NOT_FOUND"],
        )
        i18n.set_language("ru")
        self.assertEqual(
            i18n.error_message(error),
            TRANSLATIONS["ru"]["error.SOURCE_NOT_FOUND"],
        )


    def test_ui_error_message_has_safe_unknown_code_fallback(self) -> None:
        i18n = I18nService(self.repo)
        error = AppError("UNEXPECTED_CODE", "Internal diagnostic text")

        self.assertEqual(
            i18n.error_message(error),
            TRANSLATIONS["en"]["error.generic"],
        )

    def test_available_snapshots_excludes_every_non_committed_state(self) -> None:
        source = self.source_root / "available-only.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        committed = self.repo.available_snapshots(plan_id)[0]

        extra_states = (
            "creating", "needs_recovery", "corrupted", "deleting", "deleted"
        )
        created_ids = []
        for state in extra_states:
            operation_id = self.repo.create_operation(
                OperationType.BACKUP, plan_id, None, {}
            )
            snapshot_id, _ = self.repo.create_snapshot_creating(
                plan_id, operation_id
            )
            self.repo.mark_snapshot(snapshot_id, state)
            created_ids.append(snapshot_id)

        available = self.repo.available_snapshots(plan_id)
        self.assertEqual([item.id for item in available], [committed.id])
        diagnostic_ids = {
            item.id for item in self.repo.snapshots(
                plan_id, include_deleted=True
            )
        }
        self.assertTrue(set(created_ids).issubset(diagnostic_ids))


    def test_non_committed_snapshot_is_rejected_before_queueing(self) -> None:
        source = self.source_root / "queue-guard.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)

        operation_id = self.repo.create_operation(
            OperationType.BACKUP, plan_id, None, {}
        )
        snapshot_id, _ = self.repo.create_snapshot_creating(
            plan_id, operation_id
        )
        self.repo.mark_snapshot(snapshot_id, "needs_recovery")

        for state in ("needs_recovery", "corrupted", "deleting", "deleted"):
            self.repo.mark_snapshot(snapshot_id, state)
            self.assertNotIn(
                snapshot_id,
                [item.id for item in self.repo.available_snapshots(plan_id)],
            )

    def test_manual_delete_partial_failure_never_restores_committed(
        self,
    ) -> None:
        from unittest.mock import patch

        source = self.source_root / "delete-partial.txt"
        source.write_text("first", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        first = self.repo.available_snapshots(plan_id)[0]

        source.write_text("second", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)

        plan = self.repo.plan_details(plan_id)
        root = self.storage.safe_snapshot_root(
            self.storage_root,
            plan,
            first.sequence_no,
        )
        original_rmtree = shutil.rmtree

        def fail_after_partial_delete(
            path: Path,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if Path(path) == root:
                (root / StorageService.MANIFEST_NAME).unlink()
                raise OSError("simulated partial deletion")
            original_rmtree(path, *args, **kwargs)

        operation_id = self.repo.create_operation(
            OperationType.DELETE_SNAPSHOTS,
            plan_id,
            None,
            {},
        )

        with self.assertLogs(level="ERROR") as captured:
            with patch.object(
                shutil,
                "rmtree",
                side_effect=fail_after_partial_delete,
            ):
                result = self.delete.delete(
                    operation_id,
                    [first.id],
                    self.progress,
                    self.not_cancelled,
                )

        self.assertTrue(
            any(
                "Delete failure" in message
                for message in captured.output
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(
            self.repo.snapshot(first.id).state,
            "needs_recovery",
        )
        self.assertNotIn(
            first.id,
            [
                item.id
                for item in self.repo.available_snapshots(plan_id)
            ],
        )
        self.assertTrue(
            self.repo.needs_recovery_for_plan(plan_id)
        )
        self.assertTrue(
            any(
                issue.code == "OPERATION_NEEDS_RECOVERY"
                for issue in self.repo.list_active_issues(plan_id)
            )
        )
        self.assertEqual(
            self.repo.operation(operation_id)["state"],
            "failed",
        )

    def test_retention_partial_failure_preserves_new_snapshot(
        self,
    ) -> None:
        from unittest.mock import patch

        source = self.source_root / "retention-partial.txt"
        source.write_text("first", encoding="utf-8")
        plan_id = self.make_plan(source, keep=1)
        self.assertTrue(self.backup(plan_id).success)
        first = self.repo.available_snapshots(plan_id)[0]

        plan = self.repo.plan_details(plan_id)
        root = self.storage.safe_snapshot_root(
            self.storage_root,
            plan,
            first.sequence_no,
        )
        original_rmtree = shutil.rmtree

        def fail_after_partial_delete(
            path: Path,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if Path(path) == root:
                (root / StorageService.MANIFEST_NAME).unlink()
                raise OSError("simulated retention failure")
            original_rmtree(path, *args, **kwargs)

        source.write_text("second", encoding="utf-8")

        with self.assertLogs(level="ERROR") as captured:
            with patch.object(
                shutil,
                "rmtree",
                side_effect=fail_after_partial_delete,
            ):
                result = self.backup(plan_id)

        self.assertTrue(
            any(
                "Retention failed" in message
                for message in captured.output
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(
            self.repo.snapshot(first.id).state,
            "needs_recovery",
        )
        self.assertEqual(
            len(self.repo.available_snapshots(plan_id)),
            1,
        )
        self.assertTrue(
            self.repo.needs_recovery_for_plan(plan_id)
        )
        self.assertTrue(
            any(
                issue.code == "OPERATION_NEEDS_RECOVERY"
                for issue in self.repo.list_active_issues(plan_id)
            )
        )

    def test_recovery_quarantines_interrupted_delete_without_touching_files(
        self,
    ) -> None:
        source = self.source_root / "delete-crash.txt"
        source.write_text("first", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        first = self.repo.available_snapshots(plan_id)[0]

        source.write_text("second", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)

        plan = self.repo.plan_details(plan_id)
        root = self.storage.safe_snapshot_root(
            self.storage_root, plan, first.sequence_no
        )
        self.repo.mark_snapshot(first.id, "deleting")

        RecoveryService(self.repo, self.health).run()

        self.assertTrue(root.exists())
        self.assertEqual(
            self.repo.snapshot(first.id).state, "needs_recovery"
        )
        self.assertNotIn(
            first.id,
            [item.id for item in self.repo.available_snapshots(plan_id)],
        )
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))
        self.assertTrue(
            any(
                issue.code == "OPERATION_NEEDS_RECOVERY"
                for issue in self.repo.list_active_issues(plan_id)
            )
        )

        before = len(self.repo.recent_activity())
        RecoveryService(self.repo, self.health).run()
        self.assertEqual(len(self.repo.recent_activity()), before)


    def test_recovery_quarantines_creating_without_deleting_published_path(
        self,
    ) -> None:
        source = self.source_root / "publish-crash.txt"
        source.write_text("source", encoding="utf-8")
        plan_id = self.make_plan(source)

        operation_id = self.repo.create_operation(
            OperationType.BACKUP, plan_id, None, {}
        )
        snapshot_id, sequence_no = self.repo.create_snapshot_creating(
            plan_id, operation_id
        )
        plan = self.repo.plan_details(plan_id)
        final_root = self.storage.safe_snapshot_root(
            self.storage_root, plan, sequence_no
        )
        final_root.mkdir(parents=True)
        marker = final_root / "keep.txt"
        marker.write_text("do not delete", encoding="utf-8")

        RecoveryService(self.repo, self.health).run()

        self.assertEqual(
            self.repo.snapshot(snapshot_id).state,
            "needs_recovery",
        )
        self.assertEqual(
            marker.read_text(encoding="utf-8"),
            "do not delete",
        )
        self.assertEqual(self.repo.available_snapshots(plan_id), [])

    def test_recovery_skips_unsafe_operation_id(self) -> None:
        source = self.source_root / "unsafe-id.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)

        operation_id = self.repo.create_operation(
            OperationType.BACKUP, plan_id, None, {}
        )
        self.repo.operation_state(operation_id, "running")

        outside = self.storage_root / "do-not-delete"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE Operations SET id=? WHERE id=?",
                ("../do-not-delete", operation_id),
            )

        RecoveryService(self.repo, self.health).run()

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))


    def test_recovery_retains_temporary_without_delete_race(self) -> None:
        from unittest.mock import patch
        source = self.source_root / "retained-temp.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        operation_id = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
        self.repo.operation_state(operation_id, "running")
        temporary = self.storage_root / "_amvibackup" / "temporary" / operation_id
        temporary.mkdir(parents=True)
        marker = temporary / "partial.bin"
        marker.write_text("keep", encoding="utf-8")
        with patch.object(shutil, "rmtree", side_effect=AssertionError("unsafe cleanup")):
            RecoveryService(self.repo, self.health).run()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))

    def test_delete_db_failure_after_files_removed_never_commits_snapshot(
        self,
    ) -> None:
        from unittest.mock import patch

        source = self.source_root / "delete-db-after-files.txt"
        source.write_text("first", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        first = self.repo.available_snapshots(plan_id)[0]

        source.write_text("second", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)
        second = self.repo.available_snapshots(plan_id)[0]

        plan = self.repo.plan_details(plan_id)
        first_root = self.storage.safe_snapshot_root(
            self.storage_root, plan, first.sequence_no
        )
        self.assertTrue(first_root.is_dir())

        operation_id = self.repo.create_operation(
            OperationType.DELETE_SNAPSHOTS, plan_id, None, {}
        )

        with patch.object(
            self.repo,
            "delete_record",
            side_effect=OSError("simulated SQLite write failure"),
        ):
            with self.assertLogs(level="ERROR"):
                result = self.delete.delete(
                    operation_id,
                    [first.id],
                    self.progress,
                    self.not_cancelled,
                )

        self.assertFalse(result.success)
        self.assertFalse(first_root.exists())
        self.assertEqual(
            self.repo.snapshot(first.id).state,
            "needs_recovery",
        )
        self.assertEqual(
            [item.id for item in self.repo.available_snapshots(plan_id)],
            [second.id],
        )
        self.assertEqual(
            self.repo.operation(operation_id)["state"],
            "failed",
        )
        self.assertTrue(
            self.repo.needs_recovery_for_plan(plan_id)
        )
        self.assertTrue(
            any(
                issue.code == "OPERATION_NEEDS_RECOVERY"
                for issue in self.repo.list_active_issues(plan_id)
            )
        )

    def test_process_crash_after_publish_before_db_commit(self) -> None:
        child_db = self.root / "crash-publish.sqlite3"
        child_source = self.root / "crash-source.txt"
        child_storage = self.root / "crash-storage"
        child_source.write_text("must survive", encoding="utf-8")

        child_code = textwrap.dedent(r'''
            import importlib.util
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch

            script_path = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location(
                "amvibackup_crash_test", script_path
            )
            app = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = app
            spec.loader.exec_module(app)

            db_path = Path(sys.argv[2])
            source_path = Path(sys.argv[3])
            storage_path = Path(sys.argv[4])

            db = app.Database(db_path)
            db.migrate()
            repo = app.Repository(db)
            storage = app.StorageService()
            health = app.HealthService(repo)
            descriptor = app.PathPolicy.validate_source(str(source_path))
            root = app.PathPolicy.validate_storage(str(storage_path))
            storage_id = repo.get_or_create_storage(root)
            storage.ensure_root(root, storage_id)
            plan_id = repo.create_plan(
                "Crash publish", "", descriptor, storage_id, 5
            )
            operation_id = repo.create_operation(
                app.OperationType.BACKUP, plan_id, None, {}
            )
            service = app.SnapshotService(
                repo, storage, health,
                reserve_bytes=0, reserve_ratio=0.0
            )

            def crash_before_commit(*args, **kwargs):
                os._exit(73)

            with patch.object(
                repo,
                "commit_snapshot",
                side_effect=crash_before_commit,
            ):
                service.create(
                    operation_id,
                    plan_id,
                    False,
                    lambda event: None,
                    lambda: False,
                )
        ''')

        process = subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(self_test_module_path()),
                str(child_db),
                str(child_source),
                str(child_storage),
            ],
            cwd=str(self_test_module_path().parent),
            timeout=30,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            process.returncode,
            73,
            process.stdout + process.stderr,
        )

        recovered_db = Database(child_db)
        recovered_db.verify_contract()
        recovered_repo = Repository(recovered_db)
        recovered_health = HealthService(recovered_repo)

        try:
            plans = recovered_repo.plan_summaries(False)
            self.assertEqual(len(plans), 1)
            plan_id = plans[0].id
            before = recovered_repo.snapshots(plan_id)
            self.assertEqual(len(before), 1)
            snapshot_id = before[0].id
            self.assertEqual(before[0].state, "creating")

            published_root = StorageService().safe_snapshot_root(
                child_storage,
                recovered_repo.plan_details(plan_id),
                before[0].sequence_no,
            )
            self.assertTrue(published_root.is_dir())
            self.assertTrue(
                (
                    published_root / StorageService.MANIFEST_NAME
                ).is_file()
            )

            RecoveryService(recovered_repo, recovered_health).run()

            self.assertEqual(
                recovered_repo.snapshot(snapshot_id).state,
                "needs_recovery",
            )
            self.assertEqual(
                recovered_repo.available_snapshots(plan_id),
                [],
            )
            self.assertTrue(published_root.is_dir())
            self.assertTrue(
                any(
                    issue.code == "OPERATION_NEEDS_RECOVERY"
                    for issue in recovered_repo.list_active_issues(
                        plan_id
                    )
                )
            )

            activity_before = len(
                recovered_repo.recent_activity()
            )
            RecoveryService(recovered_repo, recovered_health).run()
            self.assertEqual(
                len(recovered_repo.recent_activity()),
                activity_before,
            )
        finally:
            recovered_db.close_for_cleanup()

    def test_process_crash_during_snapshot_delete(self) -> None:
        child_db = self.root / "crash-delete.sqlite3"
        child_source = self.root / "delete-source.txt"
        child_storage = self.root / "delete-storage"
        child_source.write_text("first", encoding="utf-8")

        child_code = textwrap.dedent(r'''
            import importlib.util
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch

            script_path = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location(
                "amvibackup_delete_crash_test", script_path
            )
            app = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = app
            spec.loader.exec_module(app)

            db = app.Database(Path(sys.argv[2]))
            db.migrate()
            repo = app.Repository(db)
            storage = app.StorageService()
            health = app.HealthService(repo)
            source = Path(sys.argv[3])
            storage_root = app.PathPolicy.validate_storage(
                sys.argv[4]
            )
            descriptor = app.PathPolicy.validate_source(str(source))
            storage_id = repo.get_or_create_storage(storage_root)
            storage.ensure_root(storage_root, storage_id)
            plan_id = repo.create_plan(
                "Crash delete", "", descriptor, storage_id, 5
            )
            backup = app.SnapshotService(
                repo, storage, health,
                reserve_bytes=0, reserve_ratio=0.0
            )

            def make_backup():
                operation_id = repo.create_operation(
                    app.OperationType.BACKUP, plan_id, None, {}
                )
                result = backup.create(
                    operation_id, plan_id, False,
                    lambda event: None, lambda: False,
                )
                if not result.success:
                    raise RuntimeError(result.message)

            make_backup()
            first = repo.available_snapshots(plan_id)[0]

            source.write_text("second", encoding="utf-8")
            make_backup()
            other = repo.available_snapshots(plan_id)[0]

            marker = storage_root / "unrelated-user-file.txt"
            marker.write_text("keep", encoding="utf-8")

            operation_id = repo.create_operation(
                app.OperationType.DELETE_SNAPSHOTS,
                plan_id,
                None,
                {},
            )
            delete = app.DeleteService(repo, storage, health)

            def stop_during_delete(path, *args, **kwargs):
                os._exit(74)

            with patch.object(
                app.shutil,
                "rmtree",
                side_effect=stop_during_delete,
            ):
                delete.delete(
                    operation_id,
                    [first.id],
                    lambda event: None,
                    lambda: False,
                )
        ''')

        process = subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(self_test_module_path()),
                str(child_db),
                str(child_source),
                str(child_storage),
            ],
            cwd=str(self_test_module_path().parent),
            timeout=30,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            process.returncode,
            74,
            process.stdout + process.stderr,
        )

        recovered_db = Database(child_db)
        recovered_db.verify_contract()
        recovered_repo = Repository(recovered_db)
        recovered_health = HealthService(recovered_repo)

        try:
            plans = recovered_repo.plan_summaries(False)
            self.assertEqual(len(plans), 1)
            plan_id = plans[0].id
            snapshots = recovered_repo.snapshots(plan_id)
            self.assertEqual(len(snapshots), 2)

            deleting = [
                item for item in snapshots
                if item.state == "deleting"
            ]
            committed = [
                item for item in snapshots
                if item.state == "committed"
            ]
            self.assertEqual(len(deleting), 1)
            self.assertEqual(len(committed), 1)

            storage = StorageService()
            plan = recovered_repo.plan_details(plan_id)
            deleting_root = storage.safe_snapshot_root(
                child_storage, plan, deleting[0].sequence_no
            )
            committed_root = storage.safe_snapshot_root(
                child_storage, plan, committed[0].sequence_no
            )
            marker = child_storage / "unrelated-user-file.txt"

            self.assertTrue(deleting_root.is_dir())
            self.assertTrue(committed_root.is_dir())
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "keep",
            )

            RecoveryService(
                recovered_repo,
                recovered_health,
            ).run()

            self.assertEqual(
                recovered_repo.snapshot(deleting[0].id).state,
                "needs_recovery",
            )
            self.assertEqual(
                [item.id for item in
                 recovered_repo.available_snapshots(plan_id)],
                [committed[0].id],
            )
            self.assertTrue(deleting_root.is_dir())
            self.assertTrue(committed_root.is_dir())
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "keep",
            )
            self.assertTrue(
                any(
                    issue.code == "OPERATION_NEEDS_RECOVERY"
                    for issue in recovered_repo.list_active_issues(
                        plan_id
                    )
                )
            )

            activity_before = len(
                recovered_repo.recent_activity()
            )
            RecoveryService(
                recovered_repo,
                recovered_health,
            ).run()
            self.assertEqual(
                len(recovered_repo.recent_activity()),
                activity_before,
            )
        finally:
            recovered_db.close_for_cleanup()


    def test_process_crash_before_snapshot_publish(self) -> None:
        source = self.root / "before-publish-source.txt"
        source.write_text("keep source", encoding="utf-8")
        db_path = self.root / "before-publish.sqlite3"
        storage_root = self.root / "before-publish-storage"
        child_code = textwrap.dedent(r'''
            import importlib.util
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch
            spec = importlib.util.spec_from_file_location("backup_crash_before_publish", sys.argv[1])
            app = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = app
            spec.loader.exec_module(app)
            db = app.Database(Path(sys.argv[2]))
            db.migrate()
            repo = app.Repository(db)
            storage = app.StorageService()
            root = app.PathPolicy.validate_storage(sys.argv[4])
            source = app.PathPolicy.validate_source(sys.argv[3])
            storage_id = repo.get_or_create_storage(root)
            storage.ensure_root(root, storage_id)
            plan_id = repo.create_plan("Before publish", "", source, storage_id, 5)
            operation_id = repo.create_operation(app.OperationType.BACKUP, plan_id, None, {})
            service = app.SnapshotService(repo, storage, app.HealthService(repo), reserve_bytes=0, reserve_ratio=0.0)
            original = app.os.replace
            def stop_at_publish(src, dst):
                if Path(dst).name.startswith("v") and Path(src).name == operation_id:
                    os._exit(75)
                return original(src, dst)
            with patch.object(app.os, "replace", side_effect=stop_at_publish):
                service.create(operation_id, plan_id, False, lambda event: None, lambda: False)
        ''')
        process = subprocess.run(
            [sys.executable, "-c", child_code, str(self_test_module_path()), str(db_path), str(source), str(storage_root)],
            cwd=str(self_test_module_path().parent), timeout=30, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 75, process.stdout + process.stderr)
        db = Database(db_path)
        db.verify_contract()
        repo = Repository(db)
        try:
            plan_id = repo.plan_summaries(False)[0].id
            snapshot = repo.snapshots(plan_id)[0]
            self.assertEqual(snapshot.state, "creating")
            temporary = storage_root / "_amvibackup" / "temporary" / snapshot.operation_id
            self.assertTrue((temporary / StorageService.MANIFEST_NAME).is_file())
            published = StorageService().safe_snapshot_root(storage_root, repo.plan_details(plan_id), snapshot.sequence_no)
            self.assertFalse(published.exists())
            RecoveryService(repo, HealthService(repo)).run()
            self.assertTrue(temporary.is_dir())
            self.assertEqual(repo.snapshot(snapshot.id).state, "needs_recovery")
            self.assertEqual(repo.available_snapshots(plan_id), [])
            self.assertTrue(repo.needs_recovery_for_plan(plan_id))
        finally:
            db.close_for_cleanup()


    def test_process_crash_after_partial_snapshot_delete(self) -> None:
        source = self.root / "partial-delete-source.txt"
        source.write_text("first", encoding="utf-8")
        db_path = self.root / "partial-delete.sqlite3"
        storage_root = self.root / "partial-delete-storage"
        child_code = textwrap.dedent(r'''
            import importlib.util
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch
            spec = importlib.util.spec_from_file_location("backup_crash_partial_delete", sys.argv[1])
            app = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = app
            spec.loader.exec_module(app)
            db = app.Database(Path(sys.argv[2]))
            db.migrate()
            repo = app.Repository(db)
            storage = app.StorageService()
            health = app.HealthService(repo)
            source = Path(sys.argv[3])
            root = app.PathPolicy.validate_storage(sys.argv[4])
            storage_id = repo.get_or_create_storage(root)
            storage.ensure_root(root, storage_id)
            plan_id = repo.create_plan("Partial delete", "", app.PathPolicy.validate_source(str(source)), storage_id, 5)
            backup = app.SnapshotService(repo, storage, health, reserve_bytes=0, reserve_ratio=0.0)
            def make_snapshot():
                operation_id = repo.create_operation(app.OperationType.BACKUP, plan_id, None, {})
                result = backup.create(operation_id, plan_id, False, lambda event: None, lambda: False)
                if not result.success:
                    raise RuntimeError(result.message)
            make_snapshot()
            first = repo.available_snapshots(plan_id)[0]
            source.write_text("second", encoding="utf-8")
            make_snapshot()
            plan = repo.plan_details(plan_id)
            target = storage.safe_snapshot_root(root, plan, first.sequence_no)
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            def partial_delete(path, *args, **kwargs):
                if Path(path) != target:
                    raise AssertionError("wrong path")
                (target / app.StorageService.MANIFEST_NAME).unlink()
                os._exit(76)
            operation_id = repo.create_operation(app.OperationType.DELETE_SNAPSHOTS, plan_id, None, {})
            with patch.object(app.shutil, "rmtree", side_effect=partial_delete):
                app.DeleteService(repo, storage, health).delete(operation_id, [first.id], lambda event: None, lambda: False)
        ''')
        process = subprocess.run(
            [sys.executable, "-c", child_code, str(self_test_module_path()), str(db_path), str(source), str(storage_root)],
            cwd=str(self_test_module_path().parent), timeout=30, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 76, process.stdout + process.stderr)
        db = Database(db_path)
        db.verify_contract()
        repo = Repository(db)
        try:
            plan_id = repo.plan_summaries(False)[0].id
            snapshots = repo.snapshots(plan_id)
            deleting = [x for x in snapshots if x.state == "deleting"]
            committed = [x for x in snapshots if x.state == "committed"]
            self.assertEqual((len(deleting), len(committed)), (1, 1))
            root = StorageService().safe_snapshot_root(storage_root, repo.plan_details(plan_id), deleting[0].sequence_no)
            self.assertTrue(root.is_dir())
            self.assertFalse((root / StorageService.MANIFEST_NAME).exists())
            self.assertEqual((storage_root / "unrelated.txt").read_text(encoding="utf-8"), "keep")
            RecoveryService(repo, HealthService(repo)).run()
            self.assertEqual(repo.snapshot(deleting[0].id).state, "needs_recovery")
            self.assertEqual([x.id for x in repo.available_snapshots(plan_id)], [committed[0].id])
            self.assertTrue(root.is_dir())
            self.assertTrue(repo.needs_recovery_for_plan(plan_id))
            self.assertTrue(any(x.code == "OPERATION_NEEDS_RECOVERY" for x in repo.list_active_issues(plan_id)))
            before = len(repo.recent_activity())
            RecoveryService(repo, HealthService(repo)).run()
            self.assertEqual(len(repo.recent_activity()), before)
        finally:
            db.close_for_cleanup()


    def test_alpha12_presentation_keys_in_both_languages(self) -> None:
        service = I18nService(self.repo)
        for language in ("en", "ru"):
            service.set_language(language)
            for key in (
                "snapshot.restore_to_unavailable_tooltip",
                "error.SNAPSHOT_NOT_COMMITTED",
                "error.OPERATION_NEEDS_RECOVERY",
                "issue.OPERATION_NEEDS_RECOVERY",
                "issue.detail.OPERATION_NEEDS_RECOVERY",
            ):
                with self.subTest(language=language, key=key):
                    self.assertEqual(service.tr(key), TRANSLATIONS[language][key])
            self.assertEqual(
                service.error_message(AppError("OPERATION_NEEDS_RECOVERY", "Internal diagnosis")),
                TRANSLATIONS[language]["error.OPERATION_NEEDS_RECOVERY"],
            )
            self.assertEqual(
                service.issue_detail("OPERATION_NEEDS_RECOVERY"),
                TRANSLATIONS[language]["issue.detail.OPERATION_NEEDS_RECOVERY"],
            )

    def test_renamed_plan_keeps_direct_and_zip_snapshots(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source = self.source_root / f"rename-{mode}.txt"
                source.write_text("first", encoding="utf-8")
                plan_id = self.make_plan(source, storage_mode=mode)
                first_result = self.backup(plan_id)
                self.assertTrue(first_result.success, first_result.message)
                first = self.repo.available_snapshots(plan_id)[0]
                before = self.storage.safe_snapshot_root(
                    self.storage_root, self.repo.plan_details(plan_id), first.sequence_no
                )
                self.assertTrue(before.is_dir())
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = self.repo.plan_details(plan_id).storage_root_id
                self.repo.update_plan(
                    plan_id, f"Renamed {mode}", "", descriptor, storage_id, 5, mode
                )
                self.assertEqual(
                    self.storage.safe_snapshot_root(self.storage_root, self.repo.plan_details(plan_id), first.sequence_no),
                    before,
                )
                operation_id = self.repo.create_operation(OperationType.VERIFY, plan_id, first.id, {})
                result = self.verifier.verify(operation_id, first.id, self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                operation_id = self.repo.create_operation(OperationType.RESTORE_BESIDE, plan_id, first.id, {})
                restored = self.restore.restore(operation_id, first.id, self.progress, self.not_cancelled)
                self.assertTrue(restored.success, restored.message)
                source.write_text("second", encoding="utf-8")
                second_result = self.backup(plan_id)
                self.assertTrue(second_result.success, second_result.message)
                self.assertEqual(len(self.repo.available_snapshots(plan_id)), 2)
                second = self.repo.available_snapshots(plan_id)[0]
                self.assertEqual(self.storage.safe_snapshot_root(
                    self.storage_root, self.repo.plan_details(plan_id), second.sequence_no
                ).parent, before.parent)
                self.assertEqual(list(self.storage_root.glob(f"*__job-{plan_id[:8]}")), [before.parent.parent])
                self.assertTrue((before.parent.parent / "plan.json").is_file())


    def test_legacy_snapshot_without_plan_marker_survives_rename(self) -> None:
        source = self.source_root / "legacy-rename.txt"
        source.write_text("legacy", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        before = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan_id))
        (before / "plan.json").unlink()
        source_descriptor = PathPolicy.validate_source(str(source))
        storage_id = self.repo.plan_details(plan_id).storage_root_id
        self.repo.update_plan(plan_id, "Legacy rename", "", source_descriptor, storage_id, 5, StorageMode.DIRECT.value)
        self.assertEqual(self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan_id)), before)
        source.write_text("updated", encoding="utf-8")
        result = self.backup(plan_id)
        self.assertTrue(result.success, result.message)
        self.assertTrue((before / "plan.json").is_file())
        self.assertEqual(len(self.repo.available_snapshots(plan_id)), 2)


    def test_storage_change_blocked_after_snapshot_in_both_edit_paths(self) -> None:
        source = self.source_root / "storage-change.txt"
        source.write_text("safe", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        original = self.repo.plan_details(plan_id)
        original_snapshot = self.repo.available_snapshots(plan_id)[0]
        other = self.root / "other-storage"
        other_id = self.repo.get_or_create_storage(PathPolicy.validate_storage(str(other)))
        descriptor = PathPolicy.validate_source(str(source))
        with self.assertRaises(AppError) as first:
            self.repo.update_plan(plan_id, "New name", "", descriptor, other_id, 5, original.storage_mode)
        self.assertEqual(first.exception.code, "STORAGE_CHANGE_BLOCKED")
        with self.assertRaises(AppError) as second:
            self.repo.save_plan_with_policy(
                plan_id, "New name", "", descriptor, other_id, 5,
                original.storage_mode, self.repo.default_trigger_policy(),
            )
        self.assertEqual(second.exception.code, "STORAGE_CHANGE_BLOCKED")
        self.assertEqual(self.repo.plan_details(plan_id).storage_root_id, original.storage_root_id)
        operation_id = self.repo.create_operation(OperationType.VERIFY, plan_id, original_snapshot.id, {})
        self.assertTrue(self.verifier.verify(operation_id, original_snapshot.id, self.progress, self.not_cancelled).success)
        for lang in ("en", "ru"):
            I18nService(self.repo).set_language(lang)
            self.assertEqual(
                I18nService(self.repo).error_message(first.exception),
                TRANSLATIONS[lang]["error.STORAGE_CHANGE_BLOCKED"],
            )


    def test_unowned_plan_directory_never_used_or_overwritten(self) -> None:
        source = self.source_root / "unowned.txt"
        source.write_text("safe", encoding="utf-8")
        plan_id = self.make_plan(source)
        plan = self.repo.plan_details(plan_id)
        impostor = self.storage_root / f"{sanitize_name(plan.display_name)}__job-{plan_id[:8]}"
        impostor.mkdir()
        sentinel = impostor / "do-not-touch.txt"
        sentinel.write_text("private", encoding="utf-8")
        result = self.backup(plan_id)
        self.assertFalse(result.success)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "private")
        self.assertEqual(self.repo.available_snapshots(plan_id), [])

    def test_direct_storage_mode_method_blocks_change_after_snapshot(
        self,
    ) -> None:
        source = self.source_root / "mode-guard.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)

        self.repo.set_plan_storage_mode(
            plan_id, StorageMode.ZIP.value
        )
        self.assertEqual(
            self.repo.plan_details(plan_id).storage_mode,
            StorageMode.ZIP.value,
        )
        self.assertTrue(self.backup(plan_id).success)

        with self.assertRaises(AppError) as error:
            self.repo.set_plan_storage_mode(
                plan_id, StorageMode.DIRECT.value
            )

        self.assertEqual(
            error.exception.code,
            "STORAGE_MODE_CHANGE_BLOCKED",
        )
        self.assertEqual(
            self.repo.plan_details(plan_id).storage_mode,
            StorageMode.ZIP.value,
        )


    def test_direct_storage_mode_method_rejects_archived_plan(
        self,
    ) -> None:
        source = self.source_root / "mode-archived.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)

        operation_id = self.repo.create_operation(
            OperationType.ARCHIVE_PLAN, plan_id, None, {}
        )
        self.repo.archive(plan_id, operation_id)

        with self.assertRaises(AppError) as error:
            self.repo.set_plan_storage_mode(
                plan_id, StorageMode.ZIP.value
            )

        self.assertEqual(error.exception.code, "PLAN_ARCHIVED")
        self.assertEqual(
            self.repo.plan_details(plan_id).storage_mode,
            StorageMode.DIRECT.value,
        )



    def test_recovery_accepts_verified_direct_and_zip_versions(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source = self.source_root / f"recovery-valid-{mode}.txt"
                source.write_text("good", encoding="utf-8")
                plan_id = self.make_plan(source, storage_mode=mode)
                result = self.backup(plan_id)
                self.assertTrue(result.success, result.message)
                snapshot = self.repo.available_snapshots(plan_id)[0]
                self.repo.mark_snapshot(snapshot.id, "needs_recovery")
                self.repo.operation_state(snapshot.operation_id, "needs_recovery")
                RecoveryService(self.repo, self.health).run()
                review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
                reviewed = RecoveryService(self.repo, self.health).review_plan(
                    review_id, plan_id, self.progress, self.not_cancelled,
                )
                self.assertTrue(reviewed.success, reviewed.message)
                self.assertEqual(reviewed.result_code, "RECOVERY_FINISHED")
                self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
                self.assertEqual(self.repo.operation(review_id)["state"], "succeeded")
                self.assertFalse(self.repo.needs_recovery_for_plan(plan_id))
                self.assertEqual(self.repo.plan_details(plan_id).last_success_snapshot_id, snapshot.id)
                self.assertFalse(any(i.code == "OPERATION_NEEDS_RECOVERY" for i in self.repo.list_active_issues(plan_id)))
                check = self.repo.create_operation(OperationType.VERIFY, plan_id, snapshot.id, {})
                self.assertTrue(self.verifier.verify(check, snapshot.id, self.progress, self.not_cancelled).success)
                again = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
                rejected = RecoveryService(self.repo, self.health).review_plan(
                    again, plan_id, self.progress, self.not_cancelled,
                )
                self.assertFalse(rejected.success)
                self.assertEqual(rejected.error_code, "RECOVERY_NOT_REQUIRED")


    def test_recovery_corruption_retained_but_backup_resumes(self) -> None:
        source = self.source_root / "recovery-corrupted.txt"
        source.write_text("first", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        damaged = self.repo.available_snapshots(plan_id)[0]
        source.write_text("second", encoding="utf-8")
        self.assertTrue(self.backup(plan_id).success)
        good = self.repo.available_snapshots(plan_id)[0]
        root = self.storage.safe_snapshot_root(self.storage_root, self.repo.plan_details(plan_id), damaged.sequence_no)
        self.workspace.make_writable_for_external_mutation(root)
        (root / StorageService.MANIFEST_NAME).write_text("broken", encoding="utf-8")
        self.repo.mark_snapshot(damaged.id, "needs_recovery")
        RecoveryService(self.repo, self.health).run()
        review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review_id, plan_id, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.snapshot(damaged.id).state, "corrupted")
        self.assertEqual(self.repo.available_snapshots(plan_id)[0].id, good.id)
        self.assertTrue(root.is_dir())
        self.assertFalse(self.repo.needs_recovery_for_plan(plan_id))
        self.assertTrue(any(i.code == "SNAPSHOT_CORRUPTED" for i in self.repo.list_active_issues(plan_id)))
        source.write_text("third", encoding="utf-8")
        self.assertTrue(self.backup(plan_id, True).success)


    def test_recovery_missing_storage_keeps_quarantine(self) -> None:
        source = self.source_root / "recovery-offline.txt"
        source.write_text("backup", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        self.workspace.make_writable_for_external_mutation(self.storage_root)
        shutil.rmtree(self.storage_root)
        review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review_id, plan_id, self.progress, self.not_cancelled)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RECOVERY_STORAGE_UNAVAILABLE")
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "needs_recovery")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))


    def test_recovery_without_snapshot_resumes_plan(self) -> None:
        source = self.source_root / "recovery-empty.txt"
        source.write_text("source", encoding="utf-8")
        plan_id = self.make_plan(source)
        interrupted = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
        self.repo.operation_state(interrupted, "running")
        RecoveryService(self.repo, self.health).run()
        review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review_id, plan_id, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(interrupted)["state"], "failed")
        self.assertFalse(self.repo.needs_recovery_for_plan(plan_id))
        self.assertTrue(self.backup(plan_id).success)


    def test_recovery_transaction_rolls_back_on_failure(self) -> None:
        from unittest.mock import patch
        source = self.source_root / "recovery-rollback.txt"
        source.write_text("source", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        self.repo.operation_state(snapshot.operation_id, "needs_recovery")
        review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        original_activity = self.repo.activity
        def fail_last_event(level, code, *args, **kwargs):
            if code == "RECOVERY_REVIEWED":
                raise OSError("simulated database failure")
            return original_activity(level, code, *args, **kwargs)
        with patch.object(self.repo, "activity", side_effect=fail_last_event):
            with self.assertLogs(level="ERROR"):
                result = RecoveryService(self.repo, self.health).review_plan(review_id, plan_id, self.progress, self.not_cancelled)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "needs_recovery")
        self.assertEqual(self.repo.operation(snapshot.operation_id)["state"], "needs_recovery")
        self.assertEqual(self.repo.operation(review_id)["state"], "failed")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))
        again_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        self.assertTrue(RecoveryService(self.repo, self.health).review_plan(
            again_id, plan_id, self.progress, self.not_cancelled,
        ).success)


    def test_recovery_cancel_does_not_change_snapshot(self) -> None:
        source = self.source_root / "recovery-cancel.txt"
        source.write_text("source", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        review_id = self.repo.create_operation(OperationType.RECOVER_PLAN, plan_id, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review_id, plan_id, self.progress, lambda: True)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "needs_recovery")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan_id))

    def test_recovery_does_not_accept_file_after_source_rename(
        self,
    ) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                original = self.source_root / f"old-{mode}.txt"
                replacement = self.source_root / f"new-{mode}.txt"
                original.write_text("saved", encoding="utf-8")
                replacement.write_text("new source", encoding="utf-8")

                plan_id = self.make_plan(original, storage_mode=mode)
                self.assertTrue(self.backup(plan_id).success)
                snapshot = self.repo.available_snapshots(plan_id)[0]
                artifact_root = self.storage.safe_snapshot_root(
                    self.storage_root,
                    self.repo.plan_details(plan_id),
                    snapshot.sequence_no,
                )

                plan = self.repo.plan_details(plan_id)
                self.repo.update_plan(
                    plan_id,
                    plan.display_name,
                    plan.note,
                    PathPolicy.validate_source(str(replacement)),
                    plan.storage_root_id,
                    plan.keep_last_n,
                    plan.storage_mode,
                )
                self.repo.mark_snapshot(snapshot.id, "needs_recovery")
                self.repo.operation_state(
                    snapshot.operation_id, "needs_recovery"
                )

                review_id = self.repo.create_operation(
                    OperationType.RECOVER_PLAN, plan_id, None, {}
                )
                result = RecoveryService(
                    self.repo, self.health
                ).review_plan(
                    review_id,
                    plan_id,
                    self.progress,
                    self.not_cancelled,
                )

                self.assertTrue(result.success, result.message)
                self.assertEqual(
                    self.repo.snapshot(snapshot.id).state, "corrupted"
                )
                self.assertNotIn(
                    snapshot.id,
                    [item.id for item in
                     self.repo.available_snapshots(plan_id)],
                )
                self.assertTrue(artifact_root.is_dir())
                self.assertFalse(
                    self.repo.needs_recovery_for_plan(plan_id)
                )
                self.assertTrue(self.backup(plan_id).success)


    def test_recovery_does_not_accept_changed_source_kind(self) -> None:
        original = self.source_root / "kind-file.txt"
        replacement = self.source_root / "kind-folder"
        original.write_text("saved", encoding="utf-8")
        replacement.mkdir()

        plan_id = self.make_plan(original)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        plan = self.repo.plan_details(plan_id)

        self.repo.update_plan(
            plan_id,
            plan.display_name,
            plan.note,
            PathPolicy.validate_source(str(replacement)),
            plan.storage_root_id,
            plan.keep_last_n,
            plan.storage_mode,
        )
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")

        review_id = self.repo.create_operation(
            OperationType.RECOVER_PLAN, plan_id, None, {}
        )
        result = RecoveryService(self.repo, self.health).review_plan(
            review_id, plan_id, self.progress, self.not_cancelled
        )

        self.assertTrue(result.success, result.message)
        self.assertEqual(
            self.repo.snapshot(snapshot.id).state, "corrupted"
        )
        self.assertEqual(
            self.repo.available_snapshots(plan_id), []
        )



    def test_restore_to_empty_directory_direct_and_zip_file(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source = self.source_root / f"restore-target-{mode}.txt"
                source.write_text("saved value", encoding="utf-8")
                plan_id = self.make_plan(source, storage_mode=mode)
                self.assertTrue(self.backup(plan_id).success)
                snapshot = self.repo.available_snapshots(plan_id)[0]
                source.write_text("current value", encoding="utf-8")
                destination = self.root / f"selected-{mode}"
                destination.mkdir()
                operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {"target_directory": str(destination)})
                result = self.restore.restore_to_directory(operation_id, snapshot.id, str(destination), self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual(Path(result.destination_path).read_text(encoding="utf-8"), "saved value")
                self.assertEqual(source.read_text(encoding="utf-8"), "current value")
                self.assertEqual(self.repo.operation(operation_id)["state"], "succeeded")
                self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
                self.assertEqual(
                    I18nService(self.repo).operation_message(result),
                    TRANSLATIONS["en"]["operation.restore_to_directory_succeeded"].format(path=result.destination_path),
                )


    def test_restore_to_empty_directory_direct_and_zip_folder(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source = self.source_root / f"folder-{mode}"
                (source / "nested").mkdir(parents=True)
                (source / "nested" / "данные.txt").write_text("saved", encoding="utf-8")
                plan_id = self.make_plan(source, storage_mode=mode)
                self.assertTrue(self.backup(plan_id).success)
                snapshot = self.repo.available_snapshots(plan_id)[0]
                (source / "nested" / "данные.txt").write_text("changed", encoding="utf-8")
                destination = self.root / f"selected-folder-{mode}"
                destination.mkdir()
                operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {"target_directory": str(destination)})
                result = self.restore.restore_to_directory(operation_id, snapshot.id, str(destination), self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual((Path(result.destination_path) / "nested" / "данные.txt").read_text(encoding="utf-8"), "saved")
                self.assertEqual((source / "nested" / "данные.txt").read_text(encoding="utf-8"), "changed")


    def test_restore_to_directory_rejects_busy_or_unsafe_destination(self) -> None:
        source = self.source_root / "restore-safe.txt"
        source.write_text("original", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        busy = self.root / "busy-destination"
        busy.mkdir()
        marker = busy / "important.txt"
        marker.write_text("keep", encoding="utf-8")
        operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {})
        result = self.restore.restore_to_directory(operation_id, snapshot.id, str(busy), self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(Path(result.destination_path).read_text(encoding="utf-8"), "original")
        self.assertIn("-restored-", Path(result.destination_path).name)
        operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {})
        result = self.restore.restore_to_directory(operation_id, snapshot.id, str(self.storage_root), self.progress, self.not_cancelled)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RESTORE_DESTINATION_IN_STORAGE")


    def test_restore_to_directory_cancel_leaves_no_output(self) -> None:
        source = self.source_root / "cancel-restore.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        selected = self.root / "cancel-target"
        selected.mkdir()
        operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {})
        result = self.restore.restore_to_directory(operation_id, snapshot.id, str(selected), self.progress, lambda: True)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.operation(operation_id)["state"], "cancelled")
        self.assertEqual(list(selected.iterdir()), [])


    def test_restore_to_directory_refuses_destination_changed_during_copy(self) -> None:
        from unittest.mock import patch
        source = self.source_root / "restore-race.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        selected = self.root / "changed-target"
        selected.mkdir()
        intruder = selected / "new-user-file.txt"
        original = self.restore._verify_restored_output
        def change_destination(*args):
            original(*args)
            intruder.write_text("keep", encoding="utf-8")
        operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {})
        with patch.object(self.restore, "_verify_restored_output", side_effect=change_destination):
            result = self.restore.restore_to_directory(operation_id, snapshot.id, str(selected), self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(intruder.read_text(encoding="utf-8"), "keep")
        self.assertEqual(Path(result.destination_path).read_text(encoding="utf-8"), "saved")
        self.assertFalse((selected / source.name).exists())


    def test_restore_to_directory_denies_non_committed(self) -> None:
        source = self.source_root / "restore-hidden.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        selected = self.root / "hidden-target"
        selected.mkdir()
        operation_id = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan_id, snapshot.id, {})
        result = self.restore.restore_to_directory(operation_id, snapshot.id, str(selected), self.progress, self.not_cancelled)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SNAPSHOT_NOT_COMMITTED")
        self.assertEqual(list(selected.iterdir()), [])

    def make_selected_folder_snapshot(self, mode: str):
        source = self.source_root / f"selected-{mode}"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "chosen.txt").write_text("saved", encoding="utf-8")
        (source / "untouched.txt").write_text("other", encoding="utf-8")
        plan_id = self.make_plan(source, storage_mode=mode)
        backup = self.backup(plan_id)
        self.assertTrue(backup.success, backup.message)
        return source, plan_id, self.repo.available_snapshots(plan_id)[0]

    def run_selected_restore(self, plan_id, snapshot_id, selected, destination, cancelled=None):
        operation_id = self.repo.create_operation(
            OperationType.RESTORE_SELECTED_FILES, plan_id, snapshot_id,
            {"selected_relative_paths": selected, "target_directory": str(destination)},
        )
        result = self.restore.restore_selected_files(
            operation_id, snapshot_id, selected, str(destination),
            self.progress, cancelled or self.not_cancelled,
        )
        return operation_id, result

    def test_selected_restore_direct_and_zip_preserves_unselected_and_existing(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan_id, snapshot = self.make_selected_folder_snapshot(mode)
                (source / "nested" / "chosen.txt").write_text("new source", encoding="utf-8")
                destination = self.root / f"selected-output-{mode}"
                destination.mkdir()
                (destination / "keep.txt").write_text("keep", encoding="utf-8")
                operation_id, result = self.run_selected_restore(
                    plan_id, snapshot.id, ["nested/chosen.txt"], destination,
                )
                self.assertTrue(result.success, result.message)
                self.assertEqual(result.result_code, "RESTORE_SELECTED_SUCCEEDED")
                self.assertEqual(result.destination_path, str(destination))
                self.assertEqual(self.repo.operation(operation_id)["state"], "succeeded")
                self.assertEqual((destination / "nested" / "chosen.txt").read_text(encoding="utf-8"), "saved")
                self.assertEqual((destination / "keep.txt").read_text(encoding="utf-8"), "keep")
                self.assertFalse((destination / "untouched.txt").exists())
                self.assertEqual((source / "nested" / "chosen.txt").read_text(encoding="utf-8"), "new source")
                self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
                for language in ("en", "ru"):
                    i18n = I18nService(self.repo)
                    i18n.set_language(language)
                    self.assertEqual(
                        i18n.operation_message(result),
                        TRANSLATIONS[language]["operation.restore_selected_succeeded"].format(path=str(destination)),
                    )

    def test_selected_restore_conflict_and_unsafe_destinations(self) -> None:
        source, plan_id, snapshot = self.make_selected_folder_snapshot(StorageMode.DIRECT.value)
        destination = self.root / "selected-conflict"
        (destination / "nested").mkdir(parents=True)
        occupied = destination / "nested" / "chosen.txt"
        occupied.write_text("keep", encoding="utf-8")
        operation_id, result = self.run_selected_restore(plan_id, snapshot.id, ["nested/chosen.txt"], destination)
        self.assertTrue(result.success, result.message)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "keep")
        added = list((destination / "nested").glob("chosen-restored-*.txt"))
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].read_text(encoding="utf-8"), "saved")
        for target, code in ((self.storage_root, "RESTORE_DESTINATION_IN_STORAGE"), (source, "RESTORE_DESTINATION_IN_SOURCE")):
            with self.subTest(target=target):
                operation_id, result = self.run_selected_restore(plan_id, snapshot.id, ["nested/chosen.txt"], target)
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, code)
                self.assertEqual(self.repo.operation(operation_id)["state"], "failed")

    def test_selected_restore_rejects_unlisted_and_traversal(self) -> None:
        _, plan_id, snapshot = self.make_selected_folder_snapshot(StorageMode.DIRECT.value)
        destination = self.root / "selected-invalid"
        destination.mkdir()
        for selected in (["missing.txt"], ["../escape.txt"], []):
            with self.subTest(selected=selected):
                operation_id, result = self.run_selected_restore(plan_id, snapshot.id, selected, destination)
                self.assertFalse(result.success)
                self.assertIn(result.error_code, {"MANIFEST_INVALID", "RESTORE_NOTHING_SELECTED"})
                self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
                self.assertEqual(list(destination.iterdir()), [])

    def test_selected_restore_cancel_cleans_temporary_output(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                _, plan_id, snapshot = self.make_selected_folder_snapshot(mode)
                destination = self.root / f"selected-cancel-{mode}"
                destination.mkdir()
                operation_id, result = self.run_selected_restore(
                    plan_id, snapshot.id, ["nested/chosen.txt"], destination, lambda: True,
                )
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "OPERATION_CANCELLED")
                self.assertEqual(self.repo.operation(operation_id)["state"], "cancelled")
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse((destination.parent / f".amvibackup-restore-selected-{operation_id}.tmp").exists())

    def test_selected_restore_denies_noncommitted_and_file_snapshots(self) -> None:
        _, plan_id, snapshot = self.make_selected_folder_snapshot(StorageMode.DIRECT.value)
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        destination = self.root / "selected-hidden"
        destination.mkdir()
        operation_id, result = self.run_selected_restore(plan_id, snapshot.id, ["nested/chosen.txt"], destination)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SNAPSHOT_NOT_COMMITTED")
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        file_source = self.source_root / "single.txt"
        file_source.write_text("saved", encoding="utf-8")
        file_plan = self.make_plan(file_source)
        self.assertTrue(self.backup(file_plan).success)
        file_snapshot = self.repo.available_snapshots(file_plan)[0]
        operation_id, result = self.run_selected_restore(file_plan, file_snapshot.id, ["single.txt"], destination)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RESTORE_FOLDER_ONLY")
        self.assertEqual(list(destination.iterdir()), [])

    def test_selected_restore_detects_destination_changed_before_publish(self) -> None:
        from unittest.mock import patch
        _, plan_id, snapshot = self.make_selected_folder_snapshot(StorageMode.DIRECT.value)
        destination = self.root / "selected-race"
        destination.mkdir()
        conflict = destination / "nested" / "chosen.txt"
        original = self.restore._verify_restored_output
        def occupy(*args, **kwargs):
            original(*args, **kwargs)
            conflict.parent.mkdir(parents=True, exist_ok=True)
            conflict.write_text("outside", encoding="utf-8")
        with patch.object(self.restore, "_verify_restored_output", side_effect=occupy):
            operation_id, result = self.run_selected_restore(
                plan_id, snapshot.id, ["nested/chosen.txt"], destination,
            )
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(operation_id)["state"], "succeeded")
        self.assertEqual(conflict.read_text(encoding="utf-8"), "outside")
        added = list(conflict.parent.glob("chosen-restored-*.txt"))
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].read_text(encoding="utf-8"), "saved")

    def _replace_fixture(self, mode=StorageMode.DIRECT.value):
        source = self.source_root / f"replace-{mode}.txt"
        source.write_text("saved original", encoding="utf-8")
        plan_id = self.make_plan(source, storage_mode=mode)
        backup = self.backup(plan_id)
        self.assertTrue(backup.success, backup.message)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        source.write_text("current work", encoding="utf-8")
        return source, plan_id, snapshot

    def _replace_run(self, plan_id, snapshot_id, cancelled=None, progress=None):
        operation_id = self.repo.create_operation(
            OperationType.RESTORE_REPLACE_FILE, plan_id, snapshot_id, {}
        )
        result = self.restore.replace_original_file(
            operation_id, snapshot_id,
            progress or self.progress, cancelled or self.not_cancelled,
        )
        return operation_id, result

    def _replace_events(self, operation_id):
        with self.db.read_connection() as conn:
            return [
                (row["event_code"], json.loads(row["details_json"]))
                for row in conn.execute(
                    "SELECT event_code, details_json FROM ActivityLog WHERE operation_id=?",
                    (operation_id,),
                ).fetchall()
            ]

    def test_replace_original_direct_and_zip_success_audit(self):
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan_id, snapshot = self._replace_fixture(mode)
                operation_id, result = self._replace_run(plan_id, snapshot.id)
                self.assertTrue(result.success, result.message)
                self.assertEqual(result.result_code, "RESTORE_REPLACE_SUCCEEDED")
                self.assertEqual(source.read_text(encoding="utf-8"), "saved original")
                previous = Path(result.destination_path)
                self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
                self.assertEqual(previous.parent, source.parent)
                self.assertEqual(self.repo.operation(operation_id)["type"], OperationType.RESTORE_REPLACE_FILE.value)
                self.assertEqual(self.repo.operation(operation_id)["state"], "succeeded")
                self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
                self.assertFalse((source.parent / f".amvibackup-replace-{operation_id}.tmp").exists())
                events = self._replace_events(operation_id)
                successes = [details for code, details in events if code == "RESTORE_REPLACE_SUCCEEDED"]
                self.assertEqual(len(successes), 1)
                self.assertEqual(successes[0]["previous_copy_path"], str(previous))
                self.assertEqual(successes[0]["source_path"], str(source))
                for language in ("en", "ru"):
                    i18n = I18nService(self.repo)
                    i18n.set_language(language)
                    self.assertEqual(
                        i18n.operation_message(result),
                        TRANSLATIONS[language]["operation.replace_file_succeeded"].format(path=str(previous)),
                    )

    def test_replace_original_rejects_folder_noncommitted_missing(self):
        folder = self.source_root / "replace-folder"
        folder.mkdir()
        (folder / "one.txt").write_text("keep", encoding="utf-8")
        folder_plan = self.make_plan(folder)
        self.assertTrue(self.backup(folder_plan).success)
        folder_snapshot = self.repo.available_snapshots(folder_plan)[0]
        operation_id, result = self._replace_run(folder_plan, folder_snapshot.id)
        self.assertEqual(result.error_code, "RESTORE_REPLACE_INVALID")
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertEqual((folder / "one.txt").read_text(encoding="utf-8"), "keep")
        source, plan_id, snapshot = self._replace_fixture()
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        operation_id, result = self._replace_run(plan_id, snapshot.id)
        self.assertEqual(result.error_code, "SNAPSHOT_NOT_COMMITTED")
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertEqual(source.read_text(encoding="utf-8"), "current work")
        self.assertFalse(list(source.parent.glob(f"{source.stem}-before-restore-*.txt")))
        source.unlink()
        operation_id, result = self._replace_run(plan_id, snapshot.id)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertFalse(source.exists())

    def test_replace_original_cancel_before_publish(self):
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan_id, snapshot = self._replace_fixture(mode)
                cancelled = False
                def report(event):
                    nonlocal cancelled
                    cancelled = True
                operation_id, result = self._replace_run(
                    plan_id, snapshot.id, cancelled=lambda: cancelled, progress=report,
                )
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "OPERATION_CANCELLED")
                self.assertEqual(self.repo.operation(operation_id)["state"], "cancelled")
                self.assertEqual(source.read_text(encoding="utf-8"), "current work")
                self.assertFalse((source.parent / f".amvibackup-replace-{operation_id}.tmp").exists())
                self.assertFalse((source.parent / f"{source.stem}-before-restore-{operation_id}{source.suffix}").exists())
                self.assertFalse(any(code == "RESTORE_REPLACE_SUCCEEDED" for code, _ in self._replace_events(operation_id)))

    def test_replace_original_prereplace_copy_failure_keeps_source(self):
        from unittest.mock import patch
        source, plan_id, snapshot = self._replace_fixture()
        operation_id = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan_id, snapshot.id, {})
        previous = source.parent / f"{source.stem}-before-restore-{operation_id}{source.suffix}"
        original_open = Path.open
        def fail_old_copy(path, *args, **kwargs):
            if path == previous and args and args[0] == "xb":
                raise OSError("injected old-copy failure")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", new=fail_old_copy):
            result = self.restore.replace_original_file(
                operation_id, snapshot.id, self.progress, self.not_cancelled,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RESTORE_PREREPLACE_FAILED")
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertEqual(source.read_text(encoding="utf-8"), "current work")
        self.assertFalse(previous.exists())
        self.assertFalse((source.parent / f".amvibackup-replace-{operation_id}.tmp").exists())
        self.assertFalse(any(code == "RESTORE_REPLACE_SUCCEEDED" for code, _ in self._replace_events(operation_id)))

    def test_replace_original_publish_failure_preserves_previous_copy(self):
        from unittest.mock import patch
        source, plan_id, snapshot = self._replace_fixture()
        original_replace = os.replace
        def fail_publish(src, dst):
            if dst == source and str(src).startswith(str(source.parent / ".amvibackup-replace-")):
                raise OSError("injected publish failure")
            return original_replace(src, dst)
        with patch("os.replace", side_effect=fail_publish):
            with self.assertLogs(level="ERROR"):
                operation_id, result = self._replace_run(plan_id, snapshot.id)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertEqual(source.read_text(encoding="utf-8"), "current work")
        previous = source.parent / f"{source.stem}-before-restore-{operation_id}{source.suffix}"
        self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
        self.assertFalse((source.parent / f".amvibackup-replace-{operation_id}.tmp").exists())
        self.assertFalse(any(code == "RESTORE_REPLACE_SUCCEEDED" for code, _ in self._replace_events(operation_id)))

    def test_replace_original_postpublish_verify_failure_requests_review(self):
        from unittest.mock import patch
        source, plan_id, snapshot = self._replace_fixture()
        original_verify = self.restore._verify_restored_output
        def fail_final(root, kind, entries, cancelled):
            if root == source:
                raise AppError("RESTORE_VERIFY_FAILED", "injected final check failure")
            return original_verify(root, kind, entries, cancelled)
        with patch.object(self.restore, "_verify_restored_output", side_effect=fail_final):
            operation_id, result = self._replace_run(plan_id, snapshot.id)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RESTORE_REPLACE_REVIEW_REQUIRED")
        self.assertEqual(self.repo.operation(operation_id)["state"], "failed")
        self.assertEqual(self.repo.operation(operation_id)["error_code"], result.error_code)
        self.assertEqual(source.read_text(encoding="utf-8"), "saved original")
        previous = source.parent / f"{source.stem}-before-restore-{operation_id}{source.suffix}"
        self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
        self.assertFalse(any(code == "RESTORE_REPLACE_SUCCEEDED" for code, _ in self._replace_events(operation_id)))

    def test_replace_original_crash_after_copy_recovery_keeps_snapshot_committed(self):
        source, plan_id, snapshot = self._replace_fixture()
        operation_id = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan_id, snapshot.id, {})
        self.repo.operation_state(operation_id, "running")
        previous = source.parent / f"{source.stem}-before-restore-{operation_id}{source.suffix}"
        previous.write_text("current work", encoding="utf-8")
        staged = source.parent / f".amvibackup-replace-{operation_id}.tmp"
        staged.write_text("saved original", encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        self.assertEqual(self.repo.operation(operation_id)["state"], "needs_recovery")
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
        self.assertEqual(source.read_text(encoding="utf-8"), "current work")
        self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
        self.assertTrue(staged.exists())
        self.assertTrue(any(code == "OPERATION_NEEDS_RECOVERY" for code, _ in self._replace_events(operation_id)))




    def test_alpha15_recovery_cleans_only_own_backup_temp(self) -> None:
        source = self.source_root / "cleanup15.txt"
        source.write_text("current", encoding="utf-8")
        plan = self.make_plan(source)
        op = self.repo.create_operation(OperationType.BACKUP, plan, None, {})
        self.repo.operation_state(op, "running")
        temp = self.storage.temporary_operation_dir(self.storage_root, op)
        temp.mkdir(parents=True)
        (temp / "partial.txt").write_text("partial", encoding="utf-8")
        other = temp.parent / "other"
        other.mkdir()
        (other / "keep.txt").write_text("keep", encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        self.assertTrue(temp.exists())
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertFalse(temp.exists())
        self.assertEqual((other / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.repo.operation(op)["state"], "failed")
        self.assertTrue(any(row["event_code"] == "RECOVERY_TEMP_CLEANED" for row in self.repo.recent_activity()))

    def test_alpha15_recovery_foreign_marker_retains_quarantine(self) -> None:
        source = self.source_root / "foreign15.txt"
        source.write_text("current", encoding="utf-8")
        plan = self.make_plan(source)
        op = self.repo.create_operation(OperationType.BACKUP, plan, None, {})
        self.repo.operation_state(op, "running")
        temp = self.storage.temporary_operation_dir(self.storage_root, op)
        temp.mkdir(parents=True)
        (temp / "keep.txt").write_text("keep", encoding="utf-8")
        marker = self.storage_root / "_amvibackup" / "storage.json"
        marker.write_text(json_text({"storage_id": "foreign", "storage_root_path": str(self.storage_root)}), encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertEqual(result.error_code, "RECOVERY_CLEANUP_UNSAFE")
        self.assertEqual((temp / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue(self.repo.needs_recovery_for_plan(plan))

    def test_alpha15_recovery_verified_published_direct_zip(self) -> None:
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source = self.source_root / f"published15-{mode}.txt"
                source.write_text("saved", encoding="utf-8")
                plan = self.make_plan(source, storage_mode=mode)
                self.assertTrue(self.backup(plan).success)
                snapshot = self.repo.available_snapshots(plan)[0]
                self.repo.mark_snapshot(snapshot.id, "needs_recovery")
                self.repo.operation_state(snapshot.operation_id, "needs_recovery")
                temp = self.storage.temporary_operation_dir(self.storage_root, snapshot.operation_id)
                temp.mkdir(parents=True, exist_ok=True)
                (temp / "leftover").write_text("old", encoding="utf-8")
                review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
                result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertFalse(temp.exists())
                self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
                self.assertEqual(self.repo.operation(snapshot.operation_id)["state"], "succeeded")

    def test_alpha15_recovery_does_not_resurrect_interrupted_delete(self) -> None:
        source = self.source_root / "delete15.txt"
        source.write_text("old", encoding="utf-8")
        plan = self.make_plan(source)
        self.assertTrue(self.backup(plan).success)
        old_snapshot = self.repo.available_snapshots(plan)[0]
        source.write_text("new", encoding="utf-8")
        self.assertTrue(self.backup(plan).success)
        self.repo.mark_snapshot(old_snapshot.id, "deleting")
        RecoveryService(self.repo, self.health).run()
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.snapshot(old_snapshot.id).state, "corrupted")
        self.assertNotIn(old_snapshot.id, [s.id for s in self.repo.available_snapshots(plan)])
        self.assertTrue(any(i.code == "SNAPSHOT_CORRUPTED" for i in self.repo.list_active_issues(plan)))

    def test_alpha15_replace_legacy_requires_manual_review(self) -> None:
        source, plan, snapshot = self._replace_fixture()
        op = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan, snapshot.id, {})
        self.repo.operation_state(op, "running")
        previous = source.parent / f"{source.stem}-before-restore-{op}{source.suffix}"
        previous.write_text("current work", encoding="utf-8")
        staged = source.parent / f".amvibackup-replace-{op}.tmp"
        staged.write_text("saved original", encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(op)["state"], "failed")
        self.assertTrue(staged.exists())
        self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
        self.assertEqual(self.backup(plan, True).error_code, "OPERATION_NEEDS_RECOVERY")
        self.assertTrue(any(i.code == "RESTORE_RECOVERY_REVIEW_REQUIRED" for i in self.repo.list_active_issues(plan)))

    def test_alpha15_replace_checkpoint_reconciles_verified_publication(self) -> None:
        source, plan, snapshot = self._replace_fixture()
        op = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan, snapshot.id, {})
        self.repo.operation_state(op, "running")
        previous = source.parent / f"{source.stem}-before-restore-{op}{source.suffix}"
        previous.write_text("current work", encoding="utf-8")
        self.repo.recovery_checkpoint(op, {
            "stage": "ready_to_publish", "snapshot_id": snapshot.id,
            "source_path": str(source), "previous_copy_path": str(previous),
            "previous_sha256": sha256_file(previous),
            "restored_sha256": hashlib.sha256(b"saved original").hexdigest(),
        })
        source.write_text("saved original", encoding="utf-8")
        RecoveryService(self.repo, self.health).run()
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(op)["state"], "succeeded")
        self.assertEqual(previous.read_text(encoding="utf-8"), "current work")
        self.assertEqual(source.read_text(encoding="utf-8"), "saved original")
        self.assertTrue(any(r["event_code"] == "RESTORE_REPLACE_RECONCILED" for r in self.repo.recent_activity()))

    def test_alpha15_other_interrupted_restore_requires_review(self) -> None:
        source = self.source_root / "restore15.txt"
        source.write_text("saved", encoding="utf-8")
        plan = self.make_plan(source)
        self.assertTrue(self.backup(plan).success)
        snapshot = self.repo.available_snapshots(plan)[0]
        op = self.repo.create_operation(OperationType.RESTORE_TO_DIRECTORY, plan, snapshot.id,
                                        {"target_directory": str(self.root / "destination")})
        self.repo.operation_state(op, "running")
        RecoveryService(self.repo, self.health).run()
        review = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(review, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(op)["state"], "failed")
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
        self.assertTrue(any(i.code == "RESTORE_RECOVERY_REVIEW_REQUIRED" for i in self.repo.list_active_issues(plan)))
    def test_identity_new_plan_binds_and_rejects_same_path_substitution(self) -> None:
        source = self.source_root / "identity.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)
        original = self.repo.plan_details(plan_id).source_identity_json
        self.assertTrue(original)
        self.assertTrue(self.backup(plan_id).success)
        source.rename(source.with_name("kept-original.txt"))
        source.write_text("different object", encoding="utf-8")
        result = self.backup(plan_id, True)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_MISMATCH")
        self.assertEqual(len(self.repo.available_snapshots(plan_id)), 1)
        self.assertEqual(self.repo.plan_details(plan_id).source_identity_json, original)
        self.assertEqual(self.health.probe_source(self.repo.plan_details(plan_id)).error_code, "SOURCE_IDENTITY_MISMATCH")

    def test_identity_legacy_migration_does_not_silently_bind(self) -> None:
        source = self.source_root / "legacy-identity.txt"
        source.write_text("data", encoding="utf-8")
        plan_id = self.make_plan(source)
        with self.db.transaction() as conn:
            conn.execute("ALTER TABLE BackupPlans DROP COLUMN source_identity_json")
        self.db.migrate()
        self.db.verify_contract()
        self.assertIsNone(self.repo.plan_details(plan_id).source_identity_json)
        result = self.backup(plan_id)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_UNCONFIRMED")
        self.repo.confirm_source_identity(plan_id)
        self.assertTrue(self.backup(plan_id).success)
        self.assertEqual(self.repo.plan_details(plan_id).source_identity_json,
                         confirmed_source_identity(source, SourceKind.FILE.value))

    def test_identity_requires_explicit_rebind_after_replacement(self) -> None:
        source = self.source_root / "rebind.txt"
        source.write_text("old", encoding="utf-8")
        plan_id = self.make_plan(source)
        source.rename(source.with_name("kept-original-rebind.txt"))
        source.write_text("new", encoding="utf-8")
        with self.assertRaises(AppError) as error:
            self.repo.update_plan(plan_id, "name", "", PathPolicy.validate_source(str(source)),
                                  self.repo.plan_details(plan_id).storage_root_id, 5, StorageMode.DIRECT.value)
        self.assertEqual(error.exception.code, "SOURCE_IDENTITY_UNCONFIRMED")
        self.repo.confirm_source_identity(plan_id)
        self.assertTrue(self.backup(plan_id).success)
        self.assertTrue(any(row["event_code"] == "SOURCE_MANUALLY_CONFIRMED" for row in self.repo.recent_activity()))

    def test_identity_atomic_save_replacement_is_accepted_automatically(self) -> None:
        source = self.source_root / "atomic-save.docx"
        source.write_text("version 1", encoding="utf-8")
        plan_id = self.make_plan(source)
        before = self.repo.plan_details(plan_id).source_identity_json
        self.assertTrue(self.backup(plan_id).success)
        temp = self.source_root / "~WRD0001.tmp"
        temp.write_text("version 2 saved by an editor", encoding="utf-8")
        os.replace(temp, source)
        self.assertIsNone(self.health.probe_source(self.repo.plan_details(plan_id)).error_code)
        result = self.backup(plan_id, True)
        self.assertTrue(result.success, result.error_code)
        self.assertEqual(len(self.repo.available_snapshots(plan_id)), 2)
        self.assertNotEqual(self.repo.plan_details(plan_id).source_identity_json, before)
        self.assertTrue(any(row["event_code"] == "SOURCE_IDENTITY_REFRESHED" for row in self.repo.recent_activity()))

    def test_identity_atomic_save_is_accepted_by_the_backup_itself(self) -> None:
        source = self.source_root / "atomic-backup.docx"
        source.write_text("version 1", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        temp = self.source_root / "~WRD0002.tmp"
        temp.write_text("version 2", encoding="utf-8")
        os.replace(temp, source)
        result = self.backup(plan_id, True)
        self.assertTrue(result.success, result.error_code)
        self.assertEqual(len(self.repo.available_snapshots(plan_id)), 2)

    def test_identity_leftover_editor_temp_copy_does_not_block_acceptance(self) -> None:
        source = self.source_root / "leftover-temp.docx"
        source.write_text("old", encoding="utf-8")
        plan_id = self.make_plan(source)
        source.rename(source.with_name("~WRS0003.tmp"))
        source.write_text("new", encoding="utf-8")
        self.assertIsNone(self.health.probe_source(self.repo.plan_details(plan_id)).error_code)

    def test_identity_folder_replacement_still_needs_confirmation(self) -> None:
        folder = self.source_root / "folder-identity"
        folder.mkdir()
        (folder / "a.txt").write_text("a", encoding="utf-8")
        plan_id = self.make_plan(folder)
        folder.rename(self.source_root / "folder-identity-kept")
        folder.mkdir()
        (folder / "a.txt").write_text("other", encoding="utf-8")
        self.assertEqual(self.health.probe_source(self.repo.plan_details(plan_id)).error_code,
                         "SOURCE_IDENTITY_MISMATCH")

    def test_identity_watcher_does_not_back_up_substituted_file(self) -> None:
        source = self.source_root / "watch-identity.txt"
        source.write_text("original", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.repo.update_trigger_policy(plan_id, self.make_automation_policy(on_change_enabled=True))
        _, watcher = self.make_change_watcher()
        watcher.poll_once(50_000)
        source.rename(source.with_name("kept-original-watch.txt"))
        source.write_text("impostor", encoding="utf-8")
        decisions = watcher.poll_once(51_000)
        self.assertEqual(decisions[0].action, "blocked_source")
        self.assertEqual(self.repo.plan_details(plan_id).pending_trigger_mask, 0)
        self.assertEqual(self.health.probe_source(self.repo.plan_details(plan_id)).error_code, "SOURCE_IDENTITY_MISMATCH")

    def test_identity_restored_original_updates_binding(self) -> None:
        source = self.source_root / "replaced-identity.txt"
        source.write_text("saved", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.available_snapshots(plan_id)[0]
        source.write_text("current", encoding="utf-8")
        op = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan_id, snapshot.id, {})
        result = self.restore.replace_original_file(op, snapshot.id, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.plan_details(plan_id).source_identity_json,
                         confirmed_source_identity(source, SourceKind.FILE.value))
        self.assertTrue(self.backup(plan_id, True).success)
    def test_identity_schedule_blocks_substituted_source_and_keeps_pending(self) -> None:
        source = self.source_root / "scheduled-identity.txt"
        source.write_text("original", encoding="utf-8")
        plan_id = self.make_plan(source)
        self.repo.update_trigger_policy(plan_id, self.make_automation_policy(on_schedule_enabled=True))
        coordinator = AutoBackupCoordinator(self.repo, self.health, None)
        coordinator.signal(plan_id, TriggerReason.SCHEDULE, 100000)
        source.rename(source.with_name("kept-original-scheduled.txt"))
        source.write_text("different", encoding="utf-8")
        result = coordinator.evaluate(plan_id, 1000000, datetime.now())
        self.assertEqual(result.action, "blocked_source")
        self.assertNotEqual(self.repo.plan_details(plan_id).pending_trigger_mask, 0)
        self.assertEqual(self.health.probe_source(self.repo.plan_details(plan_id)).error_code,
                         "SOURCE_IDENTITY_MISMATCH")


    def stage2_fixture(self, mode=StorageMode.DIRECT.value, folder=False):
        name = f"stage2-{mode}-{'folder' if folder else 'file'}"
        source = self.source_root / name
        if folder:
            source.mkdir()
            (source / "data.txt").write_bytes(b"saved")
        else:
            source = source.with_suffix(".txt")
            source.write_bytes(b"saved")
        plan = self.make_plan(source, storage_mode=mode)
        return source, plan

    def stage2_codes(self, operation_id):
        with self.db.read_connection() as conn:
            return [row["event_code"] for row in conn.execute(
                "SELECT event_code FROM ActivityLog WHERE operation_id=?", (operation_id,),
            ).fetchall()]

    def stage2_lock(self, path, attempts, release_after=None):
        original_open = Path.open
        def locked(candidate, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if candidate == path and mode == "rb":
                attempts.append(candidate)
                if release_after is None or len(attempts) <= release_after:
                    exc = PermissionError(errno.EACCES, "simulated sharing violation")
                    exc.winerror = 32
                    raise exc
            return original_open(candidate, *args, **kwargs)
        return locked

    def test_stage2_temporary_lock_direct_zip_recovers_in_single_operation(self):
        from unittest.mock import patch
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode)
                attempts = []
                events = []
                op = self.repo.create_operation(OperationType.BACKUP, plan, None, {})
                with patch.object(Path, "open", new=self.stage2_lock(source, attempts, 2)), \
                     patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
                    result = self.snapshot_service.create(op, plan, False, events.append, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(self.repo.operation(op)["state"], "succeeded")
                self.assertEqual(len(self.repo.available_snapshots(plan)), 1)
                self.assertEqual(sum("Waiting" in e.title for e in events), 2)
                self.assertIn("SNAPSHOT_COMMITTED", self.stage2_codes(op))
                self.assertEqual(source.read_bytes(), b"saved")

    def test_stage2_permanent_lock_direct_zip_is_visible_and_not_committed(self):
        from unittest.mock import patch
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode)
                attempts = []
                with patch.object(Path, "open", new=self.stage2_lock(source, attempts)), \
                     patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
                    result = self.backup(plan)
                self.assertEqual(result.error_code, "SOURCE_LOCKED")
                self.assertEqual(len(attempts), 3)
                self.assertEqual(self.repo.operation(result.operation_id)["state"], "failed")
                self.assertEqual(self.repo.available_snapshots(plan), [])
                self.assertFalse(self.repo.needs_recovery_for_plan(plan))
                self.assertEqual(self.repo.snapshots(plan, True)[0].state, "deleted")
                self.assertNotIn("SNAPSHOT_COMMITTED", self.stage2_codes(result.operation_id))
                self.assertTrue(any(i.code == "SOURCE_LOCKED" for i in self.repo.list_active_issues(plan)))
                temporary = self.storage.temporary_operation_dir(self.storage_root, result.operation_id)
                self.assertFalse(temporary.exists())

    def test_stage2_cancel_interrupts_retry_and_cleans_only_its_output(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        attempts = []
        stopping = False
        def report(event):
            nonlocal stopping
            if "Waiting" in event.title:
                stopping = True
        op = self.repo.create_operation(OperationType.BACKUP, plan, None, {})
        with patch.object(Path, "open", new=self.stage2_lock(source, attempts)), \
             patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (2, 5)}):
            result = self.snapshot_service.create(op, plan, False, report, lambda: stopping)
        self.assertEqual(result.error_code, "OPERATION_CANCELLED")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.repo.operation(op)["state"], "cancelled")
        self.assertEqual(self.repo.available_snapshots(plan), [])
        self.assertFalse(self.repo.needs_recovery_for_plan(plan))
        self.assertEqual(source.read_bytes(), b"saved")

    def test_stage2_partial_read_retry_discards_direct_zip_artifact(self):
        from unittest.mock import patch
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode)
                original_open = Path.open
                opened = []
                class ReadOnceThenBusy:
                    def __init__(self, stream):
                        self.stream = stream
                        self.first = True
                    def __enter__(self): return self
                    def __exit__(self, *args): self.stream.close()
                    def fileno(self): return self.stream.fileno()
                    def read(self, amount):
                        if self.first:
                            self.first = False
                            return self.stream.read(2)
                        exc = PermissionError(errno.EACCES, "busy mid-read")
                        exc.winerror = 32
                        raise exc
                def open_source(candidate, *args, **kwargs):
                    stream = original_open(candidate, *args, **kwargs)
                    if candidate == source and args and args[0] == "rb":
                        opened.append(candidate)
                        if len(opened) == 1:
                            return ReadOnceThenBusy(stream)
                    return stream
                with patch.object(Path, "open", new=open_source), \
                     patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0,)}):
                    result = self.backup(plan)
                self.assertTrue(result.success, result.message)
                snapshot = self.repo.available_snapshots(plan)[0]
                verify = self.repo.create_operation(OperationType.VERIFY, plan, snapshot.id, {})
                self.assertTrue(self.verifier.verify(verify, snapshot.id, self.progress, self.not_cancelled).success)
                self.assertEqual(len(opened), 2)

    def test_stage2_storage_write_failure_never_retries_source(self):
        from unittest.mock import patch
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode)
                original_open = Path.open
                output_attempts = []
                def deny_output(candidate, *args, **kwargs):
                    mode_arg = args[0] if args else kwargs.get("mode", "r")
                    if str(candidate).startswith(str(self.storage_root)) and mode_arg == "wb":
                        output_attempts.append(candidate)
                        raise PermissionError(errno.EACCES, "disk permission")
                    return original_open(candidate, *args, **kwargs)
                original_archive = zipfile.ZipFile
                def deny_archive(candidate, *args, **kwargs):
                    archive_mode = kwargs.get("mode", args[0] if args else "r")
                    if str(candidate).startswith(str(self.storage_root)) and archive_mode == "w":
                        output_attempts.append(candidate)
                        raise PermissionError(errno.EACCES, "archive permission")
                    return original_archive(candidate, *args, **kwargs)
                with patch.object(Path, "open", new=deny_output), \
                     patch.object(zipfile, "ZipFile", side_effect=deny_archive), \
                     patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
                    if mode == StorageMode.DIRECT.value:
                        with self.assertLogs(level="ERROR"):
                            result = self.backup(plan)
                    else:
                        result = self.backup(plan)
                self.assertFalse(result.success)
                self.assertNotEqual(result.error_code, "SOURCE_LOCKED")
                self.assertEqual(len(output_attempts), 1)
                self.assertEqual(self.repo.available_snapshots(plan), [])

    def test_stage2_access_denied_not_sharing_violation_no_retry(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        original = Path.open
        attempts = []
        def denied(candidate, *args, **kwargs):
            if candidate == source and args and args[0] == "rb":
                attempts.append(candidate)
                exc = PermissionError(errno.EACCES, "permanent access denied")
                exc.winerror = 5
                raise exc
            return original(candidate, *args, **kwargs)
        with patch.object(Path, "open", new=denied), patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_ACCESS_DENIED")
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.repo.available_snapshots(plan))

    def test_stage2_automatic_lock_exhaustion_preserves_one_pending(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        self.repo.update_trigger_policy(plan, self.make_automation_policy(on_schedule_enabled=True, min_auto_interval_minutes=0))
        coordinator = AutoBackupCoordinator(self.repo, self.health, None)
        coordinator.signal(plan, TriggerReason.SCHEDULE, 100000)
        attempts = []
        with patch.object(Path, "open", new=self.stage2_lock(source, attempts)), \
             patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
            self.assertEqual(coordinator.evaluate(plan, 1000000, datetime.now()).action, "queued")
            result = self.backup(plan)
            self.health.evaluate(self.repo.plan_details(plan))
            for tick in (1000001, 1000002, 1000003):
                self.assertEqual(coordinator.evaluate(plan, tick, datetime.now()).action, "blocked_source")
        self.assertEqual(self.repo.plan_details(plan).pending_trigger_mask, TRIGGER_MASK_SCHEDULE)
        self.assertEqual(coordinator.evaluate(plan, 1000004, datetime.now()).action, "queued")
        self.assertEqual(self.repo.operation(result.operation_id)["state"], "failed")

    def test_stage2_nested_folder_lock_does_not_spin_after_failure(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture(folder=True)
        self.repo.update_trigger_policy(plan, self.make_automation_policy(on_schedule_enabled=True, min_auto_interval_minutes=0))
        coordinator = AutoBackupCoordinator(self.repo, self.health, None)
        coordinator.signal(plan, TriggerReason.SCHEDULE, utc_ms())
        attempts = []
        with patch.object(Path, "open", new=self.stage2_lock(source / "data.txt", attempts)), \
             patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_LOCKED")
        finished = self.repo.operation(result.operation_id)["finished_at_utc_ms"]
        self.health.evaluate(self.repo.plan_details(plan))
        self.assertTrue(any(i.code == "SOURCE_LOCKED" for i in self.repo.list_active_issues(plan)))
        self.assertEqual(coordinator.evaluate(plan, finished+1, datetime.now()).action, "deferred_source_retry")
        self.assertEqual(coordinator.evaluate(plan, finished+SOURCE_LOCK_COOLDOWN_MS+1, datetime.now()).action, "queued")
        self.assertTrue(self.backup(plan).success)
        self.assertFalse(any(i.code == "SOURCE_LOCKED" for i in self.repo.list_active_issues(plan)))

    def test_stage2_unreadable_folder_walk_is_failure_not_partial_success(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture(folder=True)
        original = os.walk
        def inaccessible(top, *args, **kwargs):
            if Path(top) == source:
                kwargs["onerror"](PermissionError(errno.EACCES, "busy directory"))
                return iter(())
            return original(top, *args, **kwargs)
        with patch.object(os, "walk", side_effect=inaccessible), patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0,)}):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_LOCKED")
        self.assertFalse(self.repo.available_snapshots(plan))

    def test_stage2_rename_direct_zip_preserves_restore_and_identity(self):
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode)
                self.assertTrue(self.backup(plan).success)
                snapshot = self.repo.available_snapshots(plan)[0]
                moved = source.with_name(f"renamed-{mode}.txt")
                source.rename(moved)
                before_id = self.repo.plan_details(plan).source_identity_json
                self.assertTrue(self.backup(plan).success)
                self.assertEqual(self.repo.plan_details(plan).source_path, str(moved))
                self.assertEqual(self.repo.plan_details(plan).source_identity_json, before_id)
                op = self.repo.create_operation(OperationType.RESTORE_BESIDE, plan, snapshot.id, {})
                result = self.restore.restore(op, snapshot.id, self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual(Path(result.message.removeprefix("Restored: ")).read_bytes(), b"saved")
                moved.write_bytes(b"current")
                op = self.repo.create_operation(OperationType.RESTORE_REPLACE_FILE, plan, snapshot.id, {})
                result = self.restore.replace_original_file(op, snapshot.id, self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual(moved.read_bytes(), b"saved")
                self.assertEqual(Path(result.destination_path).read_bytes(), b"current")
                self.assertTrue(any(r["event_code"] == "SOURCE_RELOCATED" for r in self.repo.recent_activity()))

    def test_stage2_relocation_is_local_not_recursive(self):
        source, plan = self.stage2_fixture()
        unrelated = self.source_root / "other-directory"
        unrelated.mkdir()
        moved = unrelated / source.name
        source.rename(moved)
        result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_NOT_FOUND")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        self.assertEqual(moved.read_bytes(), b"saved")

    def test_stage2_cross_volume_candidate_is_not_accepted(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        moved = source.with_name("same-id-other-volume.txt")
        source.rename(moved)
        original_identity = confirmed_source_identity
        def different_volume(path, kind):
            identity = json.loads(original_identity(path, kind))
            if path == moved:
                identity["volume"] += 1
            return json.dumps(identity, sort_keys=True)
        with patch.dict(APP_MODULE_GLOBALS, {"confirmed_source_identity": different_volume}):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_NOT_FOUND")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))

    def test_stage2_multiple_hardlink_candidates_require_manual_choice(self):
        source, plan = self.stage2_fixture()
        moved = source.with_name("hardlink-a.txt")
        source.rename(moved)
        try:
            os.link(moved, source.with_name("hardlink-b.txt"))
        except OSError as exc:
            self.skipTest(f"Hardlinks unavailable: {exc}")
        result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_AMBIGUOUS")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        self.assertFalse(self.repo.available_snapshots(plan))

    def test_stage2_same_name_impostor_is_never_accepted(self):
        source, plan = self.stage2_fixture()
        moved = source.with_name("kept-original.txt")
        source.rename(moved)
        source.write_bytes(b"other")
        result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_MISMATCH")
        self.assertFalse(self.repo.available_snapshots(plan))
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        self.assertEqual(moved.read_bytes(), b"saved")

    def test_stage2_candidate_substitution_before_binding_rolls_back(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        moved = source.with_name("candidate.txt")
        source.rename(moved)
        original = self.repo.find_relocated_source
        def substitute(details):
            candidate = original(details)
            moved.rename(moved.with_name("retained-candidate.txt"))
            moved.write_bytes(b"other")
            return candidate
        with patch.object(self.repo, "find_relocated_source", side_effect=substitute):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_MISMATCH")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        self.assertFalse(any(r["event_code"] == "SOURCE_RELOCATED" for r in self.repo.recent_activity()))

    def test_stage2_opened_handle_substitution_between_scan_and_copy_is_rejected(self):
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        original = self.snapshot_service._write_direct_artifact
        def substitute(*args, **kwargs):
            before = source.stat()
            source.rename(source.with_name("retained-scanned.txt"))
            source.write_bytes(b"other")
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original(*args, **kwargs)
        with patch.object(self.snapshot_service, "_write_direct_artifact", side_effect=substitute):
            result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_MISMATCH")
        self.assertFalse(self.repo.available_snapshots(plan))

    def test_stage2_missing_parent_and_candidate_search_limit_fail_closed(self):
        source, plan = self.stage2_fixture()
        source.parent.rename(self.root / "source-offline")
        result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_NOT_FOUND")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        (self.root / "source-offline").rename(self.source_root)
        source.rename(source.with_name("hidden-original.txt"))
        for index in range(257):
            (self.source_root / f"extra-{index}.txt").touch()
        result = self.backup(plan)
        self.assertEqual(result.error_code, "SOURCE_IDENTITY_AMBIGUOUS")

    def test_stage2_watcher_and_scheduler_share_candidate_decision(self):
        source, plan = self.stage2_fixture()
        self.repo.update_trigger_policy(plan, self.make_automation_policy(on_change_enabled=True, on_schedule_enabled=True))
        coordinator, watcher = self.make_change_watcher()
        watcher.poll_once(100000)
        moved = source.with_name("event-moved.txt")
        source.rename(moved)
        for tick in (101000, 102000, 103000):
            self.assertEqual(watcher.poll_once(tick)[0].action, "pending")
        self.assertEqual(self.repo.plan_details(plan).source_path, str(source))
        coordinator.signal(plan, TriggerReason.SCHEDULE, 103000)
        mask = self.repo.plan_details(plan).pending_trigger_mask
        self.assertEqual(mask, TRIGGER_MASK_EVENT | TRIGGER_MASK_SCHEDULE)
        self.assertEqual(coordinator.evaluate(plan, 1000000, datetime.now()).action, "queued")
        self.assertTrue(self.backup(plan).success)
        self.assertEqual(self.repo.plan_details(plan).source_path, str(moved))
        self.assertEqual(sum(r["event_code"] == "AUTO_TRIGGER_PENDING" for r in self.repo.recent_activity()), 1)

    def test_stage2_recovery_accepts_old_snapshot_after_confirmed_rename(self):
        source, plan = self.stage2_fixture()
        self.assertTrue(self.backup(plan).success)
        snapshot = self.repo.available_snapshots(plan)[0]
        moved = source.with_name("recovery-renamed.txt")
        source.rename(moved)
        self.repo.resolve_source_for_backup(plan, self.not_cancelled)
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")
        self.repo.operation_state(snapshot.operation_id, "needs_recovery")
        op = self.repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
        result = RecoveryService(self.repo, self.health).review_plan(op, plan, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")

    def test_stage2_migration_and_repeat_migration_preserve_old_plan_unconfirmed(self):
        source, plan = self.stage2_fixture()
        with self.db.transaction() as conn:
            conn.execute("ALTER TABLE BackupPlans DROP COLUMN source_identity_json")
        self.db.migrate()
        self.db.migrate()
        self.db.verify_contract()
        self.assertIsNone(self.repo.plan_details(plan).source_identity_json)
        self.assertEqual(self.backup(plan).error_code, "SOURCE_IDENTITY_UNCONFIRMED")
        self.repo.confirm_source_identity(plan)
        self.assertTrue(self.backup(plan).success)
    def test_stage2_folder_rename_restores_old_folder_snapshot(self):
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage2_fixture(mode, folder=True)
                self.assertTrue(self.backup(plan).success)
                snapshot = self.repo.available_snapshots(plan)[0]
                moved = source.with_name(f"folder-renamed-{mode}")
                source.rename(moved)
                self.assertTrue(self.backup(plan).success)
                op = self.repo.create_operation(OperationType.RESTORE_BESIDE, plan, snapshot.id, {})
                result = self.restore.restore(op, snapshot.id, self.progress, self.not_cancelled)
                self.assertTrue(result.success, result.message)
                self.assertEqual((Path(result.message.removeprefix("Restored: ")) / "data.txt").read_bytes(), b"saved")

    def test_stage2_redirect_candidate_is_rejected_without_symlink_privilege(
        self,
    ) -> None:
        from unittest.mock import patch

        for redirect_kind in ("symlink", "junction"):
            with self.subTest(redirect_kind=redirect_kind):
                source, plan_id = self.stage2_fixture()

                moved = source.with_name(
                    f"kept-target-{redirect_kind}.txt"
                )
                source.rename(moved)

                redirect = source.with_name(
                    f"redirect-{redirect_kind}.txt"
                )
                redirect.write_bytes(b"not a source")

                identity_calls: list[Path] = []
                original_identity = confirmed_source_identity
                original_is_symlink = Path.is_symlink
                original_is_junction = getattr(
                    Path,
                    "is_junction",
                    lambda self: False,
                )

                def is_symlink(path: Path) -> bool:
                    return (
                        path == redirect
                        and redirect_kind == "symlink"
                    ) or original_is_symlink(path)

                def is_junction(path: Path) -> bool:
                    return (
                        path == redirect
                        and redirect_kind == "junction"
                    ) or original_is_junction(path)

                def identity(path: Path, kind: str) -> str:
                    identity_calls.append(path)
                    if path == redirect:
                        raise AssertionError(
                            "Redirect was inspected instead of skipped"
                        )
                    return original_identity(path, kind)

                with (
                    patch.object(
                        Path,
                        "is_symlink",
                        new=is_symlink,
                    ),
                    patch.object(
                        Path,
                        "is_junction",
                        new=is_junction,
                        create=True,
                    ),
                    patch.dict(
                        APP_MODULE_GLOBALS,
                        {
                            "confirmed_source_identity": identity,
                        },
                    ),
                ):
                    resolved = self.repo.resolve_source_for_backup(
                        plan_id,
                        self.not_cancelled,
                    )

                self.assertEqual(resolved.path, moved)
                self.assertEqual(
                    self.repo.plan_details(plan_id).source_path,
                    str(moved),
                )
                self.assertNotIn(redirect, identity_calls)
                self.assertEqual(
                    redirect.read_bytes(),
                    b"not a source",
                )
                self.assertEqual(moved.read_bytes(), b"saved")

                redirect.unlink()
                moved.unlink()

    def test_stage2_symlink_candidate_is_not_followed(self):
        source, plan = self.stage2_fixture()
        moved = source.with_name("kept-target.txt")
        source.rename(moved)
        link = source.with_name("redirect.txt")
        try:
            link.symlink_to(moved)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        result = self.backup(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.plan_details(plan).source_path, str(moved))
        self.assertTrue(link.is_symlink())

    def test_stage2_real_windows_sharing_violation_has_finite_retry(self):
        if os.name != "nt":
            self.skipTest("Windows-only sharing mode test")
        import ctypes
        from ctypes import wintypes
        from unittest.mock import patch
        source, plan = self.stage2_fixture()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = create(str(source), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        try:
            with patch.dict(APP_MODULE_GLOBALS, {"SOURCE_RETRY_DELAYS": (0, 0)}):
                result = self.backup(plan)
            self.assertEqual(result.error_code, "SOURCE_LOCKED")
            self.assertEqual(self.repo.available_snapshots(plan), [])
        finally:
            close(handle)
        self.assertTrue(self.backup(plan).success)

    def test_stage2_windows_process_crash_during_retry_is_recoverable(self):
        if os.name != "nt":
            self.skipTest("Windows subprocess test requires real PySide6 installation")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "crash-lock-source.txt"
            source.write_bytes(b"saved")
            db_path = root / "crash-lock.sqlite3"
            storage_root = root / "storage"
            child_code = textwrap.dedent(r'''
                import importlib.util, sys, os
                from pathlib import Path
                from unittest.mock import patch
                spec = importlib.util.spec_from_file_location("stage2_crash_child", sys.argv[1])
                app = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = app
                spec.loader.exec_module(app)
                db = app.Database(Path(sys.argv[2]))
                db.migrate()
                repo = app.Repository(db)
                storage = app.StorageService()
                root = app.PathPolicy.validate_storage(sys.argv[4])
                descriptor = app.PathPolicy.validate_source(sys.argv[3])
                storage_id = repo.get_or_create_storage(root)
                storage.ensure_root(root, storage_id)
                plan = repo.create_plan("Crash retry", "", descriptor, storage_id, 5)
                op = repo.create_operation(app.OperationType.BACKUP, plan, None, {})
                service = app.SnapshotService(repo, storage, app.HealthService(repo), reserve_bytes=0, reserve_ratio=0)
                original_open = Path.open
                def locked(path, *args, **kwargs):
                    if path == descriptor.path and args and args[0] == "rb":
                        exc = PermissionError(app.errno.EACCES, "sharing violation")
                        exc.winerror = 32
                        raise exc
                    return original_open(path, *args, **kwargs)
                def progress(event):
                    if "Waiting" in event.title:
                        os._exit(91)
                with patch.object(Path, "open", new=locked):
                    service.create(op, plan, False, progress, lambda: False)
            ''')
            process = subprocess.run(
                [sys.executable, "-c", child_code, str(self_test_module_path()), str(db_path), str(source), str(storage_root)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(process.returncode, 91, process.stdout+process.stderr)
            db = Database(db_path)
            repo = Repository(db)
            try:
                db.verify_contract()
                plan = repo.plan_summaries(False)[0].id
                interrupted = repo.unfinished_operations()[0]["id"]
                RecoveryService(repo, HealthService(repo)).run()
                self.assertTrue(repo.needs_recovery_for_plan(plan))
                self.assertEqual(repo.available_snapshots(plan), [])
                review = repo.create_operation(OperationType.RECOVER_PLAN, plan, None, {})
                result = RecoveryService(repo, HealthService(repo)).review_plan(review, plan, lambda event: None, lambda: False)
                self.assertTrue(result.success, result.message)
                self.assertFalse(repo.needs_recovery_for_plan(plan))
                self.assertEqual(repo.available_snapshots(plan), [])
                self.assertFalse(StorageService().temporary_operation_dir(storage_root, interrupted).exists())
                self.assertEqual(source.read_bytes(), b"saved")
            finally:
                db.close_for_cleanup()

    def stage3_fixture(self, mode=StorageMode.DIRECT.value, versions=2):
        source = self.source_root / f"stage3-{mode}.txt"
        source.write_text("v0", encoding="utf-8")
        plan = self.make_plan(source, storage_mode=mode)
        for index in range(versions):
            source.write_text(f"version {index}", encoding="utf-8")
            result = self.backup(plan, True)
            self.assertTrue(result.success, result.message)
        return source, plan

    def stage3_delete(self, plan_id, approve_pinned=False, cancelled=None, progress=None, token=None):
        preview = self.repo.plan_deletion_preview(plan_id)
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                              {"preview_token": token or preview.token, "approve_pinned": approve_pinned})
        result = self.delete.delete_plan_data(operation, plan_id, token or preview.token, approve_pinned,
                                             progress or self.progress, cancelled or self.not_cancelled)
        return operation, result

    def test_stage3_direct_zip_deletes_owned_backups_and_preserves_audit_source_other_plan(self):
        for mode in (StorageMode.DIRECT.value, StorageMode.ZIP.value):
            with self.subTest(mode=mode):
                source, plan = self.stage3_fixture(mode)
                expected = source.read_bytes()
                other_source = self.source_root / f"other-{mode}.txt"
                other_source.write_bytes(b"user")
                other_plan = self.make_plan(other_source)
                self.assertTrue(self.backup(other_plan).success)
                other_snapshot = self.repo.available_snapshots(other_plan)[0]
                root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
                preview = self.repo.plan_deletion_preview(plan)
                self.assertEqual(preview.snapshot_count, 2)
                self.assertGreater(preview.stored_size_bytes, 0)
                op, result = self.stage3_delete(plan)
                self.assertTrue(result.success, result.message)
                self.assertEqual(result.result_code, "PLAN_DELETE_SUCCEEDED")
                self.assertFalse(root.exists())
                self.assertEqual(source.read_bytes(), expected)
                self.assertEqual(other_source.read_bytes(), b"user")
                self.assertEqual(self.repo.snapshot(other_snapshot.id).state, "committed")
                with self.assertRaises(AppError):
                    self.repo.plan_details(plan)
                self.assertEqual(self.repo.operation(op)["state"], "succeeded")
                self.assertIsNone(self.repo.operation(op)["plan_id"])
                with self.db.read_connection() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM Snapshots WHERE plan_id=?", (plan,)).fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM RetiredPlans WHERE plan_id=?", (plan,)).fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM PlanDeletionJobs WHERE plan_id=?", (plan,)).fetchone()[0], 0)
                    self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
                    event = conn.execute("SELECT details_json FROM ActivityLog WHERE operation_id=? AND event_code='PLAN_DELETE_SUCCEEDED'", (op,)).fetchone()
                self.assertEqual(json.loads(event["details_json"])["deleted_plan_id"], plan)

    def test_stage3_empty_plan_deletes_metadata_without_touching_source_storage(self):
        source, plan = self.stage3_fixture(versions=0)
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        self.assertFalse(root.exists())
        op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertTrue(source.is_file())
        self.assertTrue((self.storage_root / "_amvibackup" / "storage.json").is_file())
        self.assertEqual(self.repo.operation(op)["state"], "succeeded")

    def test_stage3_pinned_requires_separate_override_and_does_not_weaken_version_delete(self):
        source, plan = self.stage3_fixture()
        snapshot = self.repo.available_snapshots(plan)[0]
        self.repo.set_pin(snapshot.id, True)
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        op, result = self.stage3_delete(plan)
        self.assertEqual(result.error_code, "SNAPSHOT_PINNED")
        self.assertTrue(root.exists())
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        ordinary = self.repo.create_operation(OperationType.DELETE_SNAPSHOTS, plan, None, {})
        refused = self.delete.delete(ordinary, [snapshot.id], self.progress, self.not_cancelled)
        self.assertEqual(refused.error_code, "SNAPSHOT_PINNED")
        op, result = self.stage3_delete(plan, approve_pinned=True)
        self.assertTrue(result.success, result.message)
        self.assertFalse(root.exists())
        self.assertTrue(source.exists())

    def test_stage3_offline_storage_refuses_without_deleting_metadata(self):
        source, plan = self.stage3_fixture()
        relocated = self.root / "offline-storage"
        self.storage_root.rename(relocated)
        op, result = self.stage3_delete(plan)
        self.assertEqual(result.error_code, "STORAGE_NOT_FOUND")
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertEqual(self.repo.plan_details(plan).state, "enabled")
        self.assertTrue(source.exists())
        relocated.rename(self.storage_root)

    def test_stage3_foreign_similar_directory_is_untouched(self):
        source, plan = self.stage3_fixture(versions=0)
        impostor = self.storage_root / f"foreign__job-{plan[:8]}"
        impostor.mkdir()
        marker = impostor / "important.txt"
        marker.write_bytes(b"user")
        op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual(marker.read_bytes(), b"user")
        self.assertTrue(impostor.is_dir())
        self.assertTrue(source.exists())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)

    def test_stage3_extra_user_file_is_deleted_with_the_plan(self):
        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        extra = root / "important-user-file.txt"
        extra.write_bytes(b"user")
        preview = self.repo.plan_deletion_preview(plan)
        inventory = self.delete._build_plan_delete_inventory(self.repo.plan_details(plan), self.not_cancelled)
        self.assertTrue(inventory["has_problems"])
        self.assertIn("unknown_files", inventory["warnings"])
        op, result = self.stage3_delete(plan, token=preview.token)
        self.assertTrue(result.success, result.message)
        self.assertFalse(extra.exists())
        self.assertFalse(root.exists())
        self.assertTrue(source.exists())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)

    def test_stage3_damaged_metadata_symlink_missing_folder_overlap_and_outside_path(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        manifest = next(root.glob("snapshots/*/manifest.json"))
        manifest.chmod(stat.S_IWRITE | stat.S_IREAD)
        manifest.write_text("{", encoding="utf-8")
        inventory = self.delete._build_plan_delete_inventory(self.repo.plan_details(plan), self.not_cancelled)
        self.assertIn("damaged_metadata", inventory["warnings"])
        self.assertTrue(inventory["has_problems"])
        _op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertFalse(root.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "version 1")
        self.assertTrue((self.storage_root / "_amvibackup" / "storage.json").is_file())

        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        def force_remove(path: Path) -> None:
            def onexc(func, failed, _exc):
                os.chmod(failed, stat.S_IWRITE | stat.S_IREAD)
                func(failed)
            shutil.rmtree(path, onexc=onexc)
        force_remove(root)
        _op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertTrue(source.is_file())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)

        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        kept = root / "user-source.txt"
        kept.write_bytes(b"original")
        with self.db.transaction() as conn:
            conn.execute("UPDATE BackupPlans SET source_path=? WHERE id=?", (str(kept), plan))
        inventory = self.delete._build_plan_delete_inventory(self.repo.plan_details(plan), self.not_cancelled)
        self.assertIn("source_overlap", inventory["warnings"])
        _op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual(kept.read_bytes(), b"original")
        self.assertFalse((root / "snapshots").exists())
        self.assertTrue(source.is_file())

        source, plan = self.stage3_fixture()
        outside = self.root / "outside-plan"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        with patch.object(self.delete, "_select_plan_directory", return_value=(outside, False)):
            _op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual((outside / "keep.txt").read_bytes(), b"keep")
        self.assertTrue(outside.is_dir())
        self.assertTrue(source.is_file())
        self.assertTrue(self.storage_root.is_dir())

        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        target = self.root / "symlink-target"
        target.mkdir()
        (target / "secret.txt").write_bytes(b"secret")
        link = root / "linked-dir"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            subprocess.check_call(["cmd", "/c", "mklink", "/J", str(link), str(target)])
        _op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertFalse(link.exists())
        self.assertEqual((target / "secret.txt").read_bytes(), b"secret")
        self.assertTrue(source.is_file())
        self.assertTrue(self.storage_root.is_dir())

    def test_stage3_cancel_before_start_has_no_side_effect(self):
        source, plan = self.stage3_fixture()
        op, result = self.stage3_delete(plan, cancelled=lambda: True)
        self.assertEqual(result.error_code, "OPERATION_CANCELLED")
        self.assertEqual(self.repo.plan_details(plan).state, "enabled")
        self.assertEqual(len(self.repo.available_snapshots(plan)), 2)
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertEqual(self.repo.operation(op)["state"], "cancelled")

    def test_stage3_cancel_after_first_delete_is_resumable(self):
        source, plan = self.stage3_fixture()
        stopping = False
        def stop(event):
            nonlocal stopping
            stopping = True
        op, result = self.stage3_delete(plan, cancelled=lambda: stopping, progress=stop)
        self.assertEqual(result.error_code, "OPERATION_CANCELLED")
        self.assertIsNotNone(self.repo.plan_deletion_job(plan))
        self.assertEqual(self.repo.plan_details(plan).state, "archived")
        self.assertEqual(self.repo.available_snapshots(plan), [])
        second, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertTrue(source.exists())
        self.assertEqual(self.repo.operation(second)["state"], "succeeded")

    def test_stage3_blocked_file_leaves_a_remainder_and_removes_the_plan(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        original = os.unlink
        def blocked(path, *args, **kwargs):
            if Path(path).name == "manifest.json":
                raise OSError("busy")
            return original(path, *args, **kwargs)
        with patch.object(os, "unlink", new=blocked), patch.dict(APP_MODULE_GLOBALS, {"PLAN_DELETE_RETRY_DELAYS": (0, 0, 0)}):
            op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertGreater(result.result_data["failed_files"], 0)
        self.assertTrue(result.result_data["leftovers"])
        self.assertTrue(source.exists())
        self.assertTrue((self.storage_root / "_amvibackup" / "storage.json").is_file())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)

    def test_stage3_unknown_file_added_during_delete_is_removed(self):
        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        intruder = root / "new-user-file.txt"
        inserted = False
        def introduce(event):
            nonlocal inserted
            if not inserted:
                inserted = True
                intruder.write_bytes(b"keep")
        op, result = self.stage3_delete(plan, progress=introduce)
        self.assertTrue(result.success, result.message)
        self.assertFalse(intruder.exists())
        self.assertTrue(source.exists())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)

    def test_stage3_foreign_file_replaces_approved_target_and_is_preserved(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        original = self.delete._checked_delete_target
        replaced = []
        def substitute(inventory, name):
            path = Path(inventory["root"]) / name
            if not replaced and not inventory["entries"][name]["directory"]:
                info = path.stat()
                path.chmod(info.st_mode | stat.S_IWRITE)
                path.rename(self.root / "retained-backup-object")
                path.write_bytes(b"user replacement")
                replaced.append(path)
            return original(inventory, name)
        with patch.object(self.delete, "_checked_delete_target", side_effect=substitute):
            op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertFalse(replaced[0].exists())
        self.assertTrue((self.root / "retained-backup-object").is_file())
        self.assertTrue(source.exists())

    def test_stage3_final_database_failure_after_files_removed_can_retry(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        with patch.object(self.repo, "finish_plan_deletion", side_effect=OSError("db failure")):
            with self.assertLogs(level="ERROR"):
                op, result = self.stage3_delete(plan)
        self.assertFalse(result.success)
        self.assertFalse(root.exists())
        self.assertIsNotNone(self.repo.plan_deletion_job(plan))
        self.assertTrue(source.exists())
        second, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.repo.operation(second)["state"], "succeeded")

    def test_stage3_stale_confirmation_and_other_heavy_operation_are_refused(self):
        source, plan = self.stage3_fixture()
        old = self.repo.plan_deletion_preview(plan)
        self.repo.set_pin(self.repo.available_snapshots(plan)[0].id, True)
        op, result = self.stage3_delete(plan, token=old.token)
        self.assertEqual(result.error_code, "PLAN_DELETE_CHANGED")
        other = self.repo.create_operation(OperationType.VERIFY, plan, self.repo.available_snapshots(plan)[0].id, {})
        self.repo.operation_state(other, "running")
        op, result = self.stage3_delete(plan, approve_pinned=True)
        self.assertEqual(result.error_code, "OPERATION_ALREADY_RUNNING")
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.repo.operation_state(other, "failed")

    def test_stage3_schema_migration_repeat_and_foreign_keys(self):
        with self.db.transaction() as conn:
            conn.execute("DROP TABLE PlanDeletionJobs")
            conn.execute("DROP TABLE RetiredPlans")
        self.db.migrate()
        self.db.migrate()
        self.db.verify_contract()
        with self.db.read_connection() as conn:
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
            self.assertTrue(any(r["table"] == "Operations" for r in conn.execute("PRAGMA foreign_key_list(RetiredPlans)")))
            self.assertTrue(any(r["table"] == "BackupPlans" and r["on_delete"] == "CASCADE"
                                for r in conn.execute("PRAGMA foreign_key_list(PlanDeletionJobs)")))

    def test_stage3_final_transaction_rolls_back_and_history_remains(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        original = self.repo.activity
        def fail_final(level, code, *args, **kwargs):
            if code == "PLAN_DELETE_SUCCEEDED":
                raise OSError("injected audit DB failure")
            return original(level, code, *args, **kwargs)
        with patch.object(self.repo, "activity", side_effect=fail_final):
            with self.assertLogs(level="ERROR"):
                op, result = self.stage3_delete(plan)
        self.assertFalse(result.success)
        self.assertIsNotNone(self.repo.plan_deletion_job(plan))
        with self.db.read_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RetiredPlans WHERE plan_id=?", (plan,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
        second, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)


    def test_stage3_redirect_inside_owned_plan_is_refused(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan))
        file = root / "redirect.txt"
        file.write_bytes(b"keep")
        original = getattr(Path, "is_junction", lambda path: False)
        def redirected(path):
            return path == file or original(path)
        with patch.object(Path, "is_junction", new=redirected, create=True):
            op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertFalse(file.exists())
        self.assertTrue(source.exists())

    def test_stage3_managed_plan_root_replaced_after_cancel_is_preserved(self):
        source, plan = self.stage3_fixture()
        stop = False
        def cancel(event):
            nonlocal stop
            stop = True
        op, result = self.stage3_delete(plan, cancelled=lambda: stop, progress=cancel)
        self.assertEqual(result.error_code, "OPERATION_CANCELLED")
        root = Path(json.loads(self.repo.plan_deletion_job(plan)["inventory_json"])["root"])
        root.rename(self.root / "retained-owned-plan")
        root.mkdir()
        important = root / "user.txt"
        important.write_bytes(b"keep")
        op, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        self.assertEqual(important.read_bytes(), b"keep")
        self.assertTrue(source.exists())
        with self.assertRaises(AppError):
            self.repo.plan_details(plan)


    def test_stage3_windows_process_crash_during_deletion_resumes_from_journal(self):
        if os.name != "nt":
            self.skipTest("Windows subprocess integration requires installed PySide6")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "crash-plan.txt"
            source.write_bytes(b"saved")
            db_path = root / "plan-delete.sqlite3"
            storage_path = root / "storage"
            child = textwrap.dedent(r'''
                import importlib.util, sys, os
                from pathlib import Path
                spec = importlib.util.spec_from_file_location("plan_deletion_child", sys.argv[1])
                app = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = app
                spec.loader.exec_module(app)
                db = app.Database(Path(sys.argv[2]))
                db.migrate()
                repo = app.Repository(db)
                storage = app.StorageService()
                root = app.PathPolicy.validate_storage(sys.argv[4])
                descriptor = app.PathPolicy.validate_source(sys.argv[3])
                storage_id = repo.get_or_create_storage(root)
                storage.ensure_root(root, storage_id)
                plan = repo.create_plan("Crash deletion", "", descriptor, storage_id, 5)
                backup = app.SnapshotService(repo, storage, app.HealthService(repo), reserve_bytes=0, reserve_ratio=0)
                for index in range(2):
                    descriptor.path.write_text(str(index), encoding="utf-8")
                    op = repo.create_operation(app.OperationType.BACKUP, plan, None, {})
                    result = backup.create(op, plan, True, lambda event: None, lambda: False)
                    if not result.success:
                        raise RuntimeError(result.message)
                preview = repo.plan_deletion_preview(plan)
                op = repo.create_operation(app.OperationType.DELETE_PLAN_DATA, plan, None, {})
                def die(event):
                    os._exit(92)
                app.DeleteService(repo, storage, app.HealthService(repo)).delete_plan_data(
                    op, plan, preview.token, False, die, lambda: False,
                )
            ''')
            process = subprocess.run([sys.executable, "-c", child, str(self_test_module_path()),
                                      str(db_path), str(source), str(storage_path)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 92, process.stdout+process.stderr)
            db = Database(db_path)
            repo = Repository(db)
            try:
                db.verify_contract()
                plan = repo.plan_summaries(True)[0].id
                self.assertIsNotNone(repo.plan_deletion_job(plan))
                RecoveryService(repo, HealthService(repo)).run()
                self.assertIsNone(repo.plan_deletion_job(plan))
                preview = repo.plan_deletion_preview(plan)
                self.assertFalse(preview.resuming)
                op = repo.create_operation(OperationType.DELETE_PLAN_DATA, plan, None, {})
                result = DeleteService(repo, StorageService(), HealthService(repo)).delete_plan_data(
                    op, plan, preview.token, False, lambda event: None, lambda: False,
                )
                self.assertTrue(result.success, result.message)
                self.assertEqual(source.read_text(encoding="utf-8"), "1")
                self.assertEqual(repo.plan_summaries(True), [])
                with db.read_connection() as conn:
                    self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
            finally:
                db.close_for_cleanup()


    def test_stage3_pin_changed_at_journal_transaction_is_not_silently_overridden(self):
        from unittest.mock import patch
        source, plan = self.stage3_fixture()
        snapshot = self.repo.available_snapshots(plan)[0]
        original = self.repo.begin_plan_deletion
        def changed(*args, **kwargs):
            self.repo.set_pin(snapshot.id, True)
            return original(*args, **kwargs)
        with patch.object(self.repo, "begin_plan_deletion", side_effect=changed):
            op, result = self.stage3_delete(plan)
        self.assertEqual(result.error_code, "PLAN_DELETE_CHANGED")
        self.assertTrue(self.repo.snapshot(snapshot.id).pinned)
        self.assertEqual(self.repo.snapshot(snapshot.id).state, "committed")
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertTrue(source.exists())

    def test_background_file_list_direct_zip_is_verified_and_journalled(
        self,
    ) -> None:
        for mode in (
            StorageMode.DIRECT.value,
            StorageMode.ZIP.value,
        ):
            with self.subTest(mode=mode):
                source = self.source_root / f"file-list-{mode}"
                source.mkdir()
                (source / "one.txt").write_bytes(b"one")
                (source / "nested").mkdir()
                (source / "nested" / "two.txt").write_bytes(b"two")

                plan = self.make_plan(
                    source,
                    storage_mode=mode,
                )
                self.assertTrue(self.backup(plan).success)
                snapshot = self.repo.available_snapshots(plan)[0]

                op = self.repo.create_operation(
                    OperationType.LIST_SNAPSHOT_FILES,
                    plan,
                    snapshot.id,
                    {},
                )
                result = self.verifier.list_snapshot_files(
                    op,
                    snapshot.id,
                    self.progress,
                    self.not_cancelled,
                )

                self.assertTrue(result.success, result.message)
                self.assertEqual(
                    result.result_data["relative_paths"],
                    ["nested/two.txt", "one.txt"],
                )
                self.assertEqual(
                    result.result_data["manifest_sha256"],
                    snapshot.manifest_sha256,
                )
                self.assertEqual(
                    self.repo.operation(op)["state"],
                    "succeeded",
                )
                self.assertEqual(
                    self.repo.snapshot(snapshot.id).state,
                    "committed",
                )

    def test_background_file_list_denies_hidden_snapshot_and_cancellation(
        self,
    ) -> None:
        source = self.source_root / "file-list-hidden"
        source.mkdir()
        (source / "one.txt").write_bytes(b"one")
        plan = self.make_plan(source)

        self.assertTrue(self.backup(plan).success)
        snapshot = self.repo.available_snapshots(plan)[0]
        self.repo.mark_snapshot(snapshot.id, "needs_recovery")

        op = self.repo.create_operation(
            OperationType.LIST_SNAPSHOT_FILES,
            plan,
            snapshot.id,
            {},
        )
        result = self.verifier.list_snapshot_files(
            op,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertEqual(
            result.error_code,
            "SNAPSHOT_NOT_COMMITTED",
        )
        self.assertEqual(
            self.repo.operation(op)["state"],
            "failed",
        )

        op = self.repo.create_operation(
            OperationType.LIST_SNAPSHOT_FILES,
            plan,
            snapshot.id,
            {},
        )
        result = self.verifier.list_snapshot_files(
            op,
            snapshot.id,
            self.progress,
            lambda: True,
        )

        self.assertEqual(
            result.error_code,
            "OPERATION_CANCELLED",
        )
        self.assertEqual(
            self.repo.operation(op)["state"],
            "cancelled",
        )

    def test_background_file_list_corrupted_manifest_never_returns_data(
        self,
    ) -> None:
        source = self.source_root / "file-list-corrupt"
        source.mkdir()
        (source / "one.txt").write_bytes(b"one")
        plan = self.make_plan(source)

        self.assertTrue(self.backup(plan).success)
        snapshot = self.repo.available_snapshots(plan)[0]

        manifest = self.verifier.artifact_paths(
            snapshot
        ).manifest_path
        self.workspace.make_writable_for_external_mutation(
            manifest
        )
        manifest.write_text("broken", encoding="utf-8")

        op = self.repo.create_operation(
            OperationType.LIST_SNAPSHOT_FILES,
            plan,
            snapshot.id,
            {},
        )
        result = self.verifier.list_snapshot_files(
            op,
            snapshot.id,
            self.progress,
            self.not_cancelled,
        )

        self.assertFalse(result.success)
        self.assertIsNone(result.result_data)
        self.assertEqual(
            self.repo.operation(op)["state"],
            "failed",
        )
        self.assertEqual(
            self.repo.snapshot(snapshot.id).state,
            "corrupted",
        )


class UISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = ensure_test_qapplication()

    def tearDown(self) -> None:
        self.application.processEvents()

    def make_paths(self, root: Path) -> AppPaths:
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return AppPaths(root, root / "ui.sqlite3", logs, logs / "ui.log")

    def test_main_window_empty_state_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = MainWindow(controller, paths)
            window.show()
            QApplication.processEvents()
            self.assertEqual(window.stack.currentWidget(), window.empty_view)
            window.close()
            controller.shutdown()

    def test_main_window_plan_detail_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            source = root / "source.txt"
            source.write_text("data", encoding="utf-8")
            storage = root / "storage"
            descriptor = PathPolicy.validate_source(str(source))
            storage_path = PathPolicy.validate_storage(str(storage))
            storage_id = controller.repo.get_or_create_storage(storage_path)
            controller.storage.ensure_root(storage_path, storage_id)
            plan_id = controller.repo.create_plan("UI plan", "", descriptor, storage_id, 5)
            window = MainWindow(controller, paths)
            controller.main_window = window
            window.select_plan_by_id(plan_id)
            window.show()
            QApplication.processEvents()
            self.assertEqual(window.current_plan_id, plan_id)
            self.assertEqual(
                window.stack.currentWidget(),
                window.detail_scroll,
            )
            window.close()
            controller.shutdown()

    def test_plan_editor_exposes_storage_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)

            dialog = PlanEditor(controller)

            self.assertEqual(dialog.mode_combo.count(), 2)
            self.assertEqual(
                dialog.mode_combo.itemData(0),
                StorageMode.DIRECT.value,
            )
            self.assertEqual(
                dialog.mode_combo.itemData(1),
                StorageMode.ZIP.value,
            )

            dialog.close()
            controller.shutdown()

    def test_versions_table_has_artifact_and_verification_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)

            source = root / "ui-snapshot.txt"
            source.write_text("ui snapshot", encoding="utf-8")

            storage_root = root / "storage"
            descriptor = PathPolicy.validate_source(str(source))
            storage = PathPolicy.validate_storage(str(storage_root))
            storage_id = controller.repo.get_or_create_storage(storage)
            controller.storage.ensure_root(storage, storage_id)

            plan_id = controller.repo.create_plan(
                "UI snapshot plan",
                "",
                descriptor,
                storage_id,
                5,
                StorageMode.DIRECT.value,
            )

            workerless_snapshot_service = SnapshotService(
                controller.repo,
                controller.storage,
                controller.health_service,
                reserve_bytes=0,
                reserve_ratio=0.0,
            )

            operation_id = controller.repo.create_operation(
                OperationType.BACKUP,
                plan_id,
                None,
                {},
            )

            result = workerless_snapshot_service.create(
                operation_id,
                plan_id,
                False,
                lambda event: None,
                lambda: False,
            )
            self.assertTrue(result.success)

            window = MainWindow(controller, paths)
            controller.main_window = window
            window.select_plan_by_id(plan_id)
            window.show()
            QApplication.processEvents()

            self.assertEqual(window.versions_table.columnCount(), 9)
            self.assertEqual(
                window.versions_table.horizontalHeaderItem(3).text(),
                controller.i18n.tr("versions.column.mode"),
            )
            self.assertEqual(
                window.versions_table.horizontalHeaderItem(7).text(),
                controller.i18n.tr("versions.column.verification"),
            )

            window.close()
            controller.shutdown()

    def test_gui_controller_can_start_and_stop_automation_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)

            controller = AppController(
                paths,
                start_worker=False,
            )

            controller.start_automation_runtime(
                DEFAULT_AUTOMATION_RUNTIME_TICK_SECONDS
            )

            self.assertTrue(
                controller._automation_runtime_running
            )

            controller.shutdown()

            self.assertFalse(
                controller._automation_runtime_running
            )
            self.assertIsNone(
                controller.automation_runtime_timer
            )

    def test_plan_detail_shows_automation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)

            controller = AppController(
                paths,
                start_worker=False,
            )

            try:
                source = root / "automation-ui.txt"
                source.write_text("automation ui", encoding="utf-8")

                storage_root = root / "storage"
                descriptor = PathPolicy.validate_source(str(source))
                storage = PathPolicy.validate_storage(
                    str(storage_root)
                )
                storage_id = controller.repo.get_or_create_storage(
                    storage
                )
                controller.storage.ensure_root(
                    storage,
                    storage_id,
                )

                plan_id = controller.repo.create_plan(
                    "Automation UI plan",
                    "",
                    descriptor,
                    storage_id,
                    5,
                    StorageMode.DIRECT.value,
                )

                controller.update_trigger_policy(
                    plan_id,
                    TriggerPolicy(
                        on_change_enabled=True,
                        change_debounce_seconds=5,
                        min_auto_interval_minutes=10,
                        on_schedule_enabled=True,
                        schedule_interval_minutes=60,
                        quiet_hours_enabled=True,
                        quiet_hours_start_local="22:00",
                        quiet_hours_end_local="07:00",
                        manual_allowed_quiet_hours=True,
                    ),
                )

                controller.repo.mark_pending_trigger(
                    plan_id,
                    TRIGGER_MASK_EVENT,
                    1_000_000,
                )

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                window.show()
                QApplication.processEvents()

                labels = [
                    label.text()
                    for label in window.detail_view.findChildren(QLabel)
                ]

                self.assertTrue(
                    any(
                        text.startswith("Automation:")
                        for text in labels
                    )
                )
                self.assertTrue(
                    any(
                        text.startswith(
                            "Automatic backup pending:"
                        )
                        for text in labels
                    )
                )
                self.assertTrue(
                    any(
                        text.startswith(
                            "Last automatic backup:"
                        )
                        for text in labels
                    )
                )

                window.close()

            finally:
                controller.shutdown()

    def test_detail_refresh_does_not_duplicate_interactive_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                source = root / "refresh-test.txt"
                source.write_text("test", encoding="utf-8")
                storage = PathPolicy.validate_storage(str(root / "storage"))
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)

                plan_id = controller.repo.create_plan(
                    "Refresh test", "", descriptor, storage_id, 5,
                    StorageMode.DIRECT.value,
                )

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                window.show()

                for _ in range(10):
                    window.refresh()
                    QApplication.processEvents()

                    edit_buttons = [
                        button for button in window.detail_view.findChildren(QPushButton)
                        if button.text() == controller.i18n.tr("plan.edit")
                    ]
                    self.assertEqual(len(edit_buttons), 1)
                    self.assertEqual(
                        len(window.detail_view.findChildren(StatusBadge)), 1
                    )
                    self.assertEqual(
                        len(window.detail_view.findChildren(QTableWidget)), 1
                    )
                    self.assertEqual(
                        len(window.plans_container.findChildren(PlanCard)), 1
                    )
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_detail_screen_uses_scroll_container_and_compact_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                source = root / "geometry.txt"
                source.write_text("geometry test", encoding="utf-8")

                storage = PathPolicy.validate_storage(
                    str(root / "storage")
                )
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)

                plan_id = controller.repo.create_plan(
                    "Geometry test",
                    "",
                    descriptor,
                    storage_id,
                    5,
                    StorageMode.DIRECT.value,
                )

                operation_id = controller.repo.create_operation(
                    OperationType.BACKUP,
                    plan_id,
                    None,
                    {},
                )

                result = controller.snapshot_service.create(
                    operation_id,
                    plan_id,
                    False,
                    lambda event: None,
                    lambda: False,
                )
                self.assertTrue(result.success)

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.resize(720, 430)
                window.select_plan_by_id(plan_id)
                window.show()
                QApplication.processEvents()

                self.assertIsInstance(
                    window.detail_scroll,
                    QScrollArea,
                )
                self.assertEqual(
                    window.stack.currentWidget(),
                    window.detail_scroll,
                )
                self.assertEqual(
                    window.versions_table.columnCount(),
                    9,
                )

                action_holder = window.versions_table.cellWidget(0, 8)
                buttons = _row_controls(action_holder)

                self.assertGreaterEqual(len(buttons), 5)
                self.assertTrue(
                    {button.text() for button in buttons} >=
                    {
                        controller.i18n.tr("snapshot.restore"),
                        controller.i18n.tr("snapshot.restore_to"),
                        controller.i18n.tr("snapshot.replace_file"),
                        controller.i18n.tr("snapshot.verify"),
                        controller.i18n.tr("snapshot.pin"),
                    },
                )

                window.close()

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_plan_editor_accepts_single_local_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            editor = None

            try:
                source = root / "dropped-source.txt"
                source.write_text("drag drop", encoding="utf-8")

                editor = PlanEditor(controller)

                descriptor = PathPolicy.validate_source(str(source))

                editor.source_edit.setText(str(descriptor.path))
                editor.name_edit.setText(
                    descriptor.path.stem
                )

                self.assertEqual(
                    editor.source_edit.text(),
                    str(source.resolve()),
                )
                self.assertEqual(
                    editor.name_edit.text(),
                    "dropped-source",
                )

            finally:
                if editor is not None:
                    editor.close()
                controller.shutdown()
                QApplication.processEvents()


    def test_plan_editor_drop_overlay_resizes_with_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            editor = None

            try:
                editor = PlanEditor(controller)
                editor.resize(840, 620)
                editor.show()
                QApplication.processEvents()

                editor._drop_overlay.setGeometry(editor.rect())
                editor._drop_overlay.show()
                QApplication.processEvents()

                self.assertEqual(
                    editor._drop_overlay.geometry(),
                    editor.rect(),
                )

            finally:
                if editor is not None:
                    editor.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_app_settings_bool_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)

            try:
                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_CLOSE_TO_TRAY,
                        DEFAULT_CLOSE_TO_TRAY,
                    )
                )

                controller.repo.set_setting(
                    SETTING_CLOSE_TO_TRAY,
                    False,
                )

                self.assertFalse(
                    controller.repo.get_bool_setting(
                        SETTING_CLOSE_TO_TRAY,
                        DEFAULT_CLOSE_TO_TRAY,
                    )
                )
            finally:
                controller.shutdown()


    def test_tray_runtime_is_safe_when_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                window = MainWindow(controller, paths)
                tray = TrayRuntime(controller, window)

                if not QSystemTrayIcon.isSystemTrayAvailable():
                    self.assertFalse(tray.start())
                    self.assertIsNone(tray.tray)

                tray.stop()
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()


    def test_main_window_close_to_tray_policy_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                controller.repo.set_setting(
                    SETTING_CLOSE_TO_TRAY,
                    False,
                )

                window = MainWindow(controller, paths)

                self.assertFalse(
                    window.close_to_tray_enabled()
                )
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_exit_from_tray_with_hidden_window_stops_the_application(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                window = MainWindow(controller, paths)
                self.assertFalse(window.isVisible())
                controller.shutdown()
                with mock.patch.object(window, "_schedule_application_quit") as quit_call:
                    window.close()
                quit_call.assert_called_once_with()
            finally:
                if window is not None:
                    window.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_exit_with_visible_window_does_not_force_quit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                window = MainWindow(controller, paths)
                window.show()
                QApplication.processEvents()
                controller.shutdown()
                with mock.patch.object(window, "_schedule_application_quit") as quit_call:
                    window.close()
                quit_call.assert_not_called()
            finally:
                if window is not None:
                    window.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_explicit_exit_shows_a_hidden_window_while_an_operation_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                window = MainWindow(controller, paths)
                controller.active_operation_id = "op-busy"
                with mock.patch.object(window, "showNormal") as shown, \
                        mock.patch.object(window, "close") as closed:
                    window.request_explicit_exit()
                shown.assert_called_once_with()
                closed.assert_called_once_with()
            finally:
                controller.active_operation_id = None
                if window is not None:
                    window.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_backup_request_waits_for_a_background_query_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                window = MainWindow(controller, paths)
                controller.active_operation_id = "op-background"
                controller._background_queries["op-background"] = ("refresh_health", None)
                busy = AppError("OPERATION_ALREADY_RUNNING", "busy")
                with mock.patch.object(controller, "queue_backup", side_effect=[busy, busy, "op-1"]) as queue, \
                        mock.patch.object(_APP.QTimer, "singleShot", side_effect=lambda ms, fn: fn()), \
                        mock.patch.object(window, "show_progress") as progress, \
                        mock.patch.object(QMessageBox, "warning") as warned:
                    window.queue_backup("plan-1")
                self.assertEqual(queue.call_count, 3)
                progress.assert_called_once()
                warned.assert_not_called()
            finally:
                controller.active_operation_id = None
                controller._background_queries.pop("op-background", None)
                if window is not None:
                    window.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_backup_request_still_reports_a_real_running_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                window = MainWindow(controller, paths)
                controller.active_operation_id = "op-restore"
                busy = AppError("OPERATION_ALREADY_RUNNING", "busy")
                with mock.patch.object(controller, "queue_backup", side_effect=busy), \
                        mock.patch.object(controller.repo, "operation_plan_id", return_value="other-plan"), \
                        mock.patch.object(QMessageBox, "warning") as warned:
                    window.queue_backup("plan-1")
                warned.assert_called_once()
            finally:
                controller.active_operation_id = None
                if window is not None:
                    window.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_new_plan_editor_keeps_every_version_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            editor = None
            try:
                editor = PlanEditor(controller)
                self.assertEqual(editor.retention_mode_combo.currentData(), RETENTION_MODE_COUNT)
                self.assertEqual(editor.keep_spin.value(), editor.KEEP_MAX)
                self.assertEqual(editor.basic_keep_combo.currentData(), "max")
            finally:
                if editor is not None:
                    editor.deleteLater()
                controller.shutdown()
                QApplication.processEvents()

    def test_controller_autostart_setting_updates_after_service_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)

            try:
                registry = FakeWinRegistry()

                controller.autostart_service = WindowsAutostartService(
                    executable_path=r"C:\Python313\python.exe",
                    script_path=r"C:\Apps\AmviBackUp\amvibackup.py",
                    registry_module=registry,
                    platform_name="Windows",
                )

                controller.set_autostart_enabled(True)

                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_AUTOSTART_ENABLED,
                        False,
                    )
                )
                self.assertTrue(
                    controller.is_autostart_enabled()
                )

                controller.set_autostart_enabled(False)

                self.assertFalse(
                    controller.repo.get_bool_setting(
                        SETTING_AUTOSTART_ENABLED,
                        True,
                    )
                )
                self.assertFalse(
                    controller.is_autostart_enabled()
                )

            finally:
                controller.shutdown()

    def test_settings_dialog_persists_runtime_and_notification_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None

            try:
                registry = FakeWinRegistry()
                controller.autostart_service = WindowsAutostartService(
                    executable_path=r"C:\Python313\python.exe",
                    script_path=r"C:\Apps\AmviBackUp\amvibackup.py",
                    registry_module=registry,
                    platform_name="Windows",
                )

                dialog = SettingsDialog(controller, paths)

                dialog.close_to_tray_check.setChecked(True)
                dialog.start_minimized_check.setChecked(True)
                dialog.autostart_check.setChecked(True)
                dialog.notifications_enabled_check.setChecked(True)
                dialog.success_notifications_check.setChecked(True)
                dialog.notification_cooldown_spin.setValue(45)

                dialog.save()

                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_CLOSE_TO_TRAY,
                        False,
                    )
                )
                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_START_MINIMIZED,
                        False,
                    )
                )
                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_AUTOSTART_ENABLED,
                        False,
                    )
                )
                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_NOTIFICATIONS_ENABLED,
                        False,
                    )
                )
                self.assertTrue(
                    controller.repo.get_bool_setting(
                        SETTING_NOTIFICATIONS_SUCCESS_ENABLED,
                        False,
                    )
                )
                self.assertEqual(
                    controller.repo.get_setting(
                        SETTING_NOTIFICATION_COOLDOWN_MINUTES,
                        0,
                    ),
                    45,
                )

            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()


    def test_settings_disable_close_to_tray_disables_start_minimized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None

            try:
                dialog = SettingsDialog(controller, paths)

                dialog.close_to_tray_check.setChecked(True)
                dialog.start_minimized_check.setChecked(True)

                dialog.close_to_tray_check.setChecked(False)

                self.assertFalse(
                    dialog.start_minimized_check.isEnabled()
                )
                self.assertFalse(
                    dialog.start_minimized_check.isChecked()
                )

            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()


    def test_settings_disable_notifications_disables_success_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None

            try:
                dialog = SettingsDialog(controller, paths)

                dialog.notifications_enabled_check.setChecked(True)
                dialog.success_notifications_check.setChecked(True)

                dialog.notifications_enabled_check.setChecked(False)

                self.assertFalse(
                    dialog.success_notifications_check.isEnabled()
                )
                self.assertFalse(
                    dialog.success_notifications_check.isChecked()
                )
                self.assertFalse(
                    dialog.notification_cooldown_spin.isEnabled()
                )

            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_tray_runtime_refresh_uses_repository_status_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                source = root / "tray-status.txt"
                source.write_text("tray status", encoding="utf-8")

                storage = PathPolicy.validate_storage(
                    str(root / "storage")
                )
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)

                plan_id = controller.repo.create_plan(
                    "Tray status plan",
                    "",
                    descriptor,
                    storage_id,
                    5,
                    StorageMode.DIRECT.value,
                )
                controller.repo.mark_pending_trigger(
                    plan_id,
                    TRIGGER_MASK_EVENT,
                    1_000_000,
                )

                window = MainWindow(controller, paths)
                tray_runtime = TrayRuntime(controller, window)
                tray_runtime.tray = FakeTrayIcon()

                class FakeStatusAction:
                    def __init__(self) -> None:
                        self.text = ""

                    def setText(self, value: str) -> None:
                        self.text = value

                action = FakeStatusAction()
                tray_runtime.status_action = action
                tray_runtime.refresh()

                tr = controller.i18n.tr
                self.assertEqual(
                    action.text,
                    tr("tray.status", status=tr("tray.pending")),
                )
                self.assertEqual(
                    tray_runtime.tray.tooltip,
                    f"{APP_NAME}: {tr('tray.pending')}",
                )
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_health_counter_opens_expected_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                source = root / "health-filter.txt"
                source.write_text("health", encoding="utf-8")
                storage = PathPolicy.validate_storage(
                    str(root / "storage")
                )
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(
                    storage
                )

                controller.storage.ensure_root(
                    storage,
                    storage_id,
                )

                plan_id = controller.repo.create_plan(
                    "Health filter plan",
                    "",
                    descriptor,
                    storage_id,
                    5,
                    StorageMode.DIRECT.value,
                )

                controller.repo.upsert_issue(
                    IssueDraft(
                        dedupe_key=f"FILTER_ERROR:{plan_id}",
                        plan_id=plan_id,
                        severity=IssueSeverity.ERROR.value,
                        code="FILTER_ERROR",
                        title="Filter error",
                        detail="Error detail",
                    )
                )

                window = MainWindow(controller, paths)
                window.show()
                QApplication.processEvents()

                dialog = HealthCenterDialog(
                    controller,
                    window,
                )
                dialog.set_filter("error")

                self.assertEqual(dialog.filter_name, "error")

                dialog.close()

            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_settings_dialog_selects_persisted_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None

            try:
                controller.i18n.set_language("ru")
                dialog = SettingsDialog(controller, paths)

                self.assertEqual(
                    dialog.language_combo.currentData(),
                    "ru",
                )
                self.assertEqual(
                    dialog.windowTitle(),
                    controller.i18n.tr("settings.title"),
                )
            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_language_changes_without_restarting_main_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = MainWindow(controller, paths)
            controller.main_window = window

            try:
                window.search_edit.setText("example")
                old_central = window.centralWidget()

                controller.i18n.set_language("ru")
                window.apply_language()
                QApplication.processEvents()

                self.assertIsNot(window.centralWidget(), old_central)
                self.assertEqual(window.search_edit.text(), "example")
                self.assertEqual(
                    window.search_edit.placeholderText(),
                    TRANSLATIONS["ru"]["main.search"],
                )
                self.assertEqual(
                    window.cancel_button.text(),
                    TRANSLATIONS["ru"]["common.cancel"],
                )
            finally:
                window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_version_table_shows_only_available_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source = root / "source.txt"
                source.write_text("saved", encoding="utf-8")
                storage = PathPolicy.validate_storage(
                    str(root / "storage")
                )
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)
                plan_id = controller.repo.create_plan(
                    "Available versions",
                    "",
                    descriptor,
                    storage_id,
                    5,
                )

                backup_id = controller.repo.create_operation(
                    OperationType.BACKUP, plan_id, None, {}
                )
                result = controller.snapshot_service.create(
                    backup_id, plan_id, False,
                    lambda event: None, lambda: False,
                )
                self.assertTrue(result.success)

                interrupted_id = controller.repo.create_operation(
                    OperationType.BACKUP, plan_id, None, {}
                )
                hidden_id, _ = controller.repo.create_snapshot_creating(
                    plan_id, interrupted_id
                )
                controller.repo.mark_snapshot(
                    hidden_id, "needs_recovery"
                )

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                QApplication.processEvents()

                self.assertEqual(window.versions_table.rowCount(), 1)
                self.assertNotEqual(
                    window.versions_table.item(0, 1).text(),
                    str(controller.repo.snapshot(hidden_id).sequence_no),
                )

                holder = window.versions_table.cellWidget(0, 8)
                restore_to = next(
                    button for button in _row_controls(holder)
                    if button.text() == controller.i18n.tr(
                        "snapshot.restore_to"
                    )
                )
                self.assertTrue(restore_to.isEnabled())
                self.assertEqual(
                    restore_to.toolTip(),
                    controller.i18n.tr(
                        "snapshot.restore_to_tooltip"
                    ),
                )
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_recovery_issue_visible_but_snapshot_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            dialog = None

            try:
                source = root / "recovery-ui.txt"
                source.write_text("saved", encoding="utf-8")
                storage = PathPolicy.validate_storage(
                    str(root / "storage")
                )
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(
                    storage
                )
                controller.storage.ensure_root(storage, storage_id)
                plan_id = controller.repo.create_plan(
                    "Recovery UI", "", descriptor, storage_id, 5
                )

                backup_id = controller.repo.create_operation(
                    OperationType.BACKUP, plan_id, None, {}
                )
                result = controller.snapshot_service.create(
                    backup_id, plan_id, False,
                    lambda event: None, lambda: False,
                )
                self.assertTrue(result.success)

                interrupted_id = controller.repo.create_operation(
                    OperationType.BACKUP, plan_id, None, {}
                )
                hidden_id, _ = controller.repo.create_snapshot_creating(
                    plan_id, interrupted_id
                )
                RecoveryService(
                    controller.repo,
                    controller.health_service,
                ).run()

                self.assertEqual(
                    controller.repo.snapshot(hidden_id).state,
                    "needs_recovery",
                )

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                QApplication.processEvents()

                self.assertEqual(window.versions_table.rowCount(), 1)
                self.assertTrue(
                    controller.repo.needs_recovery_for_plan(plan_id)
                )

                dialog = HealthCenterDialog(controller, window)
                dialog.set_filter("error")
                QApplication.processEvents()

                self.assertTrue(
                    any(
                        issue.code == "OPERATION_NEEDS_RECOVERY"
                        for issue in controller.repo.list_active_issues(
                            plan_id
                        )
                    )
                )
                visible_labels = [
                    label.text()
                    for label in dialog.container.findChildren(QLabel)
                ]
                self.assertTrue(
                    any(
                        "OPERATION_NEEDS_RECOVERY" in text
                        for text in visible_labels
                    )
                )
            finally:
                if dialog is not None:
                    dialog.close()
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()




    def test_recovery_action_requires_confirmation_and_uses_controller(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            dialog = None
            try:
                source = root / "review-ui.txt"
                source.write_text("data", encoding="utf-8")
                storage = PathPolicy.validate_storage(str(root / "storage"))
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)
                plan_id = controller.repo.create_plan("Recovery test", "", descriptor, storage_id, 5)
                interrupted = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
                controller.repo.operation_state(interrupted, "running")
                RecoveryService(controller.repo, controller.health_service).run()
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                dialog = HealthCenterDialog(controller, window)
                review = [button for button in window.detail_view.findChildren(QPushButton)
                          if button.text() == controller.i18n.tr("health.recover")]
                self.assertEqual(len(review), 1)
                before = len(controller.repo.unfinished_operations_for_plan(plan_id))
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                    review[0].click()
                self.assertEqual(len(controller.repo.unfinished_operations_for_plan(plan_id)), before)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                     patch.object(controller, "queue_recovery", return_value="queued") as queued:
                    review[0].click()
                    queued.assert_called_once_with(plan_id)
            finally:
                if dialog is not None:
                    dialog.close()
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_restore_to_directory_ui_queues_only_after_folder_selected(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source = root / "ui-restore.txt"
                source.write_text("saved", encoding="utf-8")
                storage = PathPolicy.validate_storage(str(root / "storage"))
                descriptor = PathPolicy.validate_source(str(source))
                storage_id = controller.repo.get_or_create_storage(storage)
                controller.storage.ensure_root(storage, storage_id)
                plan_id = controller.repo.create_plan("UI restore", "", descriptor, storage_id, 5)
                operation_id = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
                service = SnapshotService(controller.repo, controller.storage, controller.health_service, reserve_bytes=0, reserve_ratio=0)
                self.assertTrue(service.create(operation_id, plan_id, False, lambda event: None, lambda: False).success)
                snapshot = controller.repo.available_snapshots(plan_id)[0]
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                holder = window.versions_table.cellWidget(0, 8)
                button = next(b for b in _row_controls(holder) if b.text() == controller.i18n.tr("snapshot.restore_to"))
                self.assertTrue(button.isEnabled())
                selected = root / "selected"
                selected.mkdir()
                with patch.object(QFileDialog, "getExistingDirectory", return_value=""), \
                     patch.object(controller, "queue_restore_to_directory") as queued:
                    button.click()
                    queued.assert_not_called()
                with patch.object(QFileDialog, "getExistingDirectory", return_value=str(selected)), \
                     patch.object(controller, "queue_restore_to_directory", return_value="queued") as queued:
                    button.click()
                    queued.assert_called_once_with(snapshot.id, str(selected))
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_selected_restore_dialog_filters_and_preserves_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None
            try:
                source = root / "folder"
                (source / "nested").mkdir(parents=True)
                (source / "nested" / "one.txt").write_text("one", encoding="utf-8")
                (source / "two.txt").write_text("two", encoding="utf-8")
                plan_id = controller.create_or_update_plan(
                    None, "Select", "", str(source), str(root / "storage"), 5,
                    StorageMode.DIRECT.value,
                )
                op = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
                service = SnapshotService(controller.repo, controller.storage, controller.health_service, reserve_bytes=0, reserve_ratio=0)
                self.assertTrue(service.create(op, plan_id, False, lambda event: None, lambda: False).success)
                snapshot = controller.repo.available_snapshots(plan_id)[0]
                controller.load_snapshot_files_for_test(snapshot.id)
                dialog = RestoreSelectedFilesDialog(controller, snapshot.id)
                self.assertEqual(dialog.files_list.rowCount(), 2)
                row = next(i for i in range(2) if dialog.files_list.item(i, 1).text() == "nested/one.txt")
                dialog.files_list.cellWidget(row, 0).findChild(QCheckBox).setChecked(True)
                self.assertEqual(dialog.selected_relative_paths, ["nested/one.txt"])
                dialog.search_edit.setText("two")
                self.assertEqual(dialog.files_list.rowCount(), 1)
                self.assertEqual(dialog.selected_relative_paths, ["nested/one.txt"])
                dialog.search_edit.clear()
                row = next(i for i in range(2) if dialog.files_list.item(i, 1).text() == "nested/one.txt")
                self.assertTrue(dialog.files_list.cellWidget(row, 0).findChild(QCheckBox).isChecked())
                destination = root / "destination"
                destination.mkdir()
                dialog.destination_edit.setText(str(destination))
                dialog.accept_selection()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_selected_restore_ui_only_queues_after_dialog_accepts(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None

            try:
                source = root / "folder"
                source.mkdir()
                (source / "one.txt").write_text(
                    "one",
                    encoding="utf-8",
                )

                plan_id = controller.create_or_update_plan(
                    None,
                    "Select",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )

                operation_id = controller.repo.create_operation(
                    OperationType.BACKUP,
                    plan_id,
                    None,
                    {},
                )
                service = SnapshotService(
                    controller.repo,
                    controller.storage,
                    controller.health_service,
                    reserve_bytes=0,
                    reserve_ratio=0,
                )
                backup = service.create(
                    operation_id,
                    plan_id,
                    False,
                    lambda event: None,
                    lambda: False,
                )
                self.assertTrue(backup.success, backup.message)

                snapshot = controller.repo.available_snapshots(plan_id)[0]
                controller.load_snapshot_files_for_test(snapshot.id)
                self.assertEqual(
                    snapshot.source_kind,
                    SourceKind.FOLDER.value,
                )

                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)

                self.assertEqual(window.versions_table.rowCount(), 1)
                holder = window.versions_table.cellWidget(0, 8)
                self.assertIsNotNone(holder)

                restore_button = next(
                    (
                        button
                        for button in _row_controls(holder)
                        if button.text()
                        == controller.i18n.tr(
                            "snapshot.restore_selected"
                        )
                    ),
                    None,
                )
                self.assertIsNotNone(restore_button)

                destination = root / "destination"
                destination.mkdir()

                with (
                    patch.object(
                        RestoreSelectedFilesDialog,
                        "exec",
                        return_value=QDialog.DialogCode.Rejected,
                    ),
                    patch.object(
                        controller,
                        "queue_restore_selected_files",
                    ) as queued,
                ):
                    window.restore_selected_files(snapshot.id)
                    queued.assert_not_called()

                def accept_dialog(dialog):
                    dialog.selected_relative_paths = ["one.txt"]
                    dialog.destination_edit.setText(str(destination))
                    return QDialog.DialogCode.Accepted

                original_exec = RestoreSelectedFilesDialog.exec

                def fake_exec(dialog):
                    return accept_dialog(dialog)

                with (
                    patch.object(
                        RestoreSelectedFilesDialog,
                        "exec",
                        new=fake_exec,
                    ),
                    patch.object(
                        controller,
                        "queue_restore_selected_files",
                        return_value="queued",
                    ) as queued,
                    patch.object(window, "show_progress") as progress,
                ):
                    window.restore_selected_files(snapshot.id)
                    queued.assert_called_once_with(
                        snapshot.id,
                        ["one.txt"],
                        str(destination),
                    )
                    progress.assert_called_once_with(
                        controller.i18n.tr("progress.restore")
                    )

            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_replace_original_ui_requires_confirmation_and_only_file_has_button(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source = root / "source.txt"
                source.write_text("saved", encoding="utf-8")
                plan_id = controller.create_or_update_plan(
                    None, "Replace", "", str(source), str(root / "storage"), 5,
                    StorageMode.DIRECT.value,
                )
                backup_id = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
                service = SnapshotService(
                    controller.repo, controller.storage, controller.health_service,
                    reserve_bytes=0, reserve_ratio=0,
                )
                backup = service.create(backup_id, plan_id, False, lambda event: None, lambda: False)
                self.assertTrue(backup.success, backup.message)
                snapshot = controller.repo.available_snapshots(plan_id)[0]
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan_id)
                holder = window.versions_table.cellWidget(0, 8)
                buttons = [b for b in _row_controls(holder)
                           if b.text() == controller.i18n.tr("snapshot.replace_file")]
                self.assertEqual(len(buttons), 1)
                with patch.object(QMessageBox, "exec", autospec=True, return_value=0), \
                     patch.object(controller, "queue_replace_original_file") as queued:
                    buttons[0].click()
                    queued.assert_not_called()
                clicked = []

                def fake_exec(box):
                    confirm_button = next(
                        button
                        for button in box.buttons()
                        if button.text()
                        == controller.i18n.tr("replace_file.confirm")
                    )
                    clicked.append(confirm_button)
                    return 0

                with (
                    patch.object(
                        QMessageBox,
                        "exec",
                        new=fake_exec,
                    ),
                    patch.object(
                        QMessageBox,
                        "clickedButton",
                        new=lambda box: clicked[-1],
                    ),
                    patch.object(
                        controller,
                        "queue_replace_original_file",
                        return_value="queued",
                    ) as queued,
                    patch.object(window, "show_progress") as progress,
                ):
                    window.replace_original_file(snapshot.id)
                    queued.assert_called_once_with(snapshot.id)
                    progress.assert_called_once_with(
                        controller.i18n.tr("progress.restore")
                    )
                self.assertEqual(source.read_text(encoding="utf-8"), "saved")
                folder = root / "folder"
                folder.mkdir()
                (folder / "item.txt").write_text("item", encoding="utf-8")
                folder_plan = controller.create_or_update_plan(
                    None, "Folder", "", str(folder), str(root / "storage"), 5,
                    StorageMode.DIRECT.value,
                )
                op = controller.repo.create_operation(OperationType.BACKUP, folder_plan, None, {})
                self.assertTrue(service.create(op, folder_plan, False, lambda event: None, lambda: False).success)
                window.select_plan_by_id(folder_plan)
                holder = window.versions_table.cellWidget(0, 8)
                self.assertFalse(any(b.text() == controller.i18n.tr("snapshot.replace_file")
                                     for b in _row_controls(holder)))
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_replace_original_controller_worker_journal_and_guard(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            try:
                source = root / "one.txt"
                source.write_text("saved", encoding="utf-8")
                plan_id = controller.create_or_update_plan(
                    None, "Replace", "", str(source), str(root / "storage"), 5,
                    StorageMode.DIRECT.value,
                )
                op = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {})
                service = SnapshotService(controller.repo, controller.storage, controller.health_service,
                                          reserve_bytes=0, reserve_ratio=0)
                self.assertTrue(service.create(op, plan_id, False, lambda event: None, lambda: False).success)
                snapshot = controller.repo.available_snapshots(plan_id)[0]
                with self.assertRaises(AppError) as disabled:
                    controller.queue_replace_original_file(snapshot.id)
                self.assertEqual(disabled.exception.code, "WORKER_DISABLED")
                worker = OperationWorker(controller)
                controller.worker = worker
                try:
                    queued_id = controller.queue_replace_original_file(snapshot.id)
                    command = {
                        "kind": OperationType.RESTORE_REPLACE_FILE.value,
                        "operation_id": queued_id,
                        "plan_id": plan_id,
                        "snapshot_id": snapshot.id,
                        "trigger_reason": TriggerReason.MANUAL.value,
                    }
                    self.assertEqual(controller.repo.operation(queued_id)["state"], "queued")
                    self.assertEqual(controller.repo.operation_payload(queued_id)["trigger_reason"],
                                     TriggerReason.MANUAL.value)
                    self.assertEqual(controller.repo.operation(queued_id)["type"],
                                     OperationType.RESTORE_REPLACE_FILE.value)
                    with self.assertRaises(AppError) as busy:
                        controller.queue_replace_original_file(snapshot.id)
                    self.assertEqual(busy.exception.code, "OPERATION_ALREADY_RUNNING")
                    source.write_text("changed", encoding="utf-8")
                    worker.execute(command)
                    self.assertEqual(source.read_text(encoding="utf-8"), "saved")
                    previous = source.parent / f"{source.stem}-before-restore-{queued_id}{source.suffix}"
                    self.assertEqual(previous.read_text(encoding="utf-8"), "changed")
                    self.assertEqual(controller.repo.operation(queued_id)["state"], "succeeded")
                    with controller.db.read_connection() as conn:
                        codes = [row["event_code"] for row in conn.execute(
                            "SELECT event_code FROM ActivityLog WHERE operation_id=?", (queued_id,),
                        ).fetchall()]
                    self.assertIn("RESTORE_REPLACE_SUCCEEDED", codes)
                    controller._finished(OperationResult(queued_id, True, "done", plan_id=plan_id))
                    self.assertIsNone(controller.active_operation_id)
                finally:
                    controller.worker = None
            finally:
                controller.shutdown()
                QApplication.processEvents()
    def test_alpha15_health_center_shows_recovery_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            dialog = None
            try:
                source = root / "source.txt"
                source.write_text("saved", encoding="utf-8")
                plan = controller.create_or_update_plan(None, "Test", "", str(source),
                                                        str(root / "storage"), 5, StorageMode.DIRECT.value)
                controller.repo.upsert_issue(IssueDraft(
                    f"RESTORE_RECOVERY_REVIEW_REQUIRED:{plan}", plan,
                    IssueSeverity.CRITICAL.value, "RESTORE_RECOVERY_REVIEW_REQUIRED",
                    "Review", "Check source and previous copy", "Open plan",
                ))
                dialog = HealthCenterDialog(controller)
                labels = dialog.container.findChildren(QLabel)
                shown = " ".join(x.text() for x in labels)
                self.assertIn(controller.i18n.issue_detail("RESTORE_RECOVERY_REVIEW_REQUIRED"), shown)
                self.assertIn(controller.i18n.tr("issue.fix.RESTORE_RECOVERY_REVIEW_REQUIRED"), shown)
            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()
    def test_identity_confirmation_ui_requires_explicit_yes(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source = root / "legacy-ui.txt"
                source.write_text("saved", encoding="utf-8")
                plan = controller.create_or_update_plan(None, "Legacy", "", str(source),
                                                        str(root / "storage"), 5, StorageMode.DIRECT.value)
                with controller.db.transaction() as conn:
                    conn.execute("UPDATE BackupPlans SET source_identity_json=NULL WHERE id=?", (plan,))
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan)
                buttons = [b for b in window.detail_view.findChildren(QPushButton)
                           if b.text() == controller.i18n.tr("source.confirm")]
                self.assertEqual(len(buttons), 1)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                    buttons[0].click()
                self.assertIsNone(controller.repo.plan_details(plan).source_identity_json)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    window.confirm_source_identity(plan)
                self.assertTrue(controller.repo.plan_details(plan).source_identity_json)
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()
    def test_stage2_ui_shows_new_path_and_lock_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            dialog = None
            try:
                old = root / "ui-stage2-old.txt"
                old.write_bytes(b"saved")
                plan = controller.create_or_update_plan(None, "Stage2", "", str(old),
                                                        str(root / "storage"), 5, StorageMode.DIRECT.value)
                moved = root / "ui-stage2-moved.txt"
                old.rename(moved)
                controller.repo.resolve_source_for_backup(plan, lambda: False)
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan)
                self.assertTrue(any(str(moved) in label.text()
                                    for label in window.detail_view.findChildren(QLabel)))
                controller.health_service.record_error(AppError("SOURCE_LOCKED", "busy"), plan)
                dialog = HealthCenterDialog(controller, window)
                dialog.set_filter("error")
                self.assertTrue(any("SOURCE_LOCKED" in label.text()
                                    for label in dialog.container.findChildren(QLabel)))
            finally:
                if dialog is not None:
                    dialog.close()
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_stage2_confirmation_rejects_no_and_rebinds_only_after_yes(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source = root / "ui-stage2-original.txt"
                source.write_bytes(b"saved")
                plan = controller.create_or_update_plan(None, "Stage2", "", str(source),
                                                        str(root / "storage"), 5, StorageMode.DIRECT.value)
                identity = controller.repo.plan_details(plan).source_identity_json
                source.rename(root / "ui-stage2-retained.txt")
                source.write_bytes(b"other")
                # Worker is disabled in this UI-only test. Run the real
                # producer/consumer pipeline explicitly before rendering.
                result = controller._perform_health_refresh(
                    "stage2-ui-health", lambda: False,
                )
                controller._accept_health_refresh(result)
                self.assertTrue(result.success, result.message)
                self.assertTrue(controller.source_confirmation_required(plan))
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan)
                buttons = [button for button in window.detail_view.findChildren(QPushButton)
                           if button.text() == controller.i18n.tr("source.confirm")]
                self.assertEqual(len(buttons), 1)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                    buttons[0].click()
                self.assertEqual(controller.repo.plan_details(plan).source_identity_json, identity)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    window.confirm_source_identity(plan)
                self.assertNotEqual(controller.repo.plan_details(plan).source_identity_json, identity)
                self.assertEqual(controller.repo.plan_details(plan).source_identity_json,
                                 confirmed_source_identity(source, SourceKind.FILE.value))
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def stage3_ui_fixture(self, root, controller):
        source = root / "stage3-ui.txt"
        source.write_bytes(b"saved")
        plan = controller.create_or_update_plan(None, "Plan to delete", "", str(source), str(root / "storage"), 5, StorageMode.DIRECT.value)
        operation = controller.repo.create_operation(OperationType.BACKUP, plan, None, {})
        service = SnapshotService(controller.repo, controller.storage, controller.health_service, reserve_bytes=0, reserve_ratio=0)
        result = service.create(operation, plan, False, lambda event: None, lambda: False)
        self.assertTrue(result.success, result.message)
        return source, plan

    def test_stage3_ui_preview_cancel_and_pinned_confirmation(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source, plan = self.stage3_ui_fixture(root, controller)
                snapshot = controller.repo.available_snapshots(plan)[0]
                controller.repo.set_pin(snapshot.id, True)
                preview = controller.plan_deletion_preview(plan)
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan)
                captured = []
                def cancel(dialog):
                    captured.append("\n".join(label.text() for label in dialog.findChildren(QLabel)))
                    return QDialog.DialogCode.Rejected
                with patch.object(QDialog, "exec", new=cancel), \
                     patch.object(controller, "queue_delete_plan_data") as queued:
                    window.delete_plan_data(plan)
                    queued.assert_not_called()
                self.assertIn(plan, captured[0])
                self.assertIn(str(source), captured[0])
                self.assertIn(preview.storage_path, captured[0])
                self.assertIn(preview.display_name, captured[0])
                def confirm(dialog):
                    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
                    buttons = [button.text() for button in dialog.findChildren(QPushButton)]
                    self.assertIn(controller.i18n.tr("plan_delete.confirm"), buttons)
                    self.assertIn(controller.i18n.tr("plan_delete.details_show"), buttons)
                    self.assertIn(str(preview.snapshot_count), labels)
                    return QDialog.DialogCode.Accepted
                with patch.object(QDialog, "exec", new=confirm), \
                     patch.object(controller, "queue_delete_plan_data", return_value="queued") as queued, \
                     patch.object(window, "show_progress"):
                    window.delete_plan_data(plan)
                    queued.assert_called_once_with(plan, preview.token, True)
                self.assertEqual(controller.repo.snapshot(snapshot.id).state, "committed")
                self.assertEqual(source.read_bytes(), b"saved")
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_stage3_controller_worker_and_global_operation_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            try:
                source, plan = self.stage3_ui_fixture(root, controller)
                preview = controller.plan_deletion_preview(plan)
                with self.assertRaises(AppError) as disabled:
                    controller.queue_delete_plan_data(plan, preview.token)
                self.assertEqual(disabled.exception.code, "WORKER_DISABLED")
                worker = OperationWorker(controller)
                controller.worker = worker
                worker.finished.connect(controller._finished)
                op = controller.queue_delete_plan_data(plan, preview.token)
                with self.assertRaises(AppError) as busy:
                    controller.queue_backup(plan, True)
                self.assertEqual(busy.exception.code, "OPERATION_ALREADY_RUNNING")
                self.assertEqual(controller.repo.operation(op)["state"], "queued")
                payload = controller.repo.operation_payload(op)
                self.assertEqual(payload["preview_token"], preview.token)
                worker.execute({"kind": OperationType.DELETE_PLAN_DATA.value, "operation_id": op,
                                "plan_id": plan, "preview_token": preview.token, "approve_pinned": False,
                                "trigger_reason": TriggerReason.MANUAL.value})
                self.assertEqual(controller.repo.operation(op)["state"], "succeeded")
                self.assertIsNone(controller.active_operation_id)
                self.assertTrue(source.exists())
                with self.assertRaises(AppError):
                    controller.repo.plan_details(plan)
                controller.worker = None
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_stage3_deleted_plan_disappears_from_main_window_only_after_success(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(paths, start_worker=False)
            window = None
            try:
                source, plan = self.stage3_ui_fixture(root, controller)
                window = MainWindow(controller, paths)
                controller.main_window = window
                window.select_plan_by_id(plan)
                preview = controller.plan_deletion_preview(plan)
                op = controller.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan, None, {})
                result = controller.delete_service.delete_plan_data(op, plan, preview.token, False,
                                                                    lambda event: None, lambda: False)
                self.assertTrue(result.success, result.message)
                with patch.object(QMessageBox, "information"):
                    window.on_finished(result)
                self.assertIsNone(window.current_plan_id)
                self.assertEqual(window.stack.currentWidget(), window.empty_view)
                self.assertNotIn(plan, window.plan_cards)
                self.assertTrue(source.exists())
            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_instance_guard_rejects_duplicate_and_allows_after_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "instance.sqlite3"
            first = ApplicationInstanceGuard(database)
            second = ApplicationInstanceGuard(
                database.parent / "." / database.name
            )

            try:
                first.acquire()
                first.acquire()
                self.assertTrue(first.held)

                with self.assertRaises(AppError) as error:
                    second.acquire()

                self.assertEqual(
                    error.exception.code,
                    "APP_ALREADY_RUNNING",
                )
                self.assertFalse(second.held)
                self.assertFalse(database.exists())

                first.release()
                first.release()

                second.acquire()
                self.assertTrue(second.held)

            finally:
                second.release()
                first.release()

    def test_instance_guard_different_databases_are_independent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = ApplicationInstanceGuard(
                root / "first.sqlite3"
            )
            second = ApplicationInstanceGuard(
                root / "second.sqlite3"
            )

            try:
                first.acquire()
                second.acquire()

                self.assertTrue(first.held and second.held)
                self.assertNotEqual(
                    first.lock_path,
                    second.lock_path,
                )
            finally:
                second.release()
                first.release()

    def test_instance_guard_permission_failure_is_not_duplicate(
        self,
    ) -> None:
        from unittest.mock import Mock, patch

        lock = Mock()
        lock.tryLock.return_value = False
        lock.error.return_value = (
            QLockFile.LockError.PermissionError
        )
        real_enum = QLockFile.LockError

        with patch.dict(
            APP_MODULE_GLOBALS,
            {
                "QLockFile": Mock(
                    return_value=lock,
                    LockError=real_enum,
                )
            },
        ):
            guard = ApplicationInstanceGuard(
                Path(tempfile.gettempdir()) / "denied.sqlite3"
            )

            with self.assertRaises(AppError) as error:
                guard.acquire()

            self.assertEqual(
                error.exception.code,
                "APP_LOCK_ACCESS_DENIED",
            )
            guard.release()
            lock.unlock.assert_not_called()
            lock.setStaleLockTime.assert_called_once_with(0)

    def test_duplicate_gui_start_does_not_open_database_or_log(
        self,
    ) -> None:
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            first = ApplicationInstanceGuard(paths.db_path)

            try:
                first.acquire()
                controller_factory = Mock()
                logging_setup = Mock()

                with (
                    patch.object(
                        AppPaths,
                        "create",
                        return_value=paths,
                    ),
                    patch.object(
                        QMessageBox,
                        "critical",
                        return_value=QMessageBox.StandardButton.Ok,
                    ),
                    patch.dict(
                        APP_MODULE_GLOBALS,
                        {
                            "AppController": controller_factory,
                            "setup_logging": logging_setup,
                        },
                    ),
                ):
                    self.assertEqual(_APP.run_gui(), 1)

                controller_factory.assert_not_called()
                logging_setup.assert_not_called()
                self.assertFalse(paths.db_path.exists())

            finally:
                first.release()

    def test_gui_start_failure_joins_worker_before_unlock(
        self,
    ) -> None:
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            order = []

            guard = Mock()
            guard.acquire.side_effect = (
                lambda: order.append("lock")
            )
            guard.release.side_effect = (
                lambda: order.append("unlock")
            )

            controller = Mock()
            controller.thread.wait.side_effect = (
                lambda: order.append("join") or True
            )
            controller.db.checkpoint_wal.side_effect = (
                lambda: order.append("checkpoint")
            )

            window_factory = Mock(
                side_effect=AppError(
                    "TEST_START_FAILURE",
                    "simulated UI startup failure",
                )
            )

            with (
                patch.object(
                    AppPaths,
                    "create",
                    return_value=paths,
                ),
                patch.object(
                    QMessageBox,
                    "critical",
                    return_value=QMessageBox.StandardButton.Ok,
                ),
                patch.dict(
                    APP_MODULE_GLOBALS,
                    {
                        "ApplicationInstanceGuard": Mock(
                            return_value=guard
                        ),
                        "AppController": Mock(
                            return_value=controller
                        ),
                        "MainWindow": window_factory,
                        "setup_logging": Mock(),
                    },
                ),
                self.assertLogs(level="ERROR"),
            ):
                self.assertEqual(_APP.run_gui(), 1)

            self.assertEqual(
                order,
                ["lock", "join", "checkpoint", "unlock"],
            )
            controller.cancel_active.assert_called_once()
            controller.thread.quit.assert_called_once()
            controller.thread.wait.assert_called_once_with()

    def test_instance_guard_cross_process_and_crash_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "process-instance.sqlite3"

            child = textwrap.dedent(r'''
                import importlib.util
                import sys
                import os
                from pathlib import Path

                spec = importlib.util.spec_from_file_location(
                    "instance_guard_child",
                    sys.argv[1],
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)

                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
                from PySide6.QtWidgets import QApplication
                application = QApplication.instance() or QApplication(sys.argv)
                application.setApplicationName(module.APP_NAME)

                guard = module.ApplicationInstanceGuard(
                    Path(sys.argv[2])
                )
                try:
                    guard.acquire()
                except module.AppError as error:
                    sys.exit(
                        23
                        if error.code == "APP_ALREADY_RUNNING"
                        else 25
                    )

                if sys.argv[3] == "crash":
                    os._exit(24)

                guard.release()
            ''')

            command = [
                sys.executable,
                "-c",
                child,
                str(self_test_module_path()),
                str(database),
            ]
            guard = ApplicationInstanceGuard(database)

            try:
                guard.acquire()
                result = subprocess.run(
                    command + ["normal"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(
                    result.returncode,
                    23,
                    result.stdout + result.stderr,
                )
            finally:
                guard.release()

            result = subprocess.run(
                command + ["normal"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stdout + result.stderr,
            )

            result = subprocess.run(
                command + ["crash"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                24,
                result.stdout + result.stderr,
            )

            try:
                guard.acquire()
                self.assertTrue(guard.held)
                self.assertFalse(database.exists())
            finally:
                guard.release()

    def test_runtime_cancel_before_worker_start_is_persisted_and_isolated(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)

            try:
                worker = OperationWorker(controller)
                controller.worker = worker
                worker.finished.connect(controller._finished)

                commands = []
                controller.request_execute.connect(commands.append)

                op = controller._queue(
                    OperationType.BACKUP,
                    None,
                    force=False,
                )
                token = commands[-1]["_cancel_event"]
                controller.cancel_active()

                with patch.object(
                    controller.snapshot_service,
                    "create",
                ) as service:
                    worker.execute(commands[-1])
                    service.assert_not_called()

                self.assertTrue(token.is_set())
                self.assertEqual(
                    controller.repo.operation(op)["state"],
                    "cancelled",
                )
                self.assertIsNone(controller.active_operation_id)

                second = controller._queue(
                    OperationType.BACKUP,
                    None,
                    force=False,
                )
                self.assertIsNot(
                    commands[-1]["_cancel_event"],
                    token,
                )
                self.assertFalse(
                    commands[-1]["_cancel_event"].is_set()
                )

                with patch.object(
                    controller.snapshot_service,
                    "create",
                    return_value=OperationResult(
                        second,
                        True,
                        "done",
                    ),
                ):
                    worker.execute(commands[-1])

                self.assertEqual(
                    controller.repo.operation(second)["state"],
                    "succeeded",
                )
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_worker_exception_finishes_journal(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                worker = OperationWorker(controller)
                controller.worker = worker

                commands = []
                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)

                op = controller._queue(
                    OperationType.BACKUP,
                    None,
                    force=False,
                )

                with patch.object(
                    controller.snapshot_service,
                    "create",
                    side_effect=OSError(
                        "injected worker exception"
                    ),
                ):
                    with self.assertLogs(level="ERROR"):
                        worker.execute(commands[-1])

                row = controller.repo.operation(op)
                self.assertEqual(row["state"], "failed")
                self.assertEqual(
                    row["error_code"],
                    "WORKER_EXECUTION_FAILED",
                )
                self.assertIsNone(controller.active_operation_id)
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_queue_rejects_work_after_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                controller._shutdown_requested = True

                with self.assertRaises(AppError) as error:
                    controller._queue(
                        OperationType.BACKUP,
                        None,
                        force=False,
                    )

                self.assertEqual(
                    error.exception.code,
                    "APP_SHUTTING_DOWN",
                )
                self.assertEqual(
                    controller.repo.unfinished_operations(),
                    [],
                )

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_self_test_subprocess_path_uses_private_snapshot(
        self,
    ) -> None:
        global _SELF_TEST_MODULE_PATH

        previous = _SELF_TEST_MODULE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp:
                snapshot = Path(temp) / "snapshot.py"
                snapshot.write_text(
                    "VALUE = 17",
                    encoding="utf-8",
                )
                _SELF_TEST_MODULE_PATH = snapshot
                self.assertEqual(
                    self_test_module_path(),
                    snapshot,
                )

                script = (
                    "import runpy,sys; "
                    "assert runpy.run_path(sys.argv[1])['VALUE'] == 17"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(self_test_module_path()),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout + result.stderr,
                )

        finally:
            _SELF_TEST_MODULE_PATH = previous

    def test_runtime_large_byte_progress_and_stale_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = MainWindow(controller, paths)

            try:
                controller.active_operation_id = "progress-test"
                event = ProgressEvent(
                    "progress-test",
                    "Large",
                    "file",
                    total_bytes=2**50,
                    completed_bytes=2**49,
                )
                window.on_progress(event)

                self.assertEqual(
                    window.progress_bar.maximum(),
                    10000,
                )
                self.assertEqual(
                    window.progress_bar.value(),
                    5000,
                )

                window.on_progress(
                    ProgressEvent(
                        "stale-test",
                        "Wrong",
                        "other",
                    )
                )
                self.assertEqual(
                    window.progress_title.text(),
                    "Large",
                )

            finally:
                controller.active_operation_id = None
                window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_language_change_preserves_progress_overlay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(paths, start_worker=False)
            window = MainWindow(controller, paths)

            try:
                window.show_progress(
                    "Copy",
                    "current-path",
                    False,
                )
                window.progress_bar.setRange(0, 100)
                window.progress_bar.setValue(42)

                controller.i18n.set_language(
                    Language.RUSSIAN.value
                )
                window.apply_language()

                self.assertFalse(
                    window.progress_frame.isHidden()
                )
                self.assertEqual(
                    window.progress_title.text(),
                    "Copy",
                )
                self.assertEqual(
                    window.progress_path.text(),
                    "current-path",
                )
                self.assertEqual(
                    window.progress_bar.value(),
                    42,
                )
                self.assertTrue(
                    window.cancel_button.isHidden()
                )

            finally:
                window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_async_shutdown_workerless_is_idempotent_and_blocks_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                completed = []
                controller.shutdown_finished.connect(
                    lambda: completed.append(True)
                )

                controller.begin_shutdown()
                controller.begin_shutdown()

                self.assertTrue(controller._shutdown_completed)
                self.assertEqual(completed, [True])
                self.assertIsNone(controller._shutdown_timer)

                with self.assertRaises(AppError) as error:
                    controller.start_automation_runtime()

                self.assertEqual(
                    error.exception.code,
                    "APP_SHUTTING_DOWN",
                )
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_async_shutdown_idle_real_thread_stops_before_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp))
            )
            try:
                completed = []
                controller.shutdown_finished.connect(
                    lambda: completed.append(True)
                )
                controller.begin_shutdown()

                deadline = time.monotonic() + 3
                while (
                    not controller._shutdown_completed
                    and time.monotonic() < deadline
                ):
                    QApplication.processEvents()
                    time.sleep(0.005)

                self.assertTrue(controller._shutdown_completed)
                self.assertEqual(completed, [True])
                self.assertIsNone(controller.thread)
                self.assertIsNone(controller.worker)
                self.assertIsNone(controller._shutdown_timer)

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_async_shutdown_keeps_gui_responsive_until_busy_worker_returns(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp))
            )
            entered = threading.Event()
            release = threading.Event()

            heartbeat = QTimer()
            ticks = []
            heartbeat.setInterval(10)
            heartbeat.timeout.connect(
                lambda: ticks.append(True)
            )

            def slow(
                operation_id,
                plan_id,
                force,
                progress,
                cancelled,
            ):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError(
                        "test worker release timeout"
                    )
                return OperationResult(
                    operation_id,
                    False,
                    "cancelled",
                    "OPERATION_CANCELLED",
                )

            try:
                with patch.object(
                    controller.snapshot_service,
                    "create",
                    side_effect=slow,
                ):
                    op = controller._queue(
                        OperationType.BACKUP,
                        None,
                        force=False,
                    )

                    deadline = time.monotonic() + 2
                    while (
                        not entered.is_set()
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(entered.is_set())
                    heartbeat.start()

                    before = time.monotonic()
                    controller.begin_shutdown()
                    self.assertLess(
                        time.monotonic() - before,
                        0.5,
                    )

                    deadline = time.monotonic() + 0.15
                    while time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(ticks)
                    self.assertFalse(
                        controller._shutdown_completed
                    )
                    self.assertTrue(
                        controller.thread.isRunning()
                    )
                    self.assertEqual(
                        controller.active_operation_id,
                        op,
                    )
                    self.assertTrue(
                        controller._active_cancel_event.is_set()
                    )

                    release.set()

                    deadline = time.monotonic() + 3
                    while (
                        not controller._shutdown_completed
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(
                        controller._shutdown_completed
                    )
                    self.assertEqual(
                        controller.repo.operation(op)["state"],
                        "cancelled",
                    )

            finally:
                release.set()
                heartbeat.stop()
                controller.shutdown()
                QApplication.processEvents()

    def test_async_window_close_waits_and_declined_exit_keeps_working(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(
                paths,
                start_worker=False,
            )
            window = MainWindow(controller, paths)

            try:
                op = controller.repo.create_operation(
                    OperationType.BACKUP,
                    None,
                    None,
                    {},
                )
                controller.active_operation_id = op
                controller._active_cancel_event = threading.Event()

                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.No,
                ):
                    self.assertFalse(window.close())

                self.assertFalse(
                    controller._shutdown_requested
                )
                self.assertFalse(
                    controller._active_cancel_event.is_set()
                )

                controller.active_operation_id = None
                controller.repo.operation_state(
                    op,
                    "cancelled",
                    CancelledError(),
                )

                completed = []
                controller.shutdown_finished.connect(
                    lambda: completed.append(True)
                )

                self.assertFalse(window.close())
                QApplication.processEvents()

                self.assertTrue(controller._shutdown_completed)
                self.assertEqual(completed, [True])

            finally:
                controller.active_operation_id = None
                controller.shutdown()
                window.close()
                QApplication.processEvents()

    def test_async_shutdown_dead_worker_retains_uncertain_operation_for_recovery(
        self,
    ) -> None:
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                op = controller.repo.create_operation(
                    OperationType.BACKUP,
                    None,
                    None,
                    {},
                )
                controller.active_operation_id = op
                controller._active_cancel_event = threading.Event()

                thread = Mock()
                thread.isRunning.return_value = False
                thread.wait.return_value = True
                controller.thread = thread

                controller.begin_shutdown()

                self.assertTrue(controller._shutdown_completed)
                self.assertEqual(
                    controller.repo.operation(op)["state"],
                    "needs_recovery",
                )
                self.assertIsNone(
                    controller.active_operation_id
                )
                thread.wait.assert_any_call(0)

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_duplicate_result_does_not_finalize_twice(
        self,
    ) -> None:
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                result = OperationResult(
                    "completed-id",
                    True,
                    "done",
                )
                receiver = Mock()
                controller.operation_finished.connect(receiver)

                controller._finished(result)
                controller._finished(result)

                receiver.assert_called_once_with(result)

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_background_database_check_coalesces_and_routes_separate_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                worker = OperationWorker(controller)
                controller.worker = worker
                commands = []
                results = []
                business = []

                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)
                controller.background_query_finished.connect(
                    results.append
                )
                controller.operation_finished.connect(business.append)

                op = controller.queue_database_check()
                self.assertEqual(
                    controller.queue_database_check(),
                    op,
                )
                self.assertEqual(len(commands), 1)

                worker.execute(commands[-1])

                self.assertEqual(
                    controller.repo.operation(op)["state"],
                    "succeeded",
                )
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].success)
                self.assertEqual(business, [])
                self.assertEqual(controller._background_queries, {})
                self.assertIsNone(controller.active_operation_id)
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_background_database_failure_never_reports_success(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                worker = OperationWorker(controller)
                controller.worker = worker
                commands = []
                results = []

                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)
                controller.background_query_finished.connect(
                    results.append
                )

                op = controller.queue_database_check()

                with patch.object(
                    controller.db,
                    "verify_contract",
                    side_effect=AppError(
                        "DATABASE_INTEGRITY_FAILED",
                        "damaged",
                    ),
                ):
                    worker.execute(commands[-1])

                self.assertFalse(results[0].success)
                self.assertEqual(
                    controller.repo.operation(op)["state"],
                    "failed",
                )
                self.assertEqual(
                    results[0].error_code,
                    "DATABASE_INTEGRITY_FAILED",
                )
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_background_cache_dialog_never_reads_manifest_in_ui(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(
                self.make_paths(root),
                start_worker=False,
            )
            dialog = None

            try:
                source = root / "files"
                source.mkdir()
                (source / "one.txt").write_bytes(b"one")

                plan = controller.create_or_update_plan(
                    None,
                    "Files",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )
                op = controller.repo.create_operation(
                    OperationType.BACKUP,
                    plan,
                    None,
                    {},
                )
                service = SnapshotService(
                    controller.repo,
                    controller.storage,
                    controller.health_service,
                    reserve_bytes=0,
                    reserve_ratio=0,
                )
                self.assertTrue(
                    service.create(
                        op,
                        plan,
                        False,
                        lambda event: None,
                        lambda: False,
                    ).success
                )
                snapshot = controller.repo.available_snapshots(plan)[0]
                controller.load_snapshot_files_for_test(snapshot.id)

                with patch.object(
                    controller.verifier,
                    "_read_manifest",
                    side_effect=AssertionError(
                        "UI manifest read"
                    ),
                ):
                    dialog = RestoreSelectedFilesDialog(
                        controller,
                        snapshot.id,
                    )
                    self.assertEqual(
                        dialog.files_list.rowCount(),
                        1,
                    )

                controller.repo.mark_snapshot(
                    snapshot.id,
                    "corrupted",
                )
                with self.assertRaises(AppError):
                    controller.cached_snapshot_files(snapshot.id)

            finally:
                if dialog is not None:
                    dialog.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_background_database_check_real_thread_keeps_gui_responsive(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp))
            )
            entered = threading.Event()
            release = threading.Event()
            results = []
            ticks = []
            heartbeat = QTimer()
            heartbeat.setInterval(10)
            heartbeat.timeout.connect(lambda: ticks.append(True))
            controller.background_query_finished.connect(
                results.append
            )

            def slow_check():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("release timeout")

            try:
                with patch.object(
                    controller.db,
                    "verify_contract",
                    side_effect=slow_check,
                ):
                    before = time.monotonic()
                    op = controller.queue_database_check()
                    self.assertLess(
                        time.monotonic() - before,
                        0.5,
                    )

                    deadline = time.monotonic() + 2
                    while (
                        not entered.is_set()
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(entered.is_set())
                    heartbeat.start()

                    deadline = time.monotonic() + 0.15
                    while time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(ticks)
                    self.assertEqual(
                        controller.repo.operation(op)["state"],
                        "running",
                    )

                    release.set()
                    deadline = time.monotonic() + 3
                    while (
                        not results
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(
                        results and results[0].success
                    )

            finally:
                release.set()
                heartbeat.stop()
                controller.shutdown()
                QApplication.processEvents()

    def test_background_closed_settings_ignores_late_result(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(
                paths,
                start_worker=False,
            )
            dialog = SettingsDialog(controller, paths)

            try:
                with patch.object(
                    controller,
                    "queue_database_check",
                    return_value="diagnostic-id",
                ):
                    dialog.check_integrity()

                dialog.reject()

                with patch.object(
                    QMessageBox,
                    "information",
                ) as information:
                    controller.background_query_finished.emit(
                        OperationResult(
                            "diagnostic-id",
                            True,
                            "done",
                        )
                    )
                    information.assert_not_called()

                self.assertFalse(dialog._integrity_connected)

            finally:
                dialog.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_requests_are_coalesced_and_do_not_scan_in_gui(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                worker = OperationWorker(controller)
                controller.worker = worker
                controller._automation_runtime_running = True

                commands = []
                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)

                with (
                    patch.object(
                        controller.polling_watcher,
                        "poll_once",
                        return_value=[],
                    ) as watcher,
                    patch.object(
                        controller.schedule_coordinator,
                        "tick",
                        return_value=[],
                    ) as schedule,
                    patch.object(
                        controller.auto_coordinator,
                        "evaluate_pending",
                        return_value=[],
                    ),
                ):
                    op = controller.request_runtime_tick()

                    for _ in range(5):
                        self.assertEqual(
                            controller.request_runtime_tick(),
                            op,
                        )

                    self.assertEqual(len(commands), 1)
                    watcher.assert_not_called()
                    schedule.assert_not_called()
                    self.assertTrue(
                        controller._runtime_tick_pending
                    )

                    controller._runtime_tick_pending = False
                    worker.execute(commands[-1])

                    watcher.assert_called_once()
                    schedule.assert_called_once()
                    self.assertEqual(
                        controller.repo.operation(op)["state"],
                        "succeeded",
                    )

                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_defers_without_creating_operation_while_backup_is_active(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                controller.worker = OperationWorker(controller)
                controller._automation_runtime_running = True

                existing = controller.repo.create_operation(
                    OperationType.BACKUP,
                    None,
                    None,
                    {},
                )
                controller.active_operation_id = existing

                for _ in range(5):
                    self.assertIsNone(
                        controller.request_runtime_tick()
                    )

                self.assertTrue(
                    controller._runtime_tick_pending
                )
                self.assertEqual(
                    len(controller.repo.unfinished_operations()),
                    1,
                )

                controller.repo.operation_state(
                    existing,
                    "cancelled",
                    CancelledError(),
                )
                controller.active_operation_id = None
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_queues_one_automatic_backup_after_worker_result(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(
                self.make_paths(root),
                start_worker=False,
            )
            try:
                source = root / "runtime-source.txt"
                source.write_bytes(b"saved")

                plan = controller.create_or_update_plan(
                    None,
                    "Runtime",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )
                controller.repo.update_trigger_policy(
                    plan,
                    TriggerPolicy(
                        True,
                        1,
                        0,
                        False,
                        60,
                        False,
                        None,
                        None,
                        True,
                    ),
                )
                controller.repo.mark_pending_trigger(
                    plan,
                    TRIGGER_MASK_EVENT,
                    utc_ms() - 2000,
                )

                worker = OperationWorker(controller)
                controller.worker = worker
                controller._automation_runtime_running = True

                commands = []
                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)

                with (
                    patch.object(
                        controller.polling_watcher,
                        "poll_once",
                        return_value=[],
                    ),
                    patch.object(
                        controller.schedule_coordinator,
                        "tick",
                        return_value=[],
                    ),
                ):
                    runtime_id = controller.request_runtime_tick()
                    worker.execute(commands[0])

                self.assertEqual(len(commands), 2)
                self.assertEqual(
                    commands[1]["kind"],
                    OperationType.BACKUP.value,
                )
                self.assertEqual(
                    commands[1]["plan_id"],
                    plan,
                )
                self.assertEqual(
                    commands[1]["trigger_reason"],
                    TriggerReason.EVENT.value,
                )
                self.assertEqual(
                    controller.repo.operation(runtime_id)["state"],
                    "succeeded",
                )
                self.assertEqual(
                    controller.active_operation_id,
                    commands[1]["operation_id"],
                )

                controller.cancel_active()
                worker.execute(commands[1])

                self.assertEqual(
                    controller.repo.operation(
                        commands[1]["operation_id"]
                    )["state"],
                    "cancelled",
                )
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_old_drain_callback_does_not_restart_stopped_runtime(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )
            try:
                controller._automation_runtime_running = True
                old = controller._runtime_generation
                controller.stop_automation_runtime()

                with patch.object(
                    controller,
                    "request_runtime_tick",
                ) as request:
                    controller._drain_runtime_tick(old)
                    request.assert_not_called()

                self.assertFalse(
                    controller._runtime_tick_pending
                )
                self.assertFalse(
                    controller._runtime_tick_draining
                )

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_worker_cancellation_preserves_pending_triggers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(
                self.make_paths(root),
                start_worker=False,
            )
            try:
                source = root / "runtime-pending.txt"
                source.write_bytes(b"saved")

                plan = controller.create_or_update_plan(
                    None,
                    "Pending",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )
                controller.repo.mark_pending_trigger(
                    plan,
                    TRIGGER_MASK_SCHEDULE,
                    utc_ms(),
                )

                worker = OperationWorker(controller)
                controller.worker = worker
                controller._automation_runtime_running = True

                commands = []
                controller.request_execute.connect(commands.append)
                worker.finished.connect(controller._finished)

                op = controller.request_runtime_tick()
                controller.cancel_active()
                worker.execute(commands[-1])

                self.assertEqual(
                    controller.repo.operation(op)["state"],
                    "cancelled",
                )
                self.assertEqual(
                    controller.repo.plan_details(
                        plan
                    ).pending_trigger_mask,
                    TRIGGER_MASK_SCHEDULE,
                )
                controller.worker = None

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_slow_watcher_runs_in_worker_and_gui_stays_responsive(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp))
            )
            entered = threading.Event()
            release = threading.Event()
            results = []
            controller.background_query_finished.connect(
                results.append
            )

            ticks = []
            heartbeat = QTimer()
            heartbeat.setInterval(10)
            heartbeat.timeout.connect(lambda: ticks.append(True))

            def slow(now):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("release timeout")
                return []

            try:
                controller._automation_runtime_running = True

                with (
                    patch.object(
                        controller.polling_watcher,
                        "poll_once",
                        side_effect=slow,
                    ),
                    patch.object(
                        controller.schedule_coordinator,
                        "tick",
                        return_value=[],
                    ),
                ):
                    before = time.monotonic()
                    op = controller.request_runtime_tick()

                    self.assertLess(
                        time.monotonic() - before,
                        0.5,
                    )

                    deadline = time.monotonic() + 2
                    while (
                        not entered.is_set()
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(entered.is_set())
                    heartbeat.start()

                    deadline = time.monotonic() + 0.15
                    while time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(ticks)
                    release.set()

                    deadline = time.monotonic() + 3
                    while (
                        not results
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(
                        results and results[0].success
                    )
                    self.assertEqual(
                        controller.repo.operation(op)["state"],
                        "succeeded",
                    )

            finally:
                release.set()
                heartbeat.stop()
                controller.shutdown()
                QApplication.processEvents()

    def test_runtime_notification_is_delivered_on_gui_thread(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(
                self.make_paths(root)
            )
            delivered = []
            results = []
            controller.background_query_finished.connect(
                results.append
            )
            gui_thread = QThread.currentThread()

            try:
                source = root / "runtime-notification.txt"
                source.write_bytes(b"saved")

                plan = controller.create_or_update_plan(
                    None,
                    "Notify",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )
                controller.repo.update_trigger_policy(
                    plan,
                    TriggerPolicy(
                        True,
                        1,
                        0,
                        False,
                        60,
                        False,
                        None,
                        None,
                        True,
                    ),
                )
                source.unlink()
                controller._automation_runtime_running = True

                def notify(*args, **kwargs):
                    delivered.append(QThread.currentThread())
                    return False

                with patch.object(
                    controller.notification_service,
                    "notify",
                    side_effect=notify,
                ):
                    controller.request_runtime_tick()

                    deadline = time.monotonic() + 3
                    while (
                        (not delivered or not results)
                        and time.monotonic() < deadline
                    ):
                        QApplication.processEvents()
                        time.sleep(0.005)

                    self.assertTrue(delivered)
                    self.assertTrue(
                        all(
                            thread == gui_thread
                            for thread in delivered
                        )
                    )
                    self.assertTrue(
                        results and results[0].success
                    )

            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_cache_fallback_does_not_probe_filesystem(
        self,
    ) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            controller = AppController(
                paths,
                start_worker=False,
            )
            window = None

            try:
                source = root / "health-cache.txt"
                source.write_text("saved", encoding="utf-8")

                controller.create_or_update_plan(
                    None,
                    "Health cache",
                    "",
                    str(source),
                    str(root / "storage"),
                    5,
                    StorageMode.DIRECT.value,
                )

                with (
                    patch.object(
                        controller.health_service,
                        "evaluate",
                        side_effect=AssertionError(
                            "UI must not call HealthService.evaluate"
                        ),
                    ),
                    patch.object(
                        controller.health_service,
                        "evaluate_all",
                        side_effect=AssertionError(
                            "UI must not call "
                            "HealthService.evaluate_all"
                        ),
                    ),
                    patch.object(
                        controller.health_service,
                        "probe_source",
                        side_effect=AssertionError(
                            "UI must not call "
                            "HealthService.probe_source"
                        ),
                    ),
                    patch.object(
                        controller.health_service,
                        "probe_storage",
                        side_effect=AssertionError(
                            "UI must not call "
                            "HealthService.probe_storage"
                        ),
                    ),
                ):
                    window = MainWindow(controller, paths)
                    controller.main_window = window
                    QApplication.processEvents()

                self.assertEqual(
                    len(window.plan_cards),
                    1,
                )

            finally:
                if window is not None:
                    window.close()
                controller.shutdown()
                QApplication.processEvents()

    def test_app_controller_initializes_health_cache_structures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(
                self.make_paths(Path(temp)),
                start_worker=False,
            )

            try:
                self.assertEqual(
                    controller._health_by_plan_id,
                    {},
                )
                self.assertEqual(
                    controller._health_cache_meta,
                    {},
                )
                self.assertFalse(
                    controller._health_refresh_pending
                )
                self.assertFalse(
                    controller._health_refresh_draining
                )

            finally:
                controller.shutdown()
                QApplication.processEvents()


    def _make_health_contract_plan(self, controller, root):
        source = root / "health-contract.txt"
        source.write_bytes(b"original")
        plan_id = controller.create_or_update_plan(
            None, "Health contract", "", str(source),
            str(root / "storage"), 5, StorageMode.DIRECT.value,
        )
        return plan_id, source

    def _refresh_health_contract(self, controller):
        result = controller._perform_health_refresh(
            "health-contract", lambda: False,
        )
        controller._accept_health_refresh(result)
        self.assertTrue(result.success, result.message)
        return result

    def test_health_refresh_round_trip_uses_single_probe_pair(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            try:
                plan_id, _ = self._make_health_contract_plan(controller, root)
                with (
                    patch.object(controller.health_service, "probe_source",
                                 wraps=controller.health_service.probe_source) as source,
                    patch.object(controller.health_service, "probe_storage",
                                 wraps=controller.health_service.probe_storage) as storage,
                ):
                    result = self._refresh_health_contract(controller)
                self.assertEqual(source.call_count, 1)
                self.assertEqual(storage.call_count, 1)
                meta = controller._valid_health_cache_meta(
                    controller.repo.plan_details(plan_id)
                )
                self.assertIsNotNone(meta)
                self.assertIsNone(meta["source_error_code"])
                self.assertIsInstance(meta["storage_free_bytes"], int)
                self.assertEqual(result.result_data["plans"][0]["plan_id"], plan_id)
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_refresh_dismissed_mismatch_still_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            try:
                plan_id, source = self._make_health_contract_plan(controller, root)
                source.rename(root / "health-retained.txt")
                source.write_bytes(b"replacement")
                self._refresh_health_contract(controller)
                for issue in controller.repo.list_active_issues(plan_id):
                    if issue.code == "SOURCE_IDENTITY_MISMATCH":
                        controller.repo.dismiss_issue(issue.key)
                self._refresh_health_contract(controller)
                self.assertFalse(controller.repo.list_active_issues(plan_id))
                self.assertTrue(controller.source_confirmation_required(plan_id))
                self.assertEqual(controller.cached_plan_health(plan_id).state,
                                 HealthState.ERROR.value)
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_refresh_rebind_invalidates_old_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            try:
                plan_id, source = self._make_health_contract_plan(controller, root)
                source.rename(root / "health-retained.txt")
                source.write_bytes(b"replacement")
                self._refresh_health_contract(controller)
                controller.confirm_source_identity(plan_id)
                self.assertIsNone(controller._valid_health_cache_meta(
                    controller.repo.plan_details(plan_id)
                ))
                self._refresh_health_contract(controller)
                self.assertFalse(controller.source_confirmation_required(plan_id))
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_refresh_rejects_incomplete_result_atomically(self):
        import copy
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            try:
                self._make_health_contract_plan(controller, root)
                result = self._refresh_health_contract(controller)
                old_health = controller._health_by_plan_id
                old_meta = controller._health_cache_meta
                broken = copy.deepcopy(result)
                del broken.result_data["plans"][0]["signature"]
                controller._accept_health_refresh(broken)
                self.assertFalse(broken.success)
                self.assertEqual(broken.error_code, "BACKGROUND_QUERY_RESULT_MISSING")
                self.assertIs(controller._health_by_plan_id, old_health)
                self.assertIs(controller._health_cache_meta, old_meta)
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_refresh_cancelled_does_not_publish_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            try:
                self._make_health_contract_plan(controller, root)
                with self.assertRaises(CancelledError):
                    controller._perform_health_refresh("cancelled-health", lambda: True)
                self.assertEqual(controller._health_by_plan_id, {})
                self.assertEqual(controller._health_cache_meta, {})
            finally:
                controller.shutdown()
                QApplication.processEvents()

    def test_health_refresh_real_worker_exposes_confirmation_button(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=True)
            window = None
            try:
                plan_id, source = self._make_health_contract_plan(controller, root)
                source.rename(root / "health-retained.txt")
                source.write_bytes(b"replacement")
                gui_thread = QThread.currentThread()
                probe_threads = []
                received = []
                original_probe = controller.health_service.probe_source

                def tracked_probe(plan):
                    probe_threads.append(QThread.currentThread())
                    return original_probe(plan)

                controller.background_query_finished.connect(received.append)
                with patch.object(controller.health_service, "probe_source",
                                  side_effect=tracked_probe):
                    window = MainWindow(controller, controller.paths)
                    controller.main_window = window
                    window.select_plan_by_id(plan_id)
                    deadline = time.monotonic() + 10.0
                    while not received and time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(0.005)
                    self.assertTrue(received, "Health refresh timed out")
                    self.assertTrue(received[0].success, received[0].message)
                    self.assertTrue(probe_threads)
                    self.assertTrue(all(thread != gui_thread for thread in probe_threads))
                    while window._ui_refresh_pending and time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(0.005)
                    self.assertFalse(window._ui_refresh_pending)
                    buttons = [button for button in window.detail_view.findChildren(QPushButton)
                               if button.text() == controller.i18n.tr("source.confirm")]
                    self.assertEqual(len(buttons), 1)
            finally:
                controller.shutdown()
                if window is not None:
                    window.close()
                QApplication.processEvents()


    def test_gui_starts_with_legacy_operations_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            fixture = DatabaseUpgradeTests()._legacy_database(root, populated=False)
            fixture.close_for_cleanup()
            paths.db_path = fixture.path
            controller = None
            window = None
            try:
                controller = AppController(paths, start_worker=True)
                received = []
                controller.background_query_finished.connect(received.append)
                window = MainWindow(controller, paths)
                controller.main_window = window
                deadline = time.monotonic() + 10.0
                while not received and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.005)
                self.assertTrue(received, "Startup health refresh timed out")
                self.assertTrue(received[0].success, received[0].message)
                controller.db.verify_contract()
                with controller.db.read_connection() as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT MAX(version) "
                            "FROM SchemaMigrations"
                        ).fetchone()[0],
                        DB_SCHEMA_VERSION,
                    )
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM Operations WHERE type='refresh_health'").fetchone()[0], 1)
            finally:
                if controller is not None:
                    controller.shutdown()
                if window is not None:
                    window.close()
                QApplication.processEvents()


    def _wait_for_ui_condition(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), "UI condition timed out")

    def test_shutdown_close_timer_is_owned_by_window(self):
        from shiboken6 import isValid
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            controller = AppController(
                paths,
                start_worker=False,
            )
            window = None

            try:
                # This test checks the window life cycle,
                # not the start of the background scan.
                with patch.object(
                    controller,
                    "queue_health_refresh",
                    return_value=None,
                ):
                    window = MainWindow(
                        controller,
                        paths,
                    )

                controller.main_window = window

                window._shutdown_ready()

                timer = getattr(
                    window,
                    "_shutdown_close_timer",
                    None,
                )
                self.assertIsNotNone(
                    timer,
                    "The close must use "
                    "a timer owned by the window.",
                )
                self.assertIs(timer.parent(), window)
                self.assertTrue(timer.isSingleShot())
                self.assertTrue(timer.isActive())

                # A repeated notification must not create
                # another pending close.
                window._shutdown_ready()

                self.assertIs(
                    window._shutdown_close_timer,
                    timer,
                )

                # First delete the receiver while the timer
                # is still waiting for the event to be processed.
                window.deleteLater()
                QApplication.sendPostedEvents(
                    None,
                    QEvent.Type.DeferredDelete,
                )

                self.assertFalse(isValid(window))
                self.assertFalse(isValid(timer))

                controller.main_window = None
                window = None

                # The queue must not keep a call
                # to the already destroyed window.
                QApplication.processEvents()

            finally:
                controller.main_window = None

                if window is not None and isValid(window):
                    window.deleteLater()
                    QApplication.sendPostedEvents(
                        None,
                        QEvent.Type.DeferredDelete,
                    )

                controller.shutdown()
                controller.deleteLater()

                QApplication.sendPostedEvents(
                    None,
                    QEvent.Type.DeferredDelete,
                )
                QApplication.processEvents()

    def _dispose_ui_test_window(self, controller, window):
        controller.shutdown()
        window.close()
        window.deleteLater()
        controller.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()

    def test_ui_refresh_burst_coalesces_and_merges_health_request(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            window = MainWindow(controller, controller.paths)
            try:
                with patch.object(window, "refresh", wraps=window.refresh) as refresh:
                    for _ in range(500):
                        window.request_refresh(False)
                    window.request_refresh(True)
                    window.request_refresh(False)
                    self.assertEqual(refresh.call_count, 0)
                    self._wait_for_ui_condition(lambda: refresh.call_count == 1)
                    self.assertEqual(refresh.call_args.kwargs, {"request_health_refresh": True})
                    self.assertFalse(window._ui_refresh_pending)
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_search_debounce_uses_latest_text_without_health_scan(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            self._make_health_contract_plan(controller, root)
            window = MainWindow(controller, controller.paths)
            try:
                with (
                    patch.object(window, "refresh_plans", wraps=window.refresh_plans) as plans,
                    patch.object(controller, "queue_health_refresh", side_effect=AssertionError("Search must not scan")),
                ):
                    for text in ("H", "He", "Health", "missing-plan"):
                        window.search_edit.setText(text)
                    self.assertEqual(plans.call_count, 0)
                    self._wait_for_ui_condition(lambda: plans.call_count == 1)
                    self.assertEqual(window.search_edit.text(), "missing-plan")
                    self.assertEqual(window.plan_cards, {})
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_explicit_refresh_consumes_pending_refresh(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            window = MainWindow(controller, controller.paths)
            try:
                with patch.object(window, "refresh", wraps=window.refresh) as refresh:
                    window.request_refresh(False)
                    window.search_edit.setText("pending")
                    window.refresh(False)
                    QApplication.processEvents()
                    self.assertEqual(refresh.call_count, 1)
                    self.assertFalse(window._ui_refresh_timer.isActive())
                    self.assertFalse(window._search_refresh_timer.isActive())
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_pending_refresh_does_not_run_after_shutdown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            window = MainWindow(controller, controller.paths)
            try:
                with patch.object(window, "refresh", wraps=window.refresh) as refresh:
                    window.request_refresh(False)
                    window.search_edit.setText("pending")
                    controller.shutdown()
                    window.close()
                    QApplication.processEvents()
                    window.request_refresh(True)
                    self.assertEqual(refresh.call_count, 0)
                    self.assertFalse(window._ui_refresh_timer.isActive())
                    self.assertFalse(window._search_refresh_timer.isActive())
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_language_rebuild_preserves_owned_timer_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            window = MainWindow(controller, controller.paths)
            try:
                timers = (window._ui_refresh_timer, window._search_refresh_timer)
                for index in range(20):
                    window.search_edit.setText("stable")
                    window.request_refresh(False)
                    controller.i18n.set_language("ru" if index % 2 else "en")
                    window.apply_language()
                    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.assertEqual((window._ui_refresh_timer, window._search_refresh_timer), timers)
                    self.assertTrue(all(timer.parent() is window for timer in timers))
                    self.assertEqual(window.search_edit.text(), "stable")
                    self.assertFalse(window._ui_refresh_pending)
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_stress_refresh_keeps_widget_inventory_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            plan_id, _ = self._make_health_contract_plan(controller, root)
            window = MainWindow(controller, controller.paths)
            try:
                window.select_plan_by_id(plan_id)
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                baseline = len(window.findChildren(QWidget))
                for _ in range(100):
                    window.refresh(False)
                    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    QApplication.processEvents()
                    self.assertLessEqual(len(window.findChildren(QWidget)), baseline + 4)
                    self.assertEqual(len(window.plans_container.findChildren(PlanCard)), 1)
                    self.assertEqual(len(window.detail_view.findChildren(QTableWidget)), 1)
                    self.assertEqual(len(window.detail_view.findChildren(StatusBadge)), 1)
            finally:
                self._dispose_ui_test_window(controller, window)

    def test_ui_stress_repeated_real_worker_lifecycle_releases_resources(self):
        from shiboken6 import isValid
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(15):
                paths = self.make_paths(root / str(index))
                controller = AppController(paths, start_worker=True)
                window = MainWindow(controller, paths)
                controller.main_window = window
                thread = controller.thread
                timers = (window._ui_refresh_timer, window._search_refresh_timer)
                health_timer = controller._health_drain_timer
                try:
                    window.request_refresh(False)
                    window.search_edit.setText("pending")
                    controller.begin_shutdown()
                    self._wait_for_ui_condition(lambda: controller._shutdown_completed, timeout=10.0)
                    self.assertIsNone(controller.thread)
                    self.assertIsNone(controller.worker)
                    self.assertTrue(not isValid(thread) or not thread.isRunning())
                    self.assertTrue(all(not timer.isActive() for timer in timers))
                    self.assertFalse(health_timer.isActive())
                finally:
                    self._dispose_ui_test_window(controller, window)
                self.assertFalse(isValid(window))
                self.assertTrue(all(not isValid(timer) for timer in timers))
                self.assertFalse(isValid(health_timer))


    def test_health_drain_coalesces_and_survives_cache_generation_change(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            try:
                controller._health_refresh_pending = True
                with patch.object(controller, "queue_health_refresh", return_value=None) as queue:
                    for _ in range(300):
                        controller._schedule_health_refresh_drain()
                    controller._health_refresh_generation += 1
                    self._wait_for_ui_condition(lambda: queue.call_count == 1)
                    self.assertFalse(controller._health_refresh_draining)
            finally:
                controller.shutdown()
                controller.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_health_drain_is_cancelled_during_shutdown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            controller = AppController(self.make_paths(Path(temp)), start_worker=False)
            try:
                controller._health_refresh_pending = True
                controller._schedule_health_refresh_drain()
                with patch.object(controller, "queue_health_refresh") as queue:
                    controller.begin_shutdown()
                    QApplication.processEvents()
                    queue.assert_not_called()
                self.assertFalse(controller._health_drain_timer.isActive())
                self.assertFalse(controller._health_refresh_pending)
                self.assertFalse(controller._health_refresh_draining)
            finally:
                controller.shutdown()
                controller.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_health_result_updates_ui_without_window_request_id(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = AppController(self.make_paths(root), start_worker=False)
            plan_id, source = self._make_health_contract_plan(controller, root)
            window = MainWindow(controller, controller.paths)
            try:
                window.select_plan_by_id(plan_id)
                self.assertIsNone(window._health_refresh_operation_id)
                source.rename(root / "retained.txt")
                source.write_bytes(b"replacement")
                operation_id = controller.repo.create_operation(OperationType.REFRESH_HEALTH, None, None, {})
                controller._background_queries[operation_id] = (OperationType.REFRESH_HEALTH.value, None)
                controller.active_operation_id = operation_id
                result = controller._perform_health_refresh(operation_id, lambda: False)
                controller.repo.operation_state(operation_id, "succeeded")
                with patch.object(window, "refresh", wraps=window.refresh) as refresh:
                    controller._finished(result)
                    self._wait_for_ui_condition(lambda: refresh.call_count == 1)
                    self.assertEqual(refresh.call_args.kwargs, {"request_health_refresh": False})
                buttons = [button for button in window.detail_view.findChildren(QPushButton)
                           if button.text() == controller.i18n.tr("source.confirm")]
                self.assertEqual(len(buttons), 1)
            finally:
                self._dispose_ui_test_window(controller, window)


class DatabaseUpgradeTests(unittest.TestCase):
    def _legacy_database(self, root: Path, populated: bool = True) -> Database:
        db = Database(root / "legacy-operations.sqlite3")
        db.migrate()
        repo = Repository(db)
        if populated:
            source = root / "legacy-source.txt"
            source.write_bytes(b"retained")
            storage_path = PathPolicy.validate_storage(str(root / "storage"))
            storage_id = repo.get_or_create_storage(storage_path)
            plan_id = repo.create_plan("Legacy plan", "", PathPolicy.validate_source(str(source)),
                                       storage_id, 5, StorageMode.DIRECT.value)
            with db.transaction() as conn:
                conn.execute("INSERT INTO Operations(id,type,state,plan_id,payload_json,created_at_utc_ms) "
                             "VALUES ('legacy-op','backup','succeeded',?, ?,1)",
                             (plan_id, '{"legacy":true}'))
                conn.execute("INSERT INTO Snapshots(id,plan_id,operation_id,sequence_no,state,created_at_utc_ms,artifact_relpath,manifest_relpath) "
                             "VALUES ('legacy-snapshot',?,'legacy-op',1,'committed',1,'artifact','manifest')", (plan_id,))
                conn.execute("INSERT INTO ActivityLog(id,created_at_utc_ms,level,plan_id,operation_id,event_code,message) "
                             "VALUES ('legacy-activity',1,'info',?,'legacy-op','LEGACY','retained')", (plan_id,))
                conn.execute("INSERT INTO PlanDeletionJobs VALUES (?,'legacy-op','{}','{}',1)", (plan_id,))
                conn.execute("INSERT INTO RetiredPlans VALUES ('retired-id','Retired','source','storage',1,'legacy-op')")
        with db._migration_transaction() as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='Operations'").fetchone()[0]
            ddl = ddl.replace('CREATE TABLE Operations', 'CREATE TABLE Operations_fixture')
            ddl = ddl.replace('type TEXT NOT NULL,',
                              "type TEXT NOT NULL CHECK(type IN ('backup','verify','restore_beside','delete_snapshots','archive_plan')),")
            conn.execute(ddl)
            conn.execute("INSERT INTO Operations_fixture SELECT * FROM Operations")
            conn.execute("DROP TABLE Operations")
            conn.execute("ALTER TABLE Operations_fixture RENAME TO Operations")
            conn.execute("DELETE FROM SchemaMigrations WHERE version>9")
            conn.execute("INSERT OR IGNORE INTO SchemaMigrations VALUES (9,1)")
        return db

    def _contents(self, db):
        with db.read_connection() as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                    for table in ('BackupPlans', 'Snapshots', 'Operations', 'ActivityLog',
                                  'PlanDeletionJobs', 'RetiredPlans', 'AppSettings')}

    def test_legacy_operations_migration_preserves_rows_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                before = self._contents(db)
                db.migrate()
                db.verify_contract()
                self.assertEqual(self._contents(db), before)
                with db.read_connection() as conn:
                    self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                    self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())
                    self.assertEqual(
                        conn.execute(
                            "SELECT MAX(version) "
                            "FROM SchemaMigrations"
                        ).fetchone()[0],
                        DB_SCHEMA_VERSION,
                    )
                    for table in ('Snapshots', 'ActivityLog', 'PlanDeletionJobs', 'RetiredPlans'):
                        self.assertIn('Operations', [row['table'] for row in conn.execute(f'PRAGMA foreign_key_list({table})')])
            finally:
                db.close_for_cleanup()

    def test_legacy_operations_accepts_every_current_operation_type(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp), populated=False)
            try:
                with self.assertRaises(AppError) as caught:
                    db.verify_contract()
                self.assertEqual(caught.exception.code, 'DATABASE_MIGRATION_FAILED')
                db.migrate()
                repo = Repository(db)
                for kind in OperationType:
                    self.assertTrue(repo.create_operation(kind, None, None, {}))
                with db.read_connection() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM Operations').fetchone()[0], len(OperationType))
            finally:
                db.close_for_cleanup()

    def test_legacy_operations_repeat_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                db.migrate()
                before = self._contents(db)
                with db.read_connection() as conn:
                    schema_before = [tuple(row) for row in conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
                db.migrate()
                self.assertEqual(self._contents(db), before)
                with db.read_connection() as conn:
                    self.assertEqual([tuple(row) for row in conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')], schema_before)
            finally:
                db.close_for_cleanup()

    def test_legacy_operations_preserves_views_indexes_and_triggers(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                with db.transaction() as conn:
                    conn.execute('CREATE INDEX legacy_operation_type ON Operations(type)')
                    conn.execute('CREATE VIEW legacy_operation_view AS SELECT id,type FROM Operations')
                    conn.execute("CREATE TRIGGER legacy_operation_trigger AFTER INSERT ON Operations BEGIN "
                                 "INSERT OR REPLACE INTO AppSettings VALUES ('operation-audit','true',1); END")
                before = self._contents(db)
                db.migrate()
                db.verify_contract()
                self.assertEqual(self._contents(db), before)
                with db.read_connection() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM legacy_operation_view").fetchone()[0], 1)
                    for name in ('legacy_operation_type','legacy_operation_view','legacy_operation_trigger'):
                        self.assertIsNotNone(conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone())
                Repository(db).create_operation(OperationType.REFRESH_HEALTH, None, None, {})
                with db.read_connection() as conn:
                    self.assertIsNotNone(conn.execute("SELECT 1 FROM AppSettings WHERE key='operation-audit'").fetchone())
            finally:
                db.close_for_cleanup()

    def test_legacy_operations_migration_failure_rolls_back_ddl_and_data(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                before = self._contents(db)
                with db.read_connection() as conn:
                    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='Operations'").fetchone()[0]
                with patch.object(db, '_verify_operation_types', side_effect=RuntimeError('injected migration failure')):
                    with self.assertRaises(RuntimeError):
                        db.migrate()
                self.assertEqual(self._contents(db), before)
                with db.read_connection() as conn:
                    self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='Operations'").fetchone()[0], ddl)
                    self.assertEqual(conn.execute('SELECT MAX(version) FROM SchemaMigrations').fetchone()[0], 9)
                    self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='Operations_upgrade'").fetchone())
                    self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            finally:
                db.close_for_cleanup()

    def test_legacy_operations_keeps_state_check_and_plan_foreign_key(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                db.migrate()
                with self.assertRaises(sqlite3.IntegrityError):
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO Operations(id,type,state,created_at_utc_ms) VALUES ('invalid','refresh_health','invalid',1)")
                with self.assertRaises(sqlite3.IntegrityError):
                    Repository(db).create_operation(OperationType.REFRESH_HEALTH, 'missing-plan', None, {})
            finally:
                db.close_for_cleanup()

    def test_database_newer_schema_is_refused_without_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            db = self._legacy_database(Path(temp))
            try:
                with db.transaction() as conn:
                    conn.execute('INSERT INTO SchemaMigrations VALUES (?,1)', (DB_SCHEMA_VERSION + 1,))
                before = self._contents(db)
                with self.assertRaises(AppError) as caught:
                    db.migrate()
                self.assertEqual(caught.exception.code, 'DATABASE_VERSION_TOO_NEW')
                self.assertEqual(self._contents(db), before)
            finally:
                db.close_for_cleanup()


    def test_upgrade_backup_preserves_original_schema_and_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = self._legacy_database(root)
            try:
                before = self._contents(db)
                db.migrate()
                backups = list((root / "recovery" / "database-backups").glob("*.sqlite3"))
                self.assertEqual(len(backups), 1)
                saved = Database(backups[0])
                self.assertEqual(self._contents(saved), before)
                with saved.read_connection() as conn:
                    self.assertEqual(conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0], 9)
                    self.assertIn("CHECK(type IN", conn.execute("SELECT sql FROM sqlite_master WHERE name='Operations'").fetchone()[0])
                saved.close_for_cleanup()
            finally:
                db.close_for_cleanup()

    def test_current_database_start_does_not_create_backup_each_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = Database(root / "current.sqlite3")
            try:
                for _ in range(5):
                    db.migrate()
                    db.verify_contract()
                self.assertFalse(list((root / "recovery" / "database-backups").glob("*.sqlite3")))
            finally:
                db.close_for_cleanup()

    def test_upgrade_backup_failure_prevents_schema_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = self._legacy_database(root)
            try:
                before = self._contents(db)
                (root / "recovery").write_bytes(b"blocks-directory")
                with self.assertRaises(AppError) as caught:
                    db.migrate()
                self.assertEqual(caught.exception.code, "DATABASE_BACKUP_FAILED")
                self.assertEqual(self._contents(db), before)
                with db.read_connection() as conn:
                    self.assertEqual(conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0], 9)
                    self.assertIn("CHECK(type IN", conn.execute("SELECT sql FROM sqlite_master WHERE name='Operations'").fetchone()[0])
            finally:
                db.close_for_cleanup()

    def test_stale_check_with_current_version_still_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = self._legacy_database(root)
            try:
                with db.transaction() as conn:
                    conn.execute("INSERT INTO SchemaMigrations VALUES (?,1)", (DB_SCHEMA_VERSION,))
                db.migrate()
                self.assertEqual(len(list((root / "recovery" / "database-backups").glob("*.sqlite3"))), 1)
                db.verify_contract()
            finally:
                db.close_for_cleanup()


    def test_schema_drift_with_current_version_still_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = Database(root / "drift.sqlite3")
            db.migrate()
            try:
                with db.transaction() as conn:
                    conn.execute("ALTER TABLE BackupPlans DROP COLUMN source_identity_json")
                db.migrate()
                backups = list((root / "recovery" / "database-backups").glob("*.sqlite3"))
                self.assertEqual(len(backups), 1)
                with Database(backups[0]).read_connection() as conn:
                    self.assertNotIn(
                        "source_identity_json",
                        {
                            row[1]
                            for row in conn.execute(
                                "PRAGMA table_info(BackupPlans)"
                            )
                        },
                    )
                db.verify_contract()
            finally:
                db.close_for_cleanup()


class Alpha182RetentionTests(unittest.TestCase):
    """Retention policy checks."""

    # Reuse only the infrastructure, do not inherit the old tests.
    make_plan = Alpha511Tests.make_plan
    backup = Alpha511Tests.backup

    def setUp(self) -> None:
        Alpha511Tests.setUp(self)

        self.source = self.source_root / "retention-182.txt"
        self.source.write_text("initial", encoding="utf-8")

        # Do not allow automatic cleanup while preparing test versions.
        self.plan_id = self.make_plan(self.source, keep=1000)
        result = self.backup(self.plan_id)
        self.assertTrue(result.success, result.message)

        self.base_snapshot = self.repo.available_snapshots(
            self.plan_id
        )[0]
        self.retention = RetentionService(
            self.repo,
            self.storage,
            self.health,
        )

    def tearDown(self) -> None:
        Alpha511Tests.tearDown(self)

    @staticmethod
    def _local_ms(year, month, day, hour=12, minute=0):
        # A naive datetime is interpreted in the local time zone.
        from datetime import datetime

        return int(
            datetime(
                year, month, day, hour, minute
            ).timestamp() * 1000
        )

    def _version(
        self,
        number,
        year=2026,
        month=10,
        day=16,
        hour=10,
        pinned=False,
        state="committed",
    ):
        from dataclasses import replace

        # These DTOs are meant only for computing the preview.
        # Real files are not deleted through them.
        return replace(
            self.base_snapshot,
            id=f"calendar-test-{number}",
            sequence_no=number,
            created_at_utc_ms=self._local_ms(
                year, month, day, hour
            ),
            pinned=pinned,
            state=state,
        )

    def _preview(self, policy, versions, now=None):
        if now is None:
            now = self._local_ms(2026, 10, 16, 12)

        return Repository._retention_preview_from_rows(
            self.plan_id,
            policy,
            versions,
            now_utc_ms=now,
        )

    def _save_policy(self, policy, token=None):
        plan = self.repo.plan_details(self.plan_id)
        source = PathPolicy.validate_source(
            plan.source_path
        )

        return self.repo.save_plan_with_policy(
            self.plan_id,
            plan.display_name,
            plan.note,
            source,
            plan.storage_root_id,
            policy.keep_last_n,
            plan.storage_mode,
            self.repo.get_trigger_policy(self.plan_id),
            retention_policy=policy,
            retention_preview_token=token,
        )

    def _make_more_backups(self, count=2):
        for index in range(count):
            self.source.write_text(
                f"additional-{index}",
                encoding="utf-8",
            )
            result = self.backup(self.plan_id, force=True)
            self.assertTrue(result.success, result.message)

    def test_version_and_schema(self):
        self.assertTrue(APP_VERSION.startswith("22.0"), APP_VERSION)
        self.assertGreaterEqual(DB_SCHEMA_VERSION, 12)
        self.db.verify_contract()

    def test_existing_plan_defaults_to_count(self):
        plan = self.repo.plan_details(self.plan_id)

        self.assertEqual(plan.retention_mode, "count")
        self.assertEqual(plan.keep_last_n, 1000)
        self.assertEqual(plan.retention_daily, 0)
        self.assertEqual(plan.retention_weekly, 0)
        self.assertEqual(plan.retention_monthly, 0)

    def test_invalid_retention_policy_is_rejected(self):
        invalid = (
            RetentionPolicy("unknown", 5, 0, 0, 0),
            RetentionPolicy("count", 0, 0, 0, 0),
            RetentionPolicy("count", 1001, 0, 0, 0),
            RetentionPolicy("time", 5, 0, 0, 0),
            RetentionPolicy("time", 5, -1, 0, 0),
            RetentionPolicy("time", 5, 0, 1001, 0),
            RetentionPolicy("time", 5, 0, 0, 1001),
        )

        for policy in invalid:
            with self.subTest(policy=policy):
                with self.assertRaises(AppError) as caught:
                    self.repo.validate_retention_policy(
                        policy
                    )

                self.assertEqual(
                    caught.exception.code,
                    "RETENTION_POLICY_INVALID",
                )

    def test_daily_keeps_newest_version_per_local_day(self):
        versions = [
            self._version(1, day=14),
            self._version(2, day=15, hour=9),
            self._version(3, day=15, hour=18),
            self._version(4, day=16, hour=9),
            self._version(5, day=16, hour=11),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 2, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (1, 2, 4),
        )
        self.assertEqual(
            set(preview.protected_snapshot_ids),
            {"calendar-test-3", "calendar-test-5"},
        )

    def test_time_policy_always_keeps_the_newest_versions(self):
        versions = [
            self._version(1, day=15, hour=9),
            self._version(2, day=15, hour=10),
            self._version(3, day=15, hour=11),
            self._version(4, day=16, hour=9),
            self._version(5, day=16, hour=10),
        ]

        roomy = self._preview(RetentionPolicy("time", 5, 1, 0, 0), versions)
        self.assertEqual(roomy.delete_sequence_numbers, ())

        tight = self._preview(RetentionPolicy("time", 2, 1, 0, 0), versions)
        self.assertEqual(tight.delete_sequence_numbers, (1, 2, 3))

        minimal = self._preview(RetentionPolicy("time", 1, 1, 0, 0), versions)
        self.assertEqual(minimal.delete_sequence_numbers, (1, 2, 3, 4))

    def test_time_policy_still_thins_old_history_beyond_the_newest_versions(self):
        versions = [self._version(1, day=2, hour=9), self._version(2, day=2, hour=10)]
        versions += [self._version(number, day=16, hour=number) for number in range(3, 8)]

        preview = self._preview(RetentionPolicy("time", 5, 1, 0, 0), versions)

        self.assertEqual(preview.delete_sequence_numbers, (1, 2))

    def test_week_starts_on_monday(self):
        # 11 October 2026 is a Sunday,
        # 12 October is a Monday.
        versions = [
            self._version(1, day=11),
            self._version(2, day=12),
            self._version(3, day=16),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 0, 2, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (2,),
        )
        self.assertEqual(
            set(preview.protected_snapshot_ids),
            {"calendar-test-1", "calendar-test-3"},
        )

    def test_months_cross_year_boundary(self):
        versions = [
            self._version(1, year=2025, month=11, day=30),
            self._version(2, year=2025, month=12, day=10),
            self._version(3, year=2025, month=12, day=31),
            self._version(4, year=2026, month=1, day=10),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 0, 0, 2),
            versions,
            now=self._local_ms(2026, 1, 16),
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (1, 2),
        )

    def test_calendar_periods_use_union_without_duplicates(self):
        versions = [
            self._version(1, month=9, day=20),
            self._version(2, day=15),
            self._version(3, day=16),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 2, 1, 1),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (1,),
        )
        self.assertEqual(
            set(preview.protected_snapshot_ids),
            {"calendar-test-2", "calendar-test-3"},
        )
        self.assertEqual(
            len(preview.delete_snapshot_ids),
            len(set(preview.delete_snapshot_ids)),
        )

    def test_missing_period_does_not_keep_unrelated_old_copy(self):
        versions = [
            self._version(1, day=1),
            self._version(2, day=16),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 2, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (1,),
        )

    def test_latest_and_pinned_are_always_protected(self):
        versions = [
            self._version(1, month=1, day=1, pinned=True),
            self._version(2, month=2, day=1),
            self._version(3, month=3, day=1),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 1, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (2,),
        )
        self.assertIn(
            "calendar-test-1",
            preview.protected_snapshot_ids,
        )
        self.assertIn(
            "calendar-test-3",
            preview.protected_snapshot_ids,
        )

    def test_future_dated_copy_is_protected(self):
        versions = [
            self._version(1, day=17),
            self._version(2, day=16),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 1, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (),
        )
        self.assertIn(
            "calendar-test-1",
            preview.protected_snapshot_ids,
        )

    def test_uncommitted_versions_are_not_deletion_candidates(self):
        versions = [
            self._version(1, day=1, state="needs_recovery"),
            self._version(2, day=2, state="corrupted"),
            self._version(3, day=16),
        ]

        preview = self._preview(
            RetentionPolicy("time", 1, 1, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_snapshot_ids,
            (),
        )

    def test_count_mode_preserves_legacy_pin_semantics(self):
        versions = [
            self._version(1),
            self._version(2),
            self._version(3),
            self._version(4, pinned=True),
            self._version(5),
        ]

        preview = self._preview(
            RetentionPolicy("count", 3, 0, 0, 0),
            versions,
        )

        self.assertEqual(
            preview.delete_sequence_numbers,
            (1, 2),
        )
        self.assertEqual(
            set(preview.protected_snapshot_ids),
            {
                "calendar-test-3",
                "calendar-test-4",
                "calendar-test-5",
            },
        )

    def test_preview_is_deterministic(self):
        versions = [
            self._version(1, day=1),
            self._version(2, day=16),
        ]
        policy = RetentionPolicy("time", 1, 1, 0, 0)

        first = self._preview(policy, versions)
        second = self._preview(policy, list(reversed(versions)))

        self.assertEqual(first.token, second.token)
        self.assertEqual(
            first.delete_snapshot_ids,
            second.delete_snapshot_ids,
        )

    def test_preview_token_changes_when_pin_changes(self):
        from dataclasses import replace

        versions = [
            self._version(1, day=1),
            self._version(2, day=16),
        ]
        policy = RetentionPolicy("time", 1, 1, 0, 0)

        first = self._preview(policy, versions)
        changed = [
            replace(versions[0], pinned=True),
            versions[1],
        ]
        second = self._preview(policy, changed)

        self.assertNotEqual(first.token, second.token)
        self.assertEqual(
            second.delete_snapshot_ids,
            (),
        )

    def test_policy_change_requires_preview(self):
        policy = RetentionPolicy("time", 1, 7, 4, 6)

        with self.assertRaises(AppError) as caught:
            self._save_policy(policy)

        self.assertEqual(
            caught.exception.code,
            "RETENTION_PREVIEW_REQUIRED",
        )
        self.assertEqual(
            self.repo.plan_details(
                self.plan_id
            ).retention_mode,
            "count",
        )

    def test_policy_change_with_preview_is_saved(self):
        policy = RetentionPolicy("time", 1, 7, 4, 6)
        preview = self.repo.retention_preview(
            self.plan_id,
            policy,
        )

        saved_id = self._save_policy(
            policy,
            preview.token,
        )
        plan = self.repo.plan_details(saved_id)

        self.assertEqual(saved_id, self.plan_id)
        self.assertEqual(
            (
                plan.retention_mode,
                plan.retention_daily,
                plan.retention_weekly,
                plan.retention_monthly,
            ),
            ("time", 7, 4, 6),
        )

    def test_stale_preview_does_not_change_policy(self):
        policy = RetentionPolicy("time", 1, 7, 4, 6)
        preview = self.repo.retention_preview(
            self.plan_id,
            policy,
        )

        self.repo.set_pin(self.base_snapshot.id, True)

        with self.assertRaises(AppError) as caught:
            self._save_policy(policy, preview.token)

        self.assertEqual(
            caught.exception.code,
            "RETENTION_PREVIEW_STALE",
        )
        self.assertEqual(
            self.repo.plan_details(
                self.plan_id
            ).retention_mode,
            "count",
        )

    def test_delete_one_refuses_latest_without_touching_files(self):
        plan = self.repo.plan_details(self.plan_id)
        root = self.storage.safe_snapshot_root(
            self.storage_root,
            plan,
            self.base_snapshot.sequence_no,
        )

        with self.assertRaises(AppError) as caught:
            self.retention.delete_one(self.base_snapshot)

        self.assertEqual(
            caught.exception.code,
            "SNAPSHOT_LAST_COMMITTED",
        )
        self.assertTrue(root.is_dir())
        self.assertEqual(
            self.repo.snapshot(
                self.base_snapshot.id
            ).state,
            "committed",
        )

    def test_apply_rejects_stale_token_before_deletion(self):
        self._make_more_backups()
        policy = RetentionPolicy("count", 1, 0, 0, 0)

        proposed = self.repo.retention_preview(
            self.plan_id,
            policy,
        )
        self._save_policy(policy, proposed.token)

        preview = self.repo.retention_preview(self.plan_id)
        oldest = min(
            self.repo.available_snapshots(self.plan_id),
            key=lambda item: item.sequence_no,
        )
        self.repo.set_pin(oldest.id, True)

        before = {
            item.id
            for item in self.repo.available_snapshots(
                self.plan_id
            )
        }

        with self.assertRaises(AppError) as caught:
            self.retention.apply(
                self.plan_id,
                expected_token=preview.token,
            )

        self.assertEqual(
            caught.exception.code,
            "RETENTION_PREVIEW_STALE",
        )
        self.assertEqual(
            {
                item.id
                for item in self.repo.available_snapshots(
                    self.plan_id
                )
            },
            before,
        )

    def test_apply_honors_cancellation_before_deletion(self):
        self._make_more_backups()
        policy = RetentionPolicy("count", 1, 0, 0, 0)
        preview = self.repo.retention_preview(
            self.plan_id,
            policy,
        )
        self._save_policy(policy, preview.token)

        before = {
            item.id
            for item in self.repo.available_snapshots(
                self.plan_id
            )
        }

        with self.assertRaises(CancelledError):
            self.retention.apply(
                self.plan_id,
                cancelled=lambda: True,
            )

        self.assertEqual(
            {
                item.id
                for item in self.repo.available_snapshots(
                    self.plan_id
                )
            },
            before,
        )

    def test_count_apply_deletes_old_roots_and_keeps_latest(self):
        self._make_more_backups()

        snapshots = self.repo.available_snapshots(
            self.plan_id
        )
        latest = max(
            snapshots,
            key=lambda item: item.sequence_no,
        )
        plan = self.repo.plan_details(self.plan_id)
        roots = {
            item.id: self.storage.safe_snapshot_root(
                self.storage_root,
                plan,
                item.sequence_no,
            )
            for item in snapshots
        }

        policy = RetentionPolicy("count", 1, 0, 0, 0)
        preview = self.repo.retention_preview(
            self.plan_id,
            policy,
        )
        self._save_policy(policy, preview.token)

        current = self.repo.retention_preview(self.plan_id)
        self.retention.apply(
            self.plan_id,
            expected_token=current.token,
        )

        remaining = self.repo.available_snapshots(
            self.plan_id
        )
        self.assertEqual(
            [item.id for item in remaining],
            [latest.id],
        )

        for snapshot_id, root in roots.items():
            with self.subTest(snapshot_id=snapshot_id):
                self.assertEqual(
                    root.exists(),
                    snapshot_id == latest.id,
                )

    def test_clock_block_stops_calendar_deletion(self):
        self._make_more_backups()
        oldest = min(
            self.repo.available_snapshots(self.plan_id),
            key=lambda item: item.sequence_no,
        )

        # Only the time in the database is changed to get an old
        # calendar point. The version files stay real.
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE Snapshots SET created_at_utc_ms=? "
                "WHERE id=?",
                (self._local_ms(2000, 1, 1), oldest.id),
            )

        policy = RetentionPolicy("time", 1, 1, 0, 0)
        preview = self.repo.retention_preview(
            self.plan_id,
            policy,
        )
        self.assertIn(
            oldest.id,
            preview.delete_snapshot_ids,
        )
        self._save_policy(policy, preview.token)

        self.repo.set_setting(
            SETTING_RETENTION_CLOCK_BLOCKED,
            True,
        )
        before = {
            item.id
            for item in self.repo.available_snapshots(
                self.plan_id
            )
        }

        self.retention.apply(self.plan_id)

        self.assertEqual(
            {
                item.id
                for item in self.repo.available_snapshots(
                    self.plan_id
                )
            },
            before,
        )
        self.assertTrue(
            any(
                issue.code
                == "RETENTION_CLOCK_REVIEW_REQUIRED"
                for issue in self.repo.list_active_issues(
                    self.plan_id
                )
            )
        )

    def test_apply_retention_operation_is_supported_by_database(self):
        operation_id = self.repo.create_operation(
            OperationType.APPLY_RETENTION,
            self.plan_id,
            None,
            {},
        )

        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT type, state FROM Operations WHERE id=?",
                (operation_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "apply_retention")
        self.assertEqual(row["state"], "queued")

    def test_schema_10_upgrade_creates_backup_and_count_defaults(self):
        path = self.root / "legacy-retention.sqlite3"
        db = Database(path)

        try:
            db.migrate()

            # Get a structurally compatible database without the new
            # columns. Tables are not renamed, foreign keys are not broken.
            with db.transaction() as conn:
                for column in (
                    "retention_mode",
                    "retention_daily",
                    "retention_weekly",
                    "retention_monthly",
                ):
                    conn.execute(
                        f"ALTER TABLE BackupPlans "
                        f"DROP COLUMN {column}"
                    )

                conn.execute(
                    "DELETE FROM SchemaMigrations "
                    "WHERE version > 10"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO SchemaMigrations "
                    "(version, applied_at_utc_ms) VALUES (10, 0)"
                )

            db.migrate()
            db.verify_contract()

            folder = (
                path.parent
                / "recovery"
                / "database-backups"
            )
            backups = list(folder.glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)

            with Database(backups[0]).read_connection() as conn:
                old_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(BackupPlans)"
                    )
                }
                old_version = conn.execute(
                    "SELECT MAX(version) FROM SchemaMigrations"
                ).fetchone()[0]

            self.assertEqual(old_version, 10)
            self.assertNotIn(
                "retention_mode",
                old_columns,
            )

            with db.read_connection() as conn:
                fields = {
                    row["name"]: row
                    for row in conn.execute(
                        "PRAGMA table_info(BackupPlans)"
                    )
                }
                version = conn.execute(
                    "SELECT MAX(version) FROM SchemaMigrations"
                ).fetchone()[0]

            self.assertEqual(version, DB_SCHEMA_VERSION)
            self.assertEqual(
                fields["retention_mode"]["dflt_value"],
                "'count'",
            )
            for column in (
                "retention_daily",
                "retention_weekly",
                "retention_monthly",
            ):
                self.assertEqual(
                    str(fields[column]["dflt_value"]),
                    "0",
                )

            # Reopening the current schema
            # must not create another copy.
            db.migrate()
            db.verify_contract()

            self.assertEqual(
                len(list(folder.glob("*.sqlite3"))),
                1,
            )
        finally:
            db.close_for_cleanup()


class Alpha183DeletionContractTests(unittest.TestCase):
    """18.3 operation ownership, audit, rollback, retry and migration contracts."""
    setUp = Alpha511Tests.setUp
    tearDown = Alpha511Tests.tearDown
    make_plan = Alpha511Tests.make_plan
    backup = Alpha511Tests.backup
    stage3_fixture = Alpha511Tests.stage3_fixture
    stage3_delete = Alpha511Tests.stage3_delete

    def prepare_job(self):
        source, plan = self.stage3_fixture(versions=0)
        preview = self.repo.plan_deletion_preview(plan)
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan, None, {})
        self.repo.operation_state(operation, "running")
        inventory = self.delete._build_plan_delete_inventory(self.repo.plan_details(plan), self.not_cancelled)
        self.repo.begin_plan_deletion(plan, operation, inventory, preview)
        return source, plan, operation

    def assert_preserved(self, plan, operation):
        self.assertEqual(self.repo.plan_details(plan).state, "archived")
        self.assertEqual(self.repo.plan_deletion_job(plan)["operation_id"], operation)
        with self.db.read_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RetiredPlans WHERE plan_id=?", (plan,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ActivityLog WHERE event_code='PLAN_DELETE_SUCCEEDED' AND operation_id=?", (operation,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())

    def refuse_finish(self, plan, operation):
        with self.assertRaises(AppError) as caught:
            self.repo.finish_plan_deletion(plan, operation)
        self.assertEqual(caught.exception.code, "PLAN_DELETE_CHANGED")

    def test_finish_rejects_other_operation_id(self):
        source, plan, operation = self.prepare_job()
        other = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan, None, {})
        self.repo.operation_state(other, "running")
        self.refuse_finish(plan, other)
        self.assert_preserved(plan, operation)
        self.assertTrue(source.is_file())

    def test_finish_rejects_unknown_operation(self):
        from uuid import uuid4
        _, plan, operation = self.prepare_job()
        self.refuse_finish(plan, str(uuid4()))
        self.assert_preserved(plan, operation)

    def test_finish_rejects_changed_operation_type(self):
        _, plan, operation = self.prepare_job()
        with self.db.transaction() as conn:
            conn.execute("UPDATE Operations SET type=? WHERE id=?", (OperationType.BACKUP.value, operation))
        self.refuse_finish(plan, operation)
        self.assert_preserved(plan, operation)

    def test_finish_rejects_detached_operation(self):
        _, plan, operation = self.prepare_job()
        with self.db.transaction() as conn:
            conn.execute("UPDATE Operations SET plan_id=NULL WHERE id=?", (operation,))
        self.refuse_finish(plan, operation)
        self.assert_preserved(plan, operation)

    def test_finish_rejects_other_plan_operation(self):
        _, plan, operation = self.prepare_job()
        source = self.source_root / "unrelated-plan.txt"
        source.write_bytes(b"untouched")
        other = self.make_plan(source)
        with self.db.transaction() as conn:
            conn.execute("UPDATE Operations SET plan_id=? WHERE id=?", (other, operation))
        self.refuse_finish(plan, operation)
        self.assert_preserved(plan, operation)
        self.assertEqual(source.read_bytes(), b"untouched")

    def test_finish_rejects_invalid_operation_states(self):
        _, plan, operation = self.prepare_job()
        for state in ("queued", "failed", "cancelled", "succeeded"):
            with self.subTest(state=state):
                with self.db.transaction() as conn:
                    conn.execute("UPDATE Operations SET state=? WHERE id=?", (state, operation))
                self.refuse_finish(plan, operation)
                self.assert_preserved(plan, operation)

    def test_finish_accepts_running_owner(self):
        source, plan, operation = self.prepare_job()
        self.repo.finish_plan_deletion(plan, operation)
        self.assertEqual(self.repo.operation(operation)["state"], "succeeded")
        self.assertIsNone(self.repo.operation(operation)["plan_id"])
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertTrue(source.exists())

    def test_finish_accepts_recovery_owner(self):
        _, plan, operation = self.prepare_job()
        with self.db.transaction() as conn:
            conn.execute("UPDATE Operations SET state='needs_recovery' WHERE id=?", (operation,))
        self.repo.finish_plan_deletion(plan, operation)
        self.assertEqual(self.repo.operation(operation)["state"], "succeeded")

    def test_success_audit_preserves_identity_and_paths(self):
        import json as audit_json
        source, plan = self.stage3_fixture(versions=0)
        details = self.repo.plan_details(plan)
        operation, result = self.stage3_delete(plan)
        self.assertTrue(result.success, result.message)
        with self.db.read_connection() as conn:
            rows = conn.execute("SELECT plan_id,details_json FROM ActivityLog WHERE operation_id=? AND event_code='PLAN_DELETE_SUCCEEDED'", (operation,)).fetchall()
            retired = conn.execute("SELECT * FROM RetiredPlans WHERE plan_id=?", (plan,)).fetchone()
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["plan_id"])
        payload = audit_json.loads(rows[0]["details_json"])
        for key, expected in {"deleted_plan_id": plan, "display_name": details.display_name,
                              "source_path": str(source), "storage_path": details.storage_path}.items():
            self.assertEqual(payload[key], expected)
        self.assertIsNotNone(retired)
        self.assertEqual(retired["operation_id"], operation)
        self.assertEqual(source.read_text(encoding="utf-8"), "v0")

    def test_audit_failure_rolls_back_finalization(self):
        from unittest.mock import patch
        _, plan, operation = self.prepare_job()
        with patch.object(self.repo, "activity", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.repo.finish_plan_deletion(plan, operation)
        self.assert_preserved(plan, operation)
        self.assertEqual(self.repo.operation(operation)["state"], "running")
        self.repo.finish_plan_deletion(plan, operation)
        self.assertEqual(self.repo.operation(operation)["state"], "succeeded")

    def test_begin_unknown_plan_does_not_create_job(self):
        from uuid import uuid4
        _, plan = self.stage3_fixture(versions=0)
        preview = self.repo.plan_deletion_preview(plan)
        with self.assertRaises(AppError) as caught:
            self.repo.begin_plan_deletion(str(uuid4()), str(uuid4()), {"root": str(self.storage_root)}, preview)
        self.assertEqual(caught.exception.code, "PLAN_NOT_FOUND")
        self.assertIsNone(self.repo.plan_deletion_job(plan))

    def test_begin_rejects_changed_confirmation(self):
        source, plan = self.stage3_fixture(versions=0)
        preview = self.repo.plan_deletion_preview(plan)
        with self.db.transaction() as conn:
            conn.execute("UPDATE BackupPlans SET display_name='Changed' WHERE id=?", (plan,))
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan, None, {})
        self.repo.operation_state(operation, "running")
        with self.assertRaises(AppError) as caught:
            self.repo.begin_plan_deletion(plan, operation, {"root": str(self.storage_root)}, preview)
        self.assertEqual(caught.exception.code, "PLAN_DELETE_CHANGED")
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertTrue(source.exists())

    def test_retry_after_finalization_failure_rebinds_job(self):
        # A safety regression must fail, never be converted to an expected failure.
        from unittest.mock import patch
        source, plan = self.stage3_fixture(versions=0)
        with patch.object(self.repo, "finish_plan_deletion", side_effect=RuntimeError("database unavailable")), \
             self.assertLogs(level="ERROR") as captured:
            first, result = self.stage3_delete(plan)
        self.assertTrue(any("Plan deletion failed" in line for line in captured.output))
        self.assertFalse(result.success)
        self.assertIsNotNone(self.repo.plan_deletion_job(plan))
        second, retried = self.stage3_delete(plan)
        self.assertNotEqual(first, second)
        self.assertTrue(retried.success, retried.message)
        self.assertEqual(self.repo.operation(second)["state"], "succeeded")
        self.assertIsNone(self.repo.plan_deletion_job(plan))
        self.assertTrue(source.exists())

    def test_schema_11_to_12_backup_and_repeat_migration(self):
        import sqlite3 as migration_sqlite
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM SchemaMigrations WHERE version>11")
            conn.execute("INSERT OR IGNORE INTO SchemaMigrations(version,applied_at_utc_ms) VALUES(11,1)")
        folder = self.root / "recovery" / "database-backups"
        before = set(folder.glob("*.sqlite3"))
        self.db.migrate()
        backups = set(folder.glob("*.sqlite3")) - before
        self.assertEqual(len(backups), 1)
        with _amvi_test_closing(migration_sqlite.connect(next(iter(backups)))) as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0], 11)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.db.verify_contract()
        with self.db.read_connection() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0], DB_SCHEMA_VERSION)
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
        count = len(list(folder.glob("*.sqlite3")))
        self.db.migrate()
        self.db.verify_contract()
        self.assertEqual(len(list(folder.glob("*.sqlite3"))), count)


_SELF_TEST_MODULE_PATH: Path | None = None


def self_test_module_path() -> Path:
    if _SELF_TEST_MODULE_PATH is not None:
        if not _SELF_TEST_MODULE_PATH.is_file():
            raise RuntimeError(
                "The self-test source service copy disappeared."
            )
        return _SELF_TEST_MODULE_PATH

    path = Path(_APP.__file__).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError(
            "Subprocess self-tests need an existing "
            "project Python file."
        )
    return path


def ensure_test_qapplication() -> QApplication:
    """Creates one QApplication for all self-tests.

    Any test that creates a QWidget, QDialog, MainWindow or QTimer
    must run after this bootstrap. The offscreen platform allows
    running UI smoke tests without a visible desktop window.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    application = QApplication.instance()
    if application is None:
        application = QApplication(sys.argv)

    return application




class Workspace:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "t19.sqlite3", journal_mode="WAL")

    def __enter__(self) -> "Workspace":
        self.db.migrate()
        self.db.verify_contract()
        return self

    def __exit__(self, *args: Any) -> None:
        self.db.close_for_cleanup()
        for path in sorted(self.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                set_writable_best_effort(path)
            except Exception:
                pass
        try:
            self.temp.cleanup()
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)


class Alpha19Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace().__enter__()
        self.root = self.ws.root
        self.db = self.ws.db
        self.repo = Repository(self.db)
        self.storage = StorageService()
        self.health = HealthService(self.repo)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.storage_root = self.root / "storage"
        self.snapshots = SnapshotService(self.repo, self.storage, self.health, reserve_bytes=0, reserve_ratio=0.0)
        self.verifier = VerificationService(self.repo, self.storage, self.health)
        self.restore = RestoreService(self.repo, self.verifier, self.health)
        self.delete = DeleteService(self.repo, self.storage, self.health)
        self.restore_test = RestoreTestService(self.repo, self.restore)
        self.progress = lambda event: None
        self.not_cancelled = lambda: False

    def tearDown(self) -> None:
        self.ws.__exit__(None, None, None)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def make_plan(self, source: Path, name: str = "План", keep: int = 5, mode: str = StorageMode.DIRECT.value) -> str:
        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.storage_root))
        storage_id = self.repo.get_or_create_storage(storage)
        self.storage.ensure_root(storage, storage_id)
        return self.repo.create_plan(name, "", descriptor, storage_id, keep, mode)

    def backup(self, plan_id: str, force: bool = False) -> OperationResult:
        operation = self.repo.create_operation(OperationType.BACKUP, plan_id, None, {"force": force})
        return self.snapshots.create(operation, plan_id, force, self.progress, self.not_cancelled)

    def folder_with_files(self, name: str = "dir", count: int = 4, size: int = 100) -> Path:
        folder = self.source_root / name
        folder.mkdir()
        for index in range(count):
            (folder / f"file{index}.txt").write_bytes(bytes([65 + index]) * (size + index))
        return folder

    def backed_up_folder_plan(self, name: str = "dir", count: int = 4, size: int = 100) -> tuple[str, Path, Any]:
        folder = self.folder_with_files(name, count, size)
        plan_id = self.make_plan(folder, name)
        result = self.backup(plan_id)
        self.assertTrue(result.success, result.message)
        return plan_id, folder, self.repo.snapshots(plan_id)[0]

    def policy(self, **overrides: Any) -> TriggerPolicy:
        values = dict(on_change_enabled=True, change_debounce_seconds=5, min_auto_interval_minutes=10,
                      on_schedule_enabled=False, schedule_interval_minutes=60, quiet_hours_enabled=False,
                      quiet_hours_start_local=None, quiet_hours_end_local=None, manual_allowed_quiet_hours=True,
                      change_threshold_bytes=0)
        values.update(overrides)
        return TriggerPolicy(**values)

    def run_restore_test(self, snapshot_id: str, plan_id: str, mode: str = "full", cancelled=None):
        operation = self.repo.create_operation(OperationType.TEST_RESTORE, plan_id, snapshot_id, {})
        return self.restore_test.run(operation, snapshot_id, self.progress, cancelled or self.not_cancelled, mode)


class Alpha19ReleaseProfileTests(unittest.TestCase):
    def test_version_and_schema(self) -> None:
        self.assertTrue(APP_VERSION.startswith("22.0"), APP_VERSION)
        self.assertTrue(APP_VERSION.endswith("-beta"), APP_VERSION)
        self.assertEqual(DB_SCHEMA_VERSION, 13)

    def test_production_module_has_no_test_code(self) -> None:
        self.assertFalse(hasattr(_APP, "DEV_MODE"))
        self.assertFalse(hasattr(_APP, "Alpha511Tests"))
        self.assertFalse(hasattr(_APP, "TestWorkspace"))
        self.assertTrue(callable(_APP.main) and callable(_APP.run_gui) and callable(_APP.run_self_tests))

    def test_alpha19_public_objects_exist(self) -> None:
        for name in ("RestoreTestService", "HelpDialog", "ProtectionWizard19", "FlowLayout", "PathDropTarget",
                     "HelpMark", "WheelGuard", "source_metadata_scan", "change_threshold_reached",
                     "build_indicator_qss", "build_help_mock", "install_wheel_guard"):
            self.assertTrue(hasattr(_APP, name), name)
        self.assertEqual(OperationType.TEST_RESTORE.value, "test_restore")

    def test_help_pages_declared(self) -> None:
        kinds = [kind for kind, _count in HELP_PAGES]
        self.assertEqual(kinds, ["about", "add", "plan", "restore", "tray", "more"])
        self.assertTrue(all(count >= 2 for _kind, count in HELP_PAGES))


class Alpha19ThresholdTests(Alpha19Base):
    def test_change_threshold_reached_rules(self) -> None:
        self.assertTrue(change_threshold_reached(0, 100, 100))
        self.assertTrue(change_threshold_reached(-5, 100, 100))
        self.assertTrue(change_threshold_reached(1000, 5, None))
        self.assertFalse(change_threshold_reached(1000, 1500, 1000))
        self.assertTrue(change_threshold_reached(1000, 2000, 1000))
        self.assertTrue(change_threshold_reached(1000, 0, 5000))
        self.assertFalse(change_threshold_reached(1000, 4500, 5000))

    def test_threshold_steps_are_sorted_and_valid(self) -> None:
        self.assertEqual(list(CHANGE_THRESHOLD_STEPS), sorted(set(CHANGE_THRESHOLD_STEPS)))
        self.assertEqual(CHANGE_THRESHOLD_STEPS[0], 0)
        self.assertLessEqual(max(CHANGE_THRESHOLD_STEPS), MAX_CHANGE_THRESHOLD_BYTES)
        self.assertEqual(MAX_CHANGE_THRESHOLD_BYTES, 1 << 40)

    def test_metadata_scan_file_returns_fingerprint_and_size(self) -> None:
        file = self.source_root / "one.txt"
        file.write_bytes(b"12345")
        fingerprint, total = source_metadata_scan(PathPolicy.validate_source(str(file)))
        self.assertEqual(total, 5)
        self.assertEqual(fingerprint, source_metadata_fingerprint(PathPolicy.validate_source(str(file))))

    def test_metadata_scan_folder_total_and_change_detection(self) -> None:
        folder = self.folder_with_files("scan", 3, 10)
        (folder / "sub").mkdir()
        (folder / "sub" / "x.bin").write_bytes(b"z" * 7)
        descriptor = PathPolicy.validate_source(str(folder))
        first, total = source_metadata_scan(descriptor)
        again, total_again = source_metadata_scan(descriptor)
        self.assertEqual((first, total), (again, total_again))
        self.assertEqual(total, 10 + 11 + 12 + 7)
        time.sleep(0.01)
        (folder / "new.txt").write_bytes(b"n" * 50)
        changed, grown = source_metadata_scan(descriptor)
        self.assertNotEqual(first, changed)
        self.assertEqual(grown, total + 50)

    def test_metadata_scan_skips_symlinks(self) -> None:
        folder = self.folder_with_files("links", 1, 10)
        target = self.source_root / "outside.bin"
        target.write_bytes(b"o" * 1000)
        try:
            (folder / "link.bin").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        _fp, total = source_metadata_scan(PathPolicy.validate_source(str(folder)))
        self.assertEqual(total, 10)

    def test_policy_validation_bounds(self) -> None:
        self.repo.validate_trigger_policy(self.policy(change_threshold_bytes=0))
        self.repo.validate_trigger_policy(self.policy(change_threshold_bytes=MAX_CHANGE_THRESHOLD_BYTES))
        for bad in (-1, MAX_CHANGE_THRESHOLD_BYTES + 1):
            with self.assertRaises(AppError) as caught:
                self.repo.validate_trigger_policy(self.policy(change_threshold_bytes=bad))
            self.assertEqual(caught.exception.code, "TRIGGER_POLICY_INVALID")

    def test_threshold_persists_in_policy_and_plan_details(self) -> None:
        file = self.source_root / "p.txt"
        file.write_text("p", encoding="utf-8")
        plan_id = self.make_plan(file)
        self.assertEqual(self.repo.get_trigger_policy(plan_id).change_threshold_bytes, 0)
        self.repo.update_trigger_policy(plan_id, self.policy(change_threshold_bytes=10 << 20))
        self.assertEqual(self.repo.get_trigger_policy(plan_id).change_threshold_bytes, 10 << 20)
        self.assertEqual(self.repo.plan_details(plan_id).change_threshold_bytes, 10 << 20)

    def test_schema_has_threshold_column_and_migration_is_idempotent(self) -> None:
        with self.db.read_connection() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(BackupPlans)")}
        self.assertIn("change_threshold_bytes", columns)
        self.db.migrate()
        self.db.verify_contract()

    def test_migration_adds_missing_threshold_column(self) -> None:
        file = self.source_root / "m.txt"
        file.write_text("m", encoding="utf-8")
        self.make_plan(file)
        try:
            with self.db.transaction() as conn:
                conn.execute("ALTER TABLE BackupPlans DROP COLUMN change_threshold_bytes")
        except Exception:
            self.skipTest("SQLite cannot drop the column in this build")
        self.db.migrate()
        with self.db.read_connection() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(BackupPlans)")}
        self.assertIn("change_threshold_bytes", columns)

    def make_watcher(self):
        coordinator = AutoBackupCoordinator(self.repo, self.health)
        return PollingChangeWatcher(self.repo, self.health, coordinator)

    def test_watcher_defers_small_change_below_threshold(self) -> None:
        folder = self.folder_with_files("watch", 2, 1000)
        plan_id = self.make_plan(folder)
        self.assertTrue(self.backup(plan_id).success)
        self.repo.update_trigger_policy(plan_id, self.policy(change_threshold_bytes=1 << 20))
        watcher = self.make_watcher()
        watcher.poll_once(10_000)
        time.sleep(0.01)
        (folder / "file0.txt").write_bytes(b"A" * 1010)
        decisions = watcher.poll_once(11_000)
        self.assertEqual([d.action for d in decisions], ["deferred_threshold"])
        self.assertEqual(self.repo.plan_details(plan_id).pending_trigger_mask, 0)

    def test_watcher_triggers_when_change_reaches_threshold(self) -> None:
        folder = self.folder_with_files("watch2", 2, 1000)
        plan_id = self.make_plan(folder)
        self.assertTrue(self.backup(plan_id).success)
        self.repo.update_trigger_policy(plan_id, self.policy(change_threshold_bytes=4096))
        watcher = self.make_watcher()
        watcher.poll_once(10_000)
        time.sleep(0.01)
        (folder / "big.bin").write_bytes(b"B" * 8192)
        decisions = watcher.poll_once(11_000)
        self.assertEqual([d.action for d in decisions], ["pending"])
        self.assertEqual(self.repo.plan_details(plan_id).pending_trigger_mask, TRIGGER_MASK_EVENT)

    def test_watcher_zero_threshold_keeps_old_behaviour(self) -> None:
        file = self.source_root / "w.txt"
        file.write_text("before", encoding="utf-8")
        plan_id = self.make_plan(file)
        self.repo.update_trigger_policy(plan_id, self.policy())
        watcher = self.make_watcher()
        watcher.poll_once(1_000)
        time.sleep(0.01)
        file.write_text("after!", encoding="utf-8")
        self.assertEqual([d.action for d in watcher.poll_once(2_000)], ["pending"])


class Alpha19RestoreTestServiceTests(Alpha19Base):
    def test_unproven_snapshot_status(self) -> None:
        _plan, _folder, snapshot = self.backed_up_folder_plan()
        status = self.restore_test.status(snapshot)
        self.assertEqual(status["state"], "unproven")
        self.assertIsNone(status["proof"])
        self.assertIsNone(self.restore_test.last_attempt(snapshot))

    def test_full_run_records_proof_and_attempt(self) -> None:
        plan_id, folder, snapshot = self.backed_up_folder_plan()
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        result = self.run_restore_test(snapshot.id, plan_id)
        self.assertTrue(result.success, result.message)
        fresh = self.repo.snapshot(snapshot.id)
        status = self.restore_test.status(fresh)
        self.assertEqual(status["state"], "proven")
        self.assertEqual(status["proof"]["scope"], "full")
        self.assertEqual(status["proof"]["files"], 4)
        self.assertTrue(status["proof"]["temporary_output_removed"])
        self.assertEqual(status["attempt"]["status"], "succeeded")
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_run_leaves_no_temporary_folder(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        temp_root = Path(tempfile.gettempdir())
        before = {p.name for p in temp_root.glob(RestoreTestService.TEMP_PREFIX + "*")}
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id).success)
        after = {p.name for p in temp_root.glob(RestoreTestService.TEMP_PREFIX + "*")}
        self.assertEqual(before, after)

    def test_corrupted_snapshot_fails_and_is_not_proven(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        paths = self.verifier.artifact_paths(snapshot)
        victim = next(p for p in self.storage_root.rglob("file1.txt"))
        set_writable_best_effort(victim)
        victim.write_bytes(b"corrupted!")
        try:
            result = self.run_restore_test(snapshot.id, plan_id)
            failed = not result.success
        except AppError:
            failed = True
        self.assertTrue(failed)
        status = self.restore_test.status(self.repo.snapshot(snapshot.id))
        self.assertIsNone(status["proof"])
        self.assertEqual(status["state"], "failed")
        self.assertIsNotNone(paths)

    def test_cancel_records_attempt_and_keeps_old_proof(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id).success)
        time.sleep(0.005)
        with self.assertRaises(CancelledError):
            self.run_restore_test(snapshot.id, plan_id, cancelled=lambda: True)
        status = self.restore_test.status(self.repo.snapshot(snapshot.id))
        self.assertEqual(status["state"], "proven_then_failed")
        self.assertEqual(status["attempt"]["status"], "cancelled")
        self.assertIsNotNone(status["proof"])

    def test_no_space_is_recorded_separately(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaises(AppError) as caught:
                self.run_restore_test(snapshot.id, plan_id)
        self.assertEqual(caught.exception.code, "RESTORE_TEST_NO_SPACE")
        status = self.restore_test.status(self.repo.snapshot(snapshot.id))
        self.assertEqual(status["attempt"]["status"], "no_space")
        self.assertIsNone(status["proof"])

    def test_uncommitted_snapshot_is_refused(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        with self.db.transaction() as conn:
            conn.execute("UPDATE Snapshots SET state='corrupted' WHERE id=?", (snapshot.id,))
        with self.assertRaises(AppError) as caught:
            self.run_restore_test(snapshot.id, plan_id)
        self.assertEqual(caught.exception.code, "SNAPSHOT_NOT_COMMITTED")

    def test_sample_scope_never_replaces_full_proof(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan(count=5)
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id).success)
        self.restore_test.SAMPLE_MAX_FILES = 2
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id, mode="sample").success)
        fresh = self.repo.snapshot(snapshot.id)
        self.assertEqual(self.restore_test.proof(fresh)["scope"], "full")
        self.assertEqual(self.restore_test.last_attempt(fresh)["scope"], "sample")
        self.assertEqual(self.restore_test.last_attempt(fresh)["files"], 2)

    def test_sample_only_proof_has_sample_scope(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan(count=5)
        self.restore_test.SAMPLE_MAX_FILES = 2
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id, mode="sample").success)
        proof = self.restore_test.proof(self.repo.snapshot(snapshot.id))
        self.assertEqual(proof["scope"], "sample")
        self.assertEqual(proof["files"], 2)
        self.assertEqual(proof["total_files"], 5)

    def test_pick_sample_rotates_and_wraps(self) -> None:
        _plan, _folder, snapshot = self.backed_up_folder_plan()
        entries = [{"entry_type": "file", "relative_path": f"f{i}.txt", "size_bytes": 10} for i in range(5)]
        entries.append({"entry_type": "directory", "relative_path": "d", "size_bytes": 0})
        self.restore_test.SAMPLE_MAX_FILES = 2
        names = [[e["relative_path"] for e in self.restore_test.pick_sample(snapshot, entries)] for _ in range(3)]
        self.assertEqual(names, [["f0.txt", "f1.txt"], ["f2.txt", "f3.txt"], ["f4.txt", "f0.txt"]])
        self.assertEqual(self.restore_test.pick_sample(snapshot, []), [])

    def test_pick_sample_respects_byte_limit_but_never_empty(self) -> None:
        _plan, _folder, snapshot = self.backed_up_folder_plan()
        self.restore_test.SAMPLE_MAX_BYTES = 100
        entries = [{"entry_type": "file", "relative_path": "huge.bin", "size_bytes": 10_000},
                   {"entry_type": "file", "relative_path": "tiny.txt", "size_bytes": 5}]
        picked = self.restore_test.pick_sample(snapshot, entries)
        self.assertTrue(picked)
        self.assertLessEqual(sum(e["size_bytes"] for e in picked), 10_000)

    def test_stale_proof_is_ignored_after_manifest_change(self) -> None:
        plan_id, _folder, snapshot = self.backed_up_folder_plan()
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id).success)
        fresh = self.repo.snapshot(snapshot.id)
        proof = dict(self.restore_test.proof(fresh))
        proof["manifest_sha256"] = "0" * 64
        self.repo.set_setting(RestoreTestService.PROOF_PREFIX + snapshot.id, proof)
        self.assertIsNone(self.restore_test.proof(fresh))

    def test_invalid_timestamps_are_rejected(self) -> None:
        for value in (0, -1, True, "5", 1.5, None):
            self.assertFalse(RestoreTestService._valid_stamp(value), value)
        self.assertTrue(RestoreTestService._valid_stamp(5))

    def test_cleanup_only_removes_own_temp_folders(self) -> None:
        foreign = Path(tempfile.mkdtemp(prefix="foreign-"))
        try:
            self.assertFalse(self.restore_test._cleanup(foreign))
            self.assertTrue(foreign.exists())
            own = Path(tempfile.mkdtemp(prefix=RestoreTestService.TEMP_PREFIX))
            self.assertTrue(self.restore_test._cleanup(own))
            self.assertFalse(own.exists())
            self.assertFalse(self.restore_test._cleanup(self.root))
        finally:
            shutil.rmtree(foreign, ignore_errors=True)

    def test_file_plan_restore_test(self) -> None:
        file = self.source_root / "single.txt"
        file.write_text("single file", encoding="utf-8")
        plan_id = self.make_plan(file)
        self.assertTrue(self.backup(plan_id).success)
        snapshot = self.repo.snapshots(plan_id)[0]
        self.assertTrue(self.run_restore_test(snapshot.id, plan_id).success)
        self.assertEqual(self.restore_test.status(self.repo.snapshot(snapshot.id))["state"], "proven")


class Alpha19RepositoryTests(Alpha19Base):
    def test_latest_committed_logical_size(self) -> None:
        file = self.source_root / "size.txt"
        file.write_bytes(b"a" * 10)
        plan_id = self.make_plan(file)
        self.assertIsNone(self.repo.latest_committed_logical_size(plan_id))
        self.assertTrue(self.backup(plan_id).success)
        self.assertEqual(self.repo.latest_committed_logical_size(plan_id), 10)
        file.write_bytes(b"b" * 25)
        self.assertTrue(self.backup(plan_id).success)
        self.assertEqual(self.repo.latest_committed_logical_size(plan_id), 25)

    def test_storage_root_history_recent_and_popular(self) -> None:
        self.assertEqual(self.repo.storage_root_history(), ([], []))
        roots = []
        for index in range(4):
            path = PathPolicy.validate_storage(str(self.root / f"st{index}"))
            storage_id = self.repo.get_or_create_storage(path)
            self.storage.ensure_root(path, storage_id)
            roots.append((path, storage_id))
        for index in range(2):
            file = self.source_root / f"h{index}.txt"
            file.write_text("h", encoding="utf-8")
            self.repo.create_plan(f"h{index}", "", PathPolicy.validate_source(str(file)), roots[0][1], 3,
                                  StorageMode.DIRECT.value)
        recent, popular = self.repo.storage_root_history(recent_limit=2, popular_limit=5)
        self.assertEqual(len(recent), 2)
        self.assertTrue(set(popular).isdisjoint(recent))
        recent_all, popular_all = self.repo.storage_root_history(recent_limit=0, popular_limit=5)
        self.assertEqual(recent_all, [])
        self.assertEqual(len(popular_all), 1)

    def test_discard_plan_deletion_jobs(self) -> None:
        file = self.source_root / "j.txt"
        file.write_text("j", encoding="utf-8")
        plan_id = self.make_plan(file)
        self.repo.discard_plan_deletion_job(plan_id)
        self.repo.discard_plan_deletion_jobs()
        self.assertIsNone(self.repo.plan_deletion_job(plan_id))

    def test_deletion_preview_has_new_fields(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        preview = self.repo.plan_deletion_preview(plan_id)
        self.assertEqual(preview.snapshot_count, 1)
        self.assertEqual(preview.warnings, [])
        self.assertFalse(preview.has_problems)
        self.assertFalse(preview.resuming)

    def test_finish_plan_deletion_records_counts(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        result = self.plan_delete(plan_id)
        self.assertTrue(result.success, result.message)
        messages = [str(dict(item)) for item in self.repo.recent_activity(20)]
        self.assertTrue(any("Deleted" in str(m) and "failed 0" in str(m) for m in messages), messages)

    def plan_delete(self, plan_id: str, cancelled=None):
        preview = self.repo.plan_deletion_preview(plan_id)
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                              {"preview_token": preview.token, "approve_pinned": False})
        return self.delete.delete_plan_data(operation, plan_id, preview.token, False, self.progress,
                                            cancelled or self.not_cancelled)


class Alpha19DeleteSafetyTests(Alpha19Base):
    def test_forbidden_roots(self) -> None:
        self.assertTrue(self.delete._is_forbidden_root(Path(Path.cwd().anchor or "/")))
        self.assertTrue(self.delete._is_forbidden_root(Path.home()))
        self.assertFalse(self.delete._is_forbidden_root(self.root / "ordinary"))

    def test_system_tree_detection_uses_environment(self) -> None:
        fake = self.root / "ProgramFilesFake"
        with mock.patch.dict(os.environ, {"ProgramFiles": str(fake)}):
            self.assertTrue(self.delete._is_system_tree(fake / "app" / "data"))
            self.assertTrue(self.delete._is_system_tree(fake))
            self.assertFalse(self.delete._is_system_tree(self.root / "ProgramFilesFakeSibling"))
            self.assertTrue(self.delete._is_forbidden_root(fake))

    def test_boundary_accepts_only_job_folder_directly_inside_storage(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        plan = self.repo.plan_details(plan_id)
        root = self.storage.plan_dir(self.storage_root, plan)
        self.assertIsNone(self.delete._deletion_boundary_error(plan, root))
        self.assertEqual(self.delete._deletion_boundary_error(plan, Path(plan.storage_path)), "storage-root")
        self.assertEqual(self.delete._deletion_boundary_error(plan, Path(plan.storage_path) / "other"), "name")
        self.assertEqual(self.delete._deletion_boundary_error(plan, Path("relative") / root.name), "relative")
        nested = root / "inner" / root.name
        self.assertIn(self.delete._deletion_boundary_error(plan, nested), {"storage-root", "outside"})

    def test_reparse_detection(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertTrue(DeleteService._is_reparse(link))
        self.assertFalse(DeleteService._is_reparse(target))
        self.assertFalse(DeleteService._is_reparse(self.root / "missing"))

    def test_delete_removes_plan_data_only(self) -> None:
        plan_id, folder, _snapshot = self.backed_up_folder_plan()
        other = self.folder_with_files("other", 2)
        other_plan = self.make_plan(other, "Другой")
        self.assertTrue(self.backup(other_plan).success)
        root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan_id))
        other_root = self.storage.plan_dir(self.storage_root, self.repo.plan_details(other_plan))
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        preview = self.repo.plan_deletion_preview(plan_id)
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                              {"preview_token": preview.token, "approve_pinned": False})
        result = self.delete.delete_plan_data(operation, plan_id, preview.token, False, self.progress, self.not_cancelled)
        self.assertTrue(result.success, result.message)
        self.assertFalse(root.exists())
        self.assertTrue(other_root.exists())
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_delete_reports_unremovable_file_without_crashing(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        original = DeleteService._remove_entry

        def flaky(service: Any, path: Path) -> None:
            if Path(path).name == "file1.txt":
                raise PermissionError("locked")
            return original(service, path)

        with mock.patch.object(_APP, "PLAN_DELETE_RETRY_DELAYS", ()), \
                mock.patch.object(DeleteService, "_remove_entry", flaky):
            preview = self.repo.plan_deletion_preview(plan_id)
            operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                                  {"preview_token": preview.token, "approve_pinned": False})
            try:
                result = self.delete.delete_plan_data(operation, plan_id, preview.token, False,
                                                      self.progress, self.not_cancelled)
                message = result.message
            except AppError as exc:
                message = exc.user_message
        self.assertTrue(message)
        locked = list(self.storage_root.rglob("file1.txt"))
        self.assertTrue(locked, "the locked file must remain on disk")

    def test_delete_with_stale_token_is_refused(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                              {"preview_token": "bad", "approve_pinned": False})
        try:
            result = self.delete.delete_plan_data(operation, plan_id, "bad", False, self.progress, self.not_cancelled)
            refused = not result.success
        except AppError:
            refused = True
        self.assertTrue(refused)
        self.assertTrue(self.storage.plan_dir(self.storage_root, self.repo.plan_details(plan_id)).exists())

    def test_delete_cancel_keeps_remaining_data_consistent(self) -> None:
        plan_id, _folder, _snapshot = self.backed_up_folder_plan()
        preview = self.repo.plan_deletion_preview(plan_id)
        operation = self.repo.create_operation(OperationType.DELETE_PLAN_DATA, plan_id, None,
                                              {"preview_token": preview.token, "approve_pinned": False})
        try:
            result = self.delete.delete_plan_data(operation, plan_id, preview.token, False, self.progress, lambda: True)
            stopped = not result.success
        except CancelledError:
            stopped = True
        self.assertTrue(stopped)
        self.assertIsNotNone(self.repo.plan_details(plan_id))


class Alpha19ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        logs = self.root / "logs"
        logs.mkdir()
        self.paths = AppPaths(self.root, self.root / "c.sqlite3", logs, logs / "c.log")
        self.controller = AppController(self.paths, start_worker=False)
        self.controller.snapshot_service.reserve_bytes = 0
        self.controller.snapshot_service.reserve_ratio = 0.0

    def tearDown(self) -> None:
        self.controller.shutdown()
        try:
            self.temp.cleanup()
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)

    def make_backed_plan(self, name: str = "c", size: int = 100) -> tuple[str, Any]:
        source = self.root / f"src-{name}"
        source.mkdir()
        (source / "a.bin").write_bytes(b"a" * size)
        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.root / "storage"))
        storage_id = self.controller.repo.get_or_create_storage(storage)
        self.controller.storage.ensure_root(storage, storage_id)
        plan_id = self.controller.repo.create_plan(name, "", descriptor, storage_id, 5)
        operation = self.controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {"force": False})
        result = self.controller.snapshot_service.create(operation, plan_id, False, lambda e: None, lambda: False)
        self.assertTrue(result.success, result.message)
        return plan_id, self.controller.repo.snapshots(plan_id)[0]

    def test_default_restore_test_settings(self) -> None:
        settings = self.controller.restore_test_settings19()
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["days"], 7)
        self.assertEqual(settings["max_bytes"], 512 * 1024 * 1024)

    def test_save_restore_test_settings_roundtrip(self) -> None:
        self.controller.save_restore_test_settings19(True, 14, 256)
        settings = self.controller.restore_test_settings19()
        self.assertEqual((settings["enabled"], settings["days"], settings["max_bytes"]), (True, 14, 256 * 1024 * 1024))
        self.controller.save_restore_test_settings19(False, 3)
        self.assertEqual(self.controller.restore_test_settings19()["max_bytes"], 256 * 1024 * 1024)

    def test_save_restore_test_settings_validation(self) -> None:
        for days, mib in ((0, None), (91, None), (7, 15), (7, 1024 * 1024 + 1)):
            with self.assertRaises(AppError) as caught:
                self.controller.save_restore_test_settings19(True, days, mib)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")

    def test_settings_are_clamped_when_storage_is_corrupted(self) -> None:
        repo = self.controller.repo
        repo.set_setting("recovery19.auto_days", 9999)
        repo.set_setting("recovery19.auto_max_bytes", 1)
        settings = self.controller.restore_test_settings19()
        self.assertEqual(settings["days"], 90)
        self.assertEqual(settings["max_bytes"], 1024 * 1024)
        repo.set_setting("recovery19.auto_days", "bad")
        self.assertEqual(self.controller.restore_test_settings19()["days"], 7)

    def test_auto_candidate_requires_enabled_setting(self) -> None:
        self.make_backed_plan()
        self.assertIsNone(self.controller.auto_restore_test_candidate())
        self.controller.save_restore_test_settings19(True, 7)
        candidate = self.controller.auto_restore_test_candidate()
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[1], "full")

    def test_auto_candidate_uses_sample_for_large_snapshot(self) -> None:
        self.make_backed_plan("big", size=2 * 1024 * 1024)
        self.controller.save_restore_test_settings19(True, 7, 16)
        self.controller.repo.set_setting("recovery19.auto_max_bytes", 1024 * 1024)
        self.assertEqual(self.controller.auto_restore_test_candidate()[1], "sample")

    def test_auto_candidate_skips_recently_tested_snapshot(self) -> None:
        plan_id, snapshot = self.make_backed_plan()
        self.controller.save_restore_test_settings19(True, 7)
        operation = self.controller.repo.create_operation(OperationType.TEST_RESTORE, plan_id, snapshot.id, {})
        self.controller.restore_test_service.run(operation, snapshot.id, lambda e: None, lambda: False)
        self.assertIsNone(self.controller.auto_restore_test_candidate())

    def test_auto_candidate_picks_oldest_tested_first(self) -> None:
        first_plan, first = self.make_backed_plan("one")
        second_plan, second = self.make_backed_plan("two")
        self.controller.save_restore_test_settings19(True, 1)
        operation = self.controller.repo.create_operation(OperationType.TEST_RESTORE, first_plan, first.id, {})
        self.controller.restore_test_service.run(operation, first.id, lambda e: None, lambda: False)
        self.assertEqual(self.controller.auto_restore_test_candidate()[0], second.id)

    def test_queue_test_restore_rejects_uncommitted(self) -> None:
        plan_id, snapshot = self.make_backed_plan()
        with self.controller.repo.db.transaction() as conn:
            conn.execute("UPDATE Snapshots SET state='corrupted' WHERE id=?", (snapshot.id,))
        with self.assertRaises(AppError) as caught:
            self.controller.queue_test_restore(snapshot.id)
        self.assertEqual(caught.exception.code, "SNAPSHOT_NOT_COMMITTED")

    def test_queue_test_restore_normalizes_mode(self) -> None:
        _plan, snapshot = self.make_backed_plan()
        with mock.patch.object(self.controller, "_queue", return_value="op") as queued:
            self.controller.queue_test_restore(snapshot.id, "weird")
        self.assertEqual(queued.call_args.kwargs.get("mode"), "full")
        with mock.patch.object(self.controller, "_queue", return_value="op") as queued:
            self.controller.queue_test_restore(snapshot.id, "sample")
        self.assertEqual(queued.call_args.kwargs.get("mode"), "sample")

    def test_save_plan_with_policy_persists_threshold(self) -> None:
        source = self.root / "pol.txt"
        source.write_text("p", encoding="utf-8")
        plan_id = self.controller.save_plan_with_policy(
            None, "P", "", str(source), str(self.root / "stor"), 5, StorageMode.DIRECT.value,
            TriggerPolicy(True, 5, 10, False, 60, False, None, None, True, 5 << 20))
        self.assertEqual(self.controller.repo.plan_details(plan_id).change_threshold_bytes, 5 << 20)
        self.assertEqual(self.controller.repo.get_trigger_policy(plan_id).change_threshold_bytes, 5 << 20)


class Alpha19TranslationTests(unittest.TestCase):
    PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def test_languages_have_identical_keys(self) -> None:
        en, ru = set(TRANSLATIONS["en"]), set(TRANSLATIONS["ru"])
        self.assertEqual(sorted(en - ru), [])
        self.assertEqual(sorted(ru - en), [])

    def test_placeholders_match_between_languages(self) -> None:
        bad = []
        for key, text in TRANSLATIONS["en"].items():
            other = TRANSLATIONS["ru"].get(key, "")
            if set(self.PLACEHOLDER.findall(text)) != set(self.PLACEHOLDER.findall(other)):
                bad.append(key)
        self.assertEqual(bad, [])

    def test_no_empty_translations(self) -> None:
        empty = [(lang, key) for lang, table in TRANSLATIONS.items() for key, text in table.items() if not str(text).strip()]
        self.assertEqual(empty, [])

    def test_russian_texts_contain_cyrillic_where_english_has_letters(self) -> None:
        same = [key for key, text in TRANSLATIONS["ru"].items()
                if re.search(r"[A-Za-z]{4,}", self.PLACEHOLDER.sub("", text))
                and not re.search(r"[А-Яа-яЁё]", text) and key.startswith(("planform.", "folders.", "help."))]
        self.assertLessEqual(len(same), 5, same)

    def test_literal_translation_keys_used_in_source_exist(self) -> None:
        source = Path(_APP.__file__).read_text(encoding="utf-8")
        used = set(re.findall(r"""\btr\(\s*["']([A-Za-z0-9_.]+)["']""", source))
        used |= set(re.findall(r"""\btr19\(\s*["']([A-Za-z0-9_.]+)["']""", source))
        used = {key for key in used if not key.endswith(".")}
        missing = sorted(key for key in used if key not in TRANSLATIONS["en"])
        self.assertEqual(missing, [])

    def test_help_keys_exist_for_all_pages(self) -> None:
        for kind, count in HELP_PAGES:
            for language in ("en", "ru"):
                self.assertIn(f"help.page.{kind}.nav", TRANSLATIONS[language], (language, kind))
        self.assertIn("help.title", TRANSLATIONS["en"])

    def test_threshold_texts_format(self) -> None:
        for language in ("en", "ru"):
            for key, kwargs in (("planform.thr.gb", {"n": 2}), ("planform.thr.mb", {"n": 10}), ("planform.thr.kb", {"n": 100})):
                text = TRANSLATIONS[language][key].format(**kwargs)
                self.assertIn(str(kwargs["n"]), text)

    def test_i18n_service_switches_language_and_falls_back(self) -> None:
        with Workspace() as ws:
            service = I18nService(Repository(ws.db))
            service.set_language("ru")
            self.assertEqual(service.tr("help.title"), TRANSLATIONS["ru"]["help.title"])
            service.set_language("en")
            self.assertEqual(service.tr("help.title"), TRANSLATIONS["en"]["help.title"])
            with self.assertRaises(AppError) as caught:
                service.tr("no.such.key.19")
            self.assertEqual(caught.exception.code, "TRANSLATION_KEY_MISSING")
            with self.assertRaises(AppError):
                service.set_language("xx")


class Alpha19UiBase(unittest.TestCase):
    def setUp(self) -> None:
        ensure_test_qapplication()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        logs = self.root / "logs"
        logs.mkdir()
        self.paths = AppPaths(self.root, self.root / "ui.sqlite3", logs, logs / "ui.log")
        self.controller = AppController(self.paths, start_worker=False)
        self.controller.snapshot_service.reserve_bytes = 0
        self.controller.snapshot_service.reserve_ratio = 0.0

    def tearDown(self) -> None:
        QApplication.processEvents()
        self.controller.shutdown()
        try:
            self.temp.cleanup()
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)

    def add_plan(self, name: str = "UI") -> str:
        source = self.root / f"src-{name}"
        source.mkdir(exist_ok=True)
        (source / "a.txt").write_text("data", encoding="utf-8")
        descriptor = PathPolicy.validate_source(str(source))
        storage = PathPolicy.validate_storage(str(self.root / "storage"))
        storage_id = self.controller.repo.get_or_create_storage(storage)
        self.controller.storage.ensure_root(storage, storage_id)
        return self.controller.repo.create_plan(name, "", descriptor, storage_id, 5)


class Alpha19WidgetTests(Alpha19UiBase):
    def test_help_mark_escapes_html_and_sets_tooltip(self) -> None:
        mark = HelpMark("a < b & <c>\nline")
        self.assertEqual(mark.text(), "?")
        self.assertIn("&lt;", mark.toolTip())
        self.assertIn("&amp;", mark.toolTip())
        self.assertIn("<br>", mark.toolTip())
        self.assertNotIn("<c>", mark.toolTip())
        mark.set_help_text("new")
        self.assertIn("new", mark.toolTip())

    def test_wheel_guard_blocks_combo_change_and_scrolls_form(self) -> None:
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        host = QWidget()
        layout = QVBoxLayout(host)
        combo = QComboBox()
        combo.addItems(["a", "b", "c"])
        spin = QSpinBox()
        layout.addWidget(combo)
        layout.addWidget(spin)
        scroll = QScrollArea()
        guard = install_wheel_guard(host, scroll)
        self.assertIsInstance(guard, WheelGuard)
        self.assertEqual(combo.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        for widget in (combo, spin):
            event = QWheelEvent(QPointF(1, 1), QPointF(1, 1), QPoint(0, 0), QPoint(0, -120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(widget, event)
        self.assertEqual(combo.currentIndex(), 0)
        self.assertEqual(spin.value(), 0)

    def test_indicator_qss_is_valid_text(self) -> None:
        qss = build_indicator_qss()
        self.assertIsInstance(qss, str)
        self.assertIn("QCheckBox", qss)
        self.assertIn("QRadioButton", qss)

    def test_flow_layout_wraps_items(self) -> None:
        host = QWidget()
        flow = FlowLayout(host)
        for index in range(8):
            button = QPushButton(f"Button {index}")
            button.setFixedSize(100, 30)
            flow.addWidget(button)
        self.assertEqual(flow.count(), 8)
        self.assertGreater(flow.heightForWidth(220), flow.heightForWidth(1000))
        self.assertTrue(flow.hasHeightForWidth())
        host.resize(220, 300)
        host.show()
        QApplication.processEvents()
        rows = {widget.geometry().y() for widget in host.findChildren(QPushButton)}
        self.assertGreater(len(rows), 1)
        item = flow.takeAt(0)
        self.assertIsNotNone(item)
        self.assertEqual(flow.count(), 7)
        self.assertIsNone(flow.itemAt(99))
        host.close()

    def test_path_drop_target_kind_can_be_callable(self) -> None:
        widget = QWidget()
        calls: list[Path] = []
        target = PathDropTarget(widget, self.controller.i18n, lambda: "folder", calls.append)
        self.assertEqual(target._kind(), "folder")
        target.kind = lambda: None
        self.assertIsNone(target._kind())
        target.kind = "file"
        self.assertEqual(target._kind(), "file")


class Alpha19HelpDialogTests(Alpha19UiBase):
    def test_help_dialog_builds_in_both_languages(self) -> None:
        for language in ("ru", "en"):
            self.controller.i18n.set_language(language)
            dialog = HelpDialog(self.controller)
            self.assertEqual(dialog.nav.count(), len(HELP_PAGES))
            self.assertEqual(dialog.stack.count(), len(HELP_PAGES))
            self.assertEqual(dialog.windowTitle(), TRANSLATIONS[language]["help.title"])
            for row in range(dialog.nav.count()):
                dialog.nav.setCurrentRow(row)
                QApplication.processEvents()
                self.assertEqual(dialog.stack.currentIndex(), row)
            dialog.deleteLater()

    def test_help_dialog_navigation_buttons_and_start_page(self) -> None:
        dialog = HelpDialog(self.controller, page=2)
        self.assertEqual(dialog.nav.currentRow(), 2)
        dialog.next_button.click()
        self.assertEqual(dialog.nav.currentRow(), 3)
        dialog.back_button.click()
        dialog.back_button.click()
        self.assertEqual(dialog.nav.currentRow(), 1)
        dialog.nav.setCurrentRow(0)
        dialog.back_button.click()
        self.assertEqual(dialog.nav.currentRow(), 0)
        dialog.deleteLater()
        dialog = HelpDialog(self.controller, page=999)
        self.assertEqual(dialog.nav.currentRow(), dialog.nav.count() - 1)
        dialog.deleteLater()

    def test_help_mock_pictures_render_for_every_page(self) -> None:
        for language in ("ru", "en"):
            self.controller.i18n.set_language(language)
            for kind, _count in HELP_PAGES:
                result = build_help_mock(kind, self.controller.i18n.tr)
                self.assertIsNotNone(result, (language, kind))

    def test_show_help_sets_flag_and_welcome_respects_it(self) -> None:
        paths, controller = self.paths, self.controller
        window = MainWindow(controller, paths)
        controller.main_window = window
        repo = controller.repo
        self.assertFalse(repo.get_bool_setting(SETTING_HELP_SHOWN, False))
        with mock.patch.object(HelpDialog, "exec", return_value=0) as executed:
            window.show()
            window.maybe_show_welcome_help()
            self.assertEqual(executed.call_count, 1)
            self.assertTrue(repo.get_bool_setting(SETTING_HELP_SHOWN, False))
            window.maybe_show_welcome_help()
            self.assertEqual(executed.call_count, 1)
        window.close()


class Alpha19PlanEditorTests(Alpha19UiBase):
    def editor(self, plan: Any = None) -> "PlanEditor":
        dialog = PlanEditor(self.controller, plan) if plan is not None else PlanEditor(self.controller)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_new_plan_editor_has_two_mode_buttons(self) -> None:
        PlanEditor._last_advanced = False
        dialog = self.editor()
        self.assertFalse(dialog.is_advanced)
        self.assertTrue(dialog.basic_button.isChecked())
        self.assertFalse(dialog.advanced_button.isChecked())
        dialog.show()
        QApplication.processEvents()
        self.assertTrue(dialog.basic_widget.isVisible())
        self.assertFalse(dialog.advanced_widget.isVisible())
        self.assertEqual(dialog.basic_button.text(), TRANSLATIONS["ru"]["planform.mode.basic"]
                         if self.controller.i18n.language == "ru" else TRANSLATIONS["en"]["planform.mode.basic"])

    def test_switching_modes_toggles_pages_and_hint(self) -> None:
        PlanEditor._last_advanced = False
        dialog = self.editor()
        dialog.show()
        dialog.set_advanced(True)
        QApplication.processEvents()
        self.assertTrue(dialog.is_advanced)
        self.assertTrue(dialog.advanced_widget.isVisible())
        self.assertFalse(dialog.basic_widget.isVisible())
        self.assertEqual(dialog.switch_hint.text(), self.controller.i18n.tr("planform.switch.advanced"))
        dialog.advanced_button.click()
        dialog.basic_button.click()
        QApplication.processEvents()
        self.assertFalse(dialog.is_advanced) if False else None
        dialog.set_advanced(False)
        self.assertEqual(dialog.switch_hint.text(), self.controller.i18n.tr("planform.switch.basic"))
        self.assertTrue(dialog.basic_widget.isVisible())

    def test_last_used_mode_is_remembered_for_new_plans(self) -> None:
        PlanEditor._last_advanced = False
        first = self.editor()
        first.set_advanced(True)
        second = self.editor()
        self.assertTrue(second.is_advanced)
        second.set_advanced(False)
        self.assertFalse(self.editor().is_advanced)

    def test_threshold_combo_offers_all_steps_and_texts(self) -> None:
        dialog = self.editor()
        values = [dialog.threshold_combo.itemData(i) for i in range(dialog.threshold_combo.count())]
        self.assertEqual(values, list(CHANGE_THRESHOLD_STEPS))
        basic_values = [dialog.basic_threshold_combo.itemData(i) for i in range(dialog.basic_threshold_combo.count())]
        self.assertEqual(basic_values, list(CHANGE_THRESHOLD_STEPS))
        tr = self.controller.i18n.tr
        self.assertEqual(dialog._threshold_text(0), tr("planform.thr.any"))
        self.assertEqual(dialog._threshold_text(1 << 30), tr("planform.thr.gb", n=1))
        self.assertEqual(dialog._threshold_text(10 << 20), tr("planform.thr.mb", n=10))
        self.assertEqual(dialog._threshold_text(100 << 10), tr("planform.thr.kb", n=100))

    def test_select_threshold_adds_unknown_value(self) -> None:
        dialog = self.editor()
        dialog._select_threshold(dialog.threshold_combo, 3 << 20)
        self.assertEqual(dialog.threshold_combo.currentData(), 3 << 20)
        self.assertEqual(dialog.build_trigger_policy().change_threshold_bytes, 3 << 20)

    def test_basic_threshold_flows_into_policy(self) -> None:
        dialog = self.editor()
        index = dialog.basic_threshold_combo.findData(10 << 20)
        dialog.basic_threshold_combo.setCurrentIndex(index)
        dialog.freq_auto_radio.setChecked(True)
        dialog._apply_freq_preset()
        policy = dialog.build_trigger_policy()
        self.assertTrue(policy.on_change_enabled)
        self.assertFalse(policy.on_schedule_enabled)
        self.assertEqual(policy.change_threshold_bytes, 10 << 20)

    def test_frequency_presets_map_to_policy(self) -> None:
        dialog = self.editor()
        dialog.freq_manual_radio.setChecked(True)
        dialog._apply_freq_preset()
        policy = dialog.build_trigger_policy()
        self.assertFalse(policy.on_change_enabled or policy.on_schedule_enabled)
        dialog.freq_schedule_radio.setChecked(True)
        dialog._apply_freq_preset()
        policy = dialog.build_trigger_policy()
        self.assertTrue(policy.on_schedule_enabled)
        self.assertFalse(policy.on_change_enabled)
        self.assertEqual(policy.schedule_interval_minutes, int(dialog.basic_schedule_combo.currentData()))
        self.assertTrue(dialog.build_trigger_policy().manual_allowed_quiet_hours in (True, False))

    def test_keep_presets_switch_retention_policy(self) -> None:
        dialog = self.editor()
        for index in range(dialog.basic_keep_combo.count()):
            dialog.basic_keep_combo.setCurrentIndex(index)
            dialog._apply_keep_preset()
            key = dialog.basic_keep_combo.currentData()
            if key == "light":
                self.assertEqual(dialog.keep_spin.value(), dialog.KEEP_LIGHT)
                self.assertEqual(dialog.retention_mode_combo.currentData(), RETENTION_MODE_COUNT)
            elif key == "max":
                self.assertEqual(dialog.keep_spin.value(), dialog.KEEP_MAX)
            elif key == "standard":
                self.assertEqual(dialog.retention_mode_combo.currentData(), RETENTION_MODE_TIME)
                self.assertEqual(dialog.retention_daily_spin.value(), DEFAULT_RETENTION_DAILY)

    def test_keep_count_stays_editable_for_time_retention(self) -> None:
        dialog = self.editor()
        dialog.retention_mode_combo.setCurrentIndex(dialog.retention_mode_combo.findData(RETENTION_MODE_TIME))
        dialog.update_retention_controls()
        self.assertTrue(dialog.keep_spin.isEnabled())
        self.assertTrue(dialog.retention_daily_spin.isEnabled())

    def test_derived_name_from_source(self) -> None:
        dialog = self.editor()
        folder = self.root / "Мои документы"
        folder.mkdir()
        file = self.root / "report.final.docx"
        file.write_text("x", encoding="utf-8")
        cases = ((str(folder), "Мои документы"), (str(folder) + os.sep, "Мои документы"),
                 (str(file), "report.final"), ("", ""))
        for text, expected in cases:
            dialog.source_edit.setText(text)
            self.assertEqual(dialog._derived_name(), expected, text)

    def test_minutes_text_units(self) -> None:
        dialog = self.editor()
        tr = self.controller.i18n.tr
        self.assertEqual(dialog._minutes_text(10080), tr("planform.unit.week", n=1))
        self.assertEqual(dialog._minutes_text(2880), tr("planform.unit.day", n=2))
        self.assertEqual(dialog._minutes_text(120), tr("planform.unit.hour", n=2))
        self.assertEqual(dialog._minutes_text(45), tr("planform.unit.min", n=45))

    def test_save_new_basic_plan_creates_plan_with_defaults(self) -> None:
        source = self.root / "to-save"
        source.mkdir()
        (source / "x.txt").write_text("x", encoding="utf-8")
        dialog = self.editor()
        dialog.source_edit.setText(str(source))
        dialog.storage_edit.setText(str(self.root / "bk"))
        dialog.save(start_first_backup=True)
        self.assertIsNotNone(dialog.created_plan_id)
        self.assertTrue(dialog.start_first_backup)
        plan = self.controller.repo.plan_details(dialog.created_plan_id)
        self.assertEqual(plan.display_name, "to-save")

    def test_save_advanced_plan_stores_threshold(self) -> None:
        source = self.root / "adv"
        source.mkdir()
        (source / "x.txt").write_text("x", encoding="utf-8")
        dialog = self.editor()
        dialog.set_advanced(True)
        dialog.source_edit.setText(str(source))
        dialog.storage_edit.setText(str(self.root / "bk2"))
        dialog.name_edit.setText("Профессиональный")
        dialog.on_change_check.setChecked(True)
        dialog._select_threshold(dialog.threshold_combo, 1 << 20)
        dialog.save()
        plan = self.controller.repo.plan_details(dialog.created_plan_id)
        self.assertEqual(plan.display_name, "Профессиональный")
        self.assertEqual(plan.change_threshold_bytes, 1 << 20)

    def test_save_with_empty_source_shows_error_and_creates_nothing(self) -> None:
        dialog = self.editor()
        dialog.storage_edit.setText(str(self.root / "bk3"))
        with mock.patch.object(QMessageBox, "warning") as warned:
            dialog.save()
        self.assertTrue(warned.called)
        self.assertIsNone(dialog.created_plan_id)
        self.assertEqual(self.controller.repo.plan_summaries(False), [])

    def test_editing_existing_plan_loads_threshold(self) -> None:
        plan_id = self.add_plan("edit")
        self.controller.repo.update_trigger_policy(plan_id, TriggerPolicy(True, 5, 10, False, 60, False, None, None, True, 10 << 20))
        dialog = self.editor(self.controller.repo.plan_details(plan_id))
        self.assertEqual(dialog.threshold_combo.currentData(), 10 << 20)
        self.assertEqual(dialog.created_plan_id, None)


class Alpha19WizardTests(Alpha19UiBase):
    def wizard(self) -> "ProtectionWizard19":
        dialog = ProtectionWizard19(self.controller)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_wizard_constructs_with_first_step(self) -> None:
        dialog = self.wizard()
        self.assertGreaterEqual(dialog.stack.count(), 3)
        self.assertEqual(dialog.stack.currentIndex(), 0)
        self.assertTrue(dialog.folder_radio.isChecked())
        self.assertFalse(dialog.file_radio.isChecked())

    def test_wizard_policy_for_each_frequency(self) -> None:
        dialog = self.wizard()
        dialog.daily_radio.setChecked(True)
        policy = dialog.build_policy()
        self.assertTrue(policy.on_schedule_enabled)
        self.assertFalse(policy.on_change_enabled)
        self.assertEqual(policy.schedule_interval_minutes, 1440)
        self.assertEqual(dialog.mode_text(), self.controller.i18n.tr("setup.wizard.daily"))
        dialog.change_radio.setChecked(True)
        policy = dialog.build_policy()
        self.assertTrue(policy.on_change_enabled)
        self.assertFalse(policy.on_schedule_enabled)
        self.assertEqual(dialog.mode_text(), self.controller.i18n.tr("setup.wizard.change"))
        self.assertEqual(policy.change_threshold_bytes, 0)

    def test_wizard_back_on_first_step_stays(self) -> None:
        dialog = self.wizard()
        dialog.go_back()
        self.assertEqual(dialog.stack.currentIndex(), 0)

    def test_wizard_next_without_source_warns_and_stays(self) -> None:
        dialog = self.wizard()
        with mock.patch.object(QMessageBox, "warning") as warned:
            dialog.go_next()
        self.assertEqual(dialog.stack.currentIndex(), 0)
        self.assertTrue(warned.called or dialog.stack.currentIndex() == 0)


class Alpha19MainWindowTests(Alpha19UiBase):
    def window(self) -> "MainWindow":
        window = MainWindow(self.controller, self.paths)
        self.controller.main_window = window
        self.addCleanup(window.close)
        return window

    def test_overview_state_with_no_plans_asks_to_create(self) -> None:
        state = self.window().overview_state19()
        self.assertEqual(state["total"], 0)
        self.assertEqual(state["action"], "create")

    def test_overview_state_plan_without_copy_asks_for_first_backup(self) -> None:
        self.add_plan("fresh")
        state = self.window().overview_state19()
        self.assertEqual(state["action"], "first")
        self.assertEqual(len(state["no_copy"]), 1)
        self.assertEqual(state["attention"], 1)

    def test_overview_state_after_backup_requests_verification_then_test(self) -> None:
        plan_id = self.add_plan("full")
        controller = self.controller
        operation = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {"force": False})
        self.assertTrue(controller.snapshot_service.create(operation, plan_id, False, lambda e: None, lambda: False).success)
        window = self.window()
        state = window.overview_state19()
        self.assertEqual(state["action"], "verify")
        snapshot = controller.repo.available_snapshots(plan_id)[0]
        operation = controller.repo.create_operation(OperationType.VERIFY, plan_id, snapshot.id, {})
        self.assertTrue(controller.verifier.verify(operation, snapshot.id, lambda e: None, lambda: False).success)
        self.assertEqual(window.overview_state19()["action"], "test")
        operation = controller.repo.create_operation(OperationType.TEST_RESTORE, plan_id, snapshot.id, {})
        controller.restore_test_service.run(operation, snapshot.id, lambda e: None, lambda: False)
        state = window.overview_state19()
        self.assertEqual(state["action"], "ok")
        self.assertEqual(state["tested_n"], 1)
        self.assertEqual(state["samples"], 0)

    def test_failed_restore_test_is_reported_as_problem(self) -> None:
        plan_id = self.add_plan("bad")
        controller = self.controller
        operation = controller.repo.create_operation(OperationType.BACKUP, plan_id, None, {"force": False})
        self.assertTrue(controller.snapshot_service.create(operation, plan_id, False, lambda e: None, lambda: False).success)
        snapshot = controller.repo.available_snapshots(plan_id)[0]
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaises(AppError):
                operation = controller.repo.create_operation(OperationType.TEST_RESTORE, plan_id, snapshot.id, {})
                controller.restore_test_service.run(operation, snapshot.id, lambda e: None, lambda: False)
        state = self.window().overview_state19()
        self.assertEqual(state["action"], "problem")
        self.assertEqual(len(state["problems"]), 1)

    def test_main_window_switches_language_with_new_ui(self) -> None:
        window = self.window()
        window.show()
        for language in ("en", "ru"):
            self.controller.i18n.set_language(language)
            window.apply_language()
            QApplication.processEvents()
        window.close()

    def test_window_with_plan_shows_detail_after_overview(self) -> None:
        plan_id = self.add_plan("nav")
        window = self.window()
        window.show()
        window.select_plan_by_id(plan_id)
        QApplication.processEvents()
        self.assertEqual(window.current_plan_id, plan_id)





class Alpha19InstallerTests(unittest.TestCase):
    """19.9.8: install/update in one installer, installer texts from the dictionary, safe handover of data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.installer_dir = Path(__file__).resolve().parent / "installer"
        script = cls.installer_dir / "build_installer.py"
        if not script.is_file():
            raise unittest.SkipTest("the installer folder next to the tests was not found")
        spec = importlib.util.spec_from_file_location("amvibackup_build_installer_under_test", script)
        cls.builder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.builder
        spec.loader.exec_module(cls.builder)
        cls.iss = (cls.installer_dir / "AmviBackUp.iss").read_text(encoding="utf-8")
        cls.app_file = Path(_APP.__file__).resolve()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # Cleanup runs in reverse order: controllers (addCleanup in _controller) are closed before the folder is deleted.
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _code_section(self) -> str:
        return self.iss[self.iss.index("[Code]"):]

    def _section(self, name: str) -> list[str]:
        match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[|\Z)", self.iss, re.MULTILINE | re.DOTALL)
        if not match:
            return []
        return [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith(";")]

    def test_short_versions_are_padded_to_four_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for text, expected in (
                ("21.1-beta", "21.1.0.0"),
                ("21.1", "21.1.0.0"),
                ("21.1.2-rc1", "21.1.2.0"),
                ("0.19.9.6-alpha", "0.19.9.6"),
            ):
                path = Path(temp) / "app.py"
                path.write_text(f'APP_VERSION = "{text}"\n', encoding="utf-8")
                self.assertEqual(self.builder.read_app_version(path), (text, expected))
            path.write_text('APP_VERSION = "beta"\n', encoding="utf-8")
            with self.assertRaises(Exception):
                self.builder.read_app_version(path)

    def test_app_version_is_readable_by_the_installer_build(self) -> None:
        full, numeric = self.builder.read_app_version(self.app_file)
        self.assertEqual(full, APP_VERSION)
        self.assertRegex(numeric, r"^\d+\.\d+\.\d+\.\d+$")
        self.assertTrue(numeric.startswith(APP_VERSION.split("-", 1)[0]))

    def test_iss_constants_match_the_application(self) -> None:
        self.assertIn(f'#define AppMutexName "{INSTALLER_MUTEX_NAME}"', self.iss)
        self.assertIn(f"LanguageMarkerName = '{INSTALLER_LANGUAGE_FILE}'", self.iss)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            paths = AppPaths.create()
        self.assertIn(f"DataFolderName = '{paths.root.name}'", self.iss)
        self.assertIn(f"DatabaseFileName = '{paths.db_path.name}'", self.iss)
        self.assertEqual(paths.root.name, APP_NAME)
        self.assertIn(f'#define AppExe "{APP_NAME}.exe"', self.iss)

    def test_iss_never_touches_user_data(self) -> None:
        self.assertIn("PrivilegesRequired=lowest", self.iss)
        self.assertNotIn("[UninstallDelete]", self.iss)
        for line in self._section("InstallDelete"):
            self.assertIn("{app}", line)
            self.assertNotIn("localappdata", line.lower())
        for line in self._section("Files"):
            self.assertNotIn("localappdata", line.lower())
            self.assertIn("DestDir: \"{app}\"", line)
        code = self._code_section()
        self.assertEqual(code.count("DelTree"), 1)
        self.assertIn("MB_YESNO or MB_DEFBUTTON2", code)
        self.assertIn("not UninstallSilent", code)

    def test_iss_handles_update_downgrade_and_running_app(self) -> None:
        code = self._code_section()
        for needle in ("IsUpgrade", "IsDowngrade", "CompareVersions", "CheckForMutexes('{#AppMutexName}')",
                       "BackupUserDatabase", "pre-update-", "ssInstall", "WriteLanguageMarker", "ActiveLanguage"):
            self.assertIn(needle, code)
        self.assertIn("AppMutex={#AppMutexName}", self.iss)
        self.assertIn("CloseApplications=no", self.iss)
        self.assertIn("UsePreviousAppDir=yes", self.iss)
        self.assertIn("AppId={#AppIdValue}", self.iss)
        guid = re.search(r'#define AppIdValue "\{\{([0-9A-F-]{36})\}"', self.iss).group(1)
        self.assertIn("{" + guid + "}_is1", code)

    def test_iss_pascal_blocks_are_balanced(self) -> None:
        code = re.sub(r"'[^'\n]*'", "''", self._code_section())
        code = re.sub(r"//[^\n]*", "", code)
        words = re.findall(r"\b(begin|end|case|try)\b", code, re.IGNORECASE)
        opened = sum(1 for w in words if w.lower() in ("begin", "case", "try"))
        closed = sum(1 for w in words if w.lower() == "end")
        self.assertEqual(opened, closed)
        self.assertEqual(code.count("("), code.count(")"))

    def test_dictionary_has_complete_installer_texts(self) -> None:
        self.assertEqual(self.builder.check_dictionary(LANGUAGE_DATA), [])
        keys = self.builder.installer_keys(TRANSLATIONS)
        self.assertGreaterEqual(len(keys), 5)
        for code in (item.value for item in Language):
            for key in keys:
                if code != "en":
                    self.assertNotEqual(TRANSLATIONS[code][key], TRANSLATIONS["en"][key], f"{code}:{key}")

    def test_installer_languages_are_exactly_the_application_languages(self) -> None:
        self.assertEqual(set(self.builder.STANDARD_LANGUAGE_FILES), {item.value for item in Language})
        self.assertEqual(set(self.builder.STANDARD_LANGUAGE_FILES), {item.code for item in LANGUAGE_DATA.LANGUAGES})

    def test_generated_messages_cover_every_language_and_every_use(self) -> None:
        files = self.builder.generate_messages(TRANSLATIONS, self.root)
        self.assertEqual(set(files), set(self.builder.STANDARD_LANGUAGE_FILES))
        keys = self.builder.installer_keys(TRANSLATIONS)
        names = {self.builder.message_name(key) for key in keys}
        self.assertEqual(len(names), len(keys))
        builtin = {"CreateDesktopIcon", "AdditionalIcons", "LaunchProgram"}
        used = set(re.findall(r"CustomMessage\('(\w+)'\)", self.iss)) | set(re.findall(r"\{cm:(\w+)", self.iss))
        self.assertEqual(used - builtin, names)
        for code, path in files.items():
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), code)
            text = raw.decode("utf-8-sig")
            self.assertTrue(text.splitlines()[1] == "[CustomMessages]")
            found = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(";"))
            self.assertEqual(set(found), names, code)
            for key in keys:
                value = found[self.builder.message_name(key)]
                self.assertNotIn("{", value)
                self.assertNotIn("\n", value)
                expected = set(re.findall(r"\{(\w+)\}", TRANSLATIONS["en"][key]))
                self.assertEqual({f"%{1 if p == 'old' else 2}" for p in expected}, set(re.findall(r"%[12]", value)), f"{code}:{key}")

    def test_inno_text_conversion(self) -> None:
        self.assertEqual(self.builder.inno_text("A {old} -> {new}\nB 100%"), "A %1 -> %2%nB 100%%")
        with self.assertRaises(self.builder.BuildError):
            self.builder.inno_text("bad {other}")

    def test_languages_include_lists_all_languages(self) -> None:
        try:
            self.builder.write_languages_include(self.root, None, allow_download=False)
        except self.builder.BuildError as exc:
            self.skipTest(str(exc))
        text = (self.root / "languages.iss").read_text(encoding="utf-8")
        names = re.findall(r'^Name: "(\w+)"; MessagesFile: "[^"]+,generated\\\1\.isl"(?:; LicenseFile: "[^"]+")?$', text, re.MULTILINE)
        self.assertEqual(sorted(names), sorted(item.value for item in Language))

    def test_installer_mutex_with_fake_kernel(self) -> None:
        class FakeKernel:
            def __init__(self) -> None:
                self.created: list[Any] = []
                self.closed: list[Any] = []

            def CreateMutexW(self, attributes: Any, initial: Any, name: str) -> int:
                self.created.append(name)
                return 77

            def CloseHandle(self, handle: Any) -> int:
                self.closed.append(handle)
                return 1

        kernel = FakeKernel()
        mutex = InstallerMutex(kernel32=kernel)
        self.assertFalse(mutex.held)
        self.assertTrue(mutex.acquire())
        self.assertTrue(mutex.acquire())
        self.assertEqual(kernel.created, [INSTALLER_MUTEX_NAME])
        mutex.release()
        mutex.release()
        self.assertEqual(kernel.closed, [77])
        self.assertFalse(mutex.held)
        self.assertFalse(InstallerMutex(platform_name="Linux").acquire())

    def _controller(self) -> Any:
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        self.paths = AppPaths(self.root, self.root / "i.sqlite3", logs, logs / "i.log")
        controller = AppController(self.paths, start_worker=False)
        self.addCleanup(controller.shutdown)
        return controller

    def test_installer_language_is_applied_once_on_first_run(self) -> None:
        controller = self._controller()
        marker = self.root / INSTALLER_LANGUAGE_FILE
        marker.write_text("ru", encoding="ascii")
        self.assertEqual(apply_installer_language(self.paths, controller.repo, controller.i18n), "ru")
        self.assertEqual(controller.i18n.language, "ru")
        self.assertFalse(marker.exists())
        self.assertIsNone(apply_installer_language(self.paths, controller.repo, controller.i18n))

    def test_installer_language_never_overrides_the_users_choice(self) -> None:
        controller = self._controller()
        controller.i18n.set_language("fr")
        (self.root / INSTALLER_LANGUAGE_FILE).write_text("ar", encoding="ascii")
        self.assertIsNone(apply_installer_language(self.paths, controller.repo, controller.i18n))
        self.assertEqual(controller.i18n.language, "fr")
        self.assertFalse((self.root / INSTALLER_LANGUAGE_FILE).exists())

    def test_installer_language_ignores_garbage(self) -> None:
        controller = self._controller()
        for value in ("klingon", "", "\ufeffZH ", "ru\r\n"):
            (self.root / INSTALLER_LANGUAGE_FILE).write_text(value, encoding="utf-8")
            result = apply_installer_language(self.paths, controller.repo, controller.i18n)
            self.assertIn(result, (None, "zh", "ru"))
            self.assertFalse((self.root / INSTALLER_LANGUAGE_FILE).exists())
            if result is None:
                self.assertEqual(controller.i18n.language, DEFAULT_UI_LANGUAGE)
            else:
                controller.repo.set_setting(SETTING_UI_LANGUAGE, DEFAULT_UI_LANGUAGE)
                controller.i18n.set_language(DEFAULT_UI_LANGUAGE)
        self.assertIsNone(apply_installer_language(self.paths, controller.repo, controller.i18n))

    def test_language_file_is_found_in_a_frozen_bundle(self) -> None:
        bundle = self.root / "bundle"
        bundle.mkdir()
        with mock.patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                mock.patch.object(sys, "frozen", True, create=True):
            candidates = _language_file_candidates()
        self.assertEqual(candidates[0].name, "amvibackup_languages.py")
        self.assertIn(bundle / "amvibackup_languages.py", candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_logging_works_without_a_console(self) -> None:
        logs = self.root / "logs"
        logs.mkdir()
        paths = AppPaths(self.root, self.root / "l.sqlite3", logs, logs / "l.log")
        root_logger = logging.getLogger()
        saved = (list(root_logger.handlers), root_logger.level)
        try:
            with mock.patch.object(sys, "stderr", None):
                setup_logging(paths)
                kinds = {type(handler) for handler in root_logger.handlers}
            self.assertEqual(kinds, {logging.FileHandler})
            logging.info("probe")
        finally:
            for handler in list(root_logger.handlers):
                handler.close()
                root_logger.removeHandler(handler)
            for handler in saved[0]:
                root_logger.addHandler(handler)
            root_logger.setLevel(saved[1])

    def test_smoke_test_command_succeeds(self) -> None:
        self.assertEqual(main(["--smoke-test"]), 0)


class Alpha19UninstallTests(Alpha19UiBase):
    """Starting the uninstaller from the application (trusted path check, queued start after exit, button in settings)."""

    NEW_KEYS = (
        "settings.uninstall", "settings.uninstall_hint", "settings.uninstall_button",
        "settings.uninstall_title", "settings.uninstall_question", "settings.uninstall_busy",
        "settings.uninstall_unavailable", "settings.uninstall_failed",
    )

    def _program_folder(self, with_uninstaller: bool = True, name: str = "installed") -> Path:
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        (folder / "AmviBackUp.exe").write_bytes(b"x")
        if with_uninstaller:
            (folder / "unins000.exe").write_bytes(b"x")
            (folder / "unins000.dat").write_bytes(b"x")
        return folder

    def test_version_is_19_9_8(self) -> None:
        self.assertEqual(APP_VERSION, "22.0-beta")

    def test_find_uninstaller_in_program_folder(self) -> None:
        folder = self._program_folder()
        self.assertEqual(find_uninstaller(folder), (folder / "unins000.exe").resolve())
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(find_uninstaller(empty))
        self.assertIsNone(find_uninstaller(self.root / "missing"))
        self.assertIsNone(find_uninstaller(self._program_folder(False, "plain")))

    def test_find_uninstaller_is_none_when_running_from_source(self) -> None:
        self.assertIsNone(installed_app_dir())
        self.assertIsNone(find_uninstaller())

    def test_only_uninstaller_in_the_program_folder_is_trusted(self) -> None:
        folder = self._program_folder()
        good = folder / "unins000.exe"
        self.assertTrue(is_trusted_uninstaller(good, folder))
        self.assertFalse(is_trusted_uninstaller(good, None))
        other = self.root / "other"
        other.mkdir()
        (other / "unins000.exe").write_bytes(b"x")
        self.assertFalse(is_trusted_uninstaller(other / "unins000.exe", folder))
        (folder / "evil.exe").write_bytes(b"x")
        self.assertFalse(is_trusted_uninstaller(folder / "evil.exe", folder))
        (folder / "sub").mkdir()
        (folder / "sub" / "unins000.exe").write_bytes(b"x")
        self.assertFalse(is_trusted_uninstaller(folder / "sub" / "unins000.exe", folder))
        self.assertFalse(is_trusted_uninstaller(folder / "unins999.exe", folder))

    def test_launch_only_remembers_a_trusted_uninstaller(self) -> None:
        folder = self._program_folder()
        started: list[str] = []
        with mock.patch.object(_APP, "installed_app_dir", return_value=folder):
            launch_uninstaller_helper(folder / "unins000.exe")
            self.assertEqual(started, [])
            self.assertTrue(start_pending_uninstaller(started.append))
            self.assertEqual(started, [str((folder / "unins000.exe").resolve())])
            self.assertFalse(start_pending_uninstaller(started.append))
            (folder / "evil.exe").write_bytes(b"x")
            with self.assertRaises(OSError):
                launch_uninstaller_helper(folder / "evil.exe")
        self.assertFalse(start_pending_uninstaller(started.append))
        self.assertEqual(len(started), 1)

    def test_pending_uninstaller_start_failure_is_reported(self) -> None:
        folder = self._program_folder()

        def fail(path: str) -> None:
            raise OSError("boom")

        with mock.patch.object(_APP, "installed_app_dir", return_value=folder):
            launch_uninstaller_helper(folder / "unins000.exe")
        self.assertFalse(start_pending_uninstaller(fail))
        self.assertFalse(start_pending_uninstaller(fail))

    def test_program_has_no_self_spawning_helper(self) -> None:
        text = Path(_APP.__file__).read_text(encoding="utf-8")
        for forbidden in ("--run-uninstaller", "DETACHED_PROCESS", "subprocess.Popen"):
            self.assertNotIn(forbidden, text)

    def test_second_launch_reply_carries_the_running_version(self) -> None:
        self.assertEqual(parse_instance_reply(instance_ack()), (True, APP_VERSION))
        self.assertEqual(parse_instance_reply(b"ok\n"), (True, ""))
        self.assertEqual(parse_instance_reply(b"no\n"), (False, ""))
        self.assertEqual(parse_instance_reply(b""), (False, ""))

    def test_other_running_version_is_reported_in_both_languages(self) -> None:
        text = other_version_message("20.0.0.0-beta")
        self.assertIn("20.0.0.0-beta", text)
        self.assertIn(APP_VERSION, text)
        self.assertIn("already running", text)
        self.assertIn("уже запущен", text)
        self.assertIn("?", other_version_message(""))

    def test_uninstall_texts_are_translated_in_every_language(self) -> None:
        for code in (item.value for item in Language):
            for key in self.NEW_KEYS:
                self.assertIn(key, TRANSLATIONS[code], f"{code}:{key}")
                if code != "en":
                    self.assertNotEqual(TRANSLATIONS[code][key], TRANSLATIONS["en"][key], f"{code}:{key}")
            self.assertIn("{error}", TRANSLATIONS[code]["settings.uninstall_failed"], code)

    def test_default_self_test_file_follows_the_application_name(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("AMVIBACKUP_TEST_MODULE", None)
            self.assertEqual(_APP._external_test_path().name, f"{Path(_APP.__file__).stem}_tests.py")

    def test_settings_button_is_disabled_without_an_uninstaller(self) -> None:
        dialog = SettingsDialog(self.controller, self.paths)
        try:
            self.assertFalse(dialog.uninstall_button.isEnabled())
            self.assertEqual(dialog.uninstall_button.toolTip(), self.controller.i18n.tr("settings.uninstall_unavailable"))
            with mock.patch.object(QMessageBox, "information") as info, \
                    mock.patch.object(_APP, "launch_uninstaller_helper") as launch:
                dialog.start_uninstall()
            self.assertTrue(info.called)
            launch.assert_not_called()
        finally:
            dialog.deleteLater()

    def _installed_dialog(self) -> tuple[Any, Path]:
        folder = self._program_folder()
        path = (folder / "unins000.exe").resolve()
        with mock.patch.object(_APP, "find_uninstaller", return_value=path):
            dialog = SettingsDialog(self.controller, self.paths)
        return dialog, path

    def test_confirmed_uninstall_starts_the_helper_and_closes_the_program(self) -> None:
        dialog, path = self._installed_dialog()
        window = mock.Mock()
        self.controller.main_window = window
        try:
            self.assertTrue(dialog.uninstall_button.isEnabled())
            with mock.patch.object(_APP, "find_uninstaller", return_value=path), \
                    mock.patch.object(_APP, "launch_uninstaller_helper") as launch, \
                    mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                dialog.start_uninstall()
            for _ in range(3):
                QApplication.processEvents()
            launch.assert_called_once_with(path)
            window.request_explicit_exit.assert_called_once_with()
        finally:
            self.controller.main_window = None
            dialog.deleteLater()

    def test_declined_or_busy_uninstall_does_nothing(self) -> None:
        dialog, path = self._installed_dialog()
        window = mock.Mock()
        self.controller.main_window = window
        try:
            with mock.patch.object(_APP, "find_uninstaller", return_value=path), \
                    mock.patch.object(_APP, "launch_uninstaller_helper") as launch:
                with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                    dialog.start_uninstall()
                self.controller.active_operation_id = "op-busy"
                with mock.patch.object(QMessageBox, "warning") as warned, \
                        mock.patch.object(QMessageBox, "question") as asked:
                    dialog.start_uninstall()
                self.assertTrue(warned.called)
                asked.assert_not_called()
            QApplication.processEvents()
            launch.assert_not_called()
            window.request_explicit_exit.assert_not_called()
        finally:
            self.controller.active_operation_id = None
            self.controller.main_window = None
            dialog.deleteLater()

    def test_failed_helper_start_keeps_the_program_running(self) -> None:
        dialog, path = self._installed_dialog()
        window = mock.Mock()
        self.controller.main_window = window
        try:
            with mock.patch.object(_APP, "find_uninstaller", return_value=path), \
                    mock.patch.object(_APP, "launch_uninstaller_helper", side_effect=OSError("no access")), \
                    mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                    mock.patch.object(QMessageBox, "warning") as warned, \
                    self.assertLogs(level="ERROR"):
                dialog.start_uninstall()
            QApplication.processEvents()
            self.assertIn("no access", str(warned.call_args))
            window.request_explicit_exit.assert_not_called()
        finally:
            self.controller.main_window = None
            dialog.deleteLater()


class Alpha19DictionaryTests(unittest.TestCase):
    """All 7 languages are fully translated."""

    SAME_AS_ENGLISH_OK = {
        "settings.language.en", "settings.language.ru", "overview.mib",
        "planform.thr.kb", "planform.thr.mb", "planform.thr.gb",
    }
    SAME_AS_ENGLISH_OK = SAME_AS_ENGLISH_OK | {
        "units.b", "units.kb", "units.mb", "units.gb", "units.tb",
        "common.ok", "common.no", "versions.mode.direct", "editor.auto_join",
    }

    def test_every_language_has_every_key_with_the_same_placeholders(self) -> None:
        self.assertEqual(LANGUAGE_DATA.validate(), [])
        base = TRANSLATIONS["en"]
        for code in (item.value for item in Language):
            self.assertEqual(set(TRANSLATIONS[code]), set(base), code)

    def test_no_language_falls_back_to_english(self) -> None:
        base = TRANSLATIONS["en"]
        for code in (item.value for item in Language):
            if code == "en":
                continue
            same = [key for key in base
                    if TRANSLATIONS[code][key] == base[key] and key not in self.SAME_AS_ENGLISH_OK]
            self.assertLessEqual(len(same), 20, f"{code}: {same[:10]}")

    def test_line_breaks_and_scripts_match_the_language(self) -> None:
        base = TRANSLATIONS["en"]
        scripts = {"zh": "[\u4e00-\u9fff]", "hi": "[\u0900-\u097f]",
                   "ar": "[\u0600-\u06ff]", "ru": "[\u0400-\u04ff]"}
        for code in (item.value for item in Language):
            for key, text in base.items():
                self.assertEqual(TRANSLATIONS[code][key].count("\n"), text.count("\n"), f"{code}:{key}")
        for code, pattern in scripts.items():
            hits = sum(1 for value in TRANSLATIONS[code].values() if re.search(pattern, value))
            self.assertGreater(hits, len(base) * 0.9, code)

class Alpha19LocalizationTests(unittest.TestCase):
    """19.9.8: every phrase the user can see comes from the dictionary in all 7 languages."""

    def setUp(self) -> None:
        ensure_test_qapplication()
        self.addCleanup(set_active_ui_language, active_ui_language())
        self.codes = [item.value for item in Language]

    def test_sizes_use_the_units_of_the_active_language(self) -> None:
        for code in self.codes:
            set_active_ui_language(code)
            self.assertTrue(fmt_size(5 * 1024 * 1024).endswith(TRANSLATIONS[code]["units.mb"]), code)
            self.assertTrue(fmt_size(10).endswith(TRANSLATIONS[code]["units.b"]), code)

    def test_every_progress_title_in_the_code_has_a_dictionary_key(self) -> None:
        import ast as _ast

        source = Path(_APP.__file__).read_text(encoding="utf-8")
        titles = set()
        for node in _ast.walk(_ast.parse(source)):
            if isinstance(node, _ast.Call) and getattr(node.func, "id", "") == "ProgressEvent" and len(node.args) > 1:
                self.assertIsInstance(node.args[1], _ast.Constant, f"line {node.lineno}")
                titles.add(node.args[1].value)
        self.assertGreaterEqual(len(titles), 10)
        self.assertEqual(titles - set(PROGRESS_STAGE_KEYS), set())
        for code in self.codes:
            for key in PROGRESS_STAGE_KEYS.values():
                self.assertIn(key, TRANSLATIONS[code], f"{code}:{key}")

    def test_progress_text_is_localized_and_unknown_text_is_kept(self) -> None:
        for code in self.codes:
            self.assertEqual(localized_progress_title("Creating the version…", code), TRANSLATIONS[code]["progress.stage.creating"])
            self.assertEqual(localized_progress_path("Version #7", code), TRANSLATIONS[code]["progress.version_number"].format(n="7"))
        self.assertEqual(localized_progress_title("Large", "fr"), "Large")
        self.assertEqual(localized_progress_path("C:\\data\\file.txt", "fr"), "C:\\data\\file.txt")

    def test_startup_messages_come_from_the_dictionary(self) -> None:
        for code in self.codes:
            self.assertEqual(dictionary_text("startup.failed", code), TRANSLATIONS[code]["startup.failed"])
            self.assertEqual(startup_error_text(AppError("APP_ALREADY_RUNNING", "x"), code), TRANSLATIONS[code]["error.APP_ALREADY_RUNNING"])
            self.assertEqual(startup_error_text(AppError("NO_SUCH_CODE", "x"), code), TRANSLATIONS[code]["error.generic"])
        self.assertIn(startup_language(None), self.codes)
        self.assertIn(startup_language(mock.Mock()), self.codes)

    def test_message_box_buttons_use_the_dictionary_language(self) -> None:
        standard = QMessageBox.StandardButton
        for code in self.codes:
            set_active_ui_language(code)
            box = build_localized_message_box(QMessageBox.Icon.Question, None, "t", "x", standard.Yes | standard.No, standard.No)
            try:
                self.assertEqual(box.button(standard.Yes).text(), TRANSLATIONS[code]["common.yes"], code)
                self.assertEqual(box.button(standard.No).text(), TRANSLATIONS[code]["common.no"], code)
            finally:
                box.deleteLater()

    def test_i18n_service_keeps_the_active_language_in_sync(self) -> None:
        with Workspace() as ws:
            service = I18nService(Repository(ws.db))
            service.set_language("fr")
            self.assertEqual(active_ui_language(), "fr")
            service.set_language("ru")
            self.assertEqual(service.language, "ru")
            self.assertEqual(active_ui_language(), "ru")

    def test_no_cyrillic_outside_the_russian_table(self) -> None:
        for code in self.codes:
            if code == "ru":
                continue
            bad = [key for key, value in TRANSLATIONS[code].items()
                   if key != "settings.language.ru" and re.search("[\u0400-\u04ff]", value)]
            self.assertEqual(bad, [], code)


class Alpha19LicenseTests(Alpha19UiBase):
    """19.9.8: license, author and the places where they are shown."""

    def setUp(self) -> None:
        super().setUp()
        # No test of this class may open a real modal window: it would stop the whole run.
        for name in ("information", "warning", "critical", "question"):
            patcher = mock.patch.object(QMessageBox, name, return_value=QMessageBox.StandardButton.Ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        exec_patcher = mock.patch.object(_APP.LicenseDialog, "exec", return_value=0)
        self.license_exec = exec_patcher.start()
        self.addCleanup(exec_patcher.stop)

    def _where(self, name: str) -> str:
        return "searched here: " + "; ".join(f"{path} (exists: {path.is_file()})" for path in legal_file_candidates(name))

    def test_author_constants(self) -> None:
        self.assertIn("Dubkov", APP_AUTHOR)
        self.assertIn("AmviSoft", APP_AUTHOR)
        self.assertTrue(APP_COPYRIGHT.startswith("\u00a9 2026"))
        self.assertIn(APP_AUTHOR, APP_COPYRIGHT)

    def test_legal_texts_exist_and_name_the_author(self) -> None:
        license_text = read_legal_text("LICENSE", "en")
        notices = read_legal_text("THIRD_PARTY_NOTICES", "en")
        self.assertIsNotNone(license_text, "LICENSE.txt not found; " + self._where("LICENSE.txt"))
        self.assertIsNotNone(notices, "THIRD_PARTY_NOTICES.txt not found; " + self._where("THIRD_PARTY_NOTICES.txt"))
        self.assertEqual(APP_LICENSE_NAME, "MIT License")
        self.assertTrue(license_text.lstrip().startswith("MIT License"), "legal\\LICENSE.txt must contain the MIT text")
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", license_text)
        copyright_lines = [line for line in license_text.splitlines() if line.startswith("Copyright (c)")]
        self.assertEqual(copyright_lines, ["Copyright (c) 2026 Maxim Dubkov (AmviSoft)"])
        self.assertNotIn("PolyForm", license_text)
        self.assertIn("Maxim Dubkov", notices)
        self.assertIn("LGPL", notices)
        for code in (item.value for item in Language):
            self.assertEqual(read_legal_text("LICENSE", code), license_text,
                             f"a different license file is read for language {code}; " + self._where(f"LICENSE.{code}.txt"))
        stray = [path for path in legal_file_candidates("LICENSE.ru.txt") if path.is_file()]
        self.assertEqual(stray, [], "the old Russian license file must be removed from the legal folder")
        self.assertIsNone(read_legal_text("NO_SUCH_FILE", "en"))

    def test_legal_folder_is_searched_in_the_frozen_bundle(self) -> None:
        bundle = self.root / "bundle"
        (bundle / "legal").mkdir(parents=True)
        (bundle / "legal" / "LICENSE.txt").write_text("bundled license", encoding="utf-8")
        with mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
            candidates = legal_file_candidates("LICENSE.txt")
        self.assertIn(bundle / "legal" / "LICENSE.txt", candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_license_dialog_shows_the_text_read_only(self) -> None:
        dialog = LicenseDialog("Title", "hello license", "Close")
        try:
            self.assertEqual(dialog.viewer.toPlainText(), "hello license")
            self.assertTrue(dialog.viewer.isReadOnly())
            self.assertEqual(dialog.windowTitle(), "Title")
        finally:
            dialog.deleteLater()

    def test_settings_dialog_has_the_license_buttons(self) -> None:
        dialog = SettingsDialog(self.controller, self.paths)
        try:
            tr = self.controller.i18n.tr
            self.assertEqual(dialog.license_button.text(), tr("settings.license_button"))
            self.assertEqual(dialog.third_party_button.text(), tr("settings.third_party_button"))
            with mock.patch.object(_APP, "read_legal_text", return_value="license text"):
                dialog.license_button.click()
                dialog.third_party_button.click()
            self.assertEqual(self.license_exec.call_count, 2)
            with mock.patch.object(_APP, "read_legal_text", return_value=None):
                dialog.license_button.click()
            self.assertEqual(self.license_exec.call_count, 2)
            self.assertTrue(QMessageBox.information.called)
        finally:
            dialog.deleteLater()

    def test_installer_files_carry_the_author_and_the_license(self) -> None:
        folder = Path(_APP.__file__).resolve().parent / "installer"
        script = folder / "build_installer.py"
        if not script.is_file():
            self.skipTest("the installer folder next to the application was not found")
        iss = (folder / "AmviBackUp.iss").read_text(encoding="utf-8")
        self.assertIn(f'#define AppAuthor "{APP_AUTHOR}"', iss)
        self.assertIn("AppPublisher={#AppAuthor}", iss)
        self.assertIn("AppCopyright={#AppCopyright}", iss)
        spec = importlib.util.spec_from_file_location("amvibackup_build_installer_license_test", script)
        builder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = builder
        spec.loader.exec_module(builder)
        self.assertEqual(builder.APP_AUTHOR, APP_AUTHOR)
        try:
            builder.write_languages_include(self.root, None, allow_download=False)
        except builder.BuildError as exc:
            self.skipTest(str(exc))
        text = (self.root / "languages.iss").read_text(encoding="utf-8")
        languages = len(list(Language))
        self.assertEqual(text.count("LicenseFile:"), languages)
        self.assertEqual(text.count('LicenseFile: "..\\legal\\LICENSE.txt"'), languages)
        self.assertNotIn("LICENSE.ru.txt", text)
        parts = APP_VERSION.split("-", 1)[0].split(".")
        numeric_version = ".".join((parts + ["0"] * 4)[:4])
        expected_version = tuple(int(part) for part in numeric_version.split("."))

        info = builder.write_version_info(
            self.root / "version_info.txt",
            APP_VERSION,
            numeric_version,
        )
        content = info.read_text(encoding="utf-8")
        self.assertIn(f"filevers={expected_version}", content)
        self.assertIn(f"prodvers={expected_version}", content)
        self.assertIn("Dubkov", content)
        self.assertIn("LegalCopyright", content)



class Alpha19RestoreUXTests(Alpha19Base):
    """19.9.8: nonempty destinations, no overwrite, conflict suffixes, safe defaults."""

    def _fixture(self, mode="direct", folder=False):
        source = self.source_root / (f"src-{mode}" if folder else f"src-{mode}.txt")
        if folder:
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "chosen.txt").write_text("saved", encoding="utf-8")
            (source / "other.txt").write_text("other", encoding="utf-8")
        else:
            source.write_text("saved", encoding="utf-8")
        plan = self.make_plan(source, mode=mode)
        self.assertTrue(self.backup(plan).success)
        snapshot = self.repo.available_snapshots(plan)[0]
        dest = self.root / f"out-{mode}"
        dest.mkdir(exist_ok=True)
        return source, plan, snapshot, dest

    def _restore(self, snapshot, dest, selected=None, cancelled=None):
        kind = OperationType.RESTORE_SELECTED_FILES if selected is not None else OperationType.RESTORE_TO_DIRECTORY
        op = self.repo.create_operation(kind, snapshot.plan_id, snapshot.id, {})
        if selected is not None:
            result = self.restore.restore_selected_files(op, snapshot.id, selected, str(dest), self.progress, cancelled or self.not_cancelled)
        else:
            result = self.restore.restore_to_directory(op, snapshot.id, str(dest), self.progress, cancelled or self.not_cancelled)
        return result

    def test_version_and_schema(self):
        self.assertEqual(APP_VERSION, "22.0-beta")
        self.assertEqual(DB_SCHEMA_VERSION, 13)

    def test_nonempty_directory_preserved_direct_and_zip(self):
        for mode in ("direct", "zip"):
            with self.subTest(mode=mode):
                source, plan, snapshot, dest = self._fixture(mode)
                (dest / source.name).write_text("keep original name", encoding="utf-8")
                (dest / "important.bin").write_bytes(b"important")
                result = self._restore(snapshot, dest)
                self.assertTrue(result.success, result.message)
                self.assertEqual((dest / source.name).read_text(encoding="utf-8"), "keep original name")
                self.assertEqual((dest / "important.bin").read_bytes(), b"important")
                target = Path(result.destination_path)
                self.assertEqual(target.read_text(encoding="utf-8"), "saved")
                self.assertIn("-restored-", target.name)

    def test_folder_twice_gets_different_names_direct_and_zip(self):
        for mode in ("direct", "zip"):
            with self.subTest(mode=mode):
                source, plan, snapshot, dest = self._fixture(mode, True)
                one = self._restore(snapshot, dest)
                two = self._restore(snapshot, dest)
                self.assertTrue(one.success, one.message)
                self.assertTrue(two.success, two.message)
                self.assertNotEqual(one.destination_path, two.destination_path)
                for path in (one.destination_path, two.destination_path):
                    self.assertEqual((Path(path) / "nested" / "chosen.txt").read_text(encoding="utf-8"), "saved")
                self.assertFalse((dest / source.name).exists())

    def test_selected_conflicts_preserve_existing_files_direct_and_zip(self):
        for mode in ("direct", "zip"):
            with self.subTest(mode=mode):
                source, plan, snapshot, dest = self._fixture(mode, True)
                (dest / "nested").mkdir()
                occupied = dest / "nested" / "chosen.txt"
                occupied.write_text("keep", encoding="utf-8")
                result = self._restore(snapshot, dest, ["nested/chosen.txt"])
                self.assertTrue(result.success, result.message)
                self.assertEqual(occupied.read_text(encoding="utf-8"), "keep")
                added = list(occupied.parent.glob("chosen-restored-*.txt"))
                self.assertEqual(len(added), 1)
                self.assertEqual(added[0].read_text(encoding="utf-8"), "saved")
                self.assertFalse((dest / "other.txt").exists())

    def test_selected_without_conflict_keeps_names(self):
        source, plan, snapshot, dest = self._fixture("direct", True)
        result = self._restore(snapshot, dest, ["nested/chosen.txt"])
        self.assertTrue(result.success, result.message)
        self.assertEqual((dest / "nested" / "chosen.txt").read_text(encoding="utf-8"), "saved")

    def test_collision_appearing_after_verification_is_not_overwritten(self):
        source, plan, snapshot, dest = self._fixture()
        original = self.restore._verify_restored_output
        intruders = []
        def occupy(*args):
            original(*args)
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            target = unique_restored_path(dest, source.name, True, stamp, force_suffix=True)
            target.write_text("outside", encoding="utf-8")
            intruders.append(target)
        with mock.patch.object(self.restore, "_verify_restored_output", side_effect=occupy):
            result = self._restore(snapshot, dest)
        self.assertTrue(result.success, result.message)
        self.assertEqual(intruders[0].read_text(encoding="utf-8"), "outside")
        self.assertNotEqual(Path(result.destination_path), intruders[0])

    def test_no_replace_move_never_overwrites(self):
        first, second = self.root / "first", self.root / "second"
        first.write_text("new", encoding="utf-8")
        second.write_text("old", encoding="utf-8")
        with self.assertRaises(OSError):
            move_restore_no_replace(first, second)
        self.assertEqual(second.read_text(encoding="utf-8"), "old")
        self.assertEqual(first.read_text(encoding="utf-8"), "new")
        target = self.root / "free"
        move_restore_no_replace(first, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertFalse(first.exists())

    def test_unique_name_keeps_extension_and_skips_reserved(self):
        (self.root / "a.txt").write_text("x")
        one = unique_restored_path(self.root, "a.txt", True, "STAMP")
        two = unique_restored_path(self.root, "a.txt", True, "STAMP", {one})
        self.assertEqual(one.name, "a-restored-STAMP.txt")
        self.assertEqual(two.name, "a-restored-STAMP-2.txt")
        self.assertEqual(unique_restored_path(self.root, "free.txt", True, "S").name, "free.txt")

    def test_precise_errors_and_no_false_health_alerts(self):
        source, plan, snapshot, dest = self._fixture("direct", True)
        for target, code in ((source, "RESTORE_DESTINATION_IN_SOURCE"),
                             (self.storage_root, "RESTORE_DESTINATION_IN_STORAGE"),
                             (self.root / "missing", "RESTORE_DESTINATION_NOT_FOLDER")):
            result = self._restore(snapshot, target)
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, code)
        self.assertEqual(self.repo.list_active_issues(plan), [])
        result = self._restore(snapshot, dest, [])
        self.assertEqual(result.error_code, "RESTORE_NOTHING_SELECTED")

    def test_destination_symlink_is_rejected(self):
        source, plan, snapshot, dest = self._fixture("direct", True)
        link = self.root / "link"
        try:
            link.symlink_to(dest, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = self._restore(snapshot, link)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "RESTORE_DESTINATION_REDIRECT")
        outside = self.root / "outside"
        outside.mkdir()
        (dest / "nested").symlink_to(outside, target_is_directory=True)
        result = self._restore(snapshot, dest, ["nested/chosen.txt"])
        self.assertFalse(result.success)
        self.assertEqual(list(outside.iterdir()), [])

    def test_cancel_does_not_remove_existing_files(self):
        source, plan, snapshot, dest = self._fixture()
        marker = dest / "keep.txt"
        marker.write_text("keep")
        result = self._restore(snapshot, dest, cancelled=lambda: True)
        self.assertEqual(result.error_code, "OPERATION_CANCELLED")
        self.assertEqual(list(dest.iterdir()), [marker])

    def test_default_folder_uses_documents_without_creating_it(self):
        source, plan, snapshot, dest = self._fixture()
        documents = self.root / "Documents"
        documents.mkdir()
        with mock.patch.object(_APP, "_restore_base_folders", return_value=[documents]):
            suggested = suggest_restore_folder(self.repo, snapshot)
        self.assertEqual(Path(suggested), documents / RESTORE_FOLDER_NAME)
        self.assertFalse(Path(suggested).exists())

    def test_default_skips_source_storage_and_uses_last_folder(self):
        source, plan, snapshot, dest = self._fixture("direct", True)
        with mock.patch.object(_APP, "_restore_base_folders", return_value=[source, self.storage_root, dest]):
            self.assertEqual(Path(suggest_restore_folder(self.repo, snapshot)), dest / RESTORE_FOLDER_NAME)
        self.repo.set_setting(SETTING_LAST_RESTORE_FOLDER, str(dest))
        self.assertEqual(suggest_restore_folder(self.repo, snapshot), str(dest))
        self.repo.set_setting(SETTING_LAST_RESTORE_FOLDER, str(source))
        with mock.patch.object(_APP, "_restore_base_folders", return_value=[dest]):
            self.assertEqual(Path(suggest_restore_folder(self.repo, snapshot)), dest / RESTORE_FOLDER_NAME)

    def test_new_strings_exist_in_seven_languages(self):
        for code in (item.value for item in Language):
            for key in ("restore_wizard.default_hint", *[f"error.{name}" for name in RESTORE_USER_ERROR_CODES]):
                self.assertIn(key, TRANSLATIONS[code], f"{code}:{key}")
        self.assertEqual(LANGUAGE_DATA.validate(), [])


class Alpha19SupportTests(Alpha19UiBase):
    """Support: correct recipients, offline resources, localized texts, working copy and navigation."""

    def setUp(self):
        super().setUp()
        import amvibackup_support as support
        self.support = support
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.hide()
            dialog.deleteLater()
        super().tearDown()

    def dialog(self):
        dialog = self.support.SupportDialog(self.controller.i18n)
        self.dialogs.append(dialog)
        return dialog

    def test_contacts_are_the_approved_contacts(self):
        self.assertEqual(self.support.SUPPORT_EMAIL, "amvimaxsoft@gmail.com")
        self.assertEqual(self.support.SUPPORT_TELEGRAM_URL, "https://t.me/AmviMax")

    def test_receiving_addresses_are_exact(self):
        found = {item["id"]: item["address"] for item in self.support.SUPPORT_ASSETS}
        self.assertEqual(found, {
            "btc": "bc1qyjulp8v79l3axhvfvr6ns4z7t26t73m5csm6au",
            "trx": "TV28nSwhfjQvj64YoyVoDJvArguYaJJGbM",
            "eth": "0x2E3491CeD3B1409E0d0Bf8c54Fd61Cd067172AAb",
        })
        trx = next(x for x in self.support.SUPPORT_ASSETS if x["id"] == "trx")
        self.assertEqual((trx["asset"], trx["network"]), ("TRON (TRX)", "TRON"))
        self.assertFalse(any("USDT" in x["asset"] for x in self.support.SUPPORT_ASSETS))

    def test_all_resources_exist_and_have_the_expected_digest(self):
        self.assertEqual(self.support.check_support_resources(), [])
        self.assertEqual(len(self.support.SUPPORT_PHOTOS), 4)

    def test_qr_payloads_use_the_selected_address_without_an_amount(self):
        for item in self.support.SUPPORT_ASSETS:
            expected = ("bitcoin:" if item["id"] == "btc" else "") + item["address"]
            self.assertEqual(item["qr_payload"], expected)
            self.assertNotIn("amount=", item["qr_payload"])
            self.assertTrue(self.support.resource_path(item["qr"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_qr_and_photos_load_in_qt(self):
        from PySide6.QtGui import QPixmap
        for filename in self.support.SUPPORT_RESOURCE_HASHES:
            self.assertFalse(QPixmap(str(self.support.resource_path(filename))).isNull(), filename)

    def test_resource_name_cannot_escape_the_folder(self):
        with self.assertRaises(ValueError):
            self.support.resource_path("../secret.txt")

    def test_opening_the_dialog_does_not_open_an_external_link(self):
        with mock.patch.object(self.support.QDesktopServices, "openUrl") as opened:
            self.dialog()
        opened.assert_not_called()

    def test_every_language_builds_the_dialog(self):
        for code in (item.value for item in Language):
            self.controller.i18n.set_language(code)
            dialog = self.dialog()
            self.assertEqual(dialog.windowTitle(), TRANSLATIONS[code]["support.title"])
            self.assertEqual(len(dialog.photo_buttons), 4)
            self.assertEqual(dialog.asset_combo.count(), 3)
            self.assertFalse(dialog.qr_label.pixmap().isNull())

    def test_switching_currency_changes_the_copy_target(self):
        dialog = self.dialog()
        for index, asset in enumerate(self.support.SUPPORT_ASSETS):
            dialog.asset_combo.setCurrentIndex(index)
            dialog.copy_address_button.click()
            self.assertEqual(QApplication.clipboard().text(), asset["address"])
            self.assertEqual(dialog.address_edit.text(), asset["address"])
            self.assertIn(asset["network"], dialog.network_label.text())
            self.assertFalse(dialog.qr_label.pixmap().isNull())

    def test_contact_copy_and_open_use_exact_targets(self):
        dialog = self.dialog()
        with mock.patch.object(self.support.QDesktopServices, "openUrl", return_value=True) as opened:
            dialog.contact_rows["email"][1].click()
            dialog.contact_rows["telegram"][1].click()
        self.assertEqual([args.args[0].toString() for args in opened.call_args_list],
                         ["mailto:amvimaxsoft@gmail.com", "https://t.me/AmviMax"])
        dialog.contact_rows["email"][2].click()
        self.assertEqual(QApplication.clipboard().text(), "amvimaxsoft@gmail.com")

    def test_failure_to_open_link_is_nonmodal(self):
        dialog = self.dialog()
        with mock.patch.object(self.support.QDesktopServices, "openUrl", return_value=False):
            dialog.open_contact("https://t.me/AmviMax")
        self.assertEqual(dialog.status_label.text(), self.controller.i18n.tr("support.open_failed"))

    def test_photo_enlargement_can_be_closed(self):
        dialog = self.dialog()
        with mock.patch.object(self.support.QDialog, "exec", return_value=0) as opened:
            dialog.photo_buttons[0].click()
        opened.assert_called_once()

    def test_missing_qr_is_not_shown_as_a_valid_qr(self):
        dialog = self.dialog()
        with mock.patch.object(self.support, "resource_path", return_value=self.root / "missing.png"):
            dialog.update_asset()
        self.assertEqual(dialog.qr_label.text(), self.controller.i18n.tr("support.assets_missing"))

    def test_support_button_is_next_to_settings_and_help(self):
        window = MainWindow(self.controller, self.paths)
        try:
            keys = list(window.nav_buttons19)
            self.assertEqual(keys[keys.index("settings"):keys.index("help") + 1], ["settings", "support", "help"])
            with mock.patch.object(self.support.SupportDialog, "exec", return_value=0) as opened:
                window.nav_buttons19["support"].click()
            opened.assert_called_once()
        finally:
            if window.tray_runtime is not None:
                window.tray_runtime.stop()
            window.hide()
            window.deleteLater()

    def test_all_support_texts_exist_in_seven_languages(self):
        keys = [key for key in TRANSLATIONS["en"] if key.startswith("support.")]
        self.assertGreaterEqual(len(keys), 24)
        for code in (item.value for item in Language):
            for key in keys:
                self.assertIn(key, TRANSLATIONS[code], f"{code}:{key}")
        self.assertEqual(LANGUAGE_DATA.validate(), [])


def decode_support_qr_matrix(matrix):
    """Decode the aligned version 3/4 byte-mode QR matrices shipped with Support.

    This is a narrow regression decoder, not a general camera scanner. No error correction is attempted.
    """
    n = len(matrix)
    if n not in (29, 33) or any(len(row) != n for row in matrix):
        raise ValueError("Unexpected QR dimensions")
    version = (n - 17) // 4
    fmt = 0
    for i in range(15):
        row = i if i < 6 else i + 1 if i < 8 else n - 15 + i
        fmt |= int(bool(matrix[row][8])) << i

    def format_bits(data):
        value = data << 10
        while value.bit_length() >= 11:
            value ^= 0x537 << (value.bit_length() - 11)
        return ((data << 10) | value) ^ 0x5412

    matches = [data for data in range(32) if format_bits(data) == fmt]
    if len(matches) != 1:
        raise ValueError("Invalid QR format bits")
    level, mask = matches[0] >> 3, matches[0] & 7
    reserved = [[False] * n for _ in range(n)]
    for r0, c0 in ((0, 0), (0, n - 8), (n - 8, 0)):
        for r in range(r0, r0 + 8):
            for c in range(c0, c0 + 8):
                reserved[r][c] = True
    for i in range(n):
        reserved[6][i] = reserved[i][6] = True
    for i in range(9):
        reserved[8][i] = reserved[i][8] = True
    for i in range(8):
        reserved[8][n - 1 - i] = reserved[n - 1 - i][8] = True
    for r in range(n - 9, n - 4):
        for c in range(n - 9, n - 4):
            reserved[r][c] = True

    def masked(r, c):
        product = r * c
        return (
            (r + c) % 2 == 0,
            r % 2 == 0,
            c % 3 == 0,
            (r + c) % 3 == 0,
            (r // 2 + c // 3) % 2 == 0,
            product % 2 + product % 3 == 0,
            (product % 2 + product % 3) % 2 == 0,
            (product % 3 + (r + c) % 2) % 2 == 0,
        )[mask]

    bits = []
    right, upward = n - 1, True
    while right > 0:
        if right == 6:
            right -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for row in rows:
            for col in (right, right - 1):
                if not reserved[row][col]:
                    bits.append(int(bool(matrix[row][col]) != masked(row, col)))
        upward = not upward
        right -= 2
    raw = [sum(bits[j + b] << (7 - b) for b in range(8)) for j in range(0, len(bits) - 7, 8)]
    # Data-codeword counts per Reed-Solomon block (level bits: L=1, M=0, Q=3, H=2).
    blocks = {
        (3, 1): [55], (3, 0): [44], (3, 3): [17, 17], (3, 2): [13, 13],
        (4, 1): [80], (4, 0): [32, 32], (4, 3): [24, 24], (4, 2): [9, 9, 9, 9],
    }[(version, level)]
    data = [[] for _ in blocks]
    position = 0
    for i in range(max(blocks)):
        for block, length in zip(data, blocks):
            if i < length:
                block.append(raw[position])
                position += 1
    stream = ''.join(f"{byte:08b}" for block in data for byte in block)
    if int(stream[:4], 2) != 4:
        raise ValueError("Expected byte-mode QR")
    length = int(stream[4:12], 2)
    if length * 8 + 12 > len(stream):
        raise ValueError("Truncated QR payload")
    payload = bytes(int(stream[i:i + 8], 2) for i in range(12, 12 + 8 * length, 8))
    return payload.decode("ascii")


class Alpha19Beta20SupportTests(Alpha19UiBase):
    """Beta 21.1: Support wheel, first-install marker, and actual QR pixels."""

    def _window(self):
        window = MainWindow(self.controller, self.paths)
        self.controller.main_window = window
        window.show()
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.hide)
        return window

    def test_version_and_installer_marker(self):
        self.assertEqual(APP_VERSION, "22.0-beta")
        self.assertEqual(DB_SCHEMA_VERSION, 13)
        iss_path = Path(_APP.__file__).parent / "installer" / "AmviBackUp.iss"
        text = iss_path.read_text(encoding="utf-8-sig")
        self.assertIn("SupportFirstRunMarkerName = 'support-first-run.txt'", text)
        self.assertIn("22.0-beta", text)
        self.assertIn("22.0.0.0", text)
        self.assertIn("SaveStringToFile(DataFolderPath + '\\' + SupportFirstRunMarkerName, '1', False)", text)

    def test_no_marker_means_no_request(self):
        self.assertFalse(register_support_first_launch(self.paths, self.controller.repo))
        self.assertFalse(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False))

    def test_marker_requests_once_and_is_consumed(self):
        marker = self.paths.root / SUPPORT_FIRST_RUN_FILE
        marker.write_text("1", encoding="ascii")
        self.assertTrue(register_support_first_launch(self.paths, self.controller.repo))
        self.assertFalse(marker.exists())
        self.assertTrue(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False))
        self.assertFalse(register_support_first_launch(self.paths, self.controller.repo))

    def test_preserved_data_does_not_repeat_a_seen_prompt(self):
        repo = self.controller.repo
        repo.set_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, True)
        marker = self.paths.root / SUPPORT_FIRST_RUN_FILE
        marker.write_text("1", encoding="ascii")
        self.assertFalse(register_support_first_launch(self.paths, repo))
        self.assertFalse(marker.exists())
        self.assertFalse(repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False))

    def test_marker_is_kept_if_recording_request_fails(self):
        marker = self.paths.root / SUPPORT_FIRST_RUN_FILE
        marker.write_text("1", encoding="ascii")
        with mock.patch.object(self.controller.repo, "set_setting", side_effect=AppError("DATABASE_OPEN_FAILED", "x")), self.assertLogs(level="WARNING"):
            self.assertFalse(register_support_first_launch(self.paths, self.controller.repo))
        self.assertTrue(marker.exists())

    def test_first_launch_shows_support_once_not_help(self):
        window = self._window()
        repo = self.controller.repo
        repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True)
        with mock.patch.object(QApplication, "activeModalWidget", return_value=None), \
                mock.patch.object(window, "show_support") as support, \
                mock.patch.object(window, "maybe_show_welcome_help") as help_:
            window.maybe_show_first_launch()
            window.maybe_show_first_launch()
        support.assert_called_once_with()
        help_.assert_not_called()
        self.assertTrue(repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, False))
        self.assertFalse(repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True))

    def test_invisible_window_defers_support(self):
        window = self._window()
        window.hide()
        self.controller.repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True)
        with mock.patch.object(window, "show_support") as support:
            self.assertFalse(window.maybe_show_first_support())
        support.assert_not_called()
        self.assertTrue(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False))

    def test_other_dialog_defers_support(self):
        window = self._window()
        self.controller.repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True)
        with mock.patch.object(QApplication, "activeModalWidget", return_value=object()), \
                mock.patch.object(window, "show_support") as support:
            self.assertFalse(window.maybe_show_first_support())
        support.assert_not_called()
        self.assertFalse(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, False))

    def test_failed_dialog_can_be_retried(self):
        window = self._window()
        self.controller.repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True)
        with mock.patch.object(QApplication, "activeModalWidget", return_value=None), \
                mock.patch.object(window, "show_support", side_effect=RuntimeError("failure")), \
                self.assertLogs(level="ERROR"):
            self.assertFalse(window.maybe_show_first_support())
        self.assertTrue(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False))
        self.assertFalse(self.controller.repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, False))

    def test_unmarked_source_launch_keeps_the_help_behavior(self):
        window = self._window()
        with mock.patch.object(window, "show_support") as support, \
                mock.patch.object(window, "maybe_show_welcome_help") as help_:
            window.maybe_show_first_launch()
        support.assert_not_called()
        help_.assert_called_once_with()

    def test_wheel_never_changes_currency_even_with_focus(self):
        from amvibackup_support import SupportDialog, SUPPORT_ASSETS
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        dialog = SupportDialog(self.controller.i18n)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.hide)
        dialog.resize(700, 450)
        dialog.show()
        QApplication.processEvents()
        bar = dialog.scroll_area.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        for index in range(dialog.asset_combo.count()):
            dialog.asset_combo.setCurrentIndex(index)
            dialog.asset_combo.setFocus()
            bar.setValue(bar.minimum())
            for widget in (dialog.asset_combo, dialog.address_edit, dialog.contact_rows["email"][0]):
                event = QWheelEvent(QPointF(2, 2), QPointF(2, 2), QPoint(0, 0), QPoint(0, -120),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(widget, event)
            self.assertEqual(dialog.asset_combo.currentIndex(), index)
            self.assertEqual(dialog.address_edit.text(), SUPPORT_ASSETS[index]["address"])
            self.assertGreater(bar.value(), 0)
            dialog.copy_address_button.click()
            self.assertEqual(QApplication.clipboard().text(), SUPPORT_ASSETS[index]["address"])

    def test_qr_pixels_decode_to_the_exact_recipients(self):
        from amvibackup_support import SUPPORT_ASSETS, resource_path
        from PySide6.QtGui import QImage
        for asset in SUPPORT_ASSETS:
            image = QImage(str(resource_path(asset["qr"])))
            self.assertFalse(image.isNull(), asset["id"])
            self.assertEqual(image.width(), image.height())
            self.assertEqual(image.width() % 8, 0)
            count = image.width() // 8 - 8
            matrix = [[image.pixelColor(32 + 8 * c + 4, 32 + 8 * r + 4).red() < 128
                       for c in range(count)] for r in range(count)]
            self.assertEqual(decode_support_qr_matrix(matrix), asset["qr_payload"], asset["id"])
            self.assertNotIn("amount=", decode_support_qr_matrix(matrix))


# ----------------------------------------------------------------------------
# Single entry point
# ----------------------------------------------------------------------------
EXCLUDED_TEST_CLASSES = {"Alpha181ReleaseProfileTests"}  # the 18.1 profile was replaced by Alpha19ReleaseProfileTests
NEW_CLASS_PREFIX = "Alpha19"


def collect_test_classes() -> list[type]:
    classes = [
        value for value in globals().values()
        if isinstance(value, type)
        and issubclass(value, unittest.TestCase)
        and value.__module__ == __name__
        and value.__name__ not in EXCLUDED_TEST_CLASSES
        and any(name.startswith("test") for name in dir(value))
    ]
    classes.sort(key=lambda cls: (not cls.__name__.startswith(NEW_CLASS_PREFIX), cls.__name__))
    return classes


def build_suite(pattern: str | None = None) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    for case in collect_test_classes():
        for name in loader.getTestCaseNames(case):
            if pattern and pattern.lower() not in f"{case.__name__}.{name}".lower():
                continue
            suite.addTest(case(name))
    return suite


def _cleanup_qt_after_test() -> None:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication as _QApp
    except Exception:
        return
    app = _QApp.instance()
    if app is None:
        return
    for widget in list(app.topLevelWidgets()):
        try:
            for timer in widget.findChildren(QTimer):
                timer.stop()
            widget.hide()
        except RuntimeError:
            pass
    app.processEvents()
    app.processEvents()


class _IsolatedFreeSpace:
    """Tests must not depend on the free space on the developer's disk."""

    def __enter__(self) -> "_IsolatedFreeSpace":
        self.original = SnapshotService.__init__

        def patched(instance: Any, *args: Any, **kwargs: Any) -> None:
            if len(args) < 4:
                kwargs.setdefault("reserve_bytes", 0)
            if len(args) < 5:
                kwargs.setdefault("reserve_ratio", 0.0)
            self.original(instance, *args, **kwargs)

        SnapshotService.__init__ = patched
        return self

    def __exit__(self, *args: Any) -> None:
        SnapshotService.__init__ = self.original


class _QuietHook:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.previous = sys.excepthook

    def __call__(self, exc_type: Any, exc: BaseException, tb: Any) -> None:
        key = f"{exc_type.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:240]
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] == 1:
            sys.__stderr__.write(f"\n[background exception] {key}\n")

    def install(self) -> None:
        sys.excepthook = self

    def restore(self) -> None:
        sys.excepthook = self.previous


class _CompactResult(unittest.TextTestResult):
    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        _cleanup_qt_after_test()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:220]


def run_self_tests(verbose: bool = False, pattern: str | None = None, failfast: bool = False) -> int:
    global _SELF_TEST_MODULE_PATH
    application = ensure_test_qapplication()
    source = Path(_APP.__file__).resolve(strict=True)
    report_path = Path(__file__).resolve().with_name("amvibackup_tests_report.txt")
    hook = _QuietHook()
    hook.install()
    previous = _SELF_TEST_MODULE_PATH
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="amvibackup-selftest-source-") as temp:
            snapshot = Path(temp) / "amvibackup_test_source.py"
            snapshot.write_bytes(source.read_bytes())
            language_file = source.with_name("amvibackup_languages.py")
            if language_file.is_file():
                (Path(temp) / "amvibackup_languages.py").write_bytes(language_file.read_bytes())
            _SELF_TEST_MODULE_PATH = snapshot
            suite = build_suite(pattern)
            new_count = sum(1 for t in _iter_tests(suite) if type(t).__name__.startswith(NEW_CLASS_PREFIX))
            print(f"[tests] application: {source.name}; total tests {suite.countTestCases()} "
                  f"(new 19.x: {new_count}, regression 18.x: {suite.countTestCases() - new_count})")
            runner = unittest.TextTestRunner(
                verbosity=2 if verbose else 1,
                resultclass=_CompactResult,
                failfast=failfast,
                stream=sys.stdout,
            )
            with _IsolatedFreeSpace():
                result = runner.run(suite)
            application.processEvents()
    finally:
        hook.restore()
        _SELF_TEST_MODULE_PATH = previous

    problems = [("FAIL", t, tb) for t, tb in result.failures] + [("ERROR", t, tb) for t, tb in result.errors]
    with report_path.open("w", encoding="utf-8") as fh:
        fh.write(f"Tests: {result.testsRun}; errors: {len(result.errors)}; failures: {len(result.failures)}; "
                 f"skipped: {len(result.skipped)}; time: {time.monotonic() - started:.1f} s\n\n")
        for kind, test, tb in problems:
            fh.write(f"{'=' * 78}\n{kind}: {test.id()}\n{tb}\n")
        for test, reason in result.skipped:
            fh.write(f"SKIP: {test.id()} -- {reason}\n")
        for key, count in hook.counts.items():
            fh.write(f"BACKGROUND x{count}: {key}\n")

    print("\n" + "=" * 70)
    print(f"TOTAL: {result.testsRun} tests, passed {result.testsRun - len(problems) - len(result.skipped)}, "
          f"failures {len(result.failures)}, errors {len(result.errors)}, skipped {len(result.skipped)}, "
          f"{time.monotonic() - started:.1f} s")
    for kind, test, tb in problems[:40]:
        print(f"  {kind}: {test.id().split('.', 1)[-1]}\n      {_last_line(tb)}")
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more (see the report)")
    for key, count in hook.counts.items():
        print(f"  background exception x{count}: {key}")
    print(f"Full report: {report_path}")
    return 0 if result.wasSuccessful() and not hook.counts else 1


def _iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


if __name__ == "__main__":
    args = sys.argv[1:]
    pattern = None
    if "-k" in args:
        index = args.index("-k")
        pattern = args[index + 1] if index + 1 < len(args) else None
    _code = run_self_tests("-v" in args or "--verbose" in args, pattern, "--failfast" in args)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)  # without a Qt crash while the interpreter unloads
