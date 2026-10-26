
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated
from typing import Literal
import csv
from io import StringIO
from datetime import datetime, timezone
from fastapi.responses import StreamingResponse 
from pathlib import Path
import hashlib
import uuid
from fastapi import UploadFile, File, Form, Header



import jwt
import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status, Query, Request
from rate_limiter import enforce_rate_limit
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, EmailStr, Field
from pwdlib import PasswordHash
from fastapi.security import OAuth2PasswordRequestForm
import re
from datetime import datetime, timezone
from fastapi.responses import FileResponse
from fastapi.responses import Response
from photo_storage import upload_photo, delete_photo, download_photo


load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY")

if not DATABASE_URL or not SECRET_KEY:
    raise RuntimeError("DATABASE_URL and SECRET_KEY must be set in .env")

ALGORITHM = "HS256"
TOKEN_MINUTES = 30

app = FastAPI(title="GeoField Authentication API")
password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    description: str | None = Field(default=None, max_length=2000)

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ObservationCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ObservationUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    status: Literal["pending", "reviewed"] | None = None



class PhotoCaptionUpdate(BaseModel):
    caption: str | None = Field(
        default=None,
        max_length=255,
    )



def safe_csv_cell(value) -> str:
    """Prevent spreadsheet formula execution from untrusted text cells."""
    if value is None:
        return ""

    text = str(value)

    # Check after whitespace so leading spaces cannot hide a formula.
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


def get_connection():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=TOKEN_MINUTES
    )

    payload = {
        "sub": str(user_id),
        "exp": expires
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM
    )


@app.get("/")
def home():
    return {"message": "GeoField Auth API is running"}


@app.post("/register", status_code=201)
def register(
    request: Request,
    data: RegisterRequest,
):
    enforce_rate_limit(
        request=request,
        endpoint="register",
        limit=5,
        window_seconds=3600,
    )
    with get_connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (name, email, password_hash)
                    VALUES (%s, %s, %s)
                    RETURNING id, name, email
                    """,
                    (
                        data.name,
                        str(data.email).lower(),
                        hash_password(data.password)
                    )
                )
                user = cursor.fetchone()
        except UniqueViolation:
            raise HTTPException(
                status_code=409,
                detail="Email is already registered"
            )

    return dict(user)

@app.post("/login")
def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    enforce_rate_limit(
        request=request,
        endpoint="login",
        limit=10,
        window_seconds=900,
    )
    email = form_data.username.lower()
    password = form_data.password

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, email, password_hash
                FROM users
                WHERE email = %s
                """,
                (email,)
            )
            user = cursor.fetchone()

    if (
        user is None
        or not user["password_hash"]
        or not verify_password(password, user["password_hash"])
    ):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = create_access_token(user["id"])

    return {
        "access_token": token,
        "token_type": "bearer"
    }


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)]
):
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, email
                FROM users
                WHERE id = %s
                """,
                (user_id,)
            )
            user = cursor.fetchone()

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return dict(user)

@app.post("/observations/{observation_id}/photos", status_code=201)
async def upload_observation_photo(
    observation_id: int,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    idempotency_key: uuid.UUID = Header(..., alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user),
):
    if caption is not None:
        caption = caption.strip()

    if not caption:
        caption = None

    if caption is not None and len(caption) > 255:
        raise HTTPException(
            status_code=422,
            detail="Caption must not exceed 255 characters.",
        )

    allowed_types = {
        "image/jpeg": (".jpg", b"\xff\xd8\xff"),
        "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    }

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Only JPEG and PNG images are supported.",
        )

    image_data = await file.read(5 * 1024 * 1024 + 1)

    if len(image_data) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Photo must not exceed 5 MB.",
        )

    if not image_data:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is empty.",
        )

    extension, signature = allowed_types[file.content_type]

    if not image_data.startswith(signature):
        raise HTTPException(
            status_code=400,
            detail="The file contents do not match the image type.",
        )

    # Fingerprint the observation, image type, caption, and image bytes.
    metadata = (
        f"{observation_id}\0{file.content_type}\0{caption or ''}\0"
    ).encode("utf-8")

    request_hash = hashlib.sha256(
        metadata + image_data
    ).hexdigest()

    try:
        # Verify ownership and check whether this request was already saved.
        with get_connection() as conn:
            observation = conn.execute(
                """
                SELECT id
                FROM field_observations
                WHERE id = %s AND user_id = %s
                """,
                (observation_id, current_user["id"]),
            ).fetchone()

            if not observation:
                raise HTTPException(
                    status_code=404,
                    detail="Observation not found.",
                )

            existing = conn.execute(
                """
                SELECT id, observation_id, filename,
                       content_type, caption, created_at, request_hash
                FROM observation_photos
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()

        if existing:
            await file.close()

            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This idempotency key was already used "
                        "for a different upload."
                    ),
                )

            existing_photo = {
                key: value
                for key, value in existing.items()
                if key != "request_hash"
            }

            return {
                "message": "Photo upload already processed.",
                "photo": existing_photo,
            }


        filename = f"{uuid.uuid4().hex}{extension}"
        cloudinary_uploaded = False

        try:
            # Upload remotely before creating the database record.
            upload_photo(image_data, observation_id, filename)
            cloudinary_uploaded = True

            with get_connection() as conn:
                photo = conn.execute(
                    """
                    INSERT INTO observation_photos (
                        observation_id,
                        filename,
                        content_type,
                        caption,
                        idempotency_key,
                        request_hash
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, observation_id, filename,
                              content_type, caption, created_at
                    """,
                    (
                        observation_id,
                        filename,
                        file.content_type,
                        caption,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone()

            return {
                "message": "Photo uploaded successfully.",
                "photo": photo,
            }

        except UniqueViolation:
            # A concurrent request may have won the idempotency race.
            if cloudinary_uploaded:
                delete_photo(observation_id, filename)

            with get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT id, observation_id, filename,
                           content_type, caption, created_at,
                           request_hash
                    FROM observation_photos
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                ).fetchone()

            if not existing:
                raise

            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This idempotency key was already used "
                        "for a different upload."
                    ),
                )

            existing_photo = {
                key: value
                for key, value in existing.items()
                if key != "request_hash"
            }

            return {
                "message": "Photo upload already processed.",
                "photo": existing_photo,
            }

        except Exception:
            if cloudinary_uploaded:
                try:
                    delete_photo(observation_id, filename)
                except Exception:
                    # Preserve the original error; cleanup can be retried.
                    pass
            raise


    finally:
        await file.close()



@app.get("/observations/{observation_id}/photos")
def get_observation_photos(
    observation_id: int,
    current_user: dict = Depends(get_current_user),
):
    with get_connection() as conn:
        observation = conn.execute(
            """
            SELECT id
            FROM field_observations
            WHERE id = %s AND user_id = %s
            """,
            (observation_id, current_user["id"]),
        ).fetchone()

        if not observation:
            raise HTTPException(
                status_code=404,
                detail="Observation not found.",
            )

        photos = conn.execute(
            """
            SELECT
                op.id,
                op.observation_id,
                op.filename,
                op.content_type,
                op.caption,
                op.created_at
            FROM observation_photos AS op
            WHERE op.observation_id = %s
            ORDER BY op.created_at DESC
            """,
            (observation_id,),
        ).fetchall()

    return {"photos": photos}


@app.get("/observations/{observation_id}/photos/{photo_id}/image")
def get_observation_photo_image(
    observation_id: int,
    photo_id: int,
    current_user: dict = Depends(get_current_user),
):
    with get_connection() as conn:
        photo = conn.execute(
            """
            SELECT
                op.filename,
                op.content_type
            FROM observation_photos AS op
            JOIN field_observations AS fo
                ON fo.id = op.observation_id
            WHERE op.id = %s
              AND op.observation_id = %s
              AND fo.user_id = %s
            """,
            (
                photo_id,
                observation_id,
                current_user["id"],
            ),
        ).fetchone()

    if photo is None:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )


    try:
        image_data = download_photo(
            observation_id,
            photo["filename"],
        )
    except (RuntimeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve photo from storage.",
        )

    return Response(
        content=image_data,
        media_type=photo["content_type"],
        headers={"Content-Disposition": "inline"},
    )




@app.patch(
    "/observations/{observation_id}/photos/{photo_id}/caption"
)
def update_observation_photo_caption(
    observation_id: int,
    photo_id: int,
    data: PhotoCaptionUpdate,
    current_user: dict = Depends(get_current_user),
):
    caption = data.caption

    if caption is not None:
        caption = caption.strip()

        if not caption:
            caption = None

    with get_connection() as conn:
        photo = conn.execute(
            """
            UPDATE observation_photos AS op
            SET caption = %s
            FROM field_observations AS fo
            WHERE op.observation_id = fo.id
              AND op.id = %s
              AND op.observation_id = %s
              AND fo.user_id = %s
            RETURNING
                op.id,
                op.observation_id,
                op.filename,
                op.content_type,
                op.caption,
                op.created_at
            """,
            (
                caption,
                photo_id,
                observation_id,
                current_user["id"],
            ),
        ).fetchone()

        if photo is None:
            raise HTTPException(
                status_code=404,
                detail="Photo not found.",
            )

    return {
        "message": "Photo caption updated successfully.",
        "photo": photo,
    }


@app.get("/dashboard")
def get_dashboard(
    current_user: Annotated[dict, Depends(get_current_user)]
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    COUNT(t.id)::int AS task_count,
                    COUNT(t.id) FILTER (
                        WHERE t.completed = TRUE
                    )::int AS completed_tasks
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                WHERE p.user_id = %s
                GROUP BY p.id, p.name, p.description
                ORDER BY p.id DESC
                """,
                (current_user["id"],)
            )
            projects = cursor.fetchall()

    project_list = [dict(project) for project in projects]

    total_tasks = sum(p["task_count"] for p in project_list)
    completed_tasks = sum(
        p["completed_tasks"] for p in project_list
    )

    return {
        "user": current_user["name"],
        "project_count": len(project_list),
        "task_count": total_tasks,
        "completed_tasks": completed_tasks,
        "pending_tasks": total_tasks - completed_tasks,
        "projects": project_list,
    }


@app.get("/me")
def get_me(
    current_user: Annotated[dict, Depends(get_current_user)]
):
    return current_user

@app.get("/projects")
def get_projects(
    current_user: Annotated[dict, Depends(get_current_user)]
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, description
                FROM projects
                WHERE user_id = %s
                ORDER BY id DESC
                """,
                (current_user["id"],)
            )
            projects = cursor.fetchall()

    return [dict(project) for project in projects]



@app.post("/projects", status_code=201)
def create_project(
    data: ProjectCreate,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects (name, description, user_id)
                VALUES (%s, %s, %s)
                RETURNING id, name, description, user_id
                """,
                (
                    data.name,
                    data.description,
                    current_user["id"]
                )
            )
            project = cursor.fetchone()

    return dict(project)



@app.get("/projects/{project_id}/tasks")
def get_project_tasks(
    project_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    status: Literal["all", "completed", "pending"] = Query(
        default="all"
    ),
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, completed, project_id
                FROM tasks
                WHERE project_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM projects
                      WHERE projects.id = tasks.project_id
                        AND projects.user_id = %s
                  )
                  AND (
                      %s = 'all'
                      OR (%s = 'completed' AND completed = TRUE)
                      OR (%s = 'pending' AND completed = FALSE)
                  )
                ORDER BY id DESC
                """,
                (
                    project_id,
                    current_user["id"],
                    status,
                    status,
                    status,
                ),
            )

            tasks = cursor.fetchall()

    return {"tasks": tasks}

@app.post("/projects/{project_id}/tasks", status_code=201)
def create_task(
    project_id: int,
    data: TaskCreate,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM projects
                WHERE id = %s AND user_id = %s
                """,
                (project_id, current_user["id"])
            )
            project = cursor.fetchone()

            if project is None:
                raise HTTPException(
                    status_code=404,
                    detail="Project not found"
                )

            cursor.execute(
                """
                INSERT INTO tasks (title, completed, project_id)
                VALUES (%s, FALSE, %s)
                RETURNING id, title, completed, project_id
                """,
                (data.title, project_id)
            )
            task = cursor.fetchone()

    return dict(task)


@app.patch("/projects/{project_id}/tasks/{task_id}")
def update_task_status(
    project_id: int,
    task_id: int,
    completed: bool,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tasks AS t
                SET completed = %s
                FROM projects AS p
                WHERE t.id = %s
                  AND t.project_id = p.id
                  AND p.id = %s
                  AND p.user_id = %s
                RETURNING t.id, t.title, t.completed, t.project_id
                """,
                (
                    completed,
                    task_id,
                    project_id,
                    current_user["id"],
                ),
            )

            task = cursor.fetchone()

            if task is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task not found",
                )

    return {"message": "Task updated successfully", "task": task}



@app.delete("/projects/{project_id}/tasks/{task_id}")
def delete_task(
    project_id: int,
    task_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM tasks AS t
                USING projects AS p
                WHERE t.id = %s
                  AND t.project_id = p.id
                  AND p.id = %s
                  AND p.user_id = %s
                RETURNING t.id
                """,
                (
                    task_id,
                    project_id,
                    current_user["id"],
                ),
            )

            deleted_task = cursor.fetchone()

            if deleted_task is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task not found",
                )

    return {
        "message": "Task deleted successfully",
        "task_id": deleted_task["id"],
    }


@app.delete(
    "/observations/{observation_id}/photos/{photo_id}"
)
def delete_observation_photo(
    observation_id: int,
    photo_id: int,
    current_user: dict = Depends(get_current_user),
):
    # First verify ownership and retrieve the photo filename.
    with get_connection() as conn:
        photo = conn.execute(
            """
            SELECT op.filename
            FROM observation_photos AS op
            JOIN field_observations AS fo
                ON fo.id = op.observation_id
            WHERE op.id = %s
              AND op.observation_id = %s
              AND fo.user_id = %s
            """,
            (
                photo_id,
                observation_id,
                current_user["id"],
            ),
        ).fetchone()

    if photo is None:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    # Delete the private Cloudinary asset before the DB record.
    try:
        delete_photo(
            observation_id,
            photo["filename"],
        )
    except (RuntimeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Unable to delete photo from storage.",
        )

    # Delete the DB record only after Cloudinary succeeds.
    try:
        with get_connection() as conn:
            deleted_photo = conn.execute(
                """
                DELETE FROM observation_photos AS op
                USING field_observations AS fo
                WHERE op.observation_id = fo.id
                  AND op.id = %s
                  AND op.observation_id = %s
                  AND fo.user_id = %s
                RETURNING op.filename
                """,
                (
                    photo_id,
                    observation_id,
                    current_user["id"],
                ),
            ).fetchone()

            if deleted_photo is None:
                raise HTTPException(
                    status_code=404,
                    detail="Photo record no longer exists.",
                )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500,
            detail=(
                "Photo storage was deleted, but the database "
                "record could not be removed. Reconciliation is required."
            ),
        )

    # Preserve local files until the migration is fully verified.
    return {
        "message": "Photo deleted successfully.",
        "photo_id": photo_id,
    }



@app.post("/projects/{project_id}/observations", status_code=201)
def create_observation(
    project_id: int,
    observation: ObservationCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM projects
                WHERE id = %s AND user_id = %s
                """,
                (project_id, current_user["id"]),
            )

            if cursor.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="Project not found",
                )

            cursor.execute(
                """
                INSERT INTO field_observations (
                    project_id, user_id, title, notes,
                    latitude, longitude, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, project_id, title, notes,
                          latitude, longitude, status, created_at
                """,
                (
                    project_id,
                    current_user["id"],
                    observation.title,
                    observation.notes,
                    observation.latitude,
                    observation.longitude,
                    "pending",
                ),
            )

            saved = cursor.fetchone()

    return {
        "message": "Field observation created",
        "observation": saved,
    }

@app.get("/projects/{project_id}/observations")
def get_observations(
    project_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_id, title, notes,
                       latitude, longitude, status, created_at
                FROM field_observations
                WHERE project_id = %s AND user_id = %s
                ORDER BY created_at DESC
                """,
                (project_id, current_user["id"]),
            )

            observations = cursor.fetchall()

    return {"observations": observations}


@app.patch("/projects/{project_id}/observations/{observation_id}")
def update_observation(
    project_id: int,
    observation_id: int,
    observation: ObservationUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE field_observations AS fo
                SET title = %s,
                    notes = %s,
                    status = COALESCE(%s, fo.status)
                FROM projects AS p
                WHERE fo.id = %s
                  AND fo.project_id = p.id
                  AND p.id = %s
                  AND fo.user_id = %s
                  AND p.user_id = %s
                RETURNING fo.id, fo.project_id, fo.title,
                          fo.notes, fo.latitude, fo.longitude,
                          fo.status, fo.created_at
                """,
                (
                    observation.title,
                    observation.notes,
                    observation.status,
                    observation_id,
                    project_id,
                    current_user["id"],
                    current_user["id"],
                ),
            )

            updated = cursor.fetchone()

            if updated is None:
                raise HTTPException(
                    status_code=404,
                    detail="Field observation not found",
                )

    return {
        "message": "Field observation updated successfully",
        "observation": updated,
    }


@app.delete(
    "/projects/{project_id}/observations/{observation_id}"
)
def delete_observation(
    project_id: int,
    observation_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM field_observations AS fo
                USING projects AS p
                WHERE fo.id = %s
                  AND fo.project_id = p.id
                  AND p.id = %s
                  AND fo.user_id = %s
                  AND p.user_id = %s
                RETURNING fo.id
                """,
                (
                    observation_id,
                    project_id,
                    current_user["id"],
                    current_user["id"],
                ),
            )

            deleted = cursor.fetchone()

            if deleted is None:
                raise HTTPException(
                    status_code=404,
                    detail="Field observation not found",
                )

    return {
        "message": "Field observation deleted successfully",
        "observation_id": deleted["id"],
    }




# What this code does
# - Counts all observations belonging to the authenticated user across their projects.
# - Counts pending and reviewed observations separately.
# - Calculates the reviewed percentage.
# - Returns zero percent when there are no observations, avoiding division by zero.
# - Uses the existing authentication dependency so users only see their own statistics.

@app.get("/dashboard/observation-stats")
def get_observation_stats(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_observations,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending_observations,
                    COUNT(*) FILTER (
                        WHERE status = 'reviewed'
                    ) AS reviewed_observations
                FROM field_observations
                WHERE user_id = %s
                """,
                (current_user["id"],),
            )

            stats = cursor.fetchone()

    total = stats["total_observations"]
    pending = stats["pending_observations"]
    reviewed = stats["reviewed_observations"]

    review_percentage = (
        round((reviewed / total) * 100, 1)
        if total > 0
        else 0
    )

    return {
        "total_observations": total,
        "pending_observations": pending,
        "reviewed_observations": reviewed,
        "review_percentage": review_percentage,
    }





@app.get("/observations/export/csv")
def export_observations_csv(
    current_user: Annotated[dict, Depends(get_current_user)],
    status: Literal["all", "pending", "reviewed"] = "all",
):
    output = StringIO()
    writer = csv.writer(output)

    exported_at = datetime.now(timezone.utc).isoformat()
    user_name = current_user["name"]

    safe_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        user_name,
    ).strip("_") or "user"

    filename = (
        f"geofield_observations_{safe_name}_{status}.csv"
    )

    writer.writerow([
        "Observation ID",
        "Project ID",
        "Title",
        "Notes",
        "Latitude",
        "Longitude",
        "Status",
        "Created At",
        "Exported By",
        "Exported At",
    ])

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if status == "all":
                cursor.execute(
                    """
                    SELECT
                        id, project_id, title, notes,
                        latitude, longitude, status, created_at
                    FROM field_observations
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (current_user["id"],),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        id, project_id, title, notes,
                        latitude, longitude, status, created_at
                    FROM field_observations
                    WHERE user_id = %s
                      AND status = %s
                    ORDER BY created_at DESC
                    """,
                    (current_user["id"], status),
                )

            rows = cursor.fetchall()

            for row in rows:
                writer.writerow([
                    row["id"],
                    row["project_id"],
                    safe_csv_cell(row["title"]),
                    safe_csv_cell(row["notes"]),
                    row["latitude"],
                    row["longitude"],
                    row["status"],
                    row["created_at"].isoformat()
                    if row["created_at"]
                    else "",
                    safe_csv_cell(user_name),
                    exported_at,
                ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )

