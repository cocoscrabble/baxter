// Dev-only: read a PairingInput JSON on stdin, print the engine's pairings JSON.
// Used to cross-check the COP strategy against the tools/cop-oracle reference.
use std::io::Read;

fn main() {
    let mut s = String::new();
    std::io::stdin().read_to_string(&mut s).unwrap();
    println!("{}", scrabble_pairing::pair_json(&s).unwrap());
}
