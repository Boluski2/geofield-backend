from io import BytesIO
from fastapi.testclient import TestClient
from main import app, get_current_user
from unittest.mock import MagicMock, patch

from main import create_access_token
import pytest
from photo_storage import (
    photo_public_id,
    upload_photo,
    delete_photo,
    download_photo,
)

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


def test_photo_public_id_is_stable():
    assert photo_public_id(
        2,
        "65dd48f4e73a469e847f6047d5e1e817.jpg",
    ) == (
        "geofield/observations/2/"
        "65dd48f4e73a469e847f6047d5e1e817"
    )


def test_photo_public_id_rejects_invalid_filename():
    with pytest.raises(ValueError):
        photo_public_id(2, "../photo.jpg")


def test_photo_public_id_rejects_invalid_observation_id():
    with pytest.raises(ValueError):
        photo_public_id(0, "photo.jpg")


@patch("photo_storage.cloudinary.uploader.upload")
@patch("photo_storage.configure_cloudinary")
def test_upload_photo_uses_authenticated_storage(
    mock_configure,
    mock_upload,
):
    mock_upload.return_value = {"public_id": "test-photo"}

    result = upload_photo(b"fake-image-data", 2, "photo.jpg")

    assert result["public_id"] == "test-photo"
    mock_configure.assert_called_once()
    mock_upload.assert_called_once()
    uploaded_file = mock_upload.call_args.args[0]

    assert isinstance(uploaded_file, BytesIO)
    assert uploaded_file.getvalue() == b"fake-image-data"
    assert mock_upload.call_args.kwargs == {
        "public_id": "geofield/observations/2/photo",
        "resource_type": "image",
        "type": "authenticated",
        "overwrite": False,
        "unique_filename": False,
        "use_filename": False,
    }


@patch("photo_storage.cloudinary.uploader.destroy")
@patch("photo_storage.configure_cloudinary")
def test_delete_photo_uses_authenticated_storage(
    mock_configure,
    mock_destroy,
):
    mock_destroy.return_value = {"result": "ok"}

    result = delete_photo(2, "photo.jpg")

    assert result["result"] == "ok"
    mock_configure.assert_called_once()
    mock_destroy.assert_called_once_with(
        "geofield/observations/2/photo",
        resource_type="image",
        type="authenticated",
        invalidate=True,
    )


@patch("photo_storage.cloudinary.uploader.destroy")
@patch("photo_storage.configure_cloudinary")


def test_delete_photo_targets_the_expected_asset(
    mock_configure,
    mock_destroy,
):
    mock_destroy.return_value = {"result": "ok"}

    delete_photo(2, "65dd48f4e73a469e847f6047d5e1e817.jpg")

    mock_destroy.assert_called_once_with(
        "geofield/observations/2/65dd48f4e73a469e847f6047d5e1e817",
        resource_type="image",
        type="authenticated",
        invalidate=True,
    )


@patch("photo_storage.cloudinary.uploader.destroy")
@patch("photo_storage.configure_cloudinary")
@pytest.mark.parametrize(
    "cloudinary_result",
    [
        {"result": "not found"},
        {"unexpected": "response"},
        None,
    ],
)
def test_delete_photo_rejects_unconfirmed_deletion(
    mock_configure,
    mock_destroy,
    cloudinary_result,
):
    mock_destroy.return_value = cloudinary_result

    with pytest.raises(RuntimeError, match="did not confirm photo deletion"):
        delete_photo(2, "photo.jpg")

    mock_configure.assert_called_once()
    mock_destroy.assert_called_once()




def test_photo_image_endpoint_returns_cloudinary_bytes():
    token = create_access_token(1)
    mock_connection = MagicMock()
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.execute.return_value.fetchone.return_value = {
        "filename": "test-photo.jpg",
        "content_type": "image/jpeg",
    }

    image_bytes = b"\xff\xd8\xfffake-jpeg-data"
    previous_override = app.dependency_overrides.get(get_current_user)

    app.dependency_overrides[get_current_user] = lambda: {"id": 1}

    try:
        with (
            patch("main.get_connection", return_value=mock_connection),
            patch("main.download_photo", return_value=image_bytes) as mock_download,
        ):
            response = client.get(
                "/observations/2/photos/10/image",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_override

    assert response.status_code == 200
    assert response.content == image_bytes
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"] == "inline"
    mock_download.assert_called_once_with(2, "test-photo.jpg")


def test_photo_image_endpoint_does_not_expose_other_users_photo():
    token = create_access_token(1)
    mock_connection = MagicMock()
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.execute.return_value.fetchone.return_value = None

    previous_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1}

    try:
        with (
            patch("main.get_connection", return_value=mock_connection),
            patch("main.download_photo") as mock_download,
        ):
            response = client.get(
                "/observations/2/photos/10/image",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_override

    assert response.status_code == 404
    assert response.json()["detail"] == "Photo not found."
    mock_download.assert_not_called()



def test_delete_photo_endpoint_deletes_cloudinary_then_database():
    token = create_access_token(1)
    mock_connection = MagicMock()
    mock_connection.__enter__.return_value = mock_connection

    # First query verifies ownership; second query deletes the record.
    mock_connection.execute.return_value.fetchone.side_effect = [
        {"filename": "test-photo.jpg"},
        {"filename": "test-photo.jpg"},
    ]

    previous_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1}

    try:
        with (
            patch("main.get_connection", return_value=mock_connection),
            patch("main.delete_photo", return_value={"result": "ok"}) as mock_delete,
        ):
            response = client.delete(
                "/observations/2/photos/10",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_override

    assert response.status_code == 200
    assert response.json()["photo_id"] == 10
    mock_delete.assert_called_once_with(2, "test-photo.jpg")
    assert mock_connection.execute.call_count == 2


def test_delete_photo_endpoint_keeps_database_record_if_cloudinary_fails():
    token = create_access_token(1)
    mock_connection = MagicMock()
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.execute.return_value.fetchone.return_value = {
        "filename": "test-photo.jpg"
    }

    previous_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1}

    try:
        with (
            patch("main.get_connection", return_value=mock_connection),
            patch(
                "main.delete_photo",
                side_effect=RuntimeError("Cloudinary unavailable"),
            ) as mock_delete,
        ):
            response = client.delete(
                "/observations/2/photos/10",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_override

    assert response.status_code == 502
    assert response.json()["detail"] == "Unable to delete photo from storage."
    mock_delete.assert_called_once_with(2, "test-photo.jpg")

    # Only the ownership query should have run; no DB deletion query.
    assert mock_connection.execute.call_count == 1


def test_delete_photo_endpoint_does_not_delete_another_users_photo():
    token = create_access_token(1)
    mock_connection = MagicMock()
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.execute.return_value.fetchone.return_value = None

    previous_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1}

    try:
        with (
            patch("main.get_connection", return_value=mock_connection),
            patch("main.delete_photo") as mock_delete,
        ):
            response = client.delete(
                "/observations/2/photos/10",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_override

    assert response.status_code == 404
    assert response.json()["detail"] == "Photo not found."
    mock_delete.assert_not_called()
    mock_connection.execute.assert_called_once()
