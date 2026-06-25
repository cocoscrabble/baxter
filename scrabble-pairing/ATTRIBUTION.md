# Third-party attribution

This crate's own code is dual-licensed **MIT OR Apache-2.0**. The files under
`src/vendor/` are vendored from a third-party project and are licensed under
**Apache-2.0** only; you must comply with Apache-2.0 for those files regardless
of which option you choose for the rest of the crate.

## `src/vendor/max_weight_matching.rs` and `src/vendor/dictmap.rs`

- **Source:** [rustworkx-core](https://github.com/Qiskit/rustworkx) v0.18.0
  (`src/max_weight_matching.rs` and `src/dictmap.rs`).
- **Copyright:** the rustworkx authors (Matthew Treinish and contributors).
- **License:** Apache License, Version 2.0 — see `LICENSE-APACHE` and the header
  retained at the top of each file.

### Why vendored

`rustworkx-core` 0.18 has no Cargo feature flags and unconditionally depends on
`rayon`, `ndarray`, and `rand`/`getrandom`, none of which compile to
`wasm32-unknown-unknown`. The maximum-weight-matching algorithm we need does not
use any of those, so we copy just those two files to keep this crate
wasm-compatible and dependency-light.

### Modifications

The files are vendored verbatim except:
- `max_weight_matching.rs`: the import `use crate::dictmap::*;` was changed to
  `use super::dictmap::*;` to match this crate's module layout, and the doc-test
  example (which imports `rustworkx_core`) was marked ```ignore```.

To update: re-copy both files from the upstream tag, reapply the two changes
above, and bump the version noted here.
