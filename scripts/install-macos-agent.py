import plistlib
import sys
from pathlib import Path


def main():
    if sys.platform != "darwin":
        raise SystemExit("LaunchAgent устанавливается на macOS")

    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / "bin" / "python"
    if not python.is_file() or not (root / ".env.agent").is_file():
        raise SystemExit("Сначала создайте .venv и настройте .env.agent")

    label = "com.fundvam.books.agent"
    directory = Path.home() / "Library" / "LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    logs = root / "data"
    logs.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{label}.plist"

    with path.open("wb") as output:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": [str(python), "-m", "agent.run_macos"],
            "WorkingDirectory": str(root),
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "StandardOutPath": str(logs / "macos-agent.log"),
            "StandardErrorPath": str(logs / "macos-agent.log"),
        }, output)
    print(path)


if __name__ == "__main__":
    main()
