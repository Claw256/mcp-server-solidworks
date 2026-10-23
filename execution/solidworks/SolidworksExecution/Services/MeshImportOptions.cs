using System;
using Newtonsoft.Json.Linq;

namespace SolidworksExecution.Services
{
    /// <summary>
    /// Parsed + validated open_document mesh params (mesh_as / units). Pure (no COM) so it is unit-testable.
    /// Values map to swImportStlVrmlModelType_e (Graphics=0, Surface=1, Solid=2) and swLengthUnit_e
    /// (swMM=0, swCM=1, swMETER=2, swINCHES=3).
    /// </summary>
    public sealed class MeshImportOptions
    {
        public static readonly string[] MeshExtensions = { ".stl", ".obj", ".wrl", ".3mf", ".off", ".ply", ".ply2" };

        public string MeshAs { get; private set; }
        public string Units { get; private set; }
        public int ModelType { get; private set; }
        public int UnitValue { get; private set; }
        /// <summary>Surface/solid import must build a mesh BREP body (swVrmlStlImportAsPSMesh).</summary>
        public bool AsMeshBody { get { return ModelType != 0; } }

        public static bool IsMeshExtension(string ext)
        {
            return Array.IndexOf(MeshExtensions, (ext ?? "").ToLowerInvariant()) >= 0;
        }

        /// <summary>Returns null and sets error (with allowed values) on invalid input.</summary>
        public static MeshImportOptions Parse(JObject p, out string error)
        {
            error = null;
            string meshAs = p != null ? p.Value<string>("mesh_as") : null;
            string units = p != null ? p.Value<string>("units") : null;
            var o = new MeshImportOptions();
            o.MeshAs = string.IsNullOrEmpty(meshAs) ? "graphics" : meshAs.Trim().ToLowerInvariant();
            o.Units = string.IsNullOrEmpty(units) ? "mm" : units.Trim().ToLowerInvariant();
            switch (o.MeshAs)
            {
                case "graphics": o.ModelType = 0; break;
                case "surface": o.ModelType = 1; break;
                case "solid": o.ModelType = 2; break;
                default:
                    error = $"mesh_as '{meshAs}' is invalid; allowed: graphics, surface, solid.";
                    return null;
            }
            switch (o.Units)
            {
                case "mm": o.UnitValue = 0; break;
                case "cm": o.UnitValue = 1; break;
                case "m": o.UnitValue = 2; break;
                case "in": o.UnitValue = 3; break;
                default:
                    error = $"units '{units}' is invalid; allowed: mm, cm, m, in.";
                    return null;
            }
            return o;
        }
    }
}
