# Changelog

## 2026-10-18

- Fixed: Kalico refused to load `chordata.cfg` ("Missing end of comment tag" in `_CHORDATA_BELTS`), so a printer
  with Chordata included didn't start. A `{# ... #}` comment in the macro was cut off at its closing `#` by the
  config parser; it is now a plain `#` comment line.
