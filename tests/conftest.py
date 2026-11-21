import os, sys

# Make the repository root importable, so tests can `import p2_decision`,
# `import p1_perception` and `import random_audit` without an editable install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
