---
name: osrm-docker
description: >
  Get OSRM and Docker Desktop running again on Windows. Use when doctor reports osrm
  unreachable, walk times fall back to the straight-line estimate, the `docker` CLI hangs
  instead of erroring, or Docker Desktop dies with "An unexpected error occurred …
  initializing … listening on unix://… : remove …: The file cannot be accessed by the
  system". Also for "Docker won't start", "OSRM is down", "localhost:5000 is dead".
---

# OSRM / Docker recovery

OSRM gives the amber walk time (min over campus gates), which is what the 20-minute
AMBER boundary is measured with. The bot still classifies without it — a calibrated
straight-line estimate stands in — so **nothing crashes, the numbers just quietly get
worse**. That is why this is worth fixing before a `replay --apply`, not after.

## First: is it just stopped?

    docker start osrm_bgu
    curl.exe "http://localhost:5000/route/v1/foot/34.79,31.25;34.8015,31.2622?overview=false"

Expect `"code":"Ok"` and a duration. If that works, you are done.

## The orphaned-socket failure (cost most of an afternoon, 2026-08-05)

**Symptoms.** Docker Desktop dies with *"An unexpected error occurred … initializing X:
listening on unix://…: remove …: The file cannot be accessed by the system"*. The
`docker` CLI **hangs** rather than erroring, and `wsl -l -v` shows `docker-desktop`
**Stopped** with no `dockerd` inside it.

**Cause.** Those `.sock` files are zero-length **reparse points** whose backing object
died with the crash. Windows can neither open nor delete them, so `Remove-Item` fails
with the same error Docker is getting.

**A reboot does NOT clear them** — they are on disk.

### ⛔ Never click "Reset to factory defaults"

It is the other button on that dialog, and it deletes all images and containers —
`osrm_bgu` included, which is a multi-GB rebuild from the Israel PBF.

### The fix

The socket file itself cannot be touched, so **rename its PARENT DIRECTORY**. Docker
recreates it empty on the next start. Renaming beats deleting: it is reversible, and
these directories can hold more than the socket.

1. Stop every Docker process.
2. **Sweep for all of them at once.** The error MOVES TO THE NEXT SOCKET, so fixing one
   looks like it did nothing. Seen in order:
   `%LOCALAPPDATA%\Docker\run\dockerInference`, then
   `%LOCALAPPDATA%\docker-secrets-engine\engine.sock`.

   Find every candidate under both roots:

   ```powershell
   Get-ChildItem "$env:LOCALAPPDATA\Docker","$env:LOCALAPPDATA\docker-secrets-engine" `
     -Recurse -Force -ErrorAction SilentlyContinue |
     Where-Object { $_.Attributes -match "ReparsePoint" } |
     Select-Object FullName, Attributes
   ```

3. Rename each **parent directory** (e.g. `run\dockerInference` →
   `run\dockerInference.bak`).
4. Start Docker Desktop, then `docker start osrm_bgu`, then re-run the curl probe.

## Verify, and re-check what depended on it

    python doctor.py

The `osrm` row should read PASS. Anything applied while it was down used the
straight-line estimate — if a `replay --apply` ran in that window, run it again now that
real routing is back.

## Notes

- `osrm.alive()` caches its probe **once per process**, so a long-lived
  `serve_dashboard.py` started while OSRM was down keeps believing it is down. Restart
  the server too.
- `BUFFER_METERS` is deprecated; the boundary is a walk time, not a radius.
- `POST /api/walk` on the dashboard deliberately has **no straight-line fallback** —
  there is no honest estimate of *which way* you walk, so with the router down it draws
  nothing rather than a line through the railway.
