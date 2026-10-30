from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_and_data_dir(tmp_path, services):
    data = tmp_path / "data"
    client = TestClient(create_app(data, services=services))
    assert client.get("/healthz").text == "ok"
    assert data.is_dir()
