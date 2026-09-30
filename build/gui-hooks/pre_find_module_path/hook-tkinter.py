from pathlib import Path


def pre_find_module_path(hook_api):
    repo_root = Path(__file__).resolve().parents[3]
    portable_python = repo_root / "build-python-3129" / "Lib"
    if (portable_python / "tkinter").is_dir():
        # The portable build Python ships tkinter but PyInstaller cannot probe its
        # Tcl installation until the Tcl/Tk data files are added by our other hook.
        hook_api.search_dirs = [str(portable_python)]
        return

    # Ordinary Python installations can use PyInstaller's Tcl/Tk discovery.
    from PyInstaller import log as logging
    from PyInstaller.utils.hooks import tcl_tk

    if not tcl_tk.tcltk_info.available:
        logging.getLogger(__name__).warning(
            "tkinter is unavailable; install Python with Tcl/Tk support to build the GUI"
        )
        hook_api.search_dirs = []
