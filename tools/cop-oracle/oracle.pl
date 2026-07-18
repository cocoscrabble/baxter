#!/usr/bin/perl
# COP parity oracle.
#
# Drives the REAL, unmodified vendored TSH::Command::COP (vendor/tsh) as a
# reference implementation for the Rust port in scrabble-pairing. Reads one test
# case as JSON on stdin, runs COP's native cop() computation single-threaded and
# seeded (so it is reproducible), and writes the resulting pairings + key
# decisions as JSON on stdout.
#
# It never calls COP's Run() (that needs the full TSH tournament runtime); it
# calls the pure cop() function directly with constructed inputs. Two TSH modules
# COP `use`s are shadowed by empty stubs (tools/cop-oracle/stubs) so the
# unmodified COP.pm loads under a modern Perl. See README.md for setup.
#
# Usage:
#   perl -Itools/cop-oracle/stubs -Ivendor/tsh/lib/perl -Ivendor/perl5/lib/perl5 \
#        tools/cop-oracle/oracle.pl < case.json
#
# Input JSON:
#   {
#     "seed": 12345,
#     "config": {
#       "number_of_rounds": 8,
#       "number_of_rounds_remaining": 3,      # rounds still to play, incl. the one being paired
#       "round_to_pair": 5,                    # 0-indexed
#       "lowest_ranked_payout": 2,             # 0-indexed count of place prizes (2 => top 3 cash)
#       "gibson_spread": [250],                # raw arrays (as TSH config); forward-filled here
#       "hopefulness": [0.1,0.1,0.05],
#       "control_loss_thresholds": [0.25],
#       "control_loss_activation_round": 0,    # 0-indexed
#       "number_of_sims": 2000,
#       "always_wins_number_of_sims": 1000,
#       "disallow_repeat_byes": false,
#       "prepaired_players": {"3": 4},         # optional: player-id -> opponent-id, forced
#       "lowest_ranked_class_payouts": {},     # optional (class prizes; usually empty)
#       "top_class": "UNDEFINED_CLASS"         # optional
#     },
#     "players": [                             # in current standings order; WINS ARE DOUBLED
#       {"id": 1, "name": "P1", "wins": 10, "spread": 400, "class": null},
#       ...
#     ],
#     "times_played":      [[1, 2, 1], ...],   # unordered pair id1,id2 -> count (byes: id 0)
#     "previous_pairings": [[1, 2], ...]       # pairs that met LAST round
#   }
#
# Output JSON:
#   {
#     "pairings": [[1, 4], [2, 3], [5, 0]],    # opponent id 0 == bye
#     "warnings": [ ... ],                      # prohibitive-weight pairings COP was forced into
#     "gibson_rank": -1,                        # lowest gibsonized rank (-1 = none)
#     "sim_player_ids": [1,2,3,4,5,6]           # players kept for simulation (can-cash truncation)
#   }

use strict;
use warnings;
use JSON::PP;
use TSH::Command::COP;

my $UNDEF = TSH::Command::COP::UNDEFINED_CLASS();

# ---- read + parse the case -------------------------------------------------
my $raw = do { local $/; <STDIN> };
my $case = decode_json($raw);
my $cfg_in = $case->{config} or die "missing config\n";
my $players_in = $case->{players} or die "missing players\n";

my $number_of_rounds = $cfg_in->{number_of_rounds};

# ---- build the config hash cop() expects -----------------------------------
# Expand the raw per-round arrays exactly as COP's Run() does, so the oracle
# input mirrors real TSH config and the Rust adapter's scalar->array expansion.
my $gibson_raw = $cfg_in->{gibson_spread} // [250];
my %config = (
    log_filename                  => "",   # suppress file logging
    html_log_filename             => "",
    number_of_sims                => $cfg_in->{number_of_sims} // 1000,
    number_of_threads             => 1,    # force determinism (no thread split)
    number_of_rounds              => $number_of_rounds,
    round_to_pair                 => $cfg_in->{round_to_pair},
    prepaired_players             => $cfg_in->{prepaired_players} // {},
    always_wins_number_of_sims    => $cfg_in->{always_wins_number_of_sims} // 1000,
    control_loss_thresholds       => TSH::Command::COP::extend_tsh_config_array(
        $cfg_in->{control_loss_thresholds} // [0.25], $number_of_rounds),
    control_loss_activation_round => $cfg_in->{control_loss_activation_round} // 0,
    number_of_rounds_remaining    => $cfg_in->{number_of_rounds_remaining},
    lowest_ranked_payout          => $cfg_in->{lowest_ranked_payout},
    lowest_ranked_class_payouts   => $cfg_in->{lowest_ranked_class_payouts} // {},
    top_class                     => $cfg_in->{top_class} // $UNDEF,
    cumulative_gibson_spreads     =>
        TSH::Command::COP::get_cumulative_gibson_spreads($gibson_raw, $number_of_rounds),
    gibson_spreads                =>
        TSH::Command::COP::extend_tsh_config_array($gibson_raw, $number_of_rounds),
    hopefulness                   =>
        TSH::Command::COP::extend_tsh_config_array($cfg_in->{hopefulness} // [0.05], $number_of_rounds),
    bye_active                    => 0,
    disallow_repeat_byes          => $cfg_in->{disallow_repeat_byes} ? 1 : 0,
);

# ---- build tournament_players (wins already doubled by the caller) ----------
my @tp;
my $index = 0;
for my $p (@$players_in) {
    my $class = defined $p->{class} ? $p->{class} : $UNDEF;
    push @tp, TSH::Command::COP::new_tournament_player(
        $p->{id}, $p->{name}, $class, $index++, $p->{wins}, $p->{spread}, 0);
}

# ---- times_played + previous-pairing hashes --------------------------------
my %times_played;
for my $t (@{ $case->{times_played} // [] }) {
    my ($a, $b, $n) = @$t;
    $times_played{ TSH::Command::COP::create_times_played_key($a, $b) } = $n;
}
my %previous;
for my $t (@{ $case->{previous_pairings} // [] }) {
    my ($a, $b) = @$t;
    $previous{ TSH::Command::COP::create_times_played_key($a, $b) } = 1;
}

# ---- deterministic intermediates (RNG-independent) -------------------------
# Computed on a copy so cop()'s in-place sorting below is unaffected.
my @tp_probe = @tp;
TSH::Command::COP::sort_tournament_players_by_record(\@tp_probe);
my $sim = TSH::Command::COP::get_sim_tournament_players(\%config, \@tp_probe);
my $gibson_rank = TSH::Command::COP::get_lowest_gibson_rank(\%config, $sim);
my @sim_ids = map { $_->{id} } @$sim;

# ---- run COP (seeded, single-threaded => reproducible) ----------------------
srand($case->{seed} // 0);
my ($id_pairings, $warnings) =
    TSH::Command::COP::cop(\%config, \@tp, \%times_played, \%previous);

# id_pairings holds an entry per player index (both directions); keep each pair
# once, with the bye (id 0) as the opponent.
my @pairings;
my %seen;
for my $p (@$id_pairings) {
    my ($a, $b) = @$p;
    next if $seen{$a}++;
    $seen{$b}++ unless $b == TSH::Command::COP::BYE_PLAYER_ID;
    push @pairings, [ $a + 0, $b + 0 ];
}

print encode_json({
    pairings       => \@pairings,
    warnings       => $warnings,
    gibson_rank    => $gibson_rank + 0,
    sim_player_ids => \@sim_ids,
}), "\n";
