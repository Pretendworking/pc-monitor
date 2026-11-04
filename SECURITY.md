# Security and network behavior

PC Monitor is a local Windows dashboard. Its source contains no remote-command endpoint. Its single-instance activation channel is restricted to the current Windows user and only reveals the existing window.

## Expected connections and permissions

- Normal hardware/process monitoring uses local operating-system counters and supported sensor providers.
- The app may read an existing Libre Hardware Monitor endpoint at 127.0.0.1:8085. PC Monitor does not start an HTTP server.
- Keep Libre Hardware Monitor's separate Remote Web Server disabled. Prefer its local WMI provider. Some versions can fall back to listening on all interfaces, even if an unrecognized address was configured.
- Cloudflare speed tests transfer generated data only when clicked. Cloudflare sees the public IP used for those requests. Optional ICMP quality monitoring contacts the selected Cloudflare or Google target every 30 seconds while enabled.
- Favorite executable shortcuts launch only user-selected applications. The optional Windows-startup setting changes a current-user startup entry.
- FPS administrator restart requires an explicit in-app choice and Windows approval. The whole app and applications launched from its dock then inherit that access until closed. Normal operation does not require elevation.

## Files excluded from this repository

Personal settings, diagnostics, logs, CSV exports, backups, credentials, Python environments, caches, shortcuts and compiled executables are excluded. Build the launcher from its source; obtain optional PresentMon only from its official release.

## Audit scope

On 2026-11-04, the reviewed local installation passed a Microsoft Defender custom scan with no threats reported. 4,045 installed dependency files matched their package RECORD hashes; checked Python and Qt core signatures were valid. Application regression checks: 29 passed.

These results describe that installation at that time. They do not guarantee future dependencies, prove that backdoors are impossible, or certify the whole computer's open ports. Live host-wide port enumeration and a dependency vulnerability-database scan were not completed. Do not commit private logs or machine-specific configuration when reporting an issue.
