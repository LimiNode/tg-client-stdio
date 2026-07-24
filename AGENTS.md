# AGENTS.md

This repository is intended to stay usable as a standalone Telegram user-client
stdio worker and C++ client kit.

## Defaults

- Keep the worker usable without OptionX-specific concepts.
- Keep Telegram/Telethon session ownership inside the worker process.
- Keep C++ APIs focused on worker process/protocol interaction; do not add
  trading DTOs here.
- Treat stdout as protocol data only. Diagnostics must go to stderr.
- Preserve protocol compatibility through `docs/protocol-v1.md`.
- Add tests for protocol changes before changing behavior.
- Prefer one Telegram account/session per worker process. Multi-account routing
  belongs to the supervisor for now.

## Validation

- Python-only changes: `python -m pip install -e .` and then
  `python -m unittest discover -s tests/python`.
- C++ header/protocol changes:
  `cmake -S . -B build -DTG_CLIENT_STDIO_BUILD_TESTS=ON`
  then `cmake --build build --config Debug --target tg_protocol_envelope_test`.
- Run `ctest --test-dir build -C Debug --output-on-failure` when CMake tests
  are built.
