#!/usr/bin/env python3
"""
make.py — Cradlepoint NCOS SDK Tool (v1.0.0)

Single-file, OS-agnostic replacement for the original make.py. Pure Python —
no shell-outs, no pscp.exe, no sshpass, no platform branching.

Requirements: pip install paramiko requests

Usage: python3 make.py <action> [app_name]

Actions:
  create <name>   Create a new app from app_template
  clean           Remove build artifacts
  build           Clean + validate + package + verify archive
  install         Build (if needed) + upload via SCP
  start           Start app on device
  stop            Stop app on device
  uninstall       Uninstall app from device
  purge           Remove ALL apps from device
  deploy          Purge + build + install (full redeploy)
  status          Show SDK status on device
  devmode         Toggle dev mode via NCM API
  upload          Upload built tar.gz to NCM for fleet deployment
  uuid            Show/generate app UUID

Config: sdk_settings.ini (same location as this script)
"""

import configparser
import hashlib
import json
import os
import py_compile
import shutil
import sys
import tarfile
import time
import uuid as uuid_module
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    requests = None

try:
    import paramiko
except ImportError:
    paramiko = None


# =============================================================================
# Constants
# =============================================================================

DEFAULT_HTTPS_PORT = 443
DEFAULT_SSH_PORT = 22

# Files/dirs always excluded from builds
DEFAULT_IGNORE_FILES = {'buildignore', '.DS_Store'}
DEFAULT_IGNORE_DIRS = {'__pycache__', 'METADATA', '.git', '.venv', 'node_modules'}

# File types checked for CRLF (would break on router)
CR_SENSITIVE_SUFFIXES = ('.py', '.sh')

# NCM shards — probed sequentially during credential test
NCM_SHARDS = ['us0', 'us2', 'us8', 'eu4', 'au5']
NCM_SHARD_ENV = 'NCM_SHARD'

NCM_DEVMODE_FEATURE_ID = 1272402

# Browser UA for NCM API (avoids WAF issues on multipart uploads)
NCM_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/150.0.0.0 Safari/537.36'
)

# setup.py build hook timeout
SETUP_SCRIPT_TIMEOUT = 300


# =============================================================================
# Configuration
# =============================================================================

class Config:
    """Parsed sdk_settings.ini."""

    def __init__(self):
        self.app_name: str = ''
        self.dev_client_ip: str = ''
        self.dev_client_username: str = ''
        self.dev_client_password: str = ''
        self.https_port: int = DEFAULT_HTTPS_PORT
        self.ssh_port: int = DEFAULT_SSH_PORT
        self.ncm_api_id: str = ''
        self.ncm_api_key: str = ''
        self.ncm_shard: str = ''

    @classmethod
    def load(cls, app_name_override: Optional[str] = None) -> 'Config':
        """Load from sdk_settings.ini in the script's directory or cwd."""
        cfg = cls()
        settings_file = cls._find_settings_file()
        if not settings_file:
            print('ERROR: sdk_settings.ini not found')
            sys.exit(1)

        config = configparser.ConfigParser()
        config.read(settings_file)

        section = 'sdk'
        if section not in config:
            print(f'ERROR: [{section}] section not found in {settings_file}')
            sys.exit(1)

        cfg.app_name = app_name_override or config.get(section, 'app_name', fallback='')
        cfg.dev_client_ip = config.get(section, 'dev_client_ip', fallback='')
        cfg.dev_client_username = config.get(section, 'dev_client_username', fallback='')
        cfg.dev_client_password = config.get(section, 'dev_client_password', fallback='')
        cfg.https_port = int(config.get(section, 'https_port', fallback=str(DEFAULT_HTTPS_PORT)))
        cfg.ssh_port = int(config.get(section, 'ssh_port', fallback=str(DEFAULT_SSH_PORT)))

        # NCM credentials: env vars first (the canonical source), settings file as backup
        # NCM v1 API uses X-ECM-API-ID / X-ECM-API-KEY with SecurityToken auth.
        cfg.ncm_api_id = (os.environ.get('X_ECM_API_ID', '')
                          or os.environ.get('X-ECM-API-ID', '')
                          or config.get(section, 'X-ECM-API-ID', fallback=''))
        cfg.ncm_api_key = (os.environ.get('X_ECM_API_KEY', '')
                           or os.environ.get('X-ECM-API-KEY', '')
                           or config.get(section, 'X-ECM-API-KEY', fallback=''))
        cfg.ncm_shard = (config.get(section, 'ncm_shard', fallback='')
                         or os.environ.get(NCM_SHARD_ENV, ''))

        return cfg

    @staticmethod
    def _find_settings_file() -> Optional[str]:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sdk_settings.ini'),
            os.path.join(os.getcwd(), 'sdk_settings.ini'),
            os.path.join(os.path.dirname(os.getcwd()), 'sdk_settings.ini'),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None


# =============================================================================
# App Discovery (versioned subfolders)
# =============================================================================

def find_app_dir(app_name: str) -> Optional[str]:
    """Find the app directory, supporting versioned subfolder layouts.

    Checks (in order):
    1. app_name/ directly contains package.ini (flat layout)
    2. app_name/ contains version subfolders — picks the latest

    Version folder heuristics (flexible naming):
      v1.0.0, 1.0.0, version_1, v2, release-3.1, etc.
      Falls back to: any subfolder containing package.ini + a .py file + start.sh
    """
    base = Path(app_name)
    if not base.is_dir():
        return None

    # Flat: package.ini in the app folder itself
    if (base / 'package.ini').is_file():
        return str(base)

    # Versioned: look for subfolders that look like version directories
    candidates = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir() or child.name.startswith('.'):
            continue
        if child.name in DEFAULT_IGNORE_DIRS:
            continue
        # Must contain package.ini to be considered an app folder
        if not (child / 'package.ini').is_file():
            continue
        # Score it: version-like name is preferred but not required
        score = _version_score(child.name)
        candidates.append((score, child.name, str(child)))

    if candidates:
        # Highest score (most version-like), then reverse-alpha for tie-breaking
        candidates.sort(key=lambda x: (-x[0], x[1]))
        chosen = candidates[0][2]
        if len(candidates) > 1:
            print(f'  Found {len(candidates)} versions, using: {Path(chosen).name}')
        return chosen

    return None


def _version_score(name: str) -> int:
    """Score how version-like a folder name is. Higher = more likely a version."""
    import re
    score = 0
    # Starts with 'v' followed by a digit: v1, v1.0, v1.0.0
    if re.match(r'^v\d', name, re.IGNORECASE):
        score += 10
    # Contains a semver-ish pattern: 1.0.0, 2.1, etc.
    if re.search(r'\d+\.\d+', name):
        score += 5
    # Contains 'version' or 'release'
    if re.search(r'version|release', name, re.IGNORECASE):
        score += 3
    # Starts with a digit
    if name[0].isdigit():
        score += 2
    # Contains any digit at all (weakest signal)
    if re.search(r'\d', name):
        score += 1
    return score


# =============================================================================
# Device Communication
# =============================================================================

def device_base_url(cfg: Config) -> str:
    """HTTPS base URL for the device API."""
    if cfg.https_port == DEFAULT_HTTPS_PORT:
        return f'https://{cfg.dev_client_ip}'
    return f'https://{cfg.dev_client_ip}:{cfg.https_port}'


def get_auth(cfg: Config):
    """Get HTTP Basic auth for device API calls."""
    if not requests:
        print('ERROR: requests module not installed (pip install requests)')
        sys.exit(1)

    url = f'{device_base_url(cfg)}/api/status/product_info'
    try:
        resp = requests.get(
            url,
            auth=requests.auth.HTTPBasicAuth(cfg.dev_client_username, cfg.dev_client_password),
            verify=False, timeout=10
        )
        if resp.status_code == HTTPStatus.OK:
            return requests.auth.HTTPBasicAuth(cfg.dev_client_username, cfg.dev_client_password)
        elif resp.status_code == HTTPStatus.UNAUTHORIZED:
            print('ERROR: Authentication failed')
            sys.exit(1)
        else:
            print(f'ERROR: HTTP {resp.status_code} from device')
            sys.exit(1)
    except requests.exceptions.RequestException as ex:
        print(f'ERROR: Cannot reach device at {cfg.dev_client_ip}:{cfg.https_port} — {ex}')
        sys.exit(1)


def device_get(cfg: Config, path: str) -> Any:
    """GET from device API, returns parsed data."""
    if not path.startswith('/'):
        path = '/' + path
    url = f'{device_base_url(cfg)}/api{path}'
    resp = requests.get(url, auth=get_auth(cfg), verify=False, timeout=10)
    return resp.json().get('data')


def device_put_action(cfg: Config, action: str, app_uuid: str):
    """PUT an SDK action to the device."""
    url = f'{device_base_url(cfg)}/api/control/system/sdk/action'
    requests.put(
        url,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        auth=get_auth(cfg),
        data={'data': f'"{action} {app_uuid}"'},
        verify=False, timeout=10
    )


# =============================================================================
# Build Utilities
# =============================================================================

def parse_package_ini(app_dir: str) -> Dict:
    """Parse package.ini and return app metadata."""
    package_file = Path(app_dir) / 'package.ini'
    if not package_file.exists():
        raise RuntimeError(f'package.ini not found in {app_dir}')

    config = configparser.ConfigParser()
    config.read(package_file)
    if not config.sections():
        raise RuntimeError('package.ini has no sections')

    name = config.sections()[0]
    return {
        'name': name,
        'uuid': config.get(name, 'uuid', fallback=''),
        'vendor': config.get(name, 'vendor', fallback=''),
        'notes': config.get(name, 'notes', fallback=''),
        'version_major': config.getint(name, 'version_major', fallback=1),
        'version_minor': config.getint(name, 'version_minor', fallback=0),
        'version_patch': config.getint(name, 'version_patch', fallback=0),
        'auto_start': config.getboolean(name, 'auto_start', fallback=True),
        'restart': config.getboolean(name, 'restart', fallback=True),
        'reboot': config.getboolean(name, 'reboot', fallback=True),
        'firmware_major': config.getint(name, 'firmware_major', fallback=7),
        'firmware_minor': config.getint(name, 'firmware_minor', fallback=25),
    }


def ensure_uuid(app_dir: str, app_name: str) -> str:
    """Ensure package.ini has a valid UUID, generating one if needed."""
    package_file = Path(app_dir) / 'package.ini'
    config = configparser.ConfigParser()
    config.read(package_file)
    current = config.get(app_name, 'uuid', fallback='')
    try:
        uuid_module.UUID(current)
        return current
    except (ValueError, AttributeError):
        new_uuid = str(uuid_module.uuid4())
        config.set(app_name, 'uuid', new_uuid)
        with open(package_file, 'w') as f:
            config.write(f)
        print(f'  Generated UUID: {new_uuid}')
        return new_uuid


def parse_buildignore(app_dir: str) -> Tuple[Set[str], Set[str]]:
    """Parse buildignore file + defaults."""
    ignored_files = set(DEFAULT_IGNORE_FILES)
    ignored_dirs = set(DEFAULT_IGNORE_DIRS)

    ignore_path = Path(app_dir) / 'buildignore'
    if ignore_path.exists():
        for line in ignore_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.endswith('/'):
                ignored_dirs.add(line.rstrip('/'))
            else:
                ignored_files.add(line)

    return ignored_files, ignored_dirs


def strip_carriage_returns(app_dir: str) -> list:
    """Remove CR bytes from .py and .sh files. Returns list of fixed files."""
    fixed = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORE_DIRS]
        for name in files:
            if not name.endswith(CR_SENSITIVE_SUFFIXES):
                continue
            full = os.path.join(root, name)
            with open(full, 'rb') as f:
                content = f.read()
            if b'\r' not in content:
                continue
            with open(full, 'wb') as f:
                f.write(content.replace(b'\r', b''))
            fixed.append(os.path.relpath(full, app_dir))
    return fixed


def run_setup_script(app_dir: str) -> Optional[str]:
    """Run the app's setup.py build hook if present. Returns output or None."""
    import subprocess
    setup_path = Path(app_dir) / 'setup.py'
    if not setup_path.is_file():
        return None
    print('  Running setup.py build hook...')
    try:
        result = subprocess.run(
            [sys.executable, 'setup.py'],
            cwd=app_dir, capture_output=True, text=True,
            timeout=SETUP_SCRIPT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'setup.py timed out after {SETUP_SCRIPT_TIMEOUT}s')
    output = ((result.stdout or '') + (result.stderr or '')).strip()
    if result.returncode != 0:
        detail = output.splitlines()[-1] if output else 'no output'
        raise RuntimeError(f'setup.py failed (exit {result.returncode}): {detail}')
    if output:
        for line in output.splitlines()[:5]:
            print(f'    {line}')
    return output


def file_checksum(filepath: str) -> str:
    """SHA256 of a file."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def hash_dir(app_dir: str, ignored_files: Set[str], ignored_dirs: Set[str]) -> Dict[str, str]:
    """Hash all non-ignored files."""
    hashed = {}
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith('.')]
        for f in files:
            if f in ignored_files or f.startswith('.'):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, app_dir).replace('\\', '/')
            hashed[rel] = file_checksum(full)
    return hashed


# =============================================================================
# Package & Validate
# =============================================================================

def clean(app_dir: str):
    """Remove build artifacts."""
    removed = []
    for dirname in ['__pycache__', 'METADATA']:
        d = Path(app_dir) / dirname
        if d.exists():
            shutil.rmtree(d)
            removed.append(dirname + '/')
    for f in Path(app_dir).glob('*.tar.gz'):
        removed.append(f.name)
        f.unlink()
    if removed:
        print(f'  Cleaned: {", ".join(removed)}')
    else:
        print('  Nothing to clean')


def package(app_dir: str) -> str:
    """Full build: validate, clean, normalize, hook, archive, verify.

    Returns the path to the created .tar.gz.
    """
    print(f'Packaging {app_dir}')

    # --- Validate ---
    app_dict = parse_package_ini(app_dir)
    app_name = app_dict['name']
    print(f'  App: {app_name}')

    main_file = Path(app_dir) / f'{app_name}.py'
    if not main_file.exists():
        raise RuntimeError(f'Main file not found: {app_name}.py')

    app_uuid = ensure_uuid(app_dir, app_name)
    app_dict['uuid'] = app_uuid

    # Syntax check all .py files
    py_files = list(Path(app_dir).glob('*.py'))
    for pf in py_files:
        try:
            py_compile.compile(str(pf), doraise=True)
        except py_compile.PyCompileError as e:
            raise RuntimeError(f'Syntax error in {pf.name}: {e}')
    print(f'  Syntax OK ({len(py_files)} files)')

    # --- Clean ---
    clean(app_dir)

    # --- Normalize CRLF ---
    fixed = strip_carriage_returns(app_dir)
    if fixed:
        print(f'  Fixed CRLF in: {", ".join(fixed)}')

    # --- Build hook ---
    run_setup_script(app_dir)

    # --- Build manifest + archive ---
    ignored_files, ignored_dirs = parse_buildignore(app_dir)

    metadata_dir = Path(app_dir) / 'METADATA'
    metadata_dir.mkdir(exist_ok=True)

    app_dict['date'] = datetime.now().isoformat()
    app_dict['files'] = hash_dir(app_dir, ignored_files, ignored_dirs)

    manifest_dict = {k: v for k, v in app_dict.items()}
    pmf = {'version_major': 1, 'version_minor': 0, 'version_patch': 0}
    manifest_data = {'pmf': pmf, 'app': manifest_dict}

    manifest_path = metadata_dir / 'MANIFEST.json'
    with open(manifest_path, 'w') as f:
        json.dump(manifest_data, f, indent=4, sort_keys=True)

    sig_path = metadata_dir / 'SIGNATURE.DS'
    sig_bytes = file_checksum(str(manifest_path)).encode('utf-8')
    with open(sig_path, 'wb') as f:
        f.write(sig_bytes)

    version = f"{app_dict['version_major']}.{app_dict['version_minor']}.{app_dict['version_patch']}"
    archive_name = f"{app_name} v{version}.tar.gz"
    archive_path = Path(app_dir) / archive_name

    def tar_filter(tarinfo):
        basename = os.path.basename(tarinfo.name)
        if basename.startswith('.'):
            return None
        if tarinfo.isdir() and basename in ignored_dirs and basename != 'METADATA':
            return None
        if tarinfo.isfile() and basename in ignored_files:
            return None
        if tarinfo.name.endswith('.tar.gz'):
            return None
        return tarinfo

    with tarfile.open(archive_path, 'w:gz') as tar:
        tar.add(app_dir, arcname=app_name, filter=tar_filter)

    # --- Post-build validation ---
    validate_archive(str(archive_path), app_name, manifest_data)

    size_kb = archive_path.stat().st_size / 1024
    print(f'  Created: {archive_name} ({size_kb:.1f} KB)')
    return str(archive_path)


def validate_archive(archive_path: str, app_name: str, manifest_data: dict):
    """Reopen and verify the archive structure."""
    with tarfile.open(archive_path, 'r:gz') as tar:
        members = tar.getmembers()
        names = [m.name for m in members]

        # Single root dir
        top_level = {n.split('/')[0] for n in names if n.split('/')[0]}
        if len(top_level) != 1 or app_name not in top_level:
            raise RuntimeError(
                f'Archive root mismatch: got {sorted(top_level)}, expected [{app_name}]'
            )

        # MANIFEST.json present + valid
        manifest_member = f'{app_name}/METADATA/MANIFEST.json'
        if manifest_member not in names:
            raise RuntimeError(f'Missing {manifest_member}')
        mf = tar.extractfile(manifest_member)
        manifest_bytes = mf.read()
        try:
            json.loads(manifest_bytes)
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(f'MANIFEST.json invalid: {e}')

        # SIGNATURE.DS matches
        sig_member = f'{app_name}/METADATA/SIGNATURE.DS'
        if sig_member not in names:
            raise RuntimeError(f'Missing {sig_member}')
        sig_bytes = tar.extractfile(sig_member).read()
        expected = hashlib.sha256(manifest_bytes).hexdigest().encode('utf-8')
        if sig_bytes != expected:
            raise RuntimeError('SIGNATURE.DS does not match MANIFEST.json checksum')

        # Manifest files exist in archive
        app_files = manifest_data.get('app', {}).get('files', {})
        for rel in app_files:
            if f'{app_name}/{rel}' not in names:
                raise RuntimeError(f'In manifest but missing from archive: {rel}')

        # No unexpected files
        manifest_set = set(app_files.keys())
        for m in members:
            if m.isdir():
                continue
            if not m.name.startswith(f'{app_name}/'):
                continue
            rel = m.name[len(app_name) + 1:]
            if rel.startswith('METADATA/'):
                continue
            if rel not in manifest_set:
                raise RuntimeError(f'In archive but not in manifest: {rel}')

        # Required files
        for required in ['start.sh', 'package.ini']:
            if f'{app_name}/{required}' not in names:
                raise RuntimeError(f'Missing required file: {required}')

    print('  Archive validated OK')


# =============================================================================
# SCP Upload (pure paramiko, OS-agnostic)
# =============================================================================

def scp_upload(cfg: Config, local_path: str, remote_path: str = '/app_upload'):
    """Upload file to device via SCP protocol over SSH."""
    if not paramiko:
        print('ERROR: paramiko not installed (pip install paramiko)')
        sys.exit(1)

    if not os.path.isfile(local_path):
        raise RuntimeError(f'File not found: {local_path}')

    import socket
    filename = os.path.basename(local_path)
    filesize = os.path.getsize(local_path)
    print(f'  Uploading {filename} ({filesize:,} bytes) to {cfg.dev_client_ip}:{cfg.ssh_port}')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            cfg.dev_client_ip, port=cfg.ssh_port,
            username=cfg.dev_client_username, password=cfg.dev_client_password,
            timeout=30, look_for_keys=False, allow_agent=False
        )
    except paramiko.AuthenticationException:
        raise RuntimeError('SSH authentication failed')
    except socket.timeout:
        raise RuntimeError('SSH connection timeout')
    except socket.error as e:
        raise RuntimeError(f'SSH connection error: {e}')

    try:
        transport = client.get_transport()
        channel = transport.open_session()
        channel.exec_command(f'scp -t {remote_path}')

        # Wait for ready
        resp = channel.recv(1)
        if resp != b'\x00':
            raise RuntimeError(f'SCP not ready: {resp}')

        # Send header
        header = f'C0644 {filesize} {filename}\n'
        channel.send(header.encode())
        resp = channel.recv(1)
        if resp != b'\x00':
            raise RuntimeError(f'SCP header rejected')

        # Send file with progress
        bytes_sent = 0
        with open(local_path, 'rb') as f:
            while True:
                data = f.read(32768)
                if not data:
                    break
                channel.send(data)
                bytes_sent += len(data)
                pct = int(bytes_sent * 100 / filesize) if filesize else 100
                bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
                sys.stdout.write(f'\r  [{bar}] {pct}% ({bytes_sent:,}/{filesize:,})')
                sys.stdout.flush()

        sys.stdout.write('\n')

        channel.send(b'\x00')
        try:
            channel.settimeout(2)
            channel.recv(1)
        except:
            pass  # Connection drop is expected

        channel.close()
    except paramiko.SSHException as e:
        if not any(x in str(e) for x in ['dropped', 'closed', 'Socket']):
            raise RuntimeError(f'Upload failed: {e}')
    except Exception as e:
        if not any(x in str(e).lower() for x in ['reset', 'broken pipe', 'eof', 'channel closed']):
            raise RuntimeError(f'Upload failed: {e}')
    finally:
        client.close()

    print('  Upload complete')


# =============================================================================
# NCM API
# =============================================================================

def ncm_headers(cfg: Config) -> dict:
    """NCM v1 auth headers."""
    if not cfg.ncm_api_id or not cfg.ncm_api_key:
        print('ERROR: NCM API credentials not configured.')
        print('  These must be X-ECM-API-ID / X-ECM-API-KEY credentials.')
        print('  NOT X-CP-API keys — those use a different auth mechanism.')
        print()
        print('  Option 1 (preferred): Set environment variables:')
        print('    export X_ECM_API_ID="your-ecm-api-id"')
        print('    export X_ECM_API_KEY="your-ecm-api-key"')
        print()
        print('  Option 2 (fallback): Add to sdk_settings.ini:')
        print('    X-ECM-API-ID = your-ecm-api-id')
        print('    X-ECM-API-KEY = your-ecm-api-key')
        sys.exit(1)
    return {
        'Authorization': f'SecurityToken {cfg.ncm_api_id}:{cfg.ncm_api_key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': NCM_USER_AGENT,
    }


def ncm_base_url(shard: str) -> str:
    return f'https://www.{shard}.cradlepointecm.com'


def ncm_resolve_shard(cfg: Config) -> str:
    """Discover which NCM shard these credentials belong to."""
    if cfg.ncm_shard:
        # Verify the stored shard still works
        url = f'{ncm_base_url(cfg.ncm_shard)}/api/v1/accounts/?limit=1&fields=id,name'
        try:
            resp = requests.get(url, headers=ncm_headers(cfg), timeout=10)
            if resp.status_code == 200:
                return cfg.ncm_shard
        except:
            pass
        print(f'  Stored shard {cfg.ncm_shard} failed, rediscovering...')

    # Probe each shard
    headers = ncm_headers(cfg)
    for shard in NCM_SHARDS:
        url = f'{ncm_base_url(shard)}/api/v1/accounts/?limit=1&fields=id,name'
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                print(f'  NCM shard: {shard}')
                return shard
        except:
            continue

    print('ERROR: No NCM region accepted these credentials.')
    print(f'  Tried: {NCM_SHARDS}')
    print(f'  If your shard is not listed, set NCM_SHARD=<name> or ncm_shard in sdk_settings.ini')
    sys.exit(1)


def ncm_get_account_id(cfg: Config, shard: str) -> int:
    """Get the account ID for these credentials."""
    url = f'{ncm_base_url(shard)}/api/v1/accounts/?limit=1&fields=id,name'
    resp = requests.get(url, headers=ncm_headers(cfg), timeout=10)
    data = resp.json()
    accounts = data.get('data', [])
    if not accounts:
        print('ERROR: No accounts found for these credentials')
        sys.exit(1)
    acct = accounts[0]
    print(f'  Account: {acct.get("name")} (ID: {acct.get("id")})')
    return acct['id']


def ncm_upload(cfg: Config, tar_path: str):
    """Upload tar.gz to NCM device_app_versions endpoint."""
    if not os.path.isfile(tar_path):
        print(f'ERROR: File not found: {tar_path}')
        sys.exit(1)

    shard = ncm_resolve_shard(cfg)
    account_id = ncm_get_account_id(cfg, shard)

    filename = os.path.basename(tar_path)
    filesize = os.path.getsize(tar_path)
    url = f'{ncm_base_url(shard)}/api/v1/device_app_versions/'
    account_uri = f'/api/v1/accounts/{account_id}/'

    print(f'  Uploading {filename} ({filesize:,} bytes) to NCM...')
    print(f'  Endpoint: {url}')

    headers = ncm_headers(cfg)
    headers.pop('Content-Type', None)  # Let requests set multipart boundary
    headers['Accept'] = 'application/vnd.api+json'

    with open(tar_path, 'rb') as f:
        resp = requests.post(
            url,
            headers=headers,
            data={'account': account_uri},
            files={'archive': (filename, f, 'application/x-gzip')},
            timeout=120
        )

    if resp.status_code == 201:
        result = resp.json().get('data', resp.json())
        print(f'  SUCCESS: Uploaded as ID {result.get("id")}')
        print(f'    State:   {result.get("state")}')
        print(f'    Version: {result.get("version")}')
    else:
        print(f'  ERROR: Upload failed (HTTP {resp.status_code})')
        print(f'  Response: {resp.text[:500]}')
        sys.exit(1)


def ncm_devmode_toggle(cfg: Config, action: str):
    """Enable or disable dev mode via NCM API."""
    shard = ncm_resolve_shard(cfg)
    headers = ncm_headers(cfg)

    # Get router ID from the device itself
    ecm_status = device_get(cfg, '/status/ecm')
    if not ecm_status:
        print('ERROR: Could not get ECM status from device. Is it connected to NCM?')
        sys.exit(1)
    router_id = ecm_status.get('client_id')
    if not router_id:
        print('ERROR: Could not get router ID from device. Is it connected to NCM?')
        sys.exit(1)
    print(f'  Router ID: {router_id}')

    # Get router info from NCM (includes group membership)
    url = f'{ncm_base_url(shard)}/api/v1/routers/{router_id}/'
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f'ERROR: Could not get router info from NCM (HTTP {resp.status_code})')
        sys.exit(1)
    router_data = resp.json().get('data', resp.json())
    account_uri = router_data.get('account', '')
    account_id = account_uri.strip('/').split('/')[-1] if account_uri else None
    if not account_id:
        print('ERROR: Could not determine account ID')
        sys.exit(1)
    print(f'  Account ID: {account_id}')

    if action == 'enable':
        # Check group membership
        group_uri = router_data.get('group')
        group_name = router_data.get('group_name', '')
        if group_uri:
            print()
            print(f'  WARNING: This router is in NCM group: {group_name or group_uri}')
            print('  Enabling dev mode requires removing it from the group.')
            print('  The router will lose any group-pushed configuration.')
            print()
            confirm = input('  Remove from group and enable dev mode? (yes/no): ').strip().lower()
            if confirm not in ('yes', 'y'):
                print('  Aborted.')
                return

            # Save the group so we can restore it on disable
            _save_previous_group(router_id, group_uri, group_name)

            # Remove from group
            print('  Removing router from group...')
            remove_url = f'{ncm_base_url(shard)}/api/v1/routers/'
            remove_body = json.dumps({
                'data': [{
                    'id': str(router_id),
                    'account': f'/api/v1/accounts/{account_id}/',
                    'group': None
                }]
            }).encode('utf-8')
            remove_resp = requests.put(remove_url, headers=headers, data=remove_body, timeout=30)
            if remove_resp.status_code not in (200, 202):
                print(f'  ERROR: Could not remove from group (HTTP {remove_resp.status_code})')
                print(f'  Response: {remove_resp.text[:300]}')
                sys.exit(1)
            print('  Removed from group.')
            time.sleep(2)  # Give NCM a moment to process

    elif action == 'disable':
        # After disabling, offer to restore previous group
        pass  # Restoration prompt happens after the toggle below

    # Toggle feature binding
    fb_url = f'{ncm_base_url(shard)}/api/v1/featurebindings/{NCM_DEVMODE_FEATURE_ID}/routers/?parentAccount={account_id}'
    body = json.dumps([f'/api/v1/routers/{router_id}/']).encode('utf-8')
    method = 'POST' if action == 'enable' else 'DELETE'

    resp = requests.request(method, fb_url, headers=headers, data=body, timeout=30)
    if resp.status_code in (200, 201, 204):
        print(f'  Dev mode {action}d successfully.')
        print('  Note: The router typically takes 30-60 seconds to apply the change.')
    else:
        print(f'  ERROR: Dev mode {action} failed (HTTP {resp.status_code})')
        print(f'  Response: {resp.text[:500]}')
        sys.exit(1)

    # After disable: offer to restore the group
    if action == 'disable':
        prev = _load_previous_group(router_id)
        if prev:
            prev_uri, prev_name = prev
            print()
            print(f'  This router was previously in group: {prev_name or prev_uri}')
            restore = input('  Restore group membership? (yes/no): ').strip().lower()
            if restore in ('yes', 'y'):
                print('  Restoring group...')
                restore_url = f'{ncm_base_url(shard)}/api/v1/routers/'
                restore_body = json.dumps({
                    'data': [{
                        'id': str(router_id),
                        'account': f'/api/v1/accounts/{account_id}/',
                        'group': prev_uri
                    }]
                }).encode('utf-8')
                restore_resp = requests.put(restore_url, headers=headers, data=restore_body, timeout=30)
                if restore_resp.status_code in (200, 202):
                    print(f'  Restored to group: {prev_name or prev_uri}')
                    _clear_previous_group(router_id)
                else:
                    print(f'  WARNING: Could not restore group (HTTP {restore_resp.status_code})')
                    print(f'  The group URI has been saved — try manually or run devmode disable again.')
            else:
                print('  Group not restored. You can restore it later or assign via NCM.')
                print(f'  Saved group: {prev_name or prev_uri}')


def _devmode_state_file() -> Path:
    """State file for devmode group tracking, alongside sdk_settings.ini."""
    settings = Config._find_settings_file()
    if settings:
        return Path(settings).parent / '.devmode_state.json'
    return Path('.devmode_state.json')


def _save_previous_group(router_id, group_uri: str, group_name: str):
    """Remember the group a router was in before devmode removed it."""
    state_file = _devmode_state_file()
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except:
            pass
    state[str(router_id)] = {'group_uri': group_uri, 'group_name': group_name}
    state_file.write_text(json.dumps(state, indent=2))


def _load_previous_group(router_id) -> Optional[tuple]:
    """Get the saved group for a router, or None."""
    state_file = _devmode_state_file()
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text())
        entry = state.get(str(router_id))
        if entry:
            return (entry.get('group_uri'), entry.get('group_name', ''))
    except:
        pass
    return None


def _clear_previous_group(router_id):
    """Remove saved group state after successful restoration."""
    state_file = _devmode_state_file()
    if not state_file.exists():
        return
    try:
        state = json.loads(state_file.read_text())
        state.pop(str(router_id), None)
        if state:
            state_file.write_text(json.dumps(state, indent=2))
        else:
            state_file.unlink()
    except:
        pass


# =============================================================================
# Progress & Monitoring Helpers
# =============================================================================

def _spinner(message: str, stop_event):
    """Animated spinner for long-running operations. Runs in a thread."""
    import itertools
    frames = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
    while not stop_event.is_set():
        sys.stdout.write(f'\r  {next(frames)} {message}')
        sys.stdout.flush()
        stop_event.wait(0.1)
    sys.stdout.write('\r' + ' ' * (len(message) + 6) + '\r')
    sys.stdout.flush()


def _with_spinner(message: str, fn, *args, **kwargs):
    """Run fn() while showing a spinner. Returns fn's result."""
    import threading
    stop = threading.Event()
    t = threading.Thread(target=_spinner, args=(message, stop), daemon=True)
    t.start()
    try:
        result = fn(*args, **kwargs)
    finally:
        stop.set()
        t.join()
    return result


def _wait_for_app_state(cfg: Config, app_name: str, app_uuid: str,
                        target_state: str = 'started', timeout: int = 30):
    """Wait for app to reach a target state on the device, with live progress."""
    import threading
    print('  Waiting for app status...')
    stop = threading.Event()
    t = threading.Thread(target=_spinner, args=('Checking device...', stop), daemon=True)
    t.start()

    start_time = time.time()
    initial_wait = 5
    poll_interval = 2
    time.sleep(initial_wait)

    final_state = None
    installed = False

    try:
        while time.time() - start_time < timeout:
            try:
                status = device_get(cfg, '/status/system/sdk')
                if not status:
                    time.sleep(poll_interval)
                    continue

                # SDK status is a dict; apps may be under various keys
                status_str = json.dumps(status)

                # Check if UUID appears (means installed)
                if app_uuid in status_str:
                    if not installed:
                        installed = True
                        stop.set()
                        t.join()
                        print(f'  ✓ Installed (UUID: {app_uuid[:8]}...)')
                        stop = threading.Event()
                        t = threading.Thread(target=_spinner, args=('Waiting for app to start...', stop), daemon=True)
                        t.start()

                    # Look for the app's state
                    app_state = _extract_app_state(status, app_uuid)
                    if app_state:
                        final_state = app_state
                        if app_state == target_state:
                            break
                        elif app_state in ('error', 'crashed', 'failed'):
                            break

            except Exception:
                pass
            time.sleep(poll_interval)
    finally:
        stop.set()
        t.join()

    if final_state == target_state:
        print(f'  ✓ {app_name} is running')
    elif final_state in ('error', 'crashed', 'failed'):
        print(f'  ✗ {app_name} failed to start (state: {final_state})')
    elif installed:
        state_msg = f' (state: {final_state})' if final_state else ''
        print(f'  ✓ Installed{state_msg} — app may still be starting')
    else:
        print(f'  ⚠ Could not verify installation within {timeout}s')
        print(f'    Run: make-sdk status')


def _extract_app_state(sdk_status: Any, app_uuid: str) -> Optional[str]:
    """Extract the state of a specific app from the SDK status tree."""
    if isinstance(sdk_status, dict):
        # The SDK status tree varies by firmware, but apps appear with their UUID
        # Common patterns: status.system.sdk.apps[uuid].state
        # or the app data is nested differently
        for key, val in sdk_status.items():
            if isinstance(val, dict):
                if val.get('uuid') == app_uuid or key == app_uuid:
                    return val.get('state')
                # Recurse one level
                result = _extract_app_state(val, app_uuid)
                if result:
                    return result
    return None


def _tail_app_logs(cfg: Config, app_name: str, duration: int = 15):
    """Tail device logs for the app, showing new entries as they appear."""
    print(f'\n  Tailing logs for {app_name} ({duration}s)...')
    print('  ' + '-' * 50)

    seen_timestamps = set()
    start = time.time()

    while time.time() - start < duration:
        try:
            logs = device_get(cfg, '/status/log')
            if logs:
                for entry in logs:
                    # Log format: [timestamp, facility, level, message]
                    if len(entry) < 4:
                        continue
                    ts, _facility, level, message = entry[0], entry[1], entry[2], entry[3]
                    # Filter to our app
                    if app_name not in str(entry):
                        continue
                    if ts in seen_timestamps:
                        continue
                    seen_timestamps.add(ts)
                    time_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                    level_str = str(level).upper()[:4] if level else 'INFO'
                    print(f'  {time_str} [{level_str}] {message}')
        except Exception:
            pass
        time.sleep(2)

    print('  ' + '-' * 50)
    if not seen_timestamps:
        print(f'  No log entries for {app_name} in the last {duration}s')
    print()


# =============================================================================
# Pre-flight Checks
# =============================================================================

def preflight_device(cfg: Config, require_devmode: bool = False):
    """Silent pre-flight check before any device operation.

    Validates connectivity and auth BEFORE the actual operation starts, so the
    user gets a clear root-cause message instead of a cryptic downstream error.

    Args:
        cfg: Config with device credentials
        require_devmode: If True, also check that the device is in dev mode
    """
    if not cfg.dev_client_ip:
        print('ERROR: No device IP configured.')
        print('  Set dev_client_ip in sdk_settings.ini')
        sys.exit(1)

    # 1. Can we reach the device over HTTPS?
    if not requests:
        print('ERROR: requests module not installed (pip install requests)')
        sys.exit(1)

    url = f'{device_base_url(cfg)}/api/status/product_info'
    try:
        resp = requests.get(
            url,
            auth=requests.auth.HTTPBasicAuth(cfg.dev_client_username, cfg.dev_client_password),
            verify=False, timeout=10
        )
    except requests.exceptions.ConnectionError:
        print(f'ERROR: Cannot reach device at {cfg.dev_client_ip}:{cfg.https_port}')
        print('  Check that the device is powered on and reachable from this machine.')
        if cfg.https_port != DEFAULT_HTTPS_PORT:
            print(f'  Note: Using non-standard HTTPS port {cfg.https_port}')
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f'ERROR: Connection to {cfg.dev_client_ip}:{cfg.https_port} timed out')
        print('  The device may be unreachable or the port may be wrong.')
        sys.exit(1)
    except requests.exceptions.RequestException as ex:
        print(f'ERROR: Connection failed: {ex}')
        sys.exit(1)

    # 2. Are the credentials valid?
    if resp.status_code == HTTPStatus.UNAUTHORIZED:
        print('ERROR: Authentication failed.')
        print(f'  Username "{cfg.dev_client_username}" was rejected by {cfg.dev_client_ip}')
        print('  Check dev_client_username and dev_client_password in sdk_settings.ini')
        sys.exit(1)
    elif resp.status_code != HTTPStatus.OK:
        print(f'ERROR: Unexpected response from device (HTTP {resp.status_code})')
        sys.exit(1)

    # 3. Is the device in dev mode? (only checked when the operation requires it)
    if require_devmode:
        try:
            auth = requests.auth.HTTPBasicAuth(cfg.dev_client_username, cfg.dev_client_password)
            sdk_resp = requests.get(
                f'{device_base_url(cfg)}/api/status/system/sdk',
                auth=auth, verify=False, timeout=10
            )
            if sdk_resp.status_code == HTTPStatus.OK:
                sdk_data = sdk_resp.json().get('data', {})
                mode = sdk_data.get('mode', 'unknown') if isinstance(sdk_data, dict) else 'unknown'
                if mode != 'devmode':
                    print(f'ERROR: Device is not in SDK Developer Mode (current: {mode})')
                    print('  SDK operations require the device to be in dev mode.')
                    print()
                    # Offer to enable it if we have NCM credentials
                    if cfg.ncm_api_id and cfg.ncm_api_key:
                        enable = input('  Enable dev mode now? (yes/no): ').strip().lower()
                        if enable in ('yes', 'y'):
                            print()
                            action_devmode(cfg, 'enable')
                            # Give the router time to apply
                            print('  Waiting for device to apply dev mode...')
                            for _ in range(12):
                                time.sleep(5)
                                try:
                                    check = requests.get(
                                        f'{device_base_url(cfg)}/api/status/system/sdk',
                                        auth=auth, verify=False, timeout=10
                                    )
                                    if check.status_code == HTTPStatus.OK:
                                        check_data = check.json().get('data', {})
                                        check_mode = check_data.get('mode', '') if isinstance(check_data, dict) else ''
                                        if check_mode == 'devmode':
                                            print('  Device is now in dev mode. Continuing...')
                                            print()
                                            return
                                except Exception:
                                    pass
                            print('  WARNING: Dev mode not confirmed after 60s. Proceeding anyway...')
                            print()
                            return
                        else:
                            sys.exit(1)
                    else:
                        print('  Enable it with: make-sdk devmode enable')
                        print('  (Requires NCM API credentials)')
                        sys.exit(1)
        except Exception:
            # If we can't check, proceed anyway — the operation itself will fail
            # with a more specific error if devmode is actually the problem
            pass


# =============================================================================
# High-Level SDK Actions
# =============================================================================

def action_clean(cfg: Config, app_dir: str):
    print(f'Cleaning {app_dir}')
    clean(app_dir)


def action_build(cfg: Config, app_dir: str) -> str:
    return package(app_dir)


def action_install(cfg: Config, app_dir: str):
    """Build if needed, then upload to device."""
    preflight_device(cfg, require_devmode=True)
    app_dict = parse_package_ini(app_dir)
    version = f"{app_dict['version_major']}.{app_dict['version_minor']}.{app_dict['version_patch']}"
    archive_name = f"{app_dict['name']} v{version}.tar.gz"
    archive_path = Path(app_dir) / archive_name

    if not archive_path.exists():
        print('No archive found, building first...')
        package(app_dir)

    if not archive_path.exists():
        raise RuntimeError(f'Archive not found after build: {archive_name}')

    scp_upload(cfg, str(archive_path))

    # Verify installation + wait for app to be running
    app_uuid = app_dict.get('uuid') or ensure_uuid(app_dir, app_dict['name'])
    app_name = app_dict['name']
    _wait_for_app_state(cfg, app_name, app_uuid)


def action_start(cfg: Config, app_dir: str):
    preflight_device(cfg, require_devmode=True)
    app_dict = parse_package_ini(app_dir)
    app_uuid = ensure_uuid(app_dir, app_dict['name'])
    print(f'Starting {app_dict["name"]} on {cfg.dev_client_ip}')
    device_put_action(cfg, 'start', app_uuid)
    print('  Start command sent')


def action_stop(cfg: Config, app_dir: str):
    preflight_device(cfg, require_devmode=True)
    app_dict = parse_package_ini(app_dir)
    app_uuid = ensure_uuid(app_dir, app_dict['name'])
    print(f'Stopping {app_dict["name"]} on {cfg.dev_client_ip}')
    device_put_action(cfg, 'stop', app_uuid)
    print('  Stop command sent')


def action_uninstall(cfg: Config, app_dir: str):
    preflight_device(cfg, require_devmode=True)
    app_dict = parse_package_ini(app_dir)
    app_uuid = ensure_uuid(app_dir, app_dict['name'])
    print(f'Uninstalling {app_dict["name"]} from {cfg.dev_client_ip}')
    device_put_action(cfg, 'uninstall', app_uuid)
    print('  Uninstall command sent')


def action_purge(cfg: Config):
    preflight_device(cfg, require_devmode=True)
    print(f'Purging all apps from {cfg.dev_client_ip}')
    url = f'{device_base_url(cfg)}/api/control/system/sdk/action'
    requests.put(
        url,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        auth=get_auth(cfg),
        data={'data': '"purge"'},
        verify=False, timeout=10
    )
    print('  Purge command sent')


def action_deploy(cfg: Config, app_dir: str):
    """Full redeploy: purge + build + install + verify running + tail logs."""
    preflight_device(cfg, require_devmode=True)
    app_dict = parse_package_ini(app_dir)
    app_name = app_dict['name']
    print(f'Deploying {app_name} to {cfg.dev_client_ip}...')

    action_purge(cfg)
    time.sleep(2)
    package(app_dir)
    action_install(cfg, app_dir)

    # Tail logs for the app (gives visibility into startup/crash)
    _tail_app_logs(cfg, app_name, duration=15)


def action_status(cfg: Config):
    preflight_device(cfg, require_devmode=False)
    print(f'SDK status for {cfg.dev_client_ip}:')
    status = device_get(cfg, '/status/system/sdk')
    if status:
        print(json.dumps(status, indent=2))
    else:
        print('  No SDK status available')


def action_devmode(cfg: Config, sub_action: str):
    if sub_action not in ('enable', 'disable'):
        print('Usage: python3 make.py devmode <enable|disable>')
        sys.exit(1)
    print(f'{"Enabling" if sub_action == "enable" else "Disabling"} dev mode...')
    ncm_devmode_toggle(cfg, sub_action)


def action_upload(cfg: Config, app_dir: Optional[str], explicit_path: Optional[str] = None):
    """Upload built tar.gz to NCM. Accepts an explicit file path or finds it from the app."""
    if explicit_path:
        # Explicit path provided: use it directly
        tar_path = explicit_path
        if not os.path.isfile(tar_path):
            print(f'ERROR: File not found: {tar_path}')
            sys.exit(1)
        if not tar_path.endswith('.tar.gz'):
            print(f'WARNING: File does not end with .tar.gz: {tar_path}')
            confirm = input('Continue anyway? (yes/no): ').strip().lower()
            if confirm not in ('yes', 'y'):
                print('Aborted.')
                return
    else:
        # Find the archive from the app directory
        if not app_dir:
            print('ERROR: No app directory and no file path specified.')
            print('Usage: python3 make.py upload [app_name]')
            print('   or: python3 make.py upload path/to/app.tar.gz')
            sys.exit(1)
        app_dict = parse_package_ini(app_dir)
        version = f"{app_dict['version_major']}.{app_dict['version_minor']}.{app_dict['version_patch']}"
        archive_name = f"{app_dict['name']} v{version}.tar.gz"
        tar_path = str(Path(app_dir) / archive_name)

        if not os.path.isfile(tar_path):
            print('No archive found, building first...')
            package(app_dir)

        if not os.path.isfile(tar_path):
            print(f'ERROR: Archive not found: {archive_name}')
            sys.exit(1)

    print(f'Uploading {os.path.basename(tar_path)} to NCM...')
    ncm_upload(cfg, tar_path)


def action_uuid(cfg: Config, app_dir: str):
    app_dict = parse_package_ini(app_dir)
    app_uuid = ensure_uuid(app_dir, app_dict['name'])
    print(f'App: {app_dict["name"]}')
    print(f'UUID: {app_uuid}')


def action_create(name: str):
    """Create a new app from app_template."""
    if not name:
        print('ERROR: No app name provided')
        print('Usage: python3 make.py create <app_name>')
        sys.exit(1)
    if os.path.exists(name):
        print(f'ERROR: {name} already exists')
        sys.exit(1)
    template = 'app_template'
    if not os.path.isdir(template):
        print(f'ERROR: {template}/ not found in current directory')
        sys.exit(1)

    shutil.copytree(template, name)
    # Rename the main .py file
    old_py = Path(name) / 'app_template.py'
    new_py = Path(name) / f'{name}.py'
    if old_py.exists():
        old_py.rename(new_py)
    # Replace placeholder in files
    for fname in [f'{name}.py', 'package.ini', 'readme.md', 'start.sh']:
        fpath = Path(name) / fname
        if fpath.exists():
            content = fpath.read_text()
            fpath.write_text(content.replace('app_template', name))
    print(f'App {name} created successfully.')


# =============================================================================
# CLI
# =============================================================================

def output_help():
    print(__doc__)
    print('\nExamples:')
    print('  python3 make.py build my_app')
    print('  python3 make.py deploy')
    print('  python3 make.py devmode enable')
    print('  python3 make.py upload my_app')
    print('  python3 make.py upload path/to/my_app-v1.0.0.tar.gz')
    print()
    print('Config: sdk_settings.ini')
    print('  [sdk]')
    print('  app_name = my_app')
    print('  dev_client_ip = 192.168.0.1')
    print('  dev_client_username = admin')
    print('  dev_client_password = your_password')
    print('  https_port = 443        # optional')
    print('  ssh_port = 22           # optional')
    print('  X-ECM-API-ID = ...      # env var preferred: X_ECM_API_ID')
    print('  X-ECM-API-KEY = ...     # env var preferred: X_ECM_API_KEY')
    print('  ncm_shard = us0         # optional, auto-detected')


def main():
    if len(sys.argv) < 2:
        output_help()
        sys.exit(0)

    action = sys.argv[1].lower()
    option = sys.argv[2] if len(sys.argv) > 2 else None

    # Actions that don't need settings
    if action == 'create':
        action_create(option or '')
        sys.exit(0)
    if action in ('help', '--help', '-h'):
        output_help()
        sys.exit(0)

    # Load config
    cfg = Config.load(app_name_override=option if action != 'devmode' else None)

    if not cfg.app_name and action not in ('purge', 'status', 'devmode'):
        print('ERROR: No app_name set. Provide it as an argument or set it in sdk_settings.ini')
        sys.exit(1)

    # Resolve app directory (supports versioned subfolders)
    app_dir = None
    if cfg.app_name:
        app_dir = find_app_dir(cfg.app_name)
        if not app_dir and action not in ('purge', 'status', 'devmode', 'create'):
            print(f'ERROR: App directory not found for "{cfg.app_name}"')
            sys.exit(1)

    # Dispatch
    try:
        if action == 'clean':
            action_clean(cfg, app_dir)
        elif action in ('build', 'package'):
            action_build(cfg, app_dir)
        elif action == 'install':
            action_install(cfg, app_dir)
        elif action == 'start':
            action_start(cfg, app_dir)
        elif action == 'stop':
            action_stop(cfg, app_dir)
        elif action == 'uninstall':
            action_uninstall(cfg, app_dir)
        elif action == 'purge':
            action_purge(cfg)
        elif action == 'deploy':
            action_deploy(cfg, app_dir)
        elif action == 'status':
            action_status(cfg)
        elif action == 'devmode':
            action_devmode(cfg, option or '')
        elif action == 'upload':
            # Upload accepts either an app name OR an explicit tar.gz path
            if option and option.endswith('.tar.gz') and os.path.isfile(option):
                action_upload(cfg, None, explicit_path=option)
            elif option and os.path.isfile(option):
                # Maybe they passed a path without thinking about .tar.gz check
                action_upload(cfg, None, explicit_path=option)
            else:
                action_upload(cfg, app_dir)
        elif action == 'uuid':
            action_uuid(cfg, app_dir)
        else:
            print(f'Unknown action: {action}')
            output_help()
            sys.exit(1)
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
