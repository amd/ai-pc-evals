# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""``python -m kwengine``: see ``kwengine.cli``."""

import sys

from .cli import main

sys.exit(main(prog="python -m kwengine"))
