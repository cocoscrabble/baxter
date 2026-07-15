//! PyO3 binding for the `scrabble-pairing` engine.
//!
//! Exposes a single function over the crate's JSON boundary. The GIL is released
//! during pairing (`allow_threads`) — correct and cheap because the core takes
//! and returns owned strings and touches no Python objects. JSON overhead at the
//! tournament sizes we pair (≤ ~40 players) is noise, so there's deliberately no
//! typed conversion layer.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Pair a tournament from a JSON string, returning JSON. Raises ``ValueError``
/// if the input can't be parsed (or the output serialized).
#[pyfunction]
fn pair_json(py: Python<'_>, input: &str) -> PyResult<String> {
    py.allow_threads(|| scrabble_pairing::pair_json(input))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pymodule]
fn scrabble_pairing_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pair_json, m)?)?;
    Ok(())
}
