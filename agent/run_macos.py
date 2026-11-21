import asyncio
import logging
import os
from pathlib import Path
import sys

from .app.main import run


def read_env(path, *, raw=False):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise SystemExit(f"Не удалось открыть {path}") from None
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key.lstrip().startswith("#"):
            continue
        value = value.strip()
        if not raw and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("Этот запуск предназначен для macOS")
    config_path = Path(".env.agent").resolve()
    config = read_env(config_path)
    for key in ("DASHBOARD_URL", "AGENT_SHARED_SECRET", "OZON_PRINTER"):
        if not config.get(key):
            raise SystemExit(f"Укажите {key} в .env.agent")
        os.environ[key] = config[key]
    profile_path = config_path.parent / config.get("OZON_ENV_FILE", ".env.ozon")
    profiles = read_env(profile_path, raw=True)
    for suffix in ("1", "2"):
        for field in ("OZON_CLIENT_ID", "OZON_API_KEY"):
            key = f"{field}_{suffix}"
            os.environ[key] = profiles.get(key, "")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())
