import hashlib, hmac, json, re, shutil, stat, zipfile

from pathlib import Path

from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Header, HTTPException, Request

from app.config import settings

from app.db import database, redis
from app.security import audit_event, require_confirmation, require_min_role

from app.utils.log import structured_log



router = APIRouter()

RELEASES_DIR = Path("/app/releases")

BACKUPS_DIR = Path("/app/backups")

# Uploads land under the mounted releases volume (P1-1): the old
# ``/var/www/releases`` path was never mounted into the core container, so the
# upload either failed or wrote to ephemeral container storage.
UPLOAD_DIR = RELEASES_DIR / "_uploads"

APP_CURRENT = Path("/app/app")

RELEASE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")



def verify_signature(manifest_bytes: bytes, signature_hex: str) -> bool:

    expected = hmac.new(settings.update_secret.encode(), manifest_bytes, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, signature_hex)


def validate_release_version(value) -> str:
    """Return a safe release version segment from the signed manifest."""
    version = str(value or "").strip()
    if not RELEASE_VERSION_RE.fullmatch(version):
        raise ValueError("Invalid release version")
    return version


def release_dir_for(version: str) -> Path:
    """Resolve a release directory and prove it stays under RELEASES_DIR."""
    root = RELEASES_DIR.resolve()
    target = (root / f"v{version}").resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError("Release path escapes releases directory")
    return target


def require_managed_app_symlink() -> Path:
    """Hot-update may only retarget a managed symlink, never delete /app/app.

    In the docker-compose development layout, /app/app is a bind mount from
    ./core/app. The previous implementation removed this path when it was not a
    symlink, which could delete the host source tree. Refuse to deploy until the
    runtime is changed to a managed symlink layout such as /app/runtime/current.
    """
    if not APP_CURRENT.is_symlink():
        raise RuntimeError(
            "Hot update refused: /app/app is not a managed symlink. "
            "Current docker-compose bind-mounts source code at this path; refusing to delete it."
        )
    current_target = APP_CURRENT.resolve()
    if not current_target.exists() or not current_target.is_dir():
        raise RuntimeError("Hot update refused: current app symlink target is missing")
    return current_target


def _is_symlink_member(member: zipfile.ZipInfo) -> bool:
    # Unix mode is stored in the high 16 bits of external_attr.
    return stat.S_ISLNK(member.external_attr >> 16)


def safe_extract(zf: zipfile.ZipFile, target_dir: Path, allowed_names: set[str]) -> None:
    """Extract ``zf`` into ``target_dir`` rejecting unsafe or unexpected members.

    Defends the hot-update path (P0-5) against zip-slip and smuggled files:

    - no absolute paths, no ``..`` traversal, no path that resolves outside
      ``target_dir``;
    - no symlink members (which could later be followed to escape the tree);
    - every non-directory member must be ``manifest.json`` or appear in the
      signed manifest's ``files_sha256`` set — nothing extra rides along.
    """
    target_dir = target_dir.resolve()
    for member in zf.infolist():
        name = member.filename
        normalized = name.replace("\\", "/")
        if name.startswith("/") or name.startswith("\\") or ".." in Path(normalized).parts:
            raise ValueError(f"Unsafe zip path: {name}")
        if _is_symlink_member(member):
            raise ValueError(f"Symlink not allowed in update archive: {name}")
        out_path = (target_dir / normalized).resolve()
        if out_path != target_dir and not out_path.is_relative_to(target_dir):
            raise ValueError(f"Zip path escapes target dir: {name}")
        if not member.is_dir():
            base = normalized.lstrip("/")
            if base != "manifest.json" and base not in allowed_names:
                raise ValueError(f"File not listed in signed manifest: {name}")
    zf.extractall(target_dir)



@router.post("/upload")

async def upload_update(request: Request, file: UploadFile = File(...), signature: str = Header(...)):
    require_min_role(request, "owner")
    require_confirmation(request)

    try:
        old_target = require_managed_app_symlink()
    except Exception as exc:
        await audit_event(
            request,
            action="update.upload",
            resource_type="system_version",
            result="blocked",
            risk_level="critical",
            detail={"reason": str(exc)},
        )
        raise HTTPException(409, str(exc))

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = UPLOAD_DIR / "update.zip"

    with open(upload_path, "wb") as f:

        f.write(await file.read())

    try:

        with zipfile.ZipFile(upload_path, 'r') as zf:

            manifest_data = zf.read("manifest.json")

            if not verify_signature(manifest_data, signature):

                raise ValueError("Invalid signature")

            manifest = json.loads(manifest_data)
            version = validate_release_version(manifest.get("version"))

            for fname, expected_hash in manifest["files_sha256"].items():

                actual = hashlib.sha256(zf.read(fname)).hexdigest()

                if actual != expected_hash:

                    raise ValueError(f"Hash mismatch: {fname}")

            version_dir = release_dir_for(version)

            if version_dir.exists():

                shutil.rmtree(version_dir)

            allowed_names = {name.replace("\\", "/").lstrip("/") for name in manifest["files_sha256"]}
            safe_extract(zf, version_dir, allowed_names)



        backup_ver = datetime.now().strftime("%Y%m%d_%H%M%S")

        shutil.copytree(old_target, BACKUPS_DIR / backup_ver, dirs_exist_ok=True)

        APP_CURRENT.unlink()

        APP_CURRENT.symlink_to(version_dir, target_is_directory=True)



        await database.execute("""

            INSERT INTO system_versions (version, description, file_hash)

            VALUES (:ver, :desc, :hash)

        """, {"ver": version, "desc": manifest.get("description", ""), "hash": hashlib.sha256(manifest_data).hexdigest()})



        await redis.set("update_signal", "reload")

        await redis.publish("worker:reload", "1")
        await audit_event(
            request,
            action="update.upload",
            resource_type="system_version",
            resource_id=version,
            result="deployed",
            risk_level="critical",
            detail={"version": version, "description": manifest.get("description", "")},
        )

        return {"status": "deployed", "version": version}

    except Exception as e:

        structured_log("error", "update_failed", error=str(e))
        await audit_event(
            request,
            action="update.upload",
            resource_type="system_version",
            result="failed",
            risk_level="critical",
            detail={"error": str(e)},
        )

        raise HTTPException(400, str(e))



@router.post("/rollback")

async def rollback(request: Request):
    require_min_role(request, "owner")
    require_confirmation(request)

    try:
        old_target = require_managed_app_symlink()
    except Exception as exc:
        await audit_event(
            request,
            action="update.rollback",
            resource_type="system_version",
            result="blocked",
            risk_level="critical",
            detail={"reason": str(exc)},
        )
        raise HTTPException(409, str(exc))

    backups = sorted(BACKUPS_DIR.glob("*"), reverse=True)

    if not backups:

        raise HTTPException(400, "No backup")

    latest_backup = backups[0]

    shutil.copytree(old_target, BACKUPS_DIR / f"pre_rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}", dirs_exist_ok=True)

    APP_CURRENT.unlink()

    APP_CURRENT.symlink_to(latest_backup, target_is_directory=True)

    await redis.set("update_signal", "rollback")

    await redis.publish("worker:reload", "rollback")
    await audit_event(
        request,
        action="update.rollback",
        resource_type="system_version",
        resource_id=latest_backup.name,
        result="rolled_back",
        risk_level="critical",
        detail={"backup": latest_backup.name},
    )

    return {"status": "rolled_back"}
