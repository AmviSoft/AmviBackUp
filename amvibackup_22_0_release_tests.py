# -*- coding: utf-8 -*-
r"""AmviBackUp 22.0 beta: acceptance tests before a release (file amvibackup_22_0_release_tests.py).

Unlike the main suite, these tests start the REAL background copy thread and walk through the
user's path as a whole: creating a plan, clicking buttons, waiting for the result,
checking the versions table and the files on disk. Any unhandled exception in a button handler
and any "Warning/Error" window the test did not expect count as a failure.

Run (put the file next to amvibackup_22_0.py and close the program itself):

    py .\\amvibackup_22_0_release_tests.py
    py .\\amvibackup_22_0_release_tests.py -k restore     # only tests with this word
    py .\\amvibackup_22_0_release_tests.py --failfast

The result is written to amvibackup_release_report.txt (a list "check -> result").
The test changes nothing outside temporary folders; Windows autostart is replaced by a stub.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

_APP_PATH = Path(os.environ.get("AMVIBACKUP_APP_MODULE", Path(__file__).with_name("amvibackup_22_0.py"))).resolve()
_SPEC = importlib.util.spec_from_file_location("amvibackup_app_release_tests", _APP_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load the application: {_APP_PATH}")
APP = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = APP
_SPEC.loader.exec_module(APP)
for _name, _value in vars(APP).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for all files of the tree."""
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RealAppCase(unittest.TestCase):
    """The application with a real background thread; dialogs are replaced and recorded."""

    TIMEOUT = 45.0
    allow_warnings = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.qapp = ensure_test_qapplication()

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        logs = self.root / "logs"
        logs.mkdir()
        self.paths = AppPaths(self.root, self.root / "release.sqlite3", logs, logs / "release.log")
        self.src_dir = self.root / "src"
        self.src_dir.mkdir()
        self.store_dir = self.root / "storage"
        self.dialogs: list[tuple[str, str, str]] = []
        self.results: list[Any] = []
        self.background: list[Any] = []
        self.hook_errors: list[str] = []
        self.next_directory = ""
        self.question_answer = QMessageBox.StandardButton.Yes
        self._old_hook = sys.excepthook
        sys.excepthook = lambda et, ev, tb: self.hook_errors.append("".join(traceback.format_exception(et, ev, tb)))

        self._patchers: list[Any] = []

        def record(kind: str, answer: Any = None):
            def _handler(parent=None, title="", text="", *args, **kwargs):
                self.dialogs.append((kind, str(title), str(text)))
                return answer
            return _handler

        def patch(target: Any, name: str, value: Any) -> None:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self._patchers.append(patcher)

        def closed_exec(dialog: Any, *args: Any, **kwargs: Any) -> int:
            """A window shown by the test closes immediately (as if the user pressed "Cancel")."""
            dialog.done(0)
            return 0

        patch(QMessageBox, "information", record("information", QMessageBox.StandardButton.Ok))
        patch(QMessageBox, "warning", record("warning", QMessageBox.StandardButton.Ok))
        patch(QMessageBox, "critical", record("critical", QMessageBox.StandardButton.Ok))
        patch(QMessageBox, "question",
              lambda parent=None, title="", text="", *a, **k: (
                  self.dialogs.append(("question", str(title), str(text))) or self.question_answer))
        patch(QMessageBox, "exec", lambda *a, **k: 0)
        patch(QFileDialog, "getExistingDirectory", lambda *a, **k: self.next_directory)
        patch(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
        patch(QDesktopServices, "openUrl", lambda *a, **k: True)
        patch(QMenu, "exec", lambda *a, **k: None)
        patch(QDialog, "exec", closed_exec)

        self.controller = AppController(self.paths, start_worker=True)
        self.controller.autostart_service = SimpleNamespace(
            is_supported=lambda: False, is_enabled=lambda: False,
            enable=lambda: None, disable=lambda: None,
        )
        self.controller.operation_finished.connect(self.results.append)
        self.controller.background_query_finished.connect(self.background.append)
        self.window = MainWindow(self.controller, self.paths)
        self.controller.main_window = self.window
        self.window.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        try:
            try:
                self.controller.shutdown()
            finally:
                try:
                    self.window.close()
                except Exception:
                    pass
                QApplication.processEvents()
        finally:
            for patcher in reversed(self._patchers):
                patcher.stop()
            sys.excepthook = self._old_hook
            self._temp.cleanup()
        problems = list(self.hook_errors)
        bad_kinds = {"critical"} if self.allow_warnings else {"critical", "warning"}
        problems += [f"Unexpected {kind} window: {title} | {text}"
                     for kind, title, text in self.dialogs if kind in bad_kinds]
        if problems:
            self.fail("\n".join(problems))

    # ---- waiting -------------------------------------------------------------------------
    def wait(self, predicate, what: str, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or self.TIMEOUT)
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"Wait timed out: {what}. Windows: {self.dialogs}. Errors: {self.hook_errors}")

    def idle(self) -> None:
        self.wait(lambda: self.controller.active_operation_id is None and not self.window._ui_refresh_pending,
                  "the application became idle")

    def run_op(self, start) -> Any:
        """Starts an operation and waits for its result (OperationResult)."""
        self.idle()
        before = len(self.results)
        start()
        self.wait(lambda: len(self.results) > before, "the operation result")
        self.idle()
        return self.results[-1]

    # ---- data preparation -----------------------------------------------------------------
    def manual_policy(self) -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=False, change_debounce_seconds=DEFAULT_CHANGE_DEBOUNCE_SECONDS,
            min_auto_interval_minutes=DEFAULT_MIN_AUTO_INTERVAL_MINUTES, on_schedule_enabled=False,
            schedule_interval_minutes=DEFAULT_SCHEDULE_INTERVAL_MINUTES, quiet_hours_enabled=False,
            quiet_hours_start_local=None, quiet_hours_end_local=None, manual_allowed_quiet_hours=True)

    def create_plan(self, source: Path, name: str = "Release plan", mode: str = "direct",
                    retention: Any = None, keep: int = 30) -> str:
        plan_id = self.controller.save_plan_with_policy(
            None, name, "", str(source), str(self.store_dir), keep, mode, self.manual_policy(), retention)
        self.window.refresh()
        self.idle()
        return plan_id

    def backup(self, plan_id: str, force: bool = True) -> Any:
        result = self.run_op(lambda: self.controller.queue_backup(plan_id, force))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        return result

    def snapshots(self, plan_id: str) -> list[Any]:
        return self.controller.repo.available_snapshots(plan_id)

    def make_file_plan(self, versions: int = 1, name: str = "report.docx", **options: Any) -> tuple[str, Path, list[bytes]]:
        source = self.src_dir / name
        contents: list[bytes] = []
        plan_id = ""
        for number in range(1, versions + 1):
            data = (f"content of version {number} " * (20 + number)).encode("utf-8")
            source.write_bytes(data)
            contents.append(data)
            if not plan_id:
                plan_id = self.create_plan(source, **options)
            self.backup(plan_id)
        return plan_id, source, contents

    def make_folder_tree(self, name: str = "project") -> Path:
        folder = self.src_dir / name
        (folder / "docs" / "deep").mkdir(parents=True)
        (folder / "a.txt").write_text("alpha " * 50, encoding="utf-8")
        (folder / "docs" / "b.txt").write_text("beta " * 80, encoding="utf-8")
        (folder / "docs" / "deep" / "c.bin").write_bytes(bytes(range(256)) * 40)
        (folder / "unicode имя.txt").write_text("привет " * 30, encoding="utf-8")
        return folder

    def show_plan(self, plan_id: str) -> None:
        self.window.select_plan_by_id(plan_id)
        self.window.show_plan_detail(plan_id)
        QApplication.processEvents()
        self.idle()

    def table_numbers(self) -> list[int]:
        table = self.window.versions_table
        return [int(table.item(row, 1).text()) for row in range(table.rowCount())]

    def row_controls(self, row: int) -> list[Any]:
        holder = self.window.versions_table.cellWidget(row, 8)
        controls: list[Any] = []
        for button in holder.findChildren(QPushButton):
            controls.append(button)
            menu = button.menu()
            if menu is not None:
                controls.extend(action for action in menu.actions() if not action.isSeparator())
        return controls

    def click_row_control(self, row: int, key: str) -> None:
        wanted = self.controller.i18n.tr(key)
        for control in self.row_controls(row):
            if control.text() == wanted:
                if hasattr(control, "trigger"):
                    control.trigger()
                else:
                    control.click()
                return
        self.fail(f"Row {row} has no button '{wanted}' ({key}). Available: {[c.text() for c in self.row_controls(row)]}")

    def click_detail_button(self, key: str) -> None:
        wanted = self.controller.i18n.tr(key)
        for button in self.window.detail_view.findChildren(QPushButton):
            if button.text() == wanted and button.isEnabled():
                button.click()
                return
        self.fail(f"The plan page has no button '{wanted}' ({key})")

    def fill_and_save_editor(self, source: Path, first_backup: bool) -> Any:
        def fake_exec(editor: Any) -> Any:
            editor.source_edit.setText(str(source))
            editor.storage_edit.setText(str(self.store_dir))
            editor.save(first_backup)
            return QDialog.DialogCode.Accepted
        return mock.patch.object(PlanEditor, "exec", fake_exec)


# =============================================================================================
class PlanCreationTests(RealAppCase):
    def test_new_plan_from_window_makes_first_version_without_error_dialogs(self) -> None:
        """Creating a plan with the "Create and run" button: the first version appears, no error windows"""
        source = self.src_dir / "first.docx"
        source.write_bytes(b"first document " * 100)
        with self.fill_and_save_editor(source, True):
            self.window.new_plan()
        plan_id = self.controller.repo.plan_summaries()[0].id
        self.wait(lambda: len(self.snapshots(plan_id)) == 1, "the plan's first version")
        self.idle()
        self.show_plan(plan_id)
        self.assertEqual(self.table_numbers(), [1])

    def test_new_plan_without_first_backup_creates_no_version(self) -> None:
        """Creating a plan with the "Create" button without running: no versions"""
        source = self.src_dir / "later.txt"
        source.write_text("later", encoding="utf-8")
        with self.fill_and_save_editor(source, False):
            self.window.new_plan()
        self.idle()
        summaries = self.controller.repo.plan_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(self.snapshots(summaries[0].id), [])

    def test_default_policy_of_a_new_plan_keeps_every_version(self) -> None:
        """A plan with default settings: five versions in a row on one day all stay"""
        source = self.src_dir / "diary.txt"
        source.write_text("day 0", encoding="utf-8")
        with self.fill_and_save_editor(source, False):
            self.window.new_plan()
        plan_id = self.controller.repo.plan_summaries()[0].id
        for number in range(1, 6):
            source.write_text("day entry " * number, encoding="utf-8")
            self.backup(plan_id)
        self.assertEqual(len(self.snapshots(plan_id)), 5)
        self.show_plan(plan_id)
        self.assertEqual(self.table_numbers(), [5, 4, 3, 2, 1])

    def test_folder_plan_and_zip_plan_can_be_created_from_controller(self) -> None:
        """Plans for a folder: "folder" mode and ZIP mode are created and make a first version"""
        for mode in ("direct", "zip"):
            folder = self.make_folder_tree(f"folder-{mode}")
            plan_id = self.create_plan(folder, name=f"folder-{mode}", mode=mode)
            self.backup(plan_id)
            self.assertEqual(len(self.snapshots(plan_id)), 1, mode)


class VersionListTests(RealAppCase):
    def test_every_version_is_listed_in_count_mode(self) -> None:
        """Versions table: "last N" mode, four versions give four rows in descending order of number"""
        plan_id, _, _ = self.make_file_plan(4)
        self.show_plan(plan_id)
        self.assertEqual(self.table_numbers(), [4, 3, 2, 1])

    def test_every_version_is_listed_in_time_mode(self) -> None:
        """Versions table: "by time" mode, four versions in a day are all visible"""
        plan_id, _, _ = self.make_file_plan(4, retention=RetentionPolicy("time", 30, 7, 4, 6))
        self.show_plan(plan_id)
        self.assertEqual(self.table_numbers(), [4, 3, 2, 1])

    def test_new_version_is_added_to_an_open_table(self) -> None:
        """An open table updates itself after a new version (the row is not replaced)"""
        plan_id, source, _ = self.make_file_plan(2)
        self.show_plan(plan_id)
        self.assertEqual(self.table_numbers(), [2, 1])
        source.write_bytes(b"third version " * 200)
        self.backup(plan_id)
        self.wait(lambda: self.window.versions_table.rowCount() == 3, "the third row in the table")
        self.assertEqual(self.table_numbers(), [3, 2, 1])


class BackupTests(RealAppCase):
    def test_unchanged_file_is_not_saved_again_without_force(self) -> None:
        """A repeat copy of an unchanged file without "forced" does not create a version"""
        plan_id, _, _ = self.make_file_plan(1)
        result = self.run_op(lambda: self.controller.queue_backup(plan_id, False))
        self.assertTrue(result.success)
        self.assertEqual(len(self.snapshots(plan_id)), 1)

    def test_force_creates_a_version_even_without_changes(self) -> None:
        """"Create version now" (forced) creates a version even when the file is unchanged"""
        plan_id, _, _ = self.make_file_plan(1)
        self.backup(plan_id, True)
        self.assertEqual(len(self.snapshots(plan_id)), 2)

    def test_word_style_atomic_save_is_accepted_without_confirmation(self) -> None:
        """A file saved "like Word" (temporary file + replace): the copy is made without confirmations"""
        plan_id, source, _ = self.make_file_plan(1)
        for number in range(3):
            temp = self.src_dir / f"~WRD{number:04d}.tmp"
            temp.write_bytes(f"edited {number} ".encode() * 300)
            os.replace(temp, source)
            self.backup(plan_id)
        self.assertEqual(len(self.snapshots(plan_id)), 4)

    def test_backup_flushes_files_to_disk_before_publishing(self) -> None:
        """A version is flushed to disk (fsync) before publication"""
        source = self.src_dir / "durable.txt"
        source.write_text("payload " * 100, encoding="utf-8")
        plan_id = self.create_plan(source)
        with mock.patch.object(os, "fsync", wraps=os.fsync) as synced:
            self.backup(plan_id)
        self.assertGreaterEqual(synced.call_count, 3)

    def test_source_is_never_modified_by_backup(self) -> None:
        """Copying does not change the source files (same hashes and modification times)"""
        folder = self.make_folder_tree()
        before = tree_hashes(folder)
        mtimes = {p: p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}
        plan_id = self.create_plan(folder)
        self.backup(plan_id)
        self.assertEqual(tree_hashes(folder), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}, mtimes)

    def test_backup_buttons_on_plan_page(self) -> None:
        """Plan page buttons: the backup button and the force-version button"""
        plan_id, _, _ = self.make_file_plan(1)
        self.show_plan(plan_id)
        before = len(self.results)
        self.click_detail_button("home.backup_now")
        self.wait(lambda: len(self.results) > before, "the result of the backup button")
        self.idle()
        before = len(self.results)
        self.show_plan(plan_id)
        self.click_detail_button("action.force_version")
        self.wait(lambda: len(self.results) > before, "the result of the forced version")
        self.idle()
        self.assertEqual(len(self.snapshots(plan_id)), 2)


class RestoreTests(RealAppCase):
    def test_restore_button_restores_exact_content_next_to_source(self) -> None:
        """The "Restore" button: the old version returns next to the file, the original file is intact"""
        plan_id, source, contents = self.make_file_plan(3)
        self.show_plan(plan_id)
        before = file_sha256(source)
        result = self.run_op(lambda: self.click_row_control(2, "snapshot.restore"))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        restored = [p for p in self.src_dir.iterdir() if "restored" in p.name]
        self.assertEqual(len(restored), 1, [p.name for p in self.src_dir.iterdir()])
        self.assertEqual(restored[0].read_bytes(), contents[0])
        self.assertEqual(file_sha256(source), before)

    def test_restore_to_chosen_directory(self) -> None:
        """"Restore to...": the chosen folder gets an exact copy of the version"""
        plan_id, _, contents = self.make_file_plan(2)
        self.show_plan(plan_id)
        target = self.root / "chosen"
        target.mkdir()
        self.next_directory = str(target)
        result = self.run_op(lambda: self.click_row_control(1, "snapshot.restore_to"))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        found = [p for p in target.rglob("*") if p.is_file() and p.read_bytes() == contents[0]]
        self.assertEqual(len(found), 1, [str(p) for p in target.rglob("*")])

    def test_restore_to_directory_for_folder_plan_is_byte_identical(self) -> None:
        """A whole folder (normal mode and ZIP): the restored tree matches by hashes"""
        for mode in ("direct", "zip"):
            with self.subTest(mode=mode):
                folder = self.make_folder_tree(f"tree-{mode}")
                expected = tree_hashes(folder)
                plan_id = self.create_plan(folder, name=f"tree-{mode}", mode=mode)
                self.backup(plan_id)
                target = self.root / f"out-{mode}"
                target.mkdir()
                snapshot = self.snapshots(plan_id)[0]
                result = self.run_op(lambda: self.controller.queue_restore_to_directory(snapshot.id, str(target)))
                self.assertTrue(result.success, f"{result.error_code}: {result.message}")
                roots = list(target.rglob("a.txt"))
                self.assertEqual(len(roots), 1, [str(p) for p in target.rglob("*")])
                self.assertEqual(tree_hashes(roots[0].parent), expected)

    def test_restore_selected_files_only(self) -> None:
        """Restoring selected files: only they appear"""
        folder = self.make_folder_tree()
        plan_id = self.create_plan(folder)
        self.backup(plan_id)
        snapshot = self.snapshots(plan_id)[0]
        target = self.root / "picked"
        target.mkdir()
        result = self.run_op(lambda: self.controller.queue_restore_selected_files(
            snapshot.id, ["docs/b.txt"], str(target)))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        names = sorted(p.name for p in target.rglob("*") if p.is_file())
        self.assertEqual(names, ["b.txt"])

    def test_restore_selected_files_through_the_dialog(self) -> None:
        """The "Select files" window: shows all files of the version, the selected file is restored"""
        folder = self.make_folder_tree()
        plan_id = self.create_plan(folder)
        self.backup(plan_id)
        snapshot = self.snapshots(plan_id)[0]
        target = self.root / "picked-ui"
        target.mkdir()
        seen: dict[str, int] = {}

        def fake_exec(dialog: Any) -> Any:
            seen["rows"] = dialog.files_list.rowCount()
            dialog.selected_relative_paths = ["docs/b.txt"]
            return QDialog.DialogCode.Accepted

        self.idle()
        before = len(self.results)
        with mock.patch.object(RestoreSelectedFilesDialog, "exec", fake_exec):
            self.window.restore_selected_files(snapshot.id, destination=str(target))
            self.wait(lambda: len(self.results) > before, "the result of restoring selected files")
        self.idle()
        self.assertEqual(seen.get("rows"), 4)
        self.assertTrue(self.results[-1].success, f"{self.results[-1].error_code}: {self.results[-1].message}")
        self.assertEqual(sorted(p.name for p in target.rglob("*") if p.is_file()), ["b.txt"])

    def test_replace_original_file_keeps_previous_copy_and_later_backups_work(self) -> None:
        """"Replace the original file": the file is returned to the version, the previous one is kept, copying works afterwards"""
        plan_id, source, contents = self.make_file_plan(3)
        oldest = self.snapshots(plan_id)[-1]
        current = source.read_bytes()
        result = self.run_op(lambda: self.controller.queue_replace_original_file(oldest.id))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        self.assertEqual(source.read_bytes(), contents[0])
        kept = list(self.src_dir.glob("*-before-restore-*"))
        self.assertEqual(len(kept), 1, "the previous version of the file was not saved before the replacement")
        self.assertEqual(kept[0].read_bytes(), current)
        self.backup(plan_id)
        self.assertEqual(len(self.snapshots(plan_id)), 4)


class VersionActionTests(RealAppCase):
    def test_verify_marks_the_version_as_passed(self) -> None:
        """The "Verify" item: the version gets the "verified" mark"""
        plan_id, _, _ = self.make_file_plan(2)
        self.show_plan(plan_id)
        snapshot = self.snapshots(plan_id)[0]
        result = self.run_op(lambda: self.click_row_control(0, "snapshot.verify"))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        self.assertEqual(self.controller.repo.snapshot(snapshot.id).verification_state, "passed")

    def test_verify_latest_button(self) -> None:
        """The "Verify the latest version" button"""
        plan_id, _, _ = self.make_file_plan(1)
        self.show_plan(plan_id)
        snapshot = self.snapshots(plan_id)[0]
        before = len(self.results)
        self.click_detail_button("action.verify_latest")
        self.wait(lambda: len(self.results) > before, "the verification result")
        self.idle()
        self.assertTrue(self.results[-1].success)
        self.assertEqual(self.controller.repo.snapshot(snapshot.id).verification_state, "passed")

    def test_corrupted_artifact_is_detected_by_verify(self) -> None:
        """Tampering with a file in the storage is detected by the version check"""
        self.allow_warnings = True
        plan_id, _, _ = self.make_file_plan(1)
        snapshot = self.snapshots(plan_id)[0]
        stored = [p for p in self.store_dir.rglob("report.docx") if p.is_file()]
        self.assertEqual(len(stored), 1, [str(p) for p in self.store_dir.rglob("*")])
        os.chmod(stored[0], 0o666)
        stored[0].write_bytes(b"tampered")
        result = self.run_op(lambda: self.controller.queue_verify(snapshot.id))
        self.assertFalse(result.success)
        self.assertNotEqual(self.controller.repo.snapshot(snapshot.id).verification_state, "passed")

    def test_pin_and_unpin_from_the_menu(self) -> None:
        """The "Pin / Unpin" items change the version mark"""
        plan_id, _, _ = self.make_file_plan(2)
        self.show_plan(plan_id)
        target = self.snapshots(plan_id)[1]
        self.click_row_control(1, "snapshot.pin")
        self.idle()
        self.assertTrue(self.controller.repo.snapshot(target.id).pinned)
        self.show_plan(plan_id)
        self.click_row_control(1, "snapshot.unpin")
        self.idle()
        self.assertFalse(self.controller.repo.snapshot(target.id).pinned)

    def test_delete_selected_old_versions_keeps_the_latest(self) -> None:
        """Selecting versions with check boxes and "Delete": old ones are deleted, the latest stays"""
        plan_id, _, _ = self.make_file_plan(3)
        self.show_plan(plan_id)
        table = self.window.versions_table
        for row in (1, 2):
            table.cellWidget(row, 0).findChild(QCheckBox).setChecked(True)
        before = len(self.results)
        self.window.delete_selected()
        self.wait(lambda: len(self.results) > before, "the deletion result")
        self.idle()
        self.assertEqual([s.sequence_no for s in self.snapshots(plan_id)], [3])

    def test_last_version_cannot_be_deleted(self) -> None:
        """The only (latest) version cannot be deleted"""
        self.allow_warnings = True
        plan_id, _, _ = self.make_file_plan(1)
        snapshot = self.snapshots(plan_id)[0]
        try:
            result = self.run_op(lambda: self.controller.queue_delete([snapshot.id]))
            self.assertFalse(result.success)
        except AppError:
            pass
        self.assertEqual(len(self.snapshots(plan_id)), 1)

    def test_pinned_version_is_not_deleted(self) -> None:
        """A pinned version cannot be deleted"""
        self.allow_warnings = True
        plan_id, _, _ = self.make_file_plan(2)
        old = self.snapshots(plan_id)[1]
        self.controller.set_pin(old.id, True)
        try:
            result = self.run_op(lambda: self.controller.queue_delete([old.id]))
            self.assertFalse(result.success)
        except AppError:
            pass
        self.assertEqual(len(self.snapshots(plan_id)), 2)

    def test_test_restore_from_the_menu(self) -> None:
        """The "Test restore" item finishes successfully"""
        plan_id, _, _ = self.make_file_plan(1)
        self.show_plan(plan_id)
        result = self.run_op(lambda: self.click_row_control(0, "recovery.test_button"))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")


class PlanLifecycleTests(RealAppCase):
    def test_archive_button_stops_new_versions(self) -> None:
        """The "Archive" button: the plan is archived, no new versions are created"""
        self.allow_warnings = True
        plan_id, _, _ = self.make_file_plan(1)
        self.show_plan(plan_id)
        self.click_detail_button("action.archive")
        self.idle()
        self.assertEqual(self.controller.repo.plan_details(plan_id).state, PlanState.ARCHIVED.value)
        refused = False
        try:
            result = self.run_op(lambda: self.controller.queue_backup(plan_id, True))
            refused = not result.success
        except AppError:
            refused = True
        self.assertTrue(refused, "an archived plan created a version")
        self.assertEqual(len(self.snapshots(plan_id)), 1)

    def test_delete_plan_data_removes_storage_but_never_the_source(self) -> None:
        """Deleting a plan and its data: the storage is cleaned, the source file stays in place"""
        plan_id, source, contents = self.make_file_plan(2)
        preview = self.controller.plan_deletion_preview(plan_id)
        result = self.run_op(lambda: self.controller.queue_delete_plan_data(plan_id, preview.token, False))
        self.assertTrue(result.success, f"{result.error_code}: {result.message}")
        self.assertEqual(source.read_bytes(), contents[-1])
        self.assertEqual([s.id for s in self.controller.repo.plan_summaries()], [])
        leftovers = [p for p in self.store_dir.rglob("report.docx") if p.is_file()]
        self.assertEqual(leftovers, [])

    def test_retention_count_mode_removes_only_the_oldest(self) -> None:
        """Keep 2 versions: after four copies the last two remain"""
        plan_id, _, _ = self.make_file_plan(4, keep=2)
        self.assertEqual(sorted(s.sequence_no for s in self.snapshots(plan_id)), [3, 4])

    def test_retention_time_mode_keeps_the_newest_versions(self) -> None:
        """By time, keep 3: after five copies in a day the last three remain"""
        plan_id, _, _ = self.make_file_plan(5, retention=RetentionPolicy("time", 3, 7, 4, 6), keep=3)
        self.assertEqual(sorted(s.sequence_no for s in self.snapshots(plan_id)), [3, 4, 5])

    def test_database_check_operation(self) -> None:
        """The database check finishes successfully"""
        self.idle()
        before = len(self.background)
        self.controller.queue_database_check()
        self.wait(lambda: len(self.background) > before, "the database check result")
        self.assertTrue(self.background[-1].success, self.background[-1].message)


class SettingsAndLanguageTests(RealAppCase):
    def test_settings_dialog_saves_every_switch(self) -> None:
        """The settings window: switches are saved to the database and take effect"""
        dialog = SettingsDialog(self.controller, self.paths, self.window)
        try:
            dialog.close_to_tray_check.setChecked(False)
            dialog.start_minimized_check.setChecked(False)
            dialog.notifications_enabled_check.setChecked(False)
            dialog.save()
            QApplication.processEvents()
            self.assertFalse(self.window.close_to_tray_enabled())
            self.assertFalse(self.controller.repo.get_bool_setting(SETTING_CLOSE_TO_TRAY, True))
        finally:
            dialog.deleteLater()

    def test_every_interface_language_applies_without_errors(self) -> None:
        """Switching to each of the 7 languages: the window rebuilds without errors"""
        original = self.controller.i18n.language
        try:
            for code in [item.value for item in Language]:
                dialog = SettingsDialog(self.controller, self.paths, self.window)
                try:
                    dialog.language_combo.setCurrentIndex(max(0, dialog.language_combo.findData(code)))
                    dialog.save()
                    QApplication.processEvents()
                    self.assertEqual(self.controller.i18n.language, code)
                    self.assertTrue(self.window.windowTitle())
                finally:
                    dialog.deleteLater()
        finally:
            self.controller.i18n.set_language(original)
            self.window.apply_language()
            QApplication.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

    def test_support_resources_are_intact(self) -> None:
        """The support window files (QR codes, photos) are in place and unchanged"""
        from amvibackup_support import check_support_resources
        self.assertEqual(check_support_resources(), [])


class TrayAndExitTests(RealAppCase):
    def test_closing_to_tray_hides_the_window_and_explicit_exit_ends_the_program(self) -> None:
        """Exit from a hidden window after startup queries finish requests application quit."""
        self.idle()
        self.assertIsNone(self.controller.active_operation_id)
        hidden: list[bool] = []
        self.window.tray_runtime = SimpleNamespace(
            tray=object(), hide_window=lambda: (hidden.append(True), self.window.hide()),
            stop=lambda: None, exit_action=None)
        self.controller.repo.set_setting(SETTING_CLOSE_TO_TRAY, True)
        self.window.close()
        QApplication.processEvents()
        self.assertTrue(hidden)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.controller._shutdown_requested)
        self.idle()
        self.assertIsNone(self.controller.active_operation_id)
        with mock.patch.object(self.window, "_schedule_application_quit") as quit_call:
            self.window.request_explicit_exit()
            self.wait(lambda: self.controller._shutdown_completed, "shutdown completion")
            self.wait(lambda: quit_call.called, "quit request for the hidden window")
            self.assertFalse(self.window.isVisible())
        self.window.show()

    def test_second_launch_of_another_version_is_reported(self) -> None:
        """Second launch of another version: a clear warning with the version numbers"""
        text = other_version_message("20.0.0.0-beta")
        self.assertIn("20.0.0.0-beta", text)
        self.assertIn(APP_VERSION, text)


class ButtonSweepTests(RealAppCase):
    """Clicks EVERY available button and checks that there are no unhandled exceptions."""

    allow_warnings = True
    SKIP_KEYS = ("tray.exit", "settings.uninstall_button")

    def idle_quietly(self) -> None:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if self.controller.active_operation_id is None and not self.window._ui_refresh_pending:
                return
            time.sleep(0.01)

    def sweep(self, root: Any, label: str) -> int:
        skip_texts = {self.controller.i18n.tr(key) for key in self.SKIP_KEYS if self.controller.i18n.has_key(key)}
        clicked = 0
        self.question_answer = QMessageBox.StandardButton.No
        for button in list(root.findChildren(QPushButton)):
            try:
                if not button.isEnabled() or not button.isVisibleTo(root) or button.menu() is not None:
                    continue
                if button.text() in skip_texts:
                    continue
                title = button.text()
                before = len(self.hook_errors)
                button.click()
            except RuntimeError:
                continue
            QApplication.processEvents()
            self.idle_quietly()
            self.assertEqual(self.hook_errors[before:], [], f"{label}: exception after clicking the button '{title}'")
            clicked += 1
        return clicked

    def test_main_window_without_plans(self) -> None:
        """All buttons of the main window without plans"""
        self.assertGreater(self.sweep(self.window, "empty window"), 0)

    def test_main_window_with_plan_and_versions(self) -> None:
        """All buttons of the plan page with versions (dangerous confirmations are declined)"""
        plan_id, source, contents = self.make_file_plan(3)
        self.show_plan(plan_id)
        self.assertGreater(self.sweep(self.window.detail_view, "plan page"), 0)
        self.assertGreaterEqual(len(self.snapshots(plan_id)), 3)
        self.assertEqual(source.read_bytes(), contents[-1])

    def test_all_dialogs(self) -> None:
        """All buttons of the windows: settings, health center, help, plan editor"""
        plan_id, _, _ = self.make_file_plan(1)
        details = self.controller.repo.plan_details(plan_id)
        dialogs = [
            SettingsDialog(self.controller, self.paths, self.window),
            HealthCenterDialog(self.controller, self.window),
            HelpDialog(self.controller, self.window),
            PlanEditor(self.controller, None, self.window),
            PlanEditor(self.controller, details, self.window),
        ]
        total = 0
        for dialog in dialogs:
            try:
                dialog.show()
                QApplication.processEvents()
                total += self.sweep(dialog, type(dialog).__name__)
            finally:
                dialog.close()
                dialog.deleteLater()
        self.assertGreater(total, 5)


# =============================================================================================
class _ReportResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rows: list[tuple[str, str, str]] = []

    @staticmethod
    def _title(test: unittest.TestCase) -> str:
        doc = (test._testMethodDoc or "").strip().splitlines()
        return doc[0] if doc else test.id().rsplit(".", 1)[-1]

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self.rows.append(("OK", self._title(test), ""))

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self.rows.append(("FAIL", self._title(test), str(err[1]).strip()[:700]))

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self.rows.append(("ERROR", self._title(test), "".join(traceback.format_exception(*err))[-1000:]))


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", dest="keyword", default="")
    parser.add_argument("--failfast", action="store_true")
    args = parser.parse_args()
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    for case in (PlanCreationTests, VersionListTests, BackupTests, RestoreTests, VersionActionTests,
                 PlanLifecycleTests, SettingsAndLanguageTests, TrayAndExitTests, ButtonSweepTests):
        for test in loader.loadTestsFromTestCase(case):
            haystack = (test.id() + " " + (test._testMethodDoc or "")).lower()
            if not args.keyword or args.keyword.lower() in haystack:
                suite.addTest(test)
    runner = unittest.TextTestRunner(verbosity=1, resultclass=_ReportResult, failfast=args.failfast)
    result = runner.run(suite)
    lines = [f"AmviBackUp {APP_VERSION}: release acceptance tests, total {result.testsRun}, "
             f"failures {len(result.failures)}, errors {len(result.errors)}", ""]
    for status, title, detail in result.rows:
        lines.append(f"[{status}] {title}")
        if detail:
            lines.append("      " + detail.replace("\n", "\n      "))
    report = Path(__file__).with_name("amvibackup_release_report.txt")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nReport saved: {report}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
