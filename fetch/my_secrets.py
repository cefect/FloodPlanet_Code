"""Load project secrets from the user-level XDG config directory.

This module is the single loader for token-style workflow secrets. It resolves
the project config directory from `parameters.CONFIG_DIR`, which is:
`{XDG_CONFIG_HOME:-$HOME/.config}/USFloods_inference`.

Expected files
--------------
- `secrets.toml`: token/basic-auth secrets loaded into `os.environ`
- `ee-bryantseth.json`: Earth Engine service-account key stored beside the toml

Expected `secrets.toml` shape
-----------------------------
The loader reads all top-level tables and exports their key/value pairs into the
process environment. The current contract is:

```toml
[env]
EARTHDATA_TOKEN = "..."
CDSE_TOKEN = "..."
LANDSATLOOK_USERNAME = "..."
LANDSATLOOK_PASSWORD = "..."
```

How this is used
----------------
- `smk/_A.snakefile` calls `load_secrets()` for non-EP runs before declaring
  required workflow env vars.
- `smk/_B.snakefile` calls `load_secrets()` for non-EP runs before workflow B
  setup.
- `tests/test_workflow_A.py` calls `load_secrets()` so local workflow tests can
  reuse the same config path/contract.
- Earth Engine code does not load credentials from this toml; instead, fetch
  code reads `CONFIG_DIR / "ee-bryantseth.json"` directly.

Environment notes
-----------------
- WSL / direct host execution: defaults to `$HOME/.config/USFloods_inference`
  when `XDG_CONFIG_HOME` is unset.
- devcontainer: `.devcontainer/docker-compose.yml` mounts the host config dir
  and sets `XDG_CONFIG_HOME=/home/cefect/.config`.
- EP: Snakefiles skip `load_secrets()`, so jobs must receive secrets from the
  executor environment instead of a local config directory.
"""

from __future__ import annotations
import os, warnings
from pathlib import Path
import tomllib


from parameters import CONFIG_DIR
#GEE_PROJECT = os.environ.get("EE_PROJECT")


GEE_KEY_FILE = CONFIG_DIR / "ee-bryantseth.json"

if not GEE_KEY_FILE.exists():
    warnings.warn(f"GEE key file does not exist at {GEE_KEY_FILE}")

 

def secrets_file() -> Path:
    override = os.environ.get("SECRETS_ENV_FILE")
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR / "secrets.toml"

def load_secrets(override: bool = False) -> dict[str, str]:
    """
    Load KEY=VALUE or 'export KEY=VALUE' lines into os.environ.
    Does nothing if secrets file is missing.
    """
    p = secrets_file()
    if not p.exists():
        warnings.warn(f"Secrets file not found at {p}. Skipping loading secrets.")
        return {}

    data = tomllib.loads(p.read_text())
    loaded = {}
    for section in data.values():
        for k, v in section.items():
            if override or k not in os.environ:
                os.environ[k] = str(v)
                loaded[k] = str(v)

    # Ensure expected secret names are populated
    required_keys = [
        "EARTHDATA_TOKEN",
        "CDSE_TOKEN",
        "LANDSATLOOK_USERNAME",
        "LANDSATLOOK_PASSWORD",
    ]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required secrets: {', '.join(missing)}")

    return loaded
