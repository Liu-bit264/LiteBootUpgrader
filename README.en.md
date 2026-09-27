# LiteBootUpgrader — Host Tool for LiteBootLoader

[简体中文](README.md) | English

The official host tool for
[LiteBootLoader](https://github.com/Liu-bit264/LiteBootLoader) (the firmware repo,
sibling directory `../LiteBootLoader` locally): one-click serial upgrade, jump to APP,
reset, workflow self-test and a GUI. The **sole implementation** of the upgrade flow
lives in this repo's `bl_upgrade.py`; the protocol specification (frame format, command
table, state machine) is defined in the firmware repo's `docs/protocol.md`.

## Repository Layout

```text
LiteBootUpgrader/
├── bl_upgrade.py          CLI & protocol library (run_upgrade/ensure_bl/cmd_retry with log/progress callbacks)
├── bl_upgrade_gui.py      tkinter GUI (one-click upgrade / jump / reset / PING)
├── bl_powerloss_drill.py  Acceptance #9 power-loss recovery drill (depends on bl_upgrade)
├── test_host_protocol.py  Host-side hardware-free unit tests (CRC/frames/parser/CLI/GUI state, 25 checks)
├── bl_upgrade_gui.bat     Double-click GUI launcher
├── build_exe.bat          Windows executable build script (PyInstaller → dist/)
├── docs/gui.png           GUI screenshot
├── LICENSE                MIT license
└── dist/                  Build artifacts (not committed, see .gitignore)
```

## Quick Start

### Option A: Prebuilt executables (no Python needed)

Double-click `dist\bl_upgrade_gui.exe` (build instructions below), or use
`dist\bl_upgrade.exe` on the command line. Single-file, no installation; distribution
is just copying the exe.

### Option B: Run from source (dependency isolation recommended)

> **Dependency isolation (recommended)**: install Python dependencies in an isolated
> environment (`uv run --with` or a venv) instead of polluting the global interpreter;
> wheels stay in the package manager cache. The examples below use `uv`; any Python
> with pyserial installed works too.

```bash
# GUI (or double-click bl_upgrade_gui.bat)
uv run --python 3.12 --with pyserial bl_upgrade_gui.py

# CLI
uv run --python 3.12 --with pyserial bl_upgrade.py upgrade <image.bin> --port COM4
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4
```

## GUI Usage

**Basic mode** (default):

![GUI basic mode](docs/gui.png)

**Advanced mode** — tick the "advanced mode" checkbox at the bottom → confirm in the
dialog → the UI restarts with the full feature set; unticking likewise confirms and
restarts back. The mode is stored in `~/.litebootupgrader_gui.json`:

![GUI advanced mode](docs/gui_adv.png)

- **One-click upgrade**: works from any state (BL or APP) — when the target runs the
  APP, it automatically takes the "request back to BL" path (SET_META bl_request →
  reset → BL consumes the flag), with a progress bar and log throughout;
- **Jump / Reset / PING**: single-command actions, serial banner read back into the log pane;
- **Image pane**: shows the size and CRC32 after 4-byte padding (same scale as VERIFY);
- **Advanced panel (advanced mode only)**: INFO / META / ERASE (double confirmation) /
  SELFTEST / VERIFY / LISTEN / RAW / SETMETA, plus baud-rate and pace(ms) options —
  behavior aligned one-to-one with the CLI;
- **Serial exclusivity**: close VOFA+ / other serial monitors first (a COM port is exclusive).

## CLI Subcommands

| Subcommand | Purpose |
|---|---|
| `upgrade <bin>` | One-click upgrade (ensure_bl → erase → chunked write → verify) |
| `selftest` | 15-step hardware-in-the-loop upgrade self-test |
| `ping / info / meta` | Handshake / BL info & telemetry / parameter-area metadata |
| `erase / verify <size> <crc>` | Manual step-by-step operations |
| `jump / reset` | Jump to APP / reset |
| `setmeta <f> <v> / raw <hex> / listen <sec>` | Metadata / raw bytes / monitor |

Common options: `--port` (default COM4, set to your actual port), `--baud 115200`,
`--pace <ms>` (inter-command delay, for timing experiments). Run
`bl_upgrade.py -h` (`--help`) to list all subcommands and options; `--version` prints
the version.

### GUI vs CLI Capability Matrix

| Interface | Coverage |
|---|---|
| GUI basic mode (default) | One-click upgrade, jump to APP, reset, PING + serial enumeration/refresh, image CRC32 preview, progress bar/log pane |
| GUI advanced mode (tick, confirm, UI restart) | On top of basic mode: all 8 CLI debug/diagnostic operations (info / meta / erase / verify / selftest / listen / raw / setmeta) plus baud-rate and pace options — **feature parity with the CLI** |
| CLI | All 12 subcommands + `-h/--help`, `--version` |

### Retry & Timeout Conventions (mirrors the firmware repo's docs/protocol.md §7)

Single-command response timeout 1000 ms (ERASE/VERIFY 5000 ms), resend on timeout ≤3
times at 2.2 s intervals (≥ the BL's 2000 ms in-frame parser reset window); error
status codes are not retried (they are real answers).

## Compatibility

| Item | Value |
|---|---|
| Scope | Works over the LiteBootLoader upgrade protocol (VER 0x01) and is **decoupled from the chip model**: any LiteBootLoader BL implementing this protocol is supported; validated combination = BL 0.1.0 + STM32F103C8T6; multi-chip parameterization follows the firmware repo's CSP roadmap |
| Protocol version | VER 0x01 (firmware repo `docs/protocol.md`) |
| Companion firmware | LiteBootLoader BL 0.1.0+ |
| Image limit | Currently bound to the STM32F103C8T6 partition, 46 KiB (0xB800), auto-padded to 4-byte alignment with 0xFF; other chip partitions pending CLI parameterization (see the firmware repo's CSP roadmap) |
| Image verification | CRC-32/ISO-HDLC (zlib-compatible); frame check CRC16/MODBUS |
| Serial | 115200 8N1, Windows COMx / Linux ttyUSBx |

## Testing

```bash
# Host-side unit tests (no hardware; 25 checks: CRC KAT / frame templates / parser /
# response parsing / SEQ & CMD mismatch discarding / GUI import / CLI parser / GUI state persistence)
uv run --python 3.12 --with pyserial python test_host_protocol.py

# Hardware-in-the-loop: 15-step upgrade self-test (board online)
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4

# Acceptance #9 drill: write storm + reset injection (board + debug adapter required)
uv run --python 3.12 --with pyserial --with pyocd bl_powerloss_drill.py --port COM4 --rounds 10
```

## Building Windows Executables

Double-click `build_exe.bat`, or run:

```bash
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --onefile --windowed --name bl_upgrade_gui bl_upgrade_gui.py
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --clean --onefile --console --name bl_upgrade bl_upgrade.py
```

Artifacts land in `dist\` (`build/` and `*.spec` are intermediates, not committed).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Opening the port fails (access denied) | Close the occupier: VOFA+ / serial monitor / another instance of this tool |
| No COM port found | Confirm the debug adapter's CDC serial in Device Manager; try another USB cable/port (flaky contacts seen in practice) |
| All commands time out | The target may run the APP or sit halted under a debugger: reset it with the debugger and retry |
| Occasional timeout then success | USB-CDC jitter triggers the BL's 2 s in-frame timeout — normal, resent automatically |
| `verify failed: CRC_ERROR` | An interrupted upgrade left the image incomplete; run `upgrade` again (full erase + rewrite) |

## Version

Current version **v1.2.0**; history and change details in [CHANGELOG.md](CHANGELOG.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the issue and PR workflow.

## License

[MIT](LICENSE) © 2026 Qingc. LiteBootLoader is also MIT-licensed (its bundled
third_party/CMSIS retains its original license, see its LICENSES.md).
