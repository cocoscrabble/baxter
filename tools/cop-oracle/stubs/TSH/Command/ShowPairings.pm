# Stub — see TSH/PairingCommand.pm. COP only uses ShowPairings inside Run(),
# which the oracle never calls.
package TSH::Command::ShowPairings;
use strict;
use warnings;
sub new { my $class = shift; return bless {}, ref($class) || $class; }
sub Run { return 1; }
1;
