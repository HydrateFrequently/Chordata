# Local validation record

Date: 2026-10-16. Checkout: `/home/hermes/Chordata`, based on upstream
`db24b8602d068108c1da67d5a4acf8ee420480c3`. This records validation of the rewrite before publication to the fork.

## Executed

| Check | Result |
| --- | --- |
| Python unit/integration-contract tests | 42 passed on Python 3.12.13 |
| Existing synthetic signal and adjustment-planner checks | 40 passed |
| Chromium browser suite | Passed at 390 px and 1280 px |
| Installer tests | Included above: Klipper/Kalico layout, reruns, user settings/navigation preservation, malformed navigation, regular-file protection |
| Shell syntax, Python compilation, whitespace | Passed |
| Actual isolated Moonraker daemon | Component loaded; page 200; unauthenticated settings/review 401; trusted requests accepted; disabled AI review works with no Klippy connection |
| Private settings | Actual daemon saved with mode 0600; API omitted provider key |

Commands:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/test_belts.py
bash -n install.sh
.venv/bin/python -m compileall -q chordata.py chordata_belts.py moonraker
npm test
git diff --check
```

Local Chromium ran using the already installed headless-shell executable through
`CHROMIUM_EXECUTABLE`. The browser suite verifies that demo mode never opens a
WebSocket, that current belt samples replace prior samples, and that null rejected
frequencies remain visible in the table without invented chart points. It checks
CSV escaping, endpoint configuration, explicit provider-key clearing, session-only
Moonraker authentication, and invalidation of displayed AI advice on a new recording.
Moonraker API calls in that browser suite are simulated.

The separate real-daemon smoke check used Moonraker revision
`9e676eba6b02661a4dfa3ec6e7ac3f3504498e6d` in temporary directories, bound to
localhost with no Klippy socket. It exercised actual HTTP routing and authorization,
not a fake server. The daemon was stopped afterward. No installed printer service
or configuration was changed.

## Independent review

A subagent reviewed the measurement lifecycle, direct-command interlocks, installer,
UI and optional AI component, and added backend regression tests. It found that
completed AI advice remained visible after a new measurement. The UI now clears
that advice when measurements change and discards responses to outdated requests;
the browser regression passes. No blocking backend or installer defect remained in
that review. The final provider-key clear control has an additional browser test.

## Laya's contribution and limits

Native Laya selected the coding model and advised an architecture with deterministic
signal processing, optional asynchronous Moonraker review, and an extension of the
existing companion page. Laya Router was also consulted under the workspace policy.
Routing was advisory; the work's actual code, credential and printer-state boundaries
were checked independently.

Two live native Laya trials used the application's actual review question and
synthetic measurement summaries:

| Fixture | Selected answer | Confidence | Application treatment at 0.7 |
| --- | --- | --- | --- |
| Complete, consistent A/B pair | repeat | 0.1718 | Abstain |
| Partial pair with weak belt B | repeat | 0.1275 | Abstain |

The clean fixture did not receive the expected comparison answer. These trials
validate an uncertainty path, not classification accuracy. An independent subagent's
Laya review of the implementation evidence also abstained (confidence 0.0934).
A final evidence review also withheld its readiness answer (confidence 0.0674).
AI review therefore remains optional and experimental; numerical DSP remains the
measurement authority. No confidence value is presented as proof of code correctness.

Jev's coding-agent integration was unavailable because this checkout was not enrolled
and the private setup browser could not open. Native Laya remained available. The
application's Jev HTTP integration follows the current TypeSafe System One contract
and has request/response tests; no live Jev provider call was made.

## Still requires a physical printer

- Standalone recording and live sample arrival on the user's Klipper/Kalico version,
  accelerometer and host hardware.
- Comparison against reference recordings and known manufacturer targets.
- Mainsail sidebar/proxy behavior in the user's installation.
- Saved-position motion with the printer's actual coordinate offsets and travel limits.
- Labelled real recordings before treating AI quality advice as useful.

The original upstream real-recording and virtual-printer claims were not reproduced
as part of this rewrite. The publication checks GitHub Actions separately; the local
installer tests use temporary directories; the installer was not run on a real printer.

## Publication preparation

The fork adds a detailed illustrated user guide, troubleshooting reference and four
reproducible screenshots captured from the offline demo. They contain illustrative
data and no provider credentials. New installer update entries now follow
`alextverdyy/Chordata`; existing entries remain untouched and migration is documented.
