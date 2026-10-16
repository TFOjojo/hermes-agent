#!/usr/bin/env python3
"""Source adapters for the context assembler.

Each adapter reads from one data layer and returns a list of
``ContextEvidence``.  Adapters are stateless: they receive a
``PrincipalContext`` and ``ContextRequest`` and produce evidence.
"""

from __future__ import annotations