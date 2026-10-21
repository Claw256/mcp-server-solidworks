"""Filesystem anchors shared by the tool modules."""
import os

ADAPTER_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAD_PLANNER_DIR = os.path.normpath(os.path.join(ADAPTER_DIR, "..", "..", "cad-planner"))
