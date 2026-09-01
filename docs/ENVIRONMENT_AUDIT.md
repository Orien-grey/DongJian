# Phase 0 environment audit

Audit time: 2026-09-01 (Asia/Shanghai)  
Project root: `E:\Desktop\ChongZu`

Planning implications in this historical Phase 0 snapshot record the plan at
audit time. The later Architecture Refactor supersedes the Java/Tika direction:
Java/Tika is no longer a planned default dependency or fallback.

This is a read-only observation of the host environment before project bootstrapping. External tools found here are not approved production dependencies.

| Item | Observed result | Location / evidence | Project implication |
| --- | --- | --- | --- |
| Windows | **Windows 11 24H2**, build `26100.4770` | Build/display version from the read-only Windows registry; `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion` reports `ProductName = Windows 10 Home China` | The build `26100` / 24H2 identifies the actual host generation as Windows 11 24H2. The registry `ProductName` is retained as a compatibility/legacy string and is not treated as evidence that the OS is Windows 10. |
| OS/process architecture | `X64` / `X64`; 64-bit OS true | .NET `RuntimeInformation` and `Environment` | Matches the fixed Windows x64 target. |
| PowerShell | `5.1.26100.4768`, Desktop edition | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` | Provisioning/launch scripts should support Windows PowerShell 5.1 unless a project-local newer shell is deliberately added later. |
| Git | `2.54.0.windows.1` | `C:\Program Files\Git\cmd\git.exe` | Available for development only; processing should not require Git. |
| uv | `0.11.21` (`x86_64-pc-windows-msvc`) | `C:\Users\Orion\.local\bin\uv.exe` | Available for future preparation, but it is outside the project and cannot be a production runtime dependency. Future commands must redirect its cache below `cache/uv/`. |
| `python` | `3.13.13` | `D:\Miniconda3\python.exe` | External Conda interpreter; explicitly unsuitable for the fixed CPython 3.11 project runtime. Do not use it as a silent fallback. |
| Python launcher `py` | Not found | `Get-Command py` | Future launchers cannot assume the Windows Python launcher exists. |
| Java | Not found | `Get-Command java` | A pinned project-local Java runtime must be provisioned before Tika is enabled. |
| Git repository | Branch `main`; no commits at initial audit | `git status --short --branch` | Phase 0 files remain uncommitted as requested. |

The normal-privilege CIM query for `Win32_OperatingSystem` returned access denied. No elevation was requested; the audit used read-only registry and .NET APIs instead.

No software, Python package, Java runtime, model, Tika artifact, or other dependency was installed during this audit. No global environment variable or file outside the project root was modified.
