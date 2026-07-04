# Spec: release-pipeline — versioned builds from tag to public release  (status: implemented)

## Goal

Pushing a git tag `vX.Y.Z` produces a downloadable `Koe-win64-cuda-vX.Y.Z.zip`
on the GitHub Release automatically, and the app knows its own version. This is
the backbone of distribution (winget points at these release assets).

## Non-goals

- No code signing in this spec (tracked separately; SmartScreen guidance goes
  in the release checklist, not code).
- No Microsoft Store packaging.
- Never bundle models or user data into the zip (koe.spec already excludes;
  don't change that).

## Context to read (exhaustive)

- CLAUDE.md
- .github/workflows/ci.yml, build.ps1, koe.spec, koe/__init__.py, koe/tray.py
  (title function), talk.py (banner print), interpreter.py (banner print)

## Design

1. **Version source of truth**: `koe/__init__.py` gains
   `__version__ = "0.2.0"` (0.2.0 = the Koe Talk release; dictation-only era
   was 0.1.x per pyproject). `pyproject.toml` switches to
   `version = "0.2.0"` kept in sync manually (a comment in BOTH files pointing
   at each other — do not add build tooling for this).
2. **Show it**: tray title (`koe/tray.py:_title`) gains a `v{__version__}`
   suffix; `talk.py` and `interpreter.py` startup banners include it. Lazy
   import not needed (koe is already imported).
3. **Workflow** `.github/workflows/release.yml`:

```yaml
name: Release
on:
  push:
    tags: ["v*"]
jobs:
  build:
    runs-on: windows-latest
    timeout-minutes: 90
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      # Pure tests act as the release gate — same contract as ci.yml.
      - run: pip install pytest requests numpy
      - run: python -m pytest -q
      - run: pip install -r requirements.txt pyinstaller
      - run: pyinstaller koe.spec --noconfirm
      - name: Zip the app folder
        run: Compress-Archive -Path dist/Koe -DestinationPath "Koe-win64-cuda-${{ github.ref_name }}.zip"
        shell: pwsh
      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: Koe-win64-cuda-${{ github.ref_name }}.zip
          generate_release_notes: true
          draft: true   # owner reviews notes + smoke-tests the zip, then publishes
```

   `draft: true` is deliberate: a human gate before anything is public.

4. **Checklist**: new `docs/specs/RELEASE-CHECKLIST.md` (the operational
   runbook), content:
   - bump `__version__` + pyproject → commit `Release: vX.Y.Z`
   - `git tag vX.Y.Z && git push origin vX.Y.Z` → wait for the draft release
   - download zip on a Windows machine → unzip → run `Koe.exe` → dictate once,
     run interpreter 1 min, run talk 1 turn (smoke)
   - edit release notes (user-facing changes only) → publish
   - winget: first release — `wingetcreate new <zip url>`; updates —
     `wingetcreate update <PackageId> -u <zip url> -v X.Y.Z` → PR to
     microsoft/winget-pkgs (portable type)
   - unsigned-exe note: README's install section documents the SmartScreen
     "More info → Run anyway" path until a signing cert is adopted

## File-by-file changes

| File | Change |
|------|--------|
| koe/__init__.py | `__version__ = "0.2.0"` + sync comment |
| pyproject.toml | version 0.2.0 + sync comment |
| koe/tray.py | version in `_title` |
| talk.py, interpreter.py | version in startup banner (one line each) |
| .github/workflows/release.yml | new, per YAML above |
| docs/specs/RELEASE-CHECKLIST.md | new runbook |

## Tests to write

- `test_version_is_semver` (tests/test_pure.py or new tests/test_release.py):
  `koe.__version__` matches `^\d+\.\d+\.\d+$` and equals pyproject's version
  (read pyproject with a 5-line parser: find the `version =` line; no tomllib
  dependency questions — tomllib is stdlib 3.11+, using it is also fine).

## Acceptance criteria

1. Full suite green under CI constraints; compileall clean.
2. `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yml'))"`
   parses (install pyyaml only for this one-off check, do not add it anywhere).
3. Version appears in: tray title string builder, talk banner, interpreter
   banner — verified by grep, no runtime needed.
4. ci.yml untouched.
5. Self-check table submitted, one row per criterion.

## Manual checks for the owner

- Push a test tag (e.g. `v0.2.0-rc1` — note: matches `v*`, will build) on a
  branch merge; confirm the draft release + zip appear; smoke-test the zip.
- First winget submission per the checklist.

## Known pitfalls

- GitHub-hosted Windows runners have ~14GB free disk; the CUDA-bundled build
  (~1.5GB zip, several GB intermediate) fits but don't add caching of dist/.
- `Compress-Archive` produces zip fine, but paths >260 chars can fail —
  koe.spec output names are short; don't nest an extra folder.
- Release asset hard limit is 2GB per file — the CUDA zip is under it; if it
  ever grows past, split CPU/CUDA editions (decision for the Architect, not
  the Implementer).
