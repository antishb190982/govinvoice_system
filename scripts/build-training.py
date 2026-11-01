"""Build narrated screen-based manuals from freshly captured demo screens.

No simulated clicks, financial mutations or private invoice images.
The output directory is ignored by Git and publishing; assets live in App Storage.
"""
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

APP = Path(__file__).resolve().parents[1]
SERVER = APP / "server" if (APP / "server/manage.py").exists() else APP
ROOT = SERVER.parents[2] if SERVER != APP else APP
WORK = ROOT / "generated-artifacts/training"
SCRIPTS = SERVER / "docs/training-scripts.json"
SIZE = (1600, 900)
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    direct = FONT_DIR / name
    if direct.exists():
        return ImageFont.truetype(str(direct), size)
    # Nix provides fonts under a hashed store path.
    matches = list(Path("/nix/store").glob(f"*/share/fonts/truetype/{name}"))
    if not matches:
        matches = list(Path("/nix/store").glob(f"*/share/fonts/truetype/dejavu/{name}"))
    if not matches:
        raise RuntimeError(f"Install a readable TrueType font before rendering: {name}")
    return ImageFont.truetype(str(matches[0]), size)


def wrap(draw, value, face, width):
    lines, current = [], ""
    for word in value.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def paragraph(draw, text, xy, face, width, fill, leading):
    x, y = xy
    for line in wrap(draw, text, face, width):
        draw.text((x, y), line, font=face, fill=fill)
        y += leading
    return y


def frame(manual, chapter, index, target, duration):
    image = Image.new("RGB", SIZE, "#f5f4fc")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1600, 80), fill="#242043")
    draw.text((32, 22), "GovBills  /  Video manual", font=font(26, True), fill="white")
    draw.text((1080, 26), "DEMO SCREENS • AI VOICE • CAPTIONS", font=font(18), fill="#e7ddff")
    draw.rounded_rectangle((22, 104, 1082, 840), radius=14, fill="white", outline="#d9d4e8", width=2)
    source = Image.open(WORK / "screens" / f"{chapter['screen']}.png").convert("RGB")
    source.thumbnail((1036, 712), Image.Resampling.LANCZOS)
    image.paste(source, (34 + (1036-source.width)//2, 116 + (712-source.height)//2))
    draw.rounded_rectangle((1102, 104, 1578, 840), radius=14, fill="white", outline="#d9d4e8", width=2)
    y = paragraph(draw, manual["title"], (1128, 128), font(23, True), 418, "#5d45bd", 31)
    y += 22
    draw.text((1128, y), f"STEP {index + 1} OF 4", font=font(18, True), fill="#686178")
    y += 36
    y = paragraph(draw, chapter["title"], (1128, y), font(31, True), 418, "#231d38", 40)
    y += 22
    y = paragraph(draw, chapter["instruction"], (1128, y), font(23), 418, "#302a42", 33)
    if y > 746:
        raise RuntimeError(f"Instruction overflows {manual['slug']} chapter {index + 1}: {y}")
    draw.line((1128, 752, 1550, 752), fill="#ded9ed", width=2)
    paragraph(draw, chapter["action"], (1128, 768), font(19, True), 418, "#5d45bd", 25)
    draw.text((32, 859), "Pause to read • Use chapters in GovBills • Follow your assigned permissions", font=font(18), fill="#494258")
    draw.text((1322, 859), f"{index + 1}/4  •  {duration}s chapter", font=font(18), fill="#494258")
    image.save(target)
    return image


def timestamp(seconds):
    return f"{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}.000"


def main():
    scripts = json.loads(SCRIPTS.read_text())
    narration = json.loads((WORK / "narration/manifest.json").read_text())
    spoken = {(item["role"], item["chapter"]): item for item in narration}
    if len(spoken) != sum(len(role["chapters"]) for role in scripts["roles"]):
        raise RuntimeError("All narration must be generated and validated before rendering")
    capture_manifest = json.loads((WORK / "screens/manifest.json").read_text())
    captured = {item["name"]: item for item in capture_manifest}
    result = {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "notice": scripts["notice"], "language": scripts["language"], "manuals": []}
    for manual in scripts["roles"]:
        folder = WORK / "build" / manual["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        cues = ["WEBVTT", ""]
        chapters = []
        durations = []
        elapsed = 0
        for index, chapter in enumerate(manual["chapters"]):
            if chapter["screen"] not in captured:
                raise RuntimeError(f"Fresh source screenshot missing: {chapter['screen']}")
            audio = spoken[(manual["slug"], index)]
            duration = math.ceil(audio["seconds"] + 1.25)
            durations.append(duration)
            frame(manual, chapter, index, folder / f"step-{index}.png", duration)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(ROOT / audio["path"]), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,apad",
                "-t", str(duration), "-ar", "48000", "-ac", "1",
                str(folder / f"audio-{index}.wav")], check=True)
            start = elapsed
            elapsed += duration
            cues += [str(index+1), f"{timestamp(start)} --> {timestamp(elapsed)}",
                chapter["title"] + "\n" + chapter["instruction"], ""]
            chapters.append({**chapter, "start_seconds": start,
                "source_route": captured[chapter["screen"]]["route"],
                "captured_at": captured[chapter["screen"]]["captured_at"]})
        (folder / "captions.vtt").write_text("\n".join(cues), encoding="utf8")
        (folder / "frames.txt").write_text("".join(
            f"file 'step-{index}.png'\nduration {durations[index]}\n" for index in range(4)
        ) + "file 'step-3.png'\n")
        (folder / "audio.txt").write_text("".join(f"file 'audio-{index}.wav'\n" for index in range(4)))
        duration = elapsed
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", str(folder / "frames.txt"), "-f", "concat", "-safe", "0",
            "-i", str(folder / "audio.txt"), "-map", "0:v:0", "-map", "1:a:0",
            "-t", str(duration), "-vf", "fps=2", "-c:a", "aac", "-b:a", "96k",
            "-c:v", "libx264", "-preset", "fast", "-tune", "stillimage", "-crf", "27",
            "-pix_fmt", "yuv420p", "-threads", "2", "-movflags", "+faststart", str(folder / "manual.mp4")],
            check=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(folder / "manual.mp4"), "-vf", "fps=2", "-c:v", "libvpx-vp9",
            "-b:v", "0", "-crf", "36", "-cpu-used", "6", "-row-mt", "1",
            "-c:a", "libopus", "-b:a", "64k",
            "-threads", "2", "-pix_fmt", "yuv420p", str(folder / "manual.webm")], check=True)
        Image.open(folder / "step-0.png").save(folder / "poster.jpg", quality=86)
        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format",
            "-show_streams", "-of", "json", str(folder / "manual.mp4")]))
        stream = next(item for item in probe["streams"] if item["codec_type"] == "video")
        if stream["codec_name"] != "h264" or abs(float(probe["format"]["duration"]) - duration) > .2:
            raise RuntimeError("Manual failed playback format validation")
        if not any(item["codec_type"] == "audio" and item["codec_name"] == "aac" for item in probe["streams"]):
            raise RuntimeError("Narrated MP4 is missing its audio stream")
        assets = {}
        for kind, name, mime in [("video", "manual.mp4", "video/mp4"),
                ("webm", "manual.webm", "video/webm"),
                ("captions", "captions.vtt", "text/vtt"), ("poster", "poster.jpg", "image/jpeg")]:
            file = folder / name
            assets[kind] = {"name": f"training/2026-11-01/{manual['slug']}/{name}",
                "size": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "content_type": mime}
        result["manuals"].append({key: manual[key] for key in ("slug", "title", "summary", "handoff")} |
            {"duration_seconds": duration, "chapters": chapters, "assets": assets,
                "narrated": True, "narration_notice": "AI-generated English narration"})
        print(manual["slug"], duration, "seconds", assets["video"]["size"], "bytes", flush=True)
    (WORK / "catalog.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    print("Catalog complete. Upload and verify all assets before promoting metadata into the app.")


if __name__ == "__main__":
    main()
