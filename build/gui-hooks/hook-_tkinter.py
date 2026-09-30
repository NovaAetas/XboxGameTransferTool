from pathlib import Path


repo_root = Path(__file__).resolve().parents[2]
python_tcl_root = repo_root / "build-python-3129" / "tcl"

if (python_tcl_root / "tcl8.6").is_dir() and (python_tcl_root / "tk8.6").is_dir():
    # Use the checked-in portable build environment when available. PyInstaller
    # cannot probe its Tcl runtime until the bundled data are added here.
    datas = [
        (str(python_tcl_root / "tcl8.6"), "_tcl_data"),
        (str(python_tcl_root / "tk8.6"), "_tk_data"),
    ]
else:
    # A fresh clone built with a regular Tcl/Tk-enabled Python uses PyInstaller's
    # normal TclTkInfo discovery and collects the matching runtime data.
    from PyInstaller.utils.hooks.tcl_tk import tcltk_info

    if not tcltk_info.available:
        raise SystemExit("Python Tcl/Tk support is required to build Xbox Game Prep Tool")
    datas = list(tcltk_info.data_files)
