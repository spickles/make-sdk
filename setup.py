#!/usr/bin/env python3
"""
setup.py — Install make.py v2.0 (Cradlepoint NCOS SDK Tool)

What this does:
  1. Installs Python dependencies (paramiko, requests)
  2. Optionally adds 'make-sdk' to your system PATH so you can run it from anywhere

Usage:
  python3 setup.py           # Install deps + offer PATH setup
  python3 setup.py --deps    # Only install dependencies
  python3 setup.py --path    # Only do the PATH setup
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MAKE_PY = SCRIPT_DIR / 'make.py'
COMMAND_NAME = 'make-sdk'


def install_deps():
    """Install Python dependencies."""
    print('Installing dependencies...')
    req_file = SCRIPT_DIR / 'requirements.txt'
    if req_file.exists():
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', str(req_file)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print('  Dependencies installed successfully.')
        else:
            print('  pip install output:')
            print(result.stdout)
            if result.stderr:
                print(result.stderr)
            if result.returncode != 0:
                print('  WARNING: pip exited with errors. You may need to install manually:')
                print('    pip install paramiko requests')
    else:
        print('  requirements.txt not found, installing directly...')
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'paramiko', 'requests'])

    # Verify
    print('  Verifying...')
    try:
        import paramiko  # noqa: F401
        import requests  # noqa: F401
        print('  OK: paramiko and requests are importable.')
    except ImportError as e:
        print(f'  ERROR: {e}')
        print('  Please install manually: pip install paramiko requests')
        return False
    return True


def setup_path_unix():
    """Set up PATH access on macOS/Linux."""
    print()
    print(f'This will make "{COMMAND_NAME}" available from any terminal.')
    print()

    # Determine target directory
    local_bin = Path.home() / '.local' / 'bin'
    system_bin = Path('/usr/local/bin')

    # Prefer ~/.local/bin (no sudo needed), create if necessary
    if local_bin.exists() or not system_bin.exists():
        target_dir = local_bin
        needs_sudo = False
    else:
        target_dir = system_bin
        needs_sudo = True

    # Check if ~/.local/bin is on PATH
    path_dirs = os.environ.get('PATH', '').split(':')
    local_bin_on_path = str(local_bin) in path_dirs

    print(f'  Target: {target_dir / COMMAND_NAME}')
    if needs_sudo:
        print('  Note: This location requires sudo.')
    print()

    confirm = input('  Proceed? (yes/no): ').strip().lower()
    if confirm not in ('yes', 'y'):
        print('  Skipped. You can always run it directly:')
        print(f'    python3 {MAKE_PY}')
        return

    # Create the wrapper script (not a symlink — works even if this folder moves)
    target_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = target_dir / COMMAND_NAME
    wrapper_content = f'''#!/usr/bin/env python3
"""Wrapper for make.py v2.0 — installed by setup.py"""
import sys
import runpy
sys.argv[0] = "{COMMAND_NAME}"
# Point to the actual make.py location
sys.path.insert(0, r"{SCRIPT_DIR}")
runpy.run_path(r"{MAKE_PY}", run_name="__main__")
'''

    if needs_sudo:
        # Write to temp, then sudo mv
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(wrapper_content)
            tmp_path = tmp.name
        os.chmod(tmp_path, 0o755)
        result = subprocess.run(['sudo', 'mv', tmp_path, str(wrapper_path)])
        if result.returncode != 0:
            print('  ERROR: sudo mv failed.')
            return
        subprocess.run(['sudo', 'chmod', '755', str(wrapper_path)])
    else:
        wrapper_path.write_text(wrapper_content)
        wrapper_path.chmod(0o755)

    print(f'  Installed: {wrapper_path}')

    # Check if the directory is on PATH
    if not needs_sudo and not local_bin_on_path:
        shell = os.environ.get('SHELL', '')
        if 'zsh' in shell:
            rc_file = '~/.zshrc'
        elif 'bash' in shell:
            rc_file = '~/.bashrc'
        else:
            rc_file = '~/.profile'
        print()
        print(f'  NOTE: {target_dir} is not on your PATH.')
        print(f'  Add this line to {rc_file}:')
        print(f'    export PATH="$HOME/.local/bin:$PATH"')
        print(f'  Then restart your terminal or run: source {rc_file}')
    else:
        print()
        print(f'  Done! You can now run: {COMMAND_NAME} build')


def setup_path_windows():
    """Set up PATH access on Windows."""
    print()
    print(f'This will make "{COMMAND_NAME}" available from any terminal.')
    print()

    # Create a .bat wrapper in a known location
    scripts_dir = SCRIPT_DIR / 'bin'
    scripts_dir.mkdir(exist_ok=True)
    bat_path = scripts_dir / f'{COMMAND_NAME}.bat'
    bat_content = f'@echo off\r\npython "{MAKE_PY}" %*\r\n'
    bat_path.write_text(bat_content)
    print(f'  Created: {bat_path}')

    # Also create a .cmd for PowerShell compatibility
    cmd_path = scripts_dir / f'{COMMAND_NAME}.cmd'
    cmd_path.write_text(bat_content)

    # Check if scripts_dir is on PATH
    path_dirs = os.environ.get('PATH', '').split(';')
    if str(scripts_dir) in path_dirs:
        print(f'  {scripts_dir} is already on PATH.')
        print(f'  Done! Run: {COMMAND_NAME} build')
        return

    print()
    print(f'  To use from anywhere, add this folder to your PATH:')
    print(f'    {scripts_dir}')
    print()
    add_to_path = input('  Add to user PATH now? (yes/no): ').strip().lower()
    if add_to_path in ('yes', 'y'):
        # Add to user PATH via setx (persists across reboots)
        import winreg
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Environment',
                0, winreg.KEY_READ | winreg.KEY_WRITE
            )
            current_path, _ = winreg.QueryValueEx(key, 'Path')
            if str(scripts_dir) not in current_path:
                new_path = f'{current_path};{scripts_dir}'
                winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, new_path)
                print(f'  Added to user PATH.')
                print('  Restart your terminal for the change to take effect.')
            else:
                print('  Already on PATH.')
            winreg.CloseKey(key)
        except Exception as e:
            print(f'  Could not modify PATH automatically: {e}')
            print(f'  Add manually: Settings > System > Environment Variables > Path > {scripts_dir}')
    else:
        print(f'  Skipped. Add manually or run directly:')
        print(f'    python "{MAKE_PY}" <command>')


def main():
    print(r"""
   ___                         ___ _      _
  / __|_  _ _ __  ___ _ _     | _ (_)___ | |
  \__ \ || | '_ \/ -_) '_|    |  _/ / -_)|_|
  |___/\_,_| .__/\___|_|      |_| |_\___(_)
            |_|
  +----------------------------------------------------+
  |  make-sdk v2.0 - Cradlepoint NCOS SDK Tool         |
  +----------------------------------------------------+
  |  Author:  Scott Pickles                            |
  |  Email:   scott.pickles@ericsson.com               |
  |  License: MIT                                      |
  |                                                    |
  |  Copyright (c) 2026 Scott Pickles.                 |
  |  All rights reserved.                              |
  +----------------------------------------------------+
    """)
    print()

    args = sys.argv[1:]
    do_deps = '--deps' in args or not args
    do_path = '--path' in args or not args

    if do_deps:
        success = install_deps()
        if not success and '--deps' in args:
            sys.exit(1)

    if do_path:
        print()
        print('--- PATH Setup ---')
        is_windows = platform.system() == 'Windows'
        if is_windows:
            setup_path_windows()
        else:
            setup_path_unix()

    print()
    print('Setup complete!')
    print()
    print('Quick start:')
    print(f'  {COMMAND_NAME} help')
    print(f'  {COMMAND_NAME} build my_app')
    print(f'  {COMMAND_NAME} deploy')


if __name__ == '__main__':
    main()
