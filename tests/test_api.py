from fastapi.testclient import TestClient

from app.main import app
from app.textutil import chunk_text, local_embed, rank_chunks

client = TestClient(app)


def auth_header(username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_chunk_text_terminates_and_keeps_content():
    text = ("年假制度。" * 80).strip()
    chunks = chunk_text(text, size=100, overlap=20)
    assert chunks
    assert "".join(chunks).count("年假") >= text.count("年假") // 2
    assert all(chunks)


def test_rank_prefers_overlapping_chunk():
    leave = "入职满一年享有 10 天带薪年假。"
    other = "量子纠缠实验需要超导装置。"
    rows = [
        {
            "id": "leave",
            "document_id": "doc",
            "filename": "handbook.md",
            "chunk_index": 0,
            "content": leave,
            "embedding": local_embed(leave),
        },
        {
            "id": "other",
            "document_id": "doc",
            "filename": "handbook.md",
            "chunk_index": 1,
            "content": other,
            "embedding": local_embed(other),
        },
    ]
    hits = rank_chunks("年假有几天", local_embed("年假有几天"), rows, top_k=2)
    assert hits
    assert hits[0].chunk_id == "leave"


def test_health_and_admin_login():
    with client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["llm"] == "extractive"
        headers = auth_header("admin", "Admin123!")
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["role"] == "admin"


def test_private_kb_is_hidden_and_answer_cites_source():
    with client:
        admin = auth_header("admin", "Admin123!")
        created = client.post(
            "/api/v1/knowledge-bases",
            headers=admin,
            json={"name": "制度库", "visibility": "private"},
        )
        assert created.status_code == 201, created.text
        kb_id = created.json()["id"]
        guest_list = client.get("/api/v1/knowledge-bases")
        assert kb_id not in {item["id"] for item in guest_list.json()}

        upload = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            headers=admin,
            files={"file": ("leave.txt", "入职满一年的员工每年享有 10 天带薪年假。".encode(), "text/plain")},
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["status"] == "ready"

        denied = client.post("/api/v1/qa/ask", json={"question": "年假有几天", "knowledge_base_id": kb_id})
        assert denied.status_code == 404

        answer = client.post(
            "/api/v1/qa/ask",
            headers=admin,
            json={"question": "年假有几天", "knowledge_base_id": kb_id},
        )
        assert answer.status_code == 200, answer.text
        assert "10 天" in answer.text
        assert "leave.txt" in answer.text

        abstain = client.post(
            "/api/v1/qa/ask",
            headers=admin,
            json={"question": "超导量子比特如何校准", "knowledge_base_id": kb_id},
        )
        assert "没有足够相关的内容" in abstain.text


def test_department_scope_and_faq():
    with client:
        admin = auth_header("admin", "Admin123!")
        department = client.post(
            "/api/v1/departments",
            headers=admin,
            json={"code": "finance", "name": "财务部"},
        )
        assert department.status_code == 201, department.text
        department_id = department.json()["id"]

        registered = client.post(
            "/api/v1/auth/register",
            json={"username": "alice", "password": "Alice123!"},
        )
        assert registered.status_code == 201, registered.text
        alice_id = registered.json()["user"]["id"]
        patched = client.patch(
            f"/api/v1/users/{alice_id}",
            headers=admin,
            json={"department_id": department_id},
        )
        assert patched.status_code == 200, patched.text

        bob = client.post("/api/v1/auth/register", json={"username": "bob", "password": "Bobby123!"})
        assert bob.status_code == 201, bob.text

        alice = auth_header("alice", "Alice123!")
        kb = client.post(
            "/api/v1/knowledge-bases",
            headers=alice,
            json={"name": "财务制度", "visibility": "department", "department_ids": [department_id]},
        )
        assert kb.status_code == 201, kb.text
        kb_id = kb.json()["id"]

        bob_headers = auth_header("bob", "Bobby123!")
        visible = client.get("/api/v1/knowledge-bases", headers=bob_headers)
        assert kb_id not in {item["id"] for item in visible.json()}

        public_attempt = client.post(
            "/api/v1/knowledge-bases",
            headers=alice,
            json={"name": "不该公开", "visibility": "public"},
        )
        assert public_attempt.status_code == 403

        faq = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/faqs",
            headers=alice,
            json={"question": "餐补每天多少钱", "answer": "每天 80 元。"},
        )
        assert faq.status_code == 201, faq.text
        asked = client.post(
            "/api/v1/qa/ask",
            headers=alice,
            json={"question": "餐补每天多少钱", "knowledge_base_id": kb_id},
        )
        assert "每天 80 元" in asked.text
        assert '"kind": "faq"' in asked.text or '"kind":"faq"' in asked.text


def test_bad_upload_is_recorded():
    with client:
        admin = auth_header("admin", "Admin123!")
        created = client.post(
            "/api/v1/knowledge-bases",
            headers=admin,
            json={"name": "上传测试", "visibility": "private"},
        )
        kb_id = created.json()["id"]
        upload = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            headers=admin,
            files={"file": ("notes.exe", b"not-a-document", "application/octet-stream")},
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["status"] == "failed"
