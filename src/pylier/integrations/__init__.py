"""Optional framework integrations for pylier.

Each submodule is lazy-imported (never imported by ``import pylier``) and pulls
in its framework dependency only when the user calls the matching
``pylier.instrument_*`` helper. This keeps pylier's core dependency-free.
"""
