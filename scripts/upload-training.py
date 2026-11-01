"""Publish immutable training media to private App Storage, then promote catalog."""
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

APP = Path(__file__).resolve().parents[1]
SERVER = APP / "server" if (APP / "server/manage.py").exists() else APP
ROOT = SERVER.parents[2] if SERVER != APP else APP
WORK = ROOT / "generated-artifacts/training"


def main():
    sys.path.insert(0, str(SERVER))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.replit_preview")
    os.environ.setdefault("GOVBILLS_ENV", "development")
    import django
    django.setup()
    from django.core.files.base import ContentFile
    from core.app_storage import PrivateAppStorage
    storage = PrivateAppStorage()
    catalog = json.loads((WORK / "catalog.json").read_text())
    if len(catalog["manuals"]) != 9:
        raise RuntimeError("All nine role manuals must exist before promotion")
    if any(not manual.get("narrated") for manual in catalog["manuals"]):
        raise RuntimeError("Every role manual must contain narration before promotion")
    for manual in catalog["manuals"]:
        for kind, asset in manual["assets"].items():
            source_name = PurePosixPath(asset["name"]).name
            path = WORK / "build" / manual["slug"] / source_name
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise RuntimeError(f"Local checksum mismatch for {manual['slug']} {kind}")
            key = str(PurePosixPath(asset["name"]).parent / f"{asset['sha256'][:12]}-{source_name}")
            if storage.exists(key):
                with storage.open(key, "rb") as stream:
                    remote = stream.read()
                if hashlib.sha256(remote).hexdigest() != asset["sha256"]:
                    raise RuntimeError("Existing immutable asset does not match")
            else:
                key = storage.save(key, ContentFile(data))
                with storage.open(key, "rb") as stream:
                    if hashlib.sha256(stream.read()).hexdigest() != asset["sha256"]:
                        raise RuntimeError("Uploaded asset checksum mismatch")
            asset["name"] = key
        print(f"Verified private assets: {manual['slug']}", flush=True)
    # This generated metadata is small; no video bytes or credentials enter Git.
    next_catalog = SERVER / "docs/.training-catalog.next.json"
    next_catalog.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    next_catalog.replace(SERVER / "docs/training-catalog.json")
    print("Training catalog promoted after all private assets were verified.")
    package = ROOT / "generated-artifacts/GovBills-Narrated-Role-Video-Manuals.zip"
    readme = ["GOVBILLS — NARRATED ROLE VIDEO MANUALS", "", catalog["notice"], "",
        "Nine MP4 walkthroughs with AI-generated English narration.",
        "Matching WebVTT captions and the full written checklists are included.",
        "In GovBills, open Video manuals from the left navigation for chapter navigation and volume controls.", ""]
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for manual in catalog["manuals"]:
            role = manual["slug"]
            folder = WORK / "build" / role
            archive.write(folder / "manual.mp4", f"GovBills-{role}.mp4")
            archive.write(folder / "captions.vtt", f"GovBills-{role}.vtt")
            readme.extend([manual["title"], manual["summary"], "Handoff: " + manual["handoff"]])
            for index, chapter in enumerate(manual["chapters"], 1):
                readme.extend([f"{index}. {chapter['title']} ({chapter['start_seconds']}s)",
                    chapter["instruction"], "Where: " + chapter["action"]])
            readme.append("")
        archive.writestr("GovBills-Manual-Transcripts.txt", "\n".join(readme))
    print("Narrated download package ready.", flush=True)


if __name__ == "__main__":
    main()
