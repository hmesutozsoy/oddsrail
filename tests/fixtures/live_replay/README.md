# Synthetic live execution replay

`market.json` is deliberately fabricated binary-market metadata in the public
Gamma and CLOB wire shapes. It is not a captured exchange response and describes
no real market, wallet, order, or balance. Tests generate matching WebSocket JSON
frames with a controlled exchange timestamp.

The replay connects the real socket receive loop, frame parser, metadata HTTP
reader/parser, quote planner, supervisor, execution coordinator and disk SQLite
ledger. Only the socket and metadata HTTP transports, clocks, and financial venue
are test doubles. No external connection, real signature or financial action is
performed. The logical soak advances simulated time; it is not an uptime or
exchange-latency benchmark.
