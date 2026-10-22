from io import BytesIO
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import time
import cloudinary
import cloudinary.uploader
import cloudinary.utils
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def configure_cloudinary() -> None:
    """Configure Cloudinary using environment variables."""
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    if not all((cloud_name, api_key, api_secret)):
        raise RuntimeError("Cloudinary environment settings are missing.")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )



def photo_public_id(observation_id: int, filename: str) -> str:
    """Build a stable Cloudinary ID without the local file extension."""
    if (
        not filename
        or Path(filename).name != filename
        or Path(filename).suffix.lower() not in {".jpg", ".png"}
    ):
        raise ValueError("Invalid photo filename.")

    safe_filename = Path(filename).stem

    if not safe_filename.isalnum():
        raise ValueError("Invalid photo filename.")

    if observation_id < 1:
        raise ValueError("Invalid observation ID.")

    return f"geofield/observations/{observation_id}/{safe_filename}"



def upload_photo(
    image_data: bytes,
    observation_id: int,
    filename: str,
) -> dict:
    """Upload a photo as an authenticated Cloudinary image."""
    configure_cloudinary()

    public_id = photo_public_id(observation_id, filename)

    return cloudinary.uploader.upload(
        BytesIO(image_data),
        public_id=public_id,
        resource_type="image",
        type="authenticated",
        overwrite=False,
        unique_filename=False,
        use_filename=False,
    )


def delete_photo(observation_id: int, filename: str) -> dict:
    """Delete an authenticated Cloudinary image."""
    configure_cloudinary()

    result = cloudinary.uploader.destroy(
        photo_public_id(observation_id, filename),
        resource_type="image",
        type="authenticated",
        invalidate=True,
    )

    if not isinstance(result, dict) or result.get("result") != "ok":
        raise RuntimeError("Cloudinary did not confirm photo deletion.")

    return result



def download_photo(
    observation_id: int,
    filename: str,
) -> bytes:
    """Download an authenticated photo using a short-lived signed URL."""
    configure_cloudinary()

    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in {"jpg", "png"}:
        raise ValueError("Unsupported photo extension.")

    public_id = photo_public_id(observation_id, filename)

    url = cloudinary.utils.private_download_url(
        public_id,
        extension,
        resource_type="image",
        type="authenticated",
        expires_at=int(time.time()) + 300,
    )

    try:
        with urlopen(url, timeout=20) as response:
            image_data = response.read()

        if not image_data:
            raise RuntimeError("Cloudinary returned an empty photo.")

        return image_data

    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Unable to retrieve photo from Cloudinary."
        ) from exc
