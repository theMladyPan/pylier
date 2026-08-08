# Showcase topology refactor

`examples/showcase.py` now enters through one `fulfill_order` coordinator, branches into assessment plus concurrent inventory and shipping work, retains bounded repeated ranking, and reconverges for finalization. This makes Application Flow demonstrate real nested program structure rather than a trace-root star.
