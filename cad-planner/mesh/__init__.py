"""cad-planner/mesh -- the 3D-mesh front end of the reverse path (STL/OBJ/PLY -> Feature Graph IR).

    mesh file -> mesh_read (repair, meters, align) -> segment (planes/cylinders/symmetry/prism)
              -> sections (slice, fit) + smoothing -> lower (strategy ladder -> IR) -> verify

Pure Python above the IR (no MCP/HTTP/COM). Needs trimesh + scipy + numpy
(adapters/claude/requirements-mesh.txt); without them every entry point raises
MeshReaderUnavailable and nothing else in the product is affected.

IMPORT ORDER MATTERS FOR THE MCP ADAPTER (ADR-062): numpy's C extensions DEADLOCK when first loaded
off the main thread on Windows. This package imports none of them at module level (all imports are
lazy inside functions, see geom.deps()), so importing it is free -- the adapter must therefore call
`mesh.preload()` ONCE, in the main thread at startup (inside try/except), to pull numpy/scipy/trimesh
in on the safe thread. Tool calls on worker threads then only hit populated sys.modules.
"""
from .geom import MeshReaderUnavailable, preload
from .mesh_read import MeshReadError, MeshData, read_mesh, UNIT_TO_M
from .segment import analyze as segment_analyze
from .lower import plan, analyze, write_graph, PlanError, STRATEGIES
from .smoothing import SMOOTH_CHOICES
from .verify import compare as verify_compare

__all__ = ["MeshReaderUnavailable", "MeshReadError", "PlanError", "MeshData", "preload", "read_mesh",
           "segment_analyze", "plan", "analyze", "write_graph", "verify_compare", "UNIT_TO_M",
           "STRATEGIES", "SMOOTH_CHOICES"]
