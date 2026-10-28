# Validation

- After every code modification, run `PYTHONPATH=. python -m unittest discover -s tests -v`, available linters, and syntax checks. Check unused/undefined symbols, types where supported, deprecated APIs, and security issues.
- Run Fallow after every modification, covering changed code and the wider project. Consult `fallow --help`; never invent flags. Fix actionable duplication, dead code, complexity, obsolete code, and maintainability issues. Explain false positives or limitations; do not suppress warnings just to pass.
- Add regression tests for bug fixes. Cover URL/handle/ID resolution, RSS validation/polling, toggles/removal, duplicate prevention, downloads/naming, persistence, and restart recovery as relevant.
- When Docker is available, build the image, verify startup/logs and persistent volumes. Otherwise report Docker verification as unavailable.
- Rerun affected checks after fixes until introduced actionable issues are resolved. Never claim checks passed unless executed.
