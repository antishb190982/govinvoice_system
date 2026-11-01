# Updating role video manuals

The application serves authenticated, private videos from App Storage using
`docs/training-catalog.json`. The videos, source screenshots and audio are not
committed to Git. The narration scripts are in `docs/training-scripts.json`.

## Development-only authoring

Do not run screen capture or narration generation on a production instance.
Capture uses the local demo administrator and an ephemeral authenticated session;
it masks personal names, emails and account numbers. Review captured screens
before distributing any new manual.

The tools support both the standalone Django repository and the Replit workspace.
In the standalone repository they live under `scripts/`. In Replit, use
`artifacts/govbills/scripts/` instead.

1. Start the local development application and capture its current screens with
   `node scripts/capture-training.mjs`.
2. Generate cached AI narration with `python scripts/narrate-training.py`.
   This requires the configured Replit OpenAI integration and consumes workspace
   credits. Successful chapter audio is reused when its script is unchanged.
3. Render voiced MP4 and WebM videos with `python scripts/build-training.py`.
   This requires FFmpeg with H.264, AAC, VP9 and Opus, and Python Pillow.
4. Verify playback, captions and chapter seeking, then run
   `python scripts/upload-training.py` to verify private uploads and atomically
   promote the new catalogue. App Storage must already be configured.

Generated files remain under `generated-artifacts/`, outside Git. The upload
command also creates a narrated ZIP download with MP4s, subtitles and transcripts.
The capture command requires Chromium and existing local demo data. None of these
commands should be added to the application build or deployment startup: the
published application only plays existing videos and does not generate narration.
