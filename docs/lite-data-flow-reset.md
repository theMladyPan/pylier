# Lite data-flow reset

Replace OTel graph import and relation kinds with one directed handoff edge model. A trace is a root node; decorated call entry hands arguments from its caller (or fingerprint producer), and exit hands its return value back to its immediate caller. The viewer keeps retained traces and moves filters below them in a single collapsible left pane.
