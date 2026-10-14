"""Attachment API + cascade-deletion tests (isolated temp DB_ROOT)."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmp = tempfile.mkdtemp(prefix="att_api_test_")
os.environ["DB_ROOT"] = tmp
os.environ.pop("ATTACHMENT_ROOT", None)
os.environ.pop("SESSION_CONTENT_ROOT", None)
os.environ["OPEN_BROWSER"] = "0"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.attachments import router as att_router  # noqa: E402
import utils.attachment_store as store  # noqa: E402
from utils.session_context import (  # noqa: E402
    delete_message,
    delete_session,
    save_context,
    update_message,
)

app = FastAPI()
app.include_router(att_router)
client = TestClient(app)

passed = 0


def check(name, cond):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok: {name}")


try:
    # ---- API: upload -> list -> serve -> delete ----
    r = client.post(
        "/api/attachments",
        data={"session_id": "sess-api", "message_id": "m-1", "kind": "image", "alt_text": "api test"},
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nFAKEDATA", "image/png")},
    )
    check("upload ok", r.status_code == 200 and r.json()["ok"])
    body = r.json()
    fid = body["file_id"]
    check("upload returns url", body["url"] == f"/api/attachments/{fid}")

    r = client.get(f"/api/attachments/{fid}")
    check("serve blob", r.status_code == 200 and r.content.startswith(b"\x89PNG"))
    check("serve mime", r.headers["content-type"] == "image/png")

    r = client.get(f"/api/attachments/{fid}", params={"session_id": "other"})
    check("cross-session forbidden", r.status_code == 403)
    r = client.get(f"/api/attachments/{fid}", params={"session_id": "sess-api"})
    check("owner session allowed", r.status_code == 200)

    r = client.get("/api/attachments", params={"session_id": "sess-api"})
    check("list ok", r.status_code == 200 and len(r.json()["attachments"]) == 1)
    check("list has alt_text", r.json()["attachments"][0]["alt_text"] == "api test")

    r = client.delete(f"/api/attachments/{fid}")
    check("delete ok", r.status_code == 200 and r.json()["removed"] is True)
    check("gone after delete", client.get(f"/api/attachments/{fid}").status_code == 404)

    # ---- Cascade: message deletion clears bound attachments ----
    a1 = store.register(b"A1", "a1.png", "sess-c", "msg-del", "image", "image/png", "a1")
    # message content that references the image placeholder
    msg = {
        "type": "ai",
        "data": {"content": f"看截图 [图片: {a1['file_id']}]", "id": "msg-del", "name": None, "additional_kwargs": {}, "response_metadata": {}, "type": "ai"},
        "kwargs": {"content": f"看截图 [图片: {a1['file_id']}]", "id": "msg-del", "name": None, "additional_kwargs": {}, "response_metadata": {}, "type": "ai"},
    }
    save_context("sess-c", [msg])
    check("cascade: attachment exists before", store.count() == 1)
    ok = delete_message("sess-c", "msg-del")
    check("delete_message ok", ok is True)
    check("cascade: attachment cleared after msg delete", store.count() == 0)

    # ---- Cascade: editing away the placeholder clears the attachment ----
    a2 = store.register(b"A2", "a2.png", "sess-e", "msg-edit", "image", "image/png", "a2")
    msg2 = {
        "type": "ai",
        "data": {"content": f"图 [图片: {a2['file_id']}]", "id": "msg-edit", "name": None, "additional_kwargs": {}, "response_metadata": {}, "type": "ai"},
        "kwargs": {"content": f"图 [图片: {a2['file_id']}]", "id": "msg-edit", "name": None, "additional_kwargs": {}, "response_metadata": {}, "type": "ai"},
    }
    save_context("sess-e", [msg2])
    check("edit cascade: exists before", store.count() == 1)
    new_msg = dict(msg2)
    new_msg["kwargs"] = dict(msg2["kwargs"])
    new_msg["kwargs"]["content"] = "图已移除"
    new_msg["data"]["content"] = "图已移除"
    ok = update_message("sess-e", new_msg)
    check("update_message ok", ok is True)
    check("edit cascade: attachment cleared", store.count() == 0)

    # ---- Cascade: session deletion clears all attachments ----
    a3 = store.register(b"A3", "a3.png", "sess-d", None, "image", "image/png", "a3")
    a4 = store.register(b"A4", "a4.png", "sess-d", "m-x", "image", "image/png", "a4")
    check("session cascade: 2 exist before", store.count() == 2)
    save_context("sess-d", [])
    ok = delete_session("sess-d")
    check("delete_session ok", ok is True)
    check("session cascade: all cleared", store.count() == 0)

    print(f"\nALL {passed} CHECKS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
