# Xbox Game Prep Tool

Portable Windows tool for preparing Xbox 360, original Xbox, and XBLA content for an Aurora-ready USB drive.

It detects supported inputs, checks available space before preparing them, places content in the expected folders, and verifies the transfer.

![Xbox Game Prep Tool](https://github.com/user-attachments/assets/40770418-f785-42f9-b532-890ee2b07410)

## Supported inputs

- XBLA and other supported Xbox content packages
- Xbox 360 and original Xbox disc images (`.iso`)
- Extracted Xbox 360 and original Xbox game folders
- Xbox HDD-ready and Xbox content folder trees
- `.7z`, `.zip`, and `.rar` archives containing supported content

## Download and run

1. Download the portable ZIP from [Releases](https://github.com/NovaAetas/XboxHDDPrepTool/releases).
2. Extract the entire folder to a writable location.
3. Open `XboxGamePrepTool.exe`, choose the source and destination, select the games, then choose **Prepare & move**.

No installation or FTP is required. The app does not format drives. Logs, reports, and settings are saved beside the executable.

![Game selection and transfer](https://github.com/user-attachments/assets/aad3c4bf-8809-4242-bda4-d81ede647f90)

[MIT license](LICENSE). License notices for bundled utilities are included in `_internal/tools` in the portable download.
