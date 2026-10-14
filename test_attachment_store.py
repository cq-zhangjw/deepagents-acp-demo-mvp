"""Attachment store unit tests (run with a temp DB_ROOT to isolate)."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmp = tempfile.mkdtemp(prefix="att_test_")
os.environ["DB_ROOT"] = tmp
os.environ.pop("ATTACHMENT_ROOT", None)

import utils.attachment_store as store  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok: {name}")


try:
    # 1. register two images + one file across two sessions
    r1 = store.register(b"PNGDATA-1", "shot_1.png", "sess-1", "msg-1", "image", "image/png", "browser shot 1")
    r2 = store.register(b"PNGDATA-2", "shot_2.png", "sess-1", None, "image", "image/png", "browser shot 2")
    r3 = store.register(b"TEXTDATA", "note.txt", "sess-2", "msg-9", "file", "text/plain", "")
    check("register returns file_id", len(r1["file_id"]) == 32)
    check("recordType for moofile UI", r1["recordType"] == "record")
    check("get by id", store.get(r1["file_id"])["session_id"] == "sess-1")
    check("blob exists", store.resolve_abs(r1).is_file())
    check("blob name = file_id.ext", store.resolve_abs(r1).name == f"{r1['file_id']}.png")

    # 2. meta.json + db dir naming convention
    db_dirs = [p for p in Path(tmp).glob("db_attachments_*") if p.is_dir()]
    check("db_attachments_* dir created", len(db_dirs) == 1)
    check("meta.json written", (db_dirs[0] / "meta.json").is_file())
    check("_upload dir exists", (db_dirs[0] / "_upload").is_dir())

    # 3. list queries
    check("list_by_session count", len(store.list_by_session("sess-1")) == 2)
    check("list_by_message count", len(store.list_by_message("sess-1", "msg-1")) == 1)
    check("total count", store.count() == 3)

    # 4. delete one (record + blob), idempotent
    blob2 = store.resolve_abs(r2)
    check("delete one", store.delete(r2["file_id"]) is True)
    check("record gone", store.get(r2["file_id"]) is None)
    check("blob gone", not blob2.exists())
    check("delete again idempotent", store.delete(r2["file_id"]) is False)
    check("count after delete", store.count() == 2)

    # 5. delete by message
    check("delete_by_message", store.delete_by_message("sess-1", "msg-1") == 1)
    check("count after msg delete", store.count() == 1)
    check("remaining sess-1", len(store.list_by_session("sess-1")) == 0)

    # 6. delete by session
    check("delete_by_session", store.delete_by_session("sess-2") == 1)
    check("count after session delete", store.count() == 0)
    check("delete_by_session again 0", store.delete_by_session("sess-2") == 0)
    upload = [p for p in (db_dirs[0] / "_upload").iterdir()]
    check("no orphan blobs", len(upload) == 0)

    print(f"\nALL {passed} CHECKS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
