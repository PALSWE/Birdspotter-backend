import json
from pathlib import Path
from typing import Optional


class FileStorage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(exist_ok=True)

    def read_json(self, filename: str) -> Optional[dict]:
        path = self.data_dir / filename

        if not path.exists():
            return None

        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, filename: str, payload: dict, *, pretty: bool = True) -> None:
        path = self.data_dir / filename

        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            ),
            encoding="utf-8",
        )

    def read_text(self, filename: str) -> Optional[str]:
        path = self.data_dir / filename

        if not path.exists():
            return None

        return path.read_text(encoding="utf-8")
