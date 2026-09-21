//! Read a liwords `PairRequest` (protobuf JSON) on stdin and print the COP
//! port's trace — the stage-by-stage account `compare.py --stages` diffs
//! against the Go oracle's log.
//!
//!     cargo run --release --example cop_trace < request.json

use std::io::Read;

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).expect("read stdin");
    match scrabble_pairing::strategies::cop_go::trace::trace_json(&input) {
        Ok(out) => println!("{out}"),
        Err(e) => {
            eprintln!("cop_trace: {e}");
            std::process::exit(1);
        }
    }
}
