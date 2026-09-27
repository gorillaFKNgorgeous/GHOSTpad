# Protocol fixtures

- `valid/<schema>/*.json` must validate against `schemas/<schema>.schema.json`
  (`legacy/<name>` → `schemas/legacy/<name>.schema.json`).
- `invalid/<schema>/*.json` are **patches**, not documents:

  ```json
  {"why": "…", "base": "event/inspect_scene.json",
   "set": {"/json/pointer": <value>}, "remove": ["/json/pointer"],
   "expect": "substring of the specific validation error"}
  ```

  The test applies the patch to the valid `base` fixture, checks that the
  result is rejected, and checks that one of the leaf errors contains
  `expect`. Each invalid case therefore fails for the one reason it states,
  not because of an unrelated typo.

Provenance: fixtures that reuse ids and digests from the live device are
copied exactly from `diagnostics`/`job_result` output of 2026-09-27: job
`5a6a2612…` (read_script), `c2f64920…` (list_scripts), `308ccce1…` (queued
view), `d1bc311c…` (capture), scene `e6072cdd…`, boot `2f034d00…`. Every
other id (`fixture-…`, repeated-hex digests, `0000…0f01`) is synthetic and
describes an invented scenario.
