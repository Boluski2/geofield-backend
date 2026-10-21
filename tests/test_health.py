
from fastapi.testclient import TestClient

from main import app
from unittest.mock import MagicMock, patch

from main import create_access_token


client = TestClient(app)


def test_health_endpoint():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "message": "GeoField Auth API is running"
    }


def test_me_rejects_request_without_token():
    response = client.get("/me")

    assert response.status_code in (401, 403)

def test_me_rejects_invalid_token():
    response = client.get(
        "/me",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401

def test_login_rejects_missing_credentials():
    response = client.post("/login", data={})

    assert response.status_code == 422

def test_me_rejects_token_for_deleted_user():
    token = create_access_token(999999)

    mock_connection = MagicMock()
    mock_cursor = MagicMock()
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = None

    with patch("main.get_connection", return_value=mock_connection):
        response = client.get(
            "/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "User no longer exists"