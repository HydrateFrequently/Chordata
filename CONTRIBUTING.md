# Contributing to Chordata

Thanks for helping. A few rules, because people's printers update from `main`:

- Open pull requests against `dev`, not `main`. `main` is the release branch: it only takes pull requests
  from `dev`, merged by the maintainer once a change has been tested.
- Keep a pull request to one change. Measurement changes need a test in `tests/test_belts.py` (synthetic
  recordings are fine) and must keep the existing tests passing.
- `main` makes no AI decisions. AI-assisted features belong on the `ai-review` branch.
- Chordata only measures. A change must never make it move the toolhead without the user pressing a button.

Run the tests before opening a pull request:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/test_belts.py
npm ci && npx playwright install chromium && npm test
```
