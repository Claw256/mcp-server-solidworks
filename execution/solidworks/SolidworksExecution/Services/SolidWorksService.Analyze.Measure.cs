using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;

namespace SolidworksExecution.Services
{
    // SolidWorksService partial: analyze_model(measure) - distance/angle/radius/diameter/length/area between faces,
    // edges and points addressed by the SAME indices the faces/edges modes report. Read-only: entities are handed to
    // IMeasure.Calculate as an ARRAY, so the user's selection is neither used nor changed.
    //
    // name = JSON {"a": E, "b": E, "kind": "distance|angle|radius|diameter|length|area"}, E = {"face": i} |
    // {"edge": i} | {"point": [x,y,z]} (meters). b may be omitted for radius/diameter/length/area.
    // Result: ONE JSON object with only the relevant key of distance_mm | angle_deg | radius_mm | diameter_mm |
    // length_mm | area_mm2 (millimetres / degrees / mm^2, rounded to 4 dp).
    public partial class SolidWorksService
    {
        private sealed class MeasureTarget
        {
            public object Entity;      // IFace2 / IEdge
            public double[] Point;     // {"point": [...]} instead of an entity
        }

        // Same enumeration as ReadFaces/ReadEdges (FlattenFaces/FlattenEdges walk GetBodies2 -> GetFaces/GetEdges),
        // each list built at most once per call.
        private sealed class MeasureIndex
        {
            private readonly IPartDoc _part;
            private List<IFace2> _faces;
            private List<IEdge> _edges;
            public MeasureIndex(IPartDoc part) { _part = part; }
            public List<IFace2> Faces(SolidWorksService s) { return _faces ?? (_faces = s.FlattenFaces(_part)); }
            public List<IEdge> Edges(SolidWorksService s) { return _edges ?? (_edges = s.FlattenEdges(_part)); }
        }

        private MeasureTarget ResolveMeasureTarget(JToken e, string label, MeasureIndex index, out string err)
        {
            err = null;
            var o = e as JObject;
            if (o == null) { err = $"'{label}' must be an object: {{\"face\": i}}, {{\"edge\": i}} or {{\"point\": [x,y,z]}}."; return null; }
            JToken t;
            if (o.TryGetValue("face", out t))
            {
                var faces = index.Faces(this);
                int i = t.Type == JTokenType.Integer ? t.Value<int>() : -1;
                if (i < 0 || i >= faces.Count) { err = $"'{label}': face index out of range (0..{faces.Count - 1}); use analyze_model(faces)."; return null; }
                return new MeasureTarget { Entity = faces[i] };
            }
            if (o.TryGetValue("edge", out t))
            {
                var edges = index.Edges(this);
                int i = t.Type == JTokenType.Integer ? t.Value<int>() : -1;
                if (i < 0 || i >= edges.Count) { err = $"'{label}': edge index out of range (0..{edges.Count - 1}); use analyze_model(edges)."; return null; }
                return new MeasureTarget { Entity = edges[i] };
            }
            if (o.TryGetValue("point", out t))
            {
                var arr = t as JArray;
                if (arr == null || arr.Count < 3) { err = $"'{label}': point must be [x,y,z] in meters."; return null; }
                try { return new MeasureTarget { Point = new[] { arr[0].Value<double>(), arr[1].Value<double>(), arr[2].Value<double>() } }; }
                catch { err = $"'{label}': point must be three numbers."; return null; }
            }
            err = $"'{label}' needs one of face / edge / point.";
            return null;
        }

        // Distance from a point to the closest point of a face/edge (exact: IFace2/IEdge.GetClosestPointOn).
        private static double? PointToEntity(double[] p, object entity)
        {
            double[] c = null;
            var f = entity as IFace2;
            if (f != null) c = f.GetClosestPointOn(p[0], p[1], p[2]) as double[];
            else
            {
                var ed = entity as IEdge;
                if (ed != null) c = ed.GetClosestPointOn(p[0], p[1], p[2]) as double[];
            }
            if (c == null || c.Length < 3) return null;
            double dx = c[0] - p[0], dy = c[1] - p[1], dz = c[2] - p[2];
            return Math.Sqrt(dx * dx + dy * dy + dz * dz);
        }

        private JObject MeasureEntities(IModelDoc2 modelDoc, IPartDoc partDoc, string spec, out string errCode, out string errMsg)
        {
            errCode = null; errMsg = null;
            JObject s;
            try { s = JObject.Parse(spec ?? ""); }
            catch
            {
                errCode = "MISSING_PARAMETER";
                errMsg = "analysis_type='measure' requires 'name' = JSON {\"a\": {\"face\": i}, \"b\": {\"edge\": j}, \"kind\": \"distance\"} (E: face/edge index or point [x,y,z] in meters).";
                return null;
            }
            string kind = (s.Value<string>("kind") ?? "").ToLowerInvariant();
            if (kind != "distance" && kind != "angle" && kind != "radius" && kind != "diameter" && kind != "length" && kind != "area")
            {
                errCode = "INVALID_PARAMETER";
                errMsg = "measure kind must be one of: distance, angle, radius, diameter, length, area.";
                return null;
            }
            bool needsB = kind == "distance" || kind == "angle";
            if (s["a"] == null || (needsB && s["b"] == null))
            {
                errCode = "MISSING_PARAMETER";
                errMsg = needsB ? $"kind='{kind}' needs both 'a' and 'b'." : "measure needs 'a'.";
                return null;
            }

            var index = new MeasureIndex(partDoc);
            string err;
            var a = ResolveMeasureTarget(s["a"], "a", index, out err);
            if (a == null) { errCode = "INVALID_PARAMETER"; errMsg = err; return null; }
            MeasureTarget b = null;
            if (s["b"] != null && s["b"].Type != JTokenType.Null)
            {
                b = ResolveMeasureTarget(s["b"], "b", index, out err);
                if (b == null) { errCode = "INVALID_PARAMETER"; errMsg = err; return null; }
            }

            var result = new JObject();
            bool anyPoint = a.Point != null || (b != null && b.Point != null);
            if (anyPoint)
            {
                // Points are not selectable entities for IMeasure here: measure them geometrically.
                if (kind != "distance" || b == null)
                {
                    errCode = "INVALID_PARAMETER";
                    errMsg = "A point supports only kind='distance', against another point, a face or an edge.";
                    return null;
                }
                double? d;
                if (a.Point != null && b.Point != null)
                {
                    double dx = a.Point[0] - b.Point[0], dy = a.Point[1] - b.Point[1], dz = a.Point[2] - b.Point[2];
                    d = Math.Sqrt(dx * dx + dy * dy + dz * dz);
                }
                else
                {
                    d = a.Point != null ? PointToEntity(a.Point, b.Entity) : PointToEntity(b.Point, a.Entity);
                }
                if (!d.HasValue)
                {
                    errCode = "ANALYSIS_FAILED";
                    errMsg = "Could not compute the point-to-entity distance.";
                    return null;
                }
                result["distance_mm"] = Math.Round(d.Value * 1000.0, 4);
                return result;
            }

            IMeasure m = modelDoc.Extension.CreateMeasure();
            if (m == null)
            {
                errCode = "ANALYSIS_FAILED";
                errMsg = "IModelDocExtension.CreateMeasure returned null.";
                return null;
            }
            m.ArcOption = 0;     // arcs/cylinders: centre to centre (what drawing dimensions between holes mean)
            var ents = b != null ? new object[] { a.Entity, b.Entity } : new object[] { a.Entity };
            if (!m.Calculate(ents))
            {
                errCode = "ANALYSIS_FAILED";
                errMsg = "SolidWorks cannot measure that combination of entities.";
                return null;
            }

            // IMeasure reports -1 for a quantity that does not apply to the entities. Values are SI
            // (m, m^2, radians): the same units every other analyze payload uses.
            double v;
            switch (kind)
            {
                case "distance": v = m.Distance; if (v >= 0) result["distance_mm"] = Math.Round(v * 1000.0, 4); break;
                case "angle": v = m.Angle; if (v >= 0) result["angle_deg"] = Math.Round(v * 180.0 / Math.PI, 4); break;
                case "radius": v = m.Radius; if (v >= 0) result["radius_mm"] = Math.Round(v * 1000.0, 4); break;
                case "diameter": v = m.Diameter; if (v >= 0) result["diameter_mm"] = Math.Round(v * 1000.0, 4); break;
                case "length": v = m.Length; if (v >= 0) result["length_mm"] = Math.Round(v * 1000.0, 4); break;
                default: v = m.Area; if (v >= 0) result["area_mm2"] = Math.Round(v * 1e6, 4); break;
            }
            if (result.Count == 0)
            {
                errCode = "ANALYSIS_FAILED";
                errMsg = $"kind='{kind}' does not apply to these entities (IMeasure returned -1).";
                return null;
            }
            return result;
        }
    }
}
