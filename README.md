# AmviBackUp

Local backups. File versions. Flexible restore.

AmviBackUp is a free Windows desktop application for backing up files and folders with version history. Create versions manually, when files change or on a schedule. Save copies as regular folders or ZIP archives, verify saved versions using SHA-256, and restore whole versions or selected files.

## Download

Open this repository's **Releases** section and select **AmviBackUp 22.0 Beta**.
Download **AmviBackUp-Setup-21.1-beta.exe** and its matching **.sha256** file.
Do not download the source archives if you only want to install the application.

- Windows 10 / Windows 11, x64
- Installation for the current user; administrator rights are not required
- Python is not required to run the installed application
- Interface: English, Russian, Chinese, Hindi, Spanish, French, Arabic

## Features

- Manual, change-triggered and scheduled backups
- File and folder version history
- Latest-N retention or calendar-based retention with a protected set of recent versions
- Pinned versions protected from automatic cleanup
- Regular-folder and ZIP storage modes
- SHA-256 verification of saved versions
- Whole-version and selected-file restore
- Restore to another folder or replace an original file while keeping its current content in a separate copy
- Test restore and plan health information
- Notification-area operation and optional startup with Windows

## Beta limitations

A locked or unreadable file can stop a folder backup. Symbolic links and junctions inside source folders are skipped. UNC network paths are not supported. Some Hindi installer system messages may appear in English.

This is beta software. Keep an additional independent backup of important data.

## Security notice

The installer is not digitally signed. Windows SmartScreen may show a warning.
Use the official release page and compare the downloaded installer's SHA-256 with the value published there. A matching checksum confirms file integrity relative to that release; it is not an independent security certification.

## Run from source

Install Python and the dependencies listed in requirements.txt:

    py -m pip install -r requirements.txt
    py amvibackup_22_0.py

## Tests

    py amvibackup_22_0.py --self-test
    py amvibackup_22_0_release_tests.py

Tests require the project's support assets and installer source files to be present.

## Build the Windows installer

Install PyInstaller and Inno Setup 6.3 or newer. Python 3.13 was used for this release's Windows build.

    py -m pip install pyinstaller
    py installer\build_installer.py --app amvibackup_22_0.py

Output: installer/output.

## Feedback

Use this repository's Issues tab. Include the application version, Windows version and steps to reproduce. Review logs before posting them; they may contain personal file paths. Developer contacts are also available in the application's Support window.

## Support the author

Donations are optional and do not unlock features. Donation options are available in the application's Support window.

## License

Copyright (c) 2026 Maxim Dubkov (AmviSoft).

Source code: MIT License, see LICENSE. Separate terms apply to bundled project assets; see ASSETS_LICENSE.md. Third-party notices are in legal/THIRD_PARTY_NOTICES.txt.