# Release checklist — cutting a Koe release

The operational runbook for shipping a versioned build. `.github/workflows/release.yml`
does the build/zip/draft-release automatically on a `vX.Y.Z` tag push; everything
else here is manual, on purpose (see `draft: true` in that workflow — a human
gate before anything goes public).

1. **Bump the version.**
   - `koe/__init__.py`: `__version__ = "X.Y.Z"`
   - `pyproject.toml`: `version = "X.Y.Z"`
   - Commit: `Release: vX.Y.Z`

2. **Tag and push.**
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
   Wait for the `Release` workflow to finish and produce a **draft** release
   with `Koe-win64-cuda-vX.Y.Z.zip` attached.

3. **Smoke-test the zip on a real Windows machine** (a CI-only pass is not enough —
   this is the GPU/mic/Windows surface an AI in a Linux sandbox cannot validate):
   - Download the zip, unzip it, run `Koe.exe`.
   - Dictate once (hotkey → speak → text appears in a focused app).
   - Run `interpreter.py` for about a minute (captions appear).
   - Run `talk.py` for one full turn (speak, hear a reply).

4. **Edit the release notes** — keep them user-facing (what changed for someone
   using Koe), not a raw commit dump — then **publish** the release.

5. **winget packaging.**
   - First release ever: `wingetcreate new <zip url>`
   - Subsequent releases: `wingetcreate update <PackageId> -u <zip url> -v X.Y.Z`
   - Either way, the result is a PR against `microsoft/winget-pkgs` (portable
     package type).

6. **Unsigned-exe note.** Koe ships without a code-signing certificate (see
   the Non-goals in `docs/specs/release-pipeline.md`). README's install section
   documents the SmartScreen "More info → Run anyway" path until a signing
   cert is adopted — check it's still accurate before pointing users at it.
