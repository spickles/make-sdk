# make.py v2.0 — Cradlepoint NCOS SDK Tool

Single-file, OS-agnostic replacement for the original `make.py`. Pure Python — no
shell-outs, no `pscp.exe`, no `sshpass`, no platform branching. Works identically on
macOS, Linux, and Windows.

---

## Quick Start

```bash
# 1. Run setup (installs deps + optionally adds to PATH)
python3 setup.py

# 2. Configure
#    Edit sdk_settings.ini in your SDK project root

# 3. Use (from anywhere, if PATH was configured)
make-sdk build my_app
make-sdk deploy
make-sdk status
```

### What `setup.py` Does

1. **Installs dependencies** — `pip install paramiko requests`
2. **Offers to add `make-sdk` to your PATH** so it's callable from any directory

| OS | What it creates | Where |
|---|---|---|
| macOS/Linux | Executable wrapper script | `~/.local/bin/make-sdk` (no sudo) or `/usr/local/bin/make-sdk` |
| Windows | `.bat` + `.cmd` wrappers | `make-sdk/bin/make-sdk.bat` + adds to user PATH |

You can always skip the PATH setup and run directly:
```bash
python3 /path/to/make.py build my_app
```

### Running `setup.py` Options

```bash
python3 setup.py           # Full setup (deps + PATH)
python3 setup.py --deps    # Only install dependencies
python3 setup.py --path    # Only configure PATH
```

---

## Requirements

- Python 3.8+
- `paramiko` — pure-Python SSH/SCP (no external binaries)
- `requests` — HTTP client for device API and NCM

Install: `pip install -r requirements.txt`

---

## Commands

Usage: `make-sdk <command> [app_name]`

If `app_name` is omitted, it's read from `sdk_settings.ini`.

### Build Commands

| Command | Description |
|---|---|
| `create <name>` | Create a new app from `app_template/` |
| `clean` | Remove build artifacts (`__pycache__/`, `METADATA/`, `*.tar.gz`) |
| `build` | Full build pipeline: validate → syntax check → clean → CRLF fix → setup.py hook → package → archive validation |
| `uuid` | Show or generate the app's UUID |

### Local Device Commands

These operate on the **physical router** at `dev_client_ip` in your `sdk_settings.ini`.

| Command | Description |
|---|---|
| `install` | Build the app (if no archive exists), then upload it to the device via SCP. Does not remove other installed apps. |
| `deploy` | **Full redeploy.** Purges ALL apps from the device, builds fresh, uploads, verifies install, and shows recent logs. This is the "I changed code and want it running on my dev router now" command. Destructive — removes other apps. |
| `start` | Start the app on the device |
| `stop` | Stop the app on the device |
| `uninstall` | Uninstall this specific app from the device |
| `purge` | Remove ALL apps from the device (without reinstalling anything) |
| `status` | Show the current SDK status from the device (installed apps, modes, etc.) |

### NCM (Cloud) Commands

These talk to **NetCloud Manager**, not the local device. Require ECM API credentials.

| Command | Description |
|---|---|
| `devmode enable` | Enable SDK Developer Mode on the connected router via NCM. Warns and prompts if the router is in a group (removal required). Saves the previous group so it can be restored. |
| `devmode disable` | Disable Developer Mode via NCM. Offers to restore the router's previous group membership if one was saved. |
| `upload` | Upload the built `.tar.gz` to NCM for fleet deployment via groups. Accepts an app name OR an explicit file path (e.g. `make-sdk upload path/to/app.tar.gz`). |

### deploy vs install vs upload

| | Target | Destructive? | When to use |
|---|---|---|---|
| `install` | Local router | No (adds/replaces one app) | Testing alongside other apps |
| `deploy` | Local router | **Yes** (purges all apps first) | Clean slate — "just get my code running" |
| `upload` | NCM (cloud) | No | Ready to push to a fleet of routers |

### Example: `deploy` in action

```
$ make-sdk deploy my_app
Deploying my_app to 192.168.0.1...
Purging all apps from 192.168.0.1
  Purge command sent
Packaging my_app/v1.0.0
  App: my_app
  Syntax OK (3 files)
  Cleaned: __pycache__/, METADATA/
  Fixed CRLF in: start.sh
  Archive validated OK
  Created: my_app v1.0.0.tar.gz (12.4 KB)
  Uploading my_app v1.0.0.tar.gz (12,698 bytes) to 192.168.0.1:22
  [████████████████████] 100% (12,698/12,698)
  Upload complete
  Waiting for app status...
  ✓ Installed (UUID: 5f751ac9...)
  ✓ my_app is running

  Tailing logs for my_app (15s)...
  --------------------------------------------------
  14:32:05 [INFO] Starting my_app
  14:32:06 [INFO] my_app running
  14:32:06 [INFO] Web server started on port 9000
  --------------------------------------------------

$
```

What happens under the hood:
1. Purges all existing apps from the device
2. Validates source, checks syntax, fixes CRLF line endings
3. Runs `setup.py` build hook (if present)
4. Creates and validates the archive
5. Uploads via SCP with a live progress bar
6. Polls device until the app is installed and running (with spinner)
7. Tails the device log for 15 seconds so you see startup messages or crash output
8. Drops back to the terminal

---

## Configuration

### `sdk_settings.ini`

```ini
[sdk]
# Required
app_name = my_app
dev_client_ip = 192.168.0.1
dev_client_username = admin
dev_client_password = your_password

# Optional — defaults shown
https_port = 443
ssh_port = 22

# NCM API credentials (optional — only needed for devmode and upload commands)
# Environment variables are checked first, these are the fallback.
X-ECM-API-ID = your-ecm-api-id-here
X-ECM-API-KEY = your-ecm-api-key-here
```

The file is searched in this order:
1. Same directory as `make.py`
2. Current working directory
3. Parent of the current working directory

### NCM API Credentials

Required for `devmode` and `upload` commands. You need **X-ECM-API-ID** and
**X-ECM-API-KEY** specifically — these authenticate via
`Authorization: SecurityToken {id}:{key}` against the NCM v1 API.

**Important:** These are NOT the same as X-CP-API-ID / X-CP-API-KEY. Those use a
different auth mechanism and will return 401 against the NCM v1 API.

**Priority: environment variables first, settings file as fallback.**

**Option 1 — Environment variables (preferred):**
```bash
export X_ECM_API_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export X_ECM_API_KEY="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

**Option 2 — `sdk_settings.ini` (fallback):**
```ini
X-ECM-API-ID = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
X-ECM-API-KEY = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Environment variables take precedence so you don't have to put credentials in a file
that might get committed to source control.

### NCM Regional Shard

NCM is sharded by account region. **The shard is auto-detected** — when you run a
`devmode` or `upload` command, the tool probes known shards (us0, us2, us8, eu4, au5)
with your API credentials and uses whichever one responds. You don't need to configure
anything.

If you want to skip the probe on every run, you can optionally cache it:
```ini
ncm_shard = us0
```
Or via environment: `export NCM_SHARD=us0`

This is a performance hint only. If omitted or wrong, the tool auto-detects correctly.

### Port Configuration

Default ports work for local/LAN connections. If the device is behind a port-forward,
NAT, or SSH tunnel:

```ini
https_port = 8443    # Device HTTPS API port (default: 443)
ssh_port = 2222      # Device SSH port for SCP upload (default: 22)
```

---

## Project Structure (Versioned Subfolders)

The tool supports flexible project layouts:

```
sdk_root/
├── my_app/
│   ├── v1.0.0/           ← version subfolder (preferred)
│   │   ├── package.ini
│   │   ├── start.sh
│   │   ├── my_app.py
│   │   └── buildignore
│   └── v1.1.0/
│       └── ...
└── simple_app/
    ├── package.ini        ← flat layout (also works)
    └── simple_app.py
```

### Version Folder Detection

Version folders are detected using heuristics, not a rigid naming convention.
Any of these work:

- `v1.0.0` — strongest signal (score: 16)
- `v2` — still detected (score: 11)
- `release-3.1` — detected (score: 9)
- `1.0.0` — detected (score: 8)
- `version_1` — detected (score: 4)

The tool picks the folder with the highest version-likeness score. If multiple
candidates exist, it reports which one was chosen.

**Fallback:** any subfolder containing `package.ini` qualifies, even without a
version-like name. The heuristic determines priority, not eligibility.

---

## Build Pipeline

`python3 make.py build` runs these steps in order:

1. **Validate** — `package.ini` present and parseable, main `.py` file exists
2. **UUID** — checked and auto-generated if missing or invalid
3. **Syntax check** — all `.py` files pass `py_compile` (catches typos before they
   reach the router)
4. **Clean** — remove `__pycache__/`, `METADATA/`, old archives
5. **CRLF normalization** — strips `\r` from `.py` and `.sh` files. A CRLF `start.sh`
   silently fails on the router.
6. **setup.py hook** — if present, runs the app's own build hook (e.g. to vendor
   dependencies). A non-zero exit fails the build.
7. **Package** — creates `METADATA/MANIFEST.json` (with SHA256 file hashes) and
   `METADATA/SIGNATURE.DS`, then archives as `{app} v{major}.{minor}.{patch}.tar.gz`
8. **Validate archive** — reopens the `.tar.gz` and verifies:
   - Single root directory matches app name
   - `MANIFEST.json` present and valid JSON
   - `SIGNATURE.DS` matches SHA256 of manifest
   - All manifest-listed files exist in archive
   - No unexpected files in archive
   - `start.sh` and `package.ini` present

### `buildignore`

Place a `buildignore` file in the app folder to exclude files from the archive:

```
# Lines starting with # are comments
README.md
SESSION_NOTES.md
test/
docs/
```

- Lines ending in `/` exclude directories
- These are **always** excluded: `__pycache__/`, `METADATA/`, `buildignore`, `.DS_Store`,
  `.git/`, `.venv/`, `node_modules/`, and any dotfile

---

## Upload to Device (SCP)

`python3 make.py install` uploads the built archive to the device via SCP.

- Pure-Python paramiko — works on macOS, Linux, and Windows without any external tools
- Cradlepoint routers use the SCP protocol (not SFTP) at `/app_upload`
- The router drops the connection after receiving the file — this is expected behavior
- Post-upload verification polls `/status/system/sdk` for the app's UUID

---

## Upload to NCM (Fleet Deployment)

`python3 make.py upload` pushes the built package to NetCloud Manager so it can be
deployed to a fleet of routers via NCM groups.

Two ways to use it:

```bash
# By app name (discovers the versioned folder, builds if needed)
python3 make.py upload my_app

# By explicit file path (any .tar.gz, from anywhere)
python3 make.py upload path/to/my_app-v1.0.0.tar.gz
```

The explicit path form is useful when the archive was built elsewhere or lives several
folders deep from where you're running the tool.

- Uses NCM API v1 `device_app_versions/` endpoint
- Multipart form upload with `application/x-gzip` content type
- Auto-detects your NCM regional shard (no manual config needed)
- Returns the NCM version ID and processing state on success

---

## Dev Mode Toggle

`python3 make.py devmode enable` / `devmode disable`

Enables or disables SDK Developer Mode on the connected router through the NCM API,
without needing to log into the NCM web interface.

**Group removal warning:** If the router is in an NCM group, enabling dev mode requires
removing it from that group first (NCM enforces this). The tool detects this and prompts
for confirmation before proceeding:

```
  WARNING: This router is in NCM group: Production Fleet
  Enabling dev mode requires removing it from the group.
  The router will lose any group-pushed configuration.

  Remove from group and enable dev mode? (yes/no):
```

If you decline, the operation is aborted and nothing changes.

How it works:
1. Gets the router's NCM ID from the device itself (`/status/ecm`)
2. Auto-detects the NCM shard for your API credentials
3. Checks group membership — prompts if removal needed
4. Removes from group (if confirmed)
5. Toggles the SDK Developer Mode feature binding

The router typically takes 30-60 seconds to apply the change after the API call.

---

## Differences from Original make.py

| Feature | Original | v2.0 |
|---|---|---|
| Upload | `sshpass`/`scp` (POSIX) or `pscp.exe` (Windows) | Pure paramiko — OS-agnostic |
| Ports | Hardcoded 443/22 | Configurable via settings |
| Project layout | Flat only | Versioned subfolders with heuristic detection |
| Archive name | `{app}.tar.gz` | `{app} v{major}.{minor}.{patch}.tar.gz` |
| CRLF fix | `scan_for_cr()` | Same behavior, runs automatically |
| setup.py hook | Continues on failure | Fails the build (incomplete package = broken) |
| Archive validation | None | 7-point structural check |
| UUID handling | Must pre-exist | Auto-generated if missing |
| Syntax checking | None | All `.py` files pre-build |
| NCM support | None | Shard discovery + upload + dev mode toggle |
| API keys | Not supported | settings file or env vars |
| buildignore | Supported | Supported (same format) |
| Dependencies | `requests`, `OpenSSL` | `paramiko`, `requests` |
| Platform branching | Yes (`sys.platform`) | None |

---

## Troubleshooting

**"ERROR: paramiko not installed"**
→ `pip install paramiko`

**"ERROR: Cannot reach device"**
→ Check `dev_client_ip` and `https_port` in `sdk_settings.ini`. If the device is on a
non-standard port, set `https_port`.

**"ERROR: SSH authentication failed"**
→ Check username/password. If SCP uses a different port, set `ssh_port`.

**"ERROR: No NCM region accepted these credentials"**
→ Verify your API ID and key are correct. If your account is on a region not in the
built-in list, set `NCM_SHARD=<name>` or add `ncm_shard = <name>` to settings.

**"setup.py failed (exit N)"**
→ The app's build hook crashed. Fix it before building — a failed hook means an
incomplete package.

**Build succeeds but app doesn't start on router**
→ Check if `start.sh` uses the correct interpreter (`cppython` for most routers).
Check that the main `.py` file name matches the `[section]` name in `package.ini`.
