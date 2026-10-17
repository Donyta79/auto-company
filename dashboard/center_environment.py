"""Read existing environment names; probe only an explicitly selected domain."""
import os
import subprocess


def candidates():
    if os.name != "nt":
        return [{"id": "posix", "label": "Local POSIX", "domain": {"platform": "posix"}}]
    try:
        result = subprocess.run(["wsl.exe", "--list", "--quiet"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=15, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = result.stdout.decode("utf-16-le", errors="replace").replace("\x00", "").splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    # Resolving a default user would start a stopped distro. The operator must
    # supply the user before check/connect can execute in the selected domain.
    return [{"id": "wsl:" + name.strip(), "label": name.strip(), "requiresUser": True,
             "domain": {"platform": "wsl", "distribution": name.strip()}}
            for name in names if name.strip()]


def check(adapter):
    prefix = adapter._wsl() + ["--exec"] if adapter.domain.get("platform") == "wsl" else []
    checks = []
    for code, command in (("python", ["python3", "--version"]), ("git", ["git", "--version"])):
        try:
            adapter._execute(prefix + command)
            checks.append({"code": code, "ok": True, "message": code + " is available"})
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append({"code": code, "ok": False, "message": "Selected environment is missing " + code,
                           "detail": type(exc).__name__})
    return checks
