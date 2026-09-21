//! Print upstream's `PairRequest` for one COP round of a Baxter engine input —
//! to run that round through the Go oracle (`tools/cop-go-oracle`).
//!
//!     cargo run --release --example cop_request -- 5 < engine_input.json \
//!         | tools/cop-go-oracle/cop-go-oracle

use std::io::Read;

fn main() {
    let round: i32 = std::env::args()
        .nth(1)
        .and_then(|a| a.parse().ok())
        .expect("usage: cop_request <round> < engine_input.json");
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text).expect("read stdin");
    let input: scrabble_pairing::PairingInput = serde_json::from_str(&text).expect("parse input");
    match scrabble_pairing::strategies::cop_go::request_json(&input, round) {
        Ok(json) => println!("{json}"),
        Err(e) => {
            eprintln!("cop_request: {e}");
            std::process::exit(1);
        }
    }
}
