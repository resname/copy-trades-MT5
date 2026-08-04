"""Static source checks on scripts/install.ps1 — guards the GUI-uses-pythonw
invariant (no console window) and the branched copytrades.cmd (CLI subcommands
keep their console output). Reads the script text; does not execute it."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


def test_installer_defines_pythonw_venv_path():
    assert '$PyWenv = Join-Path $Venv "Scripts\\pythonw.exe"' in INSTALL_PS1


def test_start_menu_shortcut_targets_pythonw():
    assert "$sc.TargetPath = $PyWenv" in INSTALL_PS1


def test_end_of_install_launches_pythonw():
    assert "Start-Process -FilePath $PyWenv -ArgumentList \"-m\", \"manager\"" in INSTALL_PS1


def test_copytrades_cmd_branches_gui_vs_cli():
    # bare GUI launch (no args) -> pythonw (windowless); args -> python (console)
    assert 'if "%~1"==""' in INSTALL_PS1
    assert '"$PyWenv" -m manager' in INSTALL_PS1
    assert '"$PyVenv" -m manager %*' in INSTALL_PS1