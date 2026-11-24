"""crashmon - a local, standard-library crash monitoring prototype for Blender.

The package never performs network I/O. It supervises a Blender process,
captures logs and exit status, harvests locally available crash artifacts, and
writes an auditable JSON/Markdown bundle for later review.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "crashmon.bundle/1"
