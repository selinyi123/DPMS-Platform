
import hashlib, hmac, json, shutil, zipfile

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

UPLOAD_DIR = Path("/var/www/releases")

APP_CURRENT = Path("/app/app")



def verify_signature(manifest_bytes: bytes, signature_hex: str) -> bool:

    expected = hmac.new(settings.update_secret.encode(), manifest_bytes, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, signature_hex)



@router.post("/upload")

async def upload_update(request: Request, file: UploadFile = File(...), signature: str = Header(...)):
    require_min_role(request, "owner")
    require_confirmation(request)

    upload_path = UPLOAD_DIR / "update.zip"

    with open(upload_path, "wb") as f:

        f.write(await file.read())

    try:

        with zipfile.ZipFile(upload_path, 'r') as zf:

            manifest_data = zf.read("manifest.json")

            if not verify_signature(manifest_data, signature):

                raise ValueError("Invalid signature")

            manifest = json.loads(manifest_data)

            for fname, expected_hash in manifest["files_sha256"].items():

                actual = hashlib.sha256(zf.read(fname)).hexdigest()

                if actual != expected_hash:

                    raise ValueError(f"Hash mismatch: {fname}")

            version_dir = RELEASES_DIR / f"v{manifest.get('version')}"

            if version_dir.exists():

                shutil.rmtree(version_dir)

            zf.extractall(version_dir)



        if APP_CURRENT.is_symlink():

            old_target = APP_CURRENT.resolve()

            backup_ver = datetime.now().strftime("%Y%m%d_%H%M%S")

            shutil.copytree(old_target, BACKUPS_DIR / backup_ver, dirs_exist_ok=True)



        if APP_CURRENT.is_symlink():

            APP_CURRENT.unlink()

        else:

            shutil.rmtree(str(APP_CURRENT))

        APP_CURRENT.symlink_to(version_dir, target_is_directory=True)



        await database.execute("""

            INSERT INTO system_versions (version, description, file_hash)

            VALUES (:ver, :desc, :hash)

        """, {"ver": manifest.get("version"), "desc": manifest.get("description", ""), "hash": hashlib.sha256(manifest_data).hexdigest()})



        await redis.set("update_signal", "reload")

        await redis.publish("worker:reload", "1")
        await audit_event(
            request,
            action="update.upload",
            resource_type="system_version",
            resource_id=manifest.get("version"),
            result="deployed",
            risk_level="critical",
            detail={"version": manifest.get("version"), "description": manifest.get("description", "")},
        )

        return {"status": "deployed", "version": manifest.get("version")}

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

    backups = sorted(BACKUPS_DIR.glob("*"), reverse=True)

    if not backups:

        raise HTTPException(400, "No backup")

    latest_backup = backups[0]

    shutil.copytree(APP_CURRENT.resolve(), BACKUPS_DIR / f"pre_rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}", dirs_exist_ok=True)

    if APP_CURRENT.is_symlink():

        APP_CURRENT.unlink()

    else:

        shutil.rmtree(str(APP_CURRENT))

    shutil.copytree(latest_backup, str(APP_CURRENT), dirs_exist_ok=True)

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
