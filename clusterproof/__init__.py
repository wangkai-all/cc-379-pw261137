"""ClusterProof — fictional relational test-data sample lifecycle service.

Three cooperating services share one SQLite database:

  sampler   picks rows and the dependent child rows they need
  checker   enforces graph/withdrawal/stratification invariants on a candidate
  publisher freezes a candidate into an immutable, signed release manifest

Nothing in this package talks to a real system; every account, name and value
in the demo fixtures is invented.
"""

__version__ = "0.1.0"
