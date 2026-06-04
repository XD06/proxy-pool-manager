from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import AppState, utc_now_iso
from .settings import CONFIG_DIR, STATE_PATH


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.warning: str | None = None
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def load(self) -> AppState:
        self.warning = None
        if not self.path.exists():
            state = AppState()
            self.save(state)
            return state
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AppState.model_validate(data)
        except Exception as exc:
            backup = self.path.with_name(f"{self.path.name}.bak-{utc_now_iso().replace(':', '-')}")
            try:
                shutil.copy2(self.path, backup)
                self.warning = f"State file was invalid and has been backed up to {backup.name}: {exc}"
            except Exception:
                self.warning = f"State file was invalid and could not be backed up: {exc}"
            state = AppState()
            self.save(state)
            return state

    def save(self, state: AppState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = utc_now_iso()
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
