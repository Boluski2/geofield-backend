
import argparse
from pathlib import Path

import cloudinary.api
from cloudinary.exceptions import NotFound

from main import get_connection
from photo_storage import (
    configure_cloudinary,
    photo_public_id,
    upload_photo,
)


PHOTO_DIR = Path(__file__).resolve().parent / "uploads" / "observations"


def asset_exists(public_id: str) -> bool:
    """Return True if the authenticated Cloudinary image already exists."""
    try:
        cloudinary.api.resource(
            public_id,
            resource_type="image",
            type="authenticated",
        )
        return True
    except NotFound:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate GeoField photos to authenticated Cloudinary storage."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate records and local files without uploading anything.",
    )
    args = parser.parse_args()

    configure_cloudinary()

    with get_connection() as conn:
        photos = conn.execute(
            """
            SELECT id, observation_id, filename, content_type
            FROM observation_photos
            ORDER BY id
            """
        ).fetchall()

    if not photos:
        print("No photo records found.")
        return

    print(f"Found {len(photos)} photo records.")
    print(f"Local photo directory: {PHOTO_DIR}")
    print(f"Dry run: {'YES' if args.dry_run else 'NO'}")

    failures = []
    uploaded = 0
    already_present = 0

    for photo in photos:
        photo_id = photo["id"]
        observation_id = photo["observation_id"]
        filename = photo["filename"]
        content_type = photo["content_type"]
        local_path = PHOTO_DIR / filename

        try:
            public_id = photo_public_id(observation_id, filename)

            if not local_path.is_file():
                raise FileNotFoundError(
                    f"Local file is missing: {filename}"
                )

            if content_type not in ("image/jpeg", "image/png"):
                raise ValueError(
                    f"Unexpected content type: {content_type}"
                )

            image_data = local_path.read_bytes()

            print(
                f"\nPhoto ID {photo_id}: {filename} "
                f"({len(image_data):,} bytes)"
            )

            if args.dry_run:
                print(f"DRY RUN: would check and upload {public_id}")
                continue

            if asset_exists(public_id):
                already_present += 1
                print(
                    "ALREADY EXISTS: leaving the Cloudinary asset unchanged."
                )
                continue

            result = upload_photo(
                image_data,
                observation_id,
                filename,
            )

            if (
                result.get("public_id") != public_id
                or result.get("type") != "authenticated"
                or result.get("resource_type") != "image"
            ):
                raise RuntimeError(
                    "Cloudinary returned unexpected asset metadata."
                )

            uploaded += 1
            print("UPLOAD VERIFIED: authenticated image asset.")

        except Exception as exc:
            failures.append((photo_id, filename, str(exc)))
            print(f"FAILED: {type(exc).__name__}: {exc}")

    print("\nMigration summary")
    print(f"Records checked: {len(photos)}")
    print(f"New uploads: {uploaded}")
    print(f"Already present: {already_present}")
    print(f"Failures: {len(failures)}")

    if failures:
        print("\nFailed photos:")
        for photo_id, filename, reason in failures:
            print(f"- ID {photo_id}, {filename}: {reason}")
        raise SystemExit(1)

    if args.dry_run:
        print("Dry run completed. No uploads were attempted.")
    else:
        print("Migration pass completed.")
        print("Local files and database records were not modified.")


if __name__ == "__main__":
    main()
