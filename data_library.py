"""Local data selection, independent of Instagram authentication."""
import json

import export_data
from export_data import ExportError, ExportStore, atomic_json


def local_store():
    return ExportStore("local")


def selected_store():
    path = export_data.DATA_ROOT / "selection.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        return ExportStore(data.get("source"))
    except FileNotFoundError:
        return local_store()
    except (ValueError, UnicodeError) as exc:
        raise ExportError("선택한 자료의 기록이 손상되었습니다. 자료를 다시 선택해주세요.") from exc


def select_source(source, require_data=True):
    store = ExportStore(source)
    if require_data and source != "local" and store.metadata() is None:
        raise ExportError("선택한 자료가 없습니다.")
    atomic_json(export_data.DATA_ROOT / "selection.json", {"source": source})


def sources():
    items = [{"id": "local", "label": "가져온 파일"}]
    if export_data.DATA_ROOT.exists():
        for directory in sorted(export_data.DATA_ROOT.iterdir()):
            if not directory.is_dir() or not directory.name.isascii() or not directory.name.isdigit():
                continue
            try:
                metadata = ExportStore(directory.name).metadata()
            except (ExportError, OSError):
                continue
            if metadata:
                items.append({"id": directory.name, "label": "@" + (metadata.get("username") or directory.name)})
    return items


def status():
    items = sources()
    try:
        store = selected_store()
        metadata = store.metadata()
        return {"source": store.directory.name, "sources": items, "has_data": metadata is not None,
                "saved_at": metadata.get("saved_at") if metadata else None}
    except (ExportError, OSError) as exc:
        return {"source": None, "sources": items, "has_data": False,
                "message": str(exc) if isinstance(exc, ExportError) else "자료 기록을 읽을 수 없습니다."}
