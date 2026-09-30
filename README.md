# Xbox Game Prep Tool

A portable Windows tool that detects supported Xbox games, prepares each game,
and moves it to the correct location on a locally mounted drive used with
Aurora. It handles Xbox 360 disc images, Xbox Live Arcade packages, Games on
Demand, downloadable content, and original Xbox images. It does not format,
partition, erase, or otherwise prepare the drive itself, and it does not use
FTP.

## Run it

Open `dist\XboxGamePrepTool\XboxGamePrepTool.exe` or use
`Start Xbox Game Prep Tool.cmd` in the same folder. Choose one game or a folder
containing multiple games. The GUI lists each detected game with a native
checkbox, type, and estimated output size. Select or clear individual games; the
header checkbox selects or clears all ready games and shows a mixed state for
partial selections. A compact **Clear selection** link sits beside the selected
size total beneath the list. Choose the locally mounted Xbox
360 storage drive, review the summary, then select **Prepare & Move**. Before
unpacking each selected input, the app compares its estimated output size with
the destination's current free space and skips inputs that cannot fit. It
checks the exact prepared size again before copying. Each game reports its own
checking, preparation, transfer, verification, completion, or error status.
Select **Activity history…** beside the compact activity area (or from the
Tools menu) for a readable timeline of major steps and errors. The game list
shows a short failure reason such as **Not enough space** or **File conflict**;
select that status for more detail. The storage section compares the selected
size with the drive's free space. It warns about over-selection but still lets
you proceed, checking each game before unpacking.

Choose **Options → Dark mode** to switch between light and dark appearances
instantly, including the game list, activity history, and end-of-run summary.
The same toggle is available in the Options dialog. Appearance changes do not
interrupt a transfer or clear your selections. The preference is saved in
`gui-settings.json` beside the application and remembered on the next launch.
Windows-managed file pickers and system message boxes use the Windows theme.

Every run saves a plain-text report in `Reports` and a detailed diagnostic log
in `Logs`. A cancelled run also produces both files. Cancellation stops the
active helper safely; already completed files remain in place and temporary
partial copies are never promoted to final files.

The original command-line interface remains available in
`dist\XboxHDDPrep\Start Xbox HDD Prep.cmd`. It first asks for the source and
destination directories. Press Enter at either prompt to accept the defaults:
`D:\Xbox360Staging` for the source and `E:\` for the destination. It then lists
detected inputs; select a number, several comma-separated numbers, or `A` for
all.

The portable `dist\XboxHDDPrep` folder includes the application, extractors,
and content verifier. Keep the entire folder together. Python, 7-Zip, and
other tools do not need to be installed by the user.

For a different location or an unattended run, call `XboxHDDPrep.exe` directly:

```text
XboxHDDPrep.exe --source "D:\Xbox360Staging" --destination E:\ --all
XboxHDDPrep.exe --list
XboxHDDPrep.exe --help
```

`--list` only inventories inputs. `--all` starts a complete run immediately.
`--work-dir` changes where temporary unpacked data is held. The work area must
be on a drive other than the output drive, with room for one unpacked archive
and one extracted disc at a time. `--skip-stfs-integrity` is a troubleshooting
and compatibility switch that disables deep STFS package checking for that
run. Leave it off for normal use. Header routing checks and the final SHA-256
copy verification still run when the switch is used, but the report will not
claim that the package received deep integrity validation.

## Supported inputs and output

| Input | Output on the selected drive |
| --- | --- |
| `.zip`, `.rar`, `.7z`, including common split archive first volumes | Unpacked, inspected, then routed by contents |
| Xbox 360 `.iso` | `Games\Xbox 360\<game>\default.xex` and its files |
| Original Xbox `.iso` or `.xiso.iso` | `Games\Xbox Original\<game>\default.xbe` and its files |
| Folder with `default.xex` or `default.xbe` | Same game folders as above |
| Xbox `LIVE`, `PIRS`, or `CON` content package; existing Content tree | `Content\0000000000000000\<TitleID>\<ContentType>\...` |

XBLA uses content type `000D0000`; Games on Demand uses `00007000`;
installable content/DLC uses `00000002`; and game demos use `00080000`. The
folder names surrounding a package on a source disc are treated as hints, not
as its final location. The app reads the package's internal STFS metadata and
routes it to the canonical path for the detected Title ID and content type:

```text
Content\0000000000000000\<detected Title ID>\<detected content type>\...
```

This handles compilation discs whose packages sit beneath the compilation's
Title ID, DLC placed in an XBLA folder, and stock installer placeholders such
as `FFED2000\FFFFFFFF`. The placeholder path itself is never copied to the
destination. The app preserves each package together with its matching `.data`
companion directory. It does not unlock, patch, or change licences.

If more than one source bundle maps to the same canonical destination, the app
compares the complete bundle: the main package and every file in its `.data`
directory. Byte-for-byte identical bundles are safely deduplicated using
SHA-256. A different package, a missing companion file, or different companion
data is reported as a conflict before transfer; files from inconsistent bundles
are never silently combined.

Forza Motorsport 3 Ultimate Collection Disc 2 is treated as install content:
its `Content\0000000000000000\4D53084D\00000002` files go to the drive's
Content tree, while Disc 1 is the playable game folder. Stock installer discs
whose content is entirely beneath `FFED2000\FFFFFFFF` are also treated as
installer-only: their packages are routed by internal metadata without adding
the installer program as a playable game. Compilation discs are different.
Their packages are routed separately to the Content tree, while their launcher
and other non-Content files remain together under `Games` so the compilation
menu can still be launched.

The default source scan skips `XboxHDDReady` and the old FTP transfer project.
If a loose ISO and a same-named archive are both present, the archive is hidden
from the selection to avoid duplicate work. Source files are always retained.
One archive nested inside another is handled automatically up to three levels.
Mixed collections containing both disc images and separate game packages stop
with an explicit error so nothing is silently left out.

Before unpacking an input, the app takes its name without the archive or ISO
extension and checks for a folder with that exact name in `Games\Xbox 360` or
`Games\Xbox Original` on the destination. If the folder exists, it skips the
entire input immediately. It does not inspect the archive, check any files in
that folder, or read older verification records. The final summary counts
these skips separately from transferred games. Xbox content packages without
a same-named game folder are prepared normally.

## Verification and failures

The app extracts each ISO in full and compares every listed file and size with
the extracted result, including files beneath `$SystemUpdate`. Only after that
complete extraction has passed verification does it omit `$SystemUpdate` from
the destination copy plan. This verifies the whole source image without placing
an old dashboard update on the Xbox drive.

Before an STFS package is transferred, the bundled `stfschk` verifier checks
Volume Type 0 packages more deeply than a header check. It verifies the metadata
hash, hash tables and data blocks, directory structure and block chains, and
detects missing blocks or truncation. A failed retail/header signature is
advisory rather than proof of damaged data: intentionally unlocked content for
an RGH console can have a changed or unknown signature while all hashes and
files remain valid. The warning is recorded, but the package can continue when
the structural and data checks pass.

`stfschk` does not support SVOD packages, which are commonly used by Games on
Demand. When the package header identifies an SVOD volume, the app records that
deep validation was not applicable and continues with its normal header checks,
canonical routing, and transfer SHA-256 verification. It does not claim to have
checked SVOD data hashes, and it does not label the package corrupt for this
limitation. A missing, stalled, crashed, or unparseable verifier is reported as
an application/tool problem rather than evidence of a bad source file.

For each game, the app writes all new output files to temporary siblings on the
destination drive first. It then checks the length and SHA-256 hash of every
file in that game, including any existing destination files. Only after the
whole batch passes verification are new files renamed to their final names.
A mismatch is never overwritten. If one game fails
extraction, copying, or verification, the app records its error and continues
with the next selected game. The final report lists every failed game and its
error; the run exits with a nonzero status if any game failed. A matching game
folder is always skipped without checking its contents, even if it contains a
partial earlier transfer.

Every user run, including list-only runs, cancellations, and early setup
failures, creates two matching troubleshooting artifacts beside the app:

- `Reports\...-report.txt` is the first file to read. It includes a quick source
  inventory, run totals, every failed input, and a plain-language assessment.
- `Logs\...-diagnostic.jsonl` contains timestamped structured events for a human
  or AI: application and tool details, run settings, processing stages,
  extractor output tails, STFS expected and detected metadata, preparation-plan
  details, transfer results, SHA-256 hashes, stable error codes, and exception
  context.

Failures labelled `SOURCE FILE REJECTED` mean the input was demonstrably
damaged, incomplete, unsafe, internally inconsistent, or unsupported; obtaining
a different complete dump/release is normally the next step. Failures labelled
`PREPARATION / TRANSFER / APPLICATION PROBLEM` do **not** prove the source is
bad. Check the work and destination drives, free space, filesystem, connection,
and detailed log before replacing that source. This distinction is assigned at
the point of failure rather than guessed from the final error wording.

If power is lost during a file copy, its temporary `.part` file is removed when
that file is retried.

The default no-progress timeout is ten minutes. Change it with
`--idle-timeout SECONDS` if a very slow drive needs more time. Progress and a
heartbeat appear about every ten seconds.

The current E: drive is FAT32. FAT32 cannot store one file larger than
4 GiB minus one byte. The app checks each prepared output file and stops before
copying a file that exceeds that limit; it will not silently split or truncate
one. Another format or a separate conversion workflow would be needed for
such a game.

## On the Xbox

This E: drive is an external USB drive, so Aurora will see it as a USB volume
such as `Usb0:` or `Usb1:`, not as `Hdd1:`. Add that volume's `Games` folder as
an Aurora scan path, with enough depth to reach each `default.xex` or
`default.xbe`. Add its `Content` folder for XBLA/Games on Demand packages if
Aurora does not discover them automatically. Original Xbox games also require
the console's backward-compatibility setup; copying files alone does not add it.
Digital content still needs a valid licence for the console or profile.

## Included tools and sources

Xbox HDD Prep itself is released under the [MIT License](LICENSE). Bundled
third-party tools remain subject to their own licences listed below.

- [XboxDev extract-xiso](https://github.com/XboxDev/extract-xiso) is bundled
  with its licence in `tools\extract-xiso-LICENSE.txt`.
- [7-Zip](https://www.7-zip.org/) is bundled with its licence in
  `tools\7z-LICENSE.txt`. Its unRAR component carries the licence restriction
  in that file.
- [stfschk](https://github.com/emoose/xbox-reversing/tree/master/stfschk),
  Copyright (c) 2019 emoose, is bundled under the BSD 3-Clause License. The
  complete licence is in `tools\stfschk-LICENSE.txt`.
- [Aurora game backup layout](https://consolemods.org/wiki/Xbox_360:Playing_Game_Backups)
  and [Aurora scan paths](https://consolemods.org/wiki/Xbox_360:Aurora) describe
  the console-side folders.
- [FAT32 file limits](https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison)
  are documented by Microsoft.

The game detection and STFS header offsets were adapted from the user's
`Xbox-Aurora-Transfer-v1.1.0` source. The local copy and verification engine
is separate from its FTP code.
