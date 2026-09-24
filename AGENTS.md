# Repository Guidelines

## Project Structure

This repository is an Omarchy/Quickshell plugin. `Panel.qml` defines the bar panel, `Service.qml` connects the UI to helper processes, and `Model.js` shapes API data for the UI. Standalone executables live in `bin/`; shared DLNA primitives are in `lib/dlna.py`. Plugin metadata is in `manifest.json`. User and developer documentation, plus screenshots, are in `README.md` and `docs/`. There is currently no dedicated test directory.

## Development and Checks

There is no build system or test runner; Quickshell loads the QML files directly. The helpers can be exercised independently, for example:

```sh
bin/dromify-api status
bin/dromify-player status
bin/dromify-output devices
DROMIFY_DLNA_DEBUG=1 bin/dromify-dlna devices
```

These commands may require the runtime dependencies and a configured server or local network. For quick syntax checks, use `bash -n bin/dromify-*` and `python3 -m py_compile lib/dlna.py`. Test UI changes in a running Quickshell/Omarchy session.

## Style and Conventions

Follow the existing style: two-space indentation in QML, JavaScript, and shell scripts; Python uses four spaces and standard-library type hints where useful. Keep helper commands standalone and document their usage in their headers or the README. Use descriptive camelCase for QML/JS properties and functions, and lowercase hyphenated names for executables. Keep credentials and authenticated URLs out of command-line arguments and debug logs; preserve the HTTPS requirement for non-loopback server URLs.

## Testing Changes

No automated test framework or coverage target is configured. For behavior changes, run the relevant helper command and manually verify the affected panel or playback path. For DLNA changes, check discovery and playback against a renderer when available. Avoid logging passwords, tokens, or full authenticated stream URLs.

## Commits and Pull Requests

Recent commits use short, imperative, lowercase subjects with a conventional prefix, such as `fix(ui): ...`, `feat(dlna): ...`, or `docs: ...`; use that pattern and keep each commit focused. A pull request should explain the user-visible change, list relevant manual checks, link related issues when applicable, and include screenshots for visible UI changes. Call out dependency or security-sensitive behavior changes.

## Configuration and Documentation

Do not commit personal server settings, credentials, or machine-specific state. Update `README.md` or the relevant file under `docs/` when changing setup, commands, dependencies, or runtime behavior.

## Volume Control Rules

- Volume controls the currently selected playback output. Local output controls mpv; DLNA output controls the renderer's UPnP RenderingControl `Master` channel. Do not describe it as the desktop mixer volume.
- Treat local mpv volume as 0–100. DLNA volume values are renderer-native: use the minimum, maximum, and step declared for `GetVolume` in its SCPD when available. Align writes to the declared step. Never assume every renderer uses 0–100.
- If a DLNA renderer does not publish a maximum, show its native value and use relative step controls. If it lacks `GetVolume`, disable the control and explain why; if it lacks `SetVolume`, show the reported value as read-only.
- Keep “unsupported,” “unknown range,” and a temporary read/write error distinct. Never coerce a missing/null DLNA volume to zero or silently substitute 100.
- Read volume when the Output popup opens and after writes or output changes. Do not add a `GetVolume` SOAP call to the frequent transport status poll.
- A slider previews while dragging and sends only on release. Serialize volume writes, keep the newest pending value, and discard stale reads after output changes or newer user input.
