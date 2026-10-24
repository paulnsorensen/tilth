"""Evolve tilth with GEPA: candidates are commits over ``prompts/`` and ``src/``, scored by cost per correct.

``cli.main`` runs one search. ``Evolution.evaluate``, ``Evolution.cascade``,
``Evolution.finish``, ``Proposer.propose_src_patch``, and
``Materializer.materialize`` are its stages; ``engine`` is the only module that
imports ``gepa``.
"""
