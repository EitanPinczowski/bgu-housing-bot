---
name: osrm-docker
description: >
  Get OSRM and Docker Desktop running again on Windows. Use when doctor reports osrm
  unreachable, walk times fall back to the straight-line estimate, the `docker` CLI hangs
  instead of erroring, or Docker Desktop dies with "An unexpected error occurred …
  initializing … listening on unix://… : remove …: The file cannot be accessed by the
  system". Also for "Docker won't start", "OSRM is down", "localhost:5000 is dead", and
  for a wedged WSL where `wsl -l -v` and `wsl --shutdown` hang.
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

**"Is a reparse point" does NOT mean "is broken"** (2026-08-11). A *live* AF_UNIX socket
on Windows is also an un-stat-able reparse point, so the sweep below lists healthy sockets
next to dead ones and cannot tell them apart. The fault is not their existence — it is
Docker failing to REMOVE a stale one at startup. Do not go looking for a way to identify
"the bad one"; clear the root and let Docker recreate what it needs.

### ⛔ Never click "Reset to factory defaults"

It is the other button on that dialog, and it deletes all images and containers —
`osrm_bgu` included, which is a multi-GB rebuild from the Israel PBF.

### The fix

The socket file itself cannot be touched, so **rename its PARENT DIRECTORY**. Docker
recreates it empty on the next start. Renaming beats deleting: it is reversible, and
these directories can hold more than the socket.

1. **Stop every Docker process — this needs ELEVATION.** A normal shell gets *"Access is
   denied"* on `com.docker.backend` and most of the `Docker Desktop` processes, and it
   fails per-process, so a partial kill looks like success.
2. **Clear BOTH roots at once.** The error MOVES TO THE NEXT SOCKET, so fixing one looks
   like it did nothing. Seen in order: `%LOCALAPPDATA%\Docker\run\dockerInference`, then
   `%LOCALAPPDATA%\docker-secrets-engine\engine.sock`.

   Rename the two directories themselves — **not** the socket files, which cannot be
   touched at all:

   ```powershell
   $t = Get-Date -Format 'yyyyMMdd-HHmmss'
   foreach ($d in @("$env:LOCALAPPDATA\Docker\run","$env:LOCALAPPDATA\docker-secrets-engine")) {
       if (Test-Path $d) { Rename-Item $d "$d.stale-$t" }
   }
   ```

3. Start Docker Desktop, then `docker start osrm_bgu`, then re-run the curl probe.
4. **If it crashes again, loop 1–3 rather than diagnosing.** Each attempt gets one socket
   further. Four attempts in one elevated script cleared it on 2026-08-11; chasing them
   one restart at a time costs a UAC prompt per hop.

> **The listing command in this skill used to be wrong, and its wrongness was invisible.**
> `Get-ChildItem -Recurse -Force -ErrorAction SilentlyContinue` returns **nothing** here:
> it cannot enumerate a directory that holds one of these files, and the suppressed error
> made an empty result read as "no orphaned sockets". That produced a confident wrong
> diagnosis on 2026-08-11. To actually list them, enumerate with `cmd` and stat each path
> on its own:
>
> ```powershell
> cmd /c "dir /a /s /b `"$env:LOCALAPPDATA\Docker`"" |
>   ForEach-Object { try { Get-Item -LiteralPath $_ -Force } catch { "UNREADABLE $_" } }
> ```

## When the real fault is WSL, not Docker (2026-08-11)

**Tell.** `wsl -l -v`, `wsl --list --running` and even `wsl --shutdown` all **hang**. Docker
cannot start its VM, so the engine pipe `//./pipe/dockerDesktopLinuxEngine` never appears
and the socket dance above just keeps failing. Clearing sockets cannot fix this.

- **`LxssManager` does not exist** on this machine — modern WSL is **`WSLService`**.
- **It needs elevation.** Unelevated, `Restart-Service WSLService -Force` returns *"Cannot
  open WSLService service on computer '.'"*, and `Stop-Process` on `wslservice` returns
  *"Access is denied"*. Both are permission walls, not evidence that the service is fine.
- The fix is one command in an **elevated** shell, and it worked immediately —
  afterwards `wsl -l -v` answered with both distros `Stopped`:

      Restart-Service WSLService -Force

- To get elevation from a normal session, launch it through UAC and let the user approve:
  `Start-Process powershell.exe -Verb RunAs -ArgumentList '-File','<script>'`. Have the
  script log to a file — you cannot read the elevated process's stdout.

### ⛔ Do not kill `vmmemWSL`

It is the WSL VM holding `docker-desktop-data`, where `osrm_bgu` lives. Killing it is a
hard power-off of that disk. Restarting `WSLService` is the supported reset and makes it
unnecessary.

## Turn off the feature that keeps orphaning sockets

Every crash dialog on 2026-08-11 named the **Inference manager** (Docker's AI / Model
Runner). Nothing in this project uses it — the bot needs the engine and `osrm_bgu`, and
that is all. Disabling it means that listener is never opened, so a crash cannot leave its
socket behind:

    %APPDATA%\Docker\settings-store.json   →   "EnableDockerAI": false

Back the file up first (`settings-store.json.bak-<date>`); it is a one-key revert if the
feature is ever wanted. Cleaning the socket only fixes it until the next crash — this is
the part that stops the loop.

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
