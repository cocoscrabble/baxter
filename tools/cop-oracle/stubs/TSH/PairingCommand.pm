# Minimal stub so the real, unmodified TSH::Command::COP loads under modern Perl
# without dragging in the full (and syntactically stale) TSH runtime. COP's
# cop() computation never calls into TSH::PairingCommand; only its Run() method
# (which the oracle does not invoke) does. See tools/cop-oracle/README.md.
package TSH::PairingCommand;
use strict;
use warnings;
sub new { my $class = shift; return bless {}, ref($class) || $class; }
1;
