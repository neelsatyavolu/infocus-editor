# InFocus Editor Setup (macOS app)

The signed app a person opens on the editing Mac. It runs the installer and links the Mac to the Portal. The spec, including the installer progress protocol, is in `../docs/setup-app.md`.

```
swift test                                      # protocol parser + step state machine
./build.sh --package "<InFocus 2026 Package>"   # unsigned local build in build/
./build.sh --sign    --package …                # Developer ID signature (1Password)
./build.sh --release --package …                # + notarize + staple → dist/InFocus Editor Setup.dmg
```

- **Release builds** come from a clean checkout of a `v*` tag. The app's `infocus-editor.bundle` then shares history with GitHub, so the Mac's later updates fetch as fast-forwards. Any other build is marked as a test build.
- **Signing** follows `~/Documents/GitHub/APPLE_SIGNING.md`. Credentials are read from 1Password into a temp keychain that is deleted on exit.
- **Testing the UI without installing:** set `INFOCUS_SETUP_INSTALLER=/path/to/fake-installer.sh` to run a fake installer instead (see `Tests/SetupCoreTests/Fixtures/fake-installer.sh`).
