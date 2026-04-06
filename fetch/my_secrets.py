"""Load project secrets from the user-level XDG config directory.

This module is the single loader for token-style workflow secrets. It resolves
the project config directory from the XDG config root:
`{XDG_CONFIG_HOME:-$HOME/.config}/USFloods_inference`.

Expected files
--------------
- `secrets.toml`: token/basic-auth secrets loaded into `os.environ`
- `ee-bryantseth.json`: Earth Engine service-account key stored beside the toml

Expected `secrets.toml` shape
-----------------------------
The loader reads all top-level tables and exports their key/value pairs into the
process environment. The current local contract includes:

```toml
[env]
PL_API_KEY = "..."
EARTHDATA_TOKEN = "..."
CDSE_TOKEN = "..."
LANDSATLOOK_USERNAME = "..."
LANDSATLOOK_PASSWORD = "..."
```
"""

from __future__ import annotations

import os, tomllib, warnings
from pathlib import Path


def get_config_dir() -> Path:
    """Return the project config directory under the XDG config root."""
    xdg_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return xdg_root / "USFloods_inference"


CONFIG_DIR = get_config_dir()
GEE_KEY_FILE = CONFIG_DIR / "ee-bryantseth.json"

if not GEE_KEY_FILE.exists():
    warnings.warn(f"GEE key file does not exist at {GEE_KEY_FILE}")


def secrets_file() -> Path:
    """Return the secrets TOML filepath, honoring an explicit override."""
    override = os.environ.get("SECRETS_ENV_FILE")
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR / "secrets.toml"


def load_secrets(override=False, required_keys=None) -> dict[str, str]:
    """Load configured secrets into `os.environ` and validate any required keys."""
    required_keys = list(required_keys or [])

    p = secrets_file()
    if not p.exists():
        warnings.warn(f"Secrets file not found at {p}. Skipping loading secrets.")
        return {}

    data = tomllib.loads(p.read_text())
    loaded = {}

    # Flatten all top-level tables into the process environment.
    for section_name, section_d in data.items():
        assert isinstance(section_d, dict), section_name
        for key, value in section_d.items():
            if override or key not in os.environ:
                os.environ[key] = str(value)
                loaded[key] = str(value)

    missing = [key for key in required_keys if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Missing required secrets: {', '.join(missing)}")

    return loaded


def load_planet_secrets(override=False) -> dict[str, str]:
    """Load only the Planet SDK credential contract used by fetch workflows."""
    return load_secrets(override=override, required_keys=["PL_API_KEY"])


def get_planet_api_key() -> str:
    """Return the Planet API key, loading it from config when needed."""
    if not os.environ.get("PL_API_KEY"):
        load_planet_secrets(override=False)
    api_key = os.environ.get("PL_API_KEY")
    assert api_key, "PL_API_KEY is required for Planet SDK workflows"
    return api_key
