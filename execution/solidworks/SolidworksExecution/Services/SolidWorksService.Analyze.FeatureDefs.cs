using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;

namespace SolidworksExecution.Services
{
    // SolidWorksService partial: read-only feature DEFINITIONS for analyze_model(features) - revolve, fillet/chamfer,
    // hole wizard, loft/sweep and the extrude extras (draft, thin, direction 2, start condition, merge, up-to entity).
    // Same discipline as TryReadMirror/TryReadEdgeFlange: plain properties are read directly; anything that is a
    // SELECTION sits behind AccessSelections (which temporarily rolls the model back) and is released in finally.
    // Every reader is best-effort - a key is emitted only when it could be read, and a failure omits the block.
    public partial class SolidWorksService
    {
        private const int MaxDefEdges = 24;     // fillet/chamfer: edges listed (edge_count always reports the full set)

        private static double Deg(double rad) { return R6(rad * 180.0 / Math.PI); }

        // Dispatch by feature type name (swFeatureNameID_e short names).
        private void TryReadDefinition(IFeature feat, string type, IModelDoc2 modelDoc, JObject fj)
        {
            switch (type)
            {
                case "Revolution":
                case "RevCut":
                    TryReadRevolve(feat, modelDoc, fj); break;
                case "Fillet":
                    TryReadFillet(feat, modelDoc, fj); break;
                case "Chamfer":
                    TryReadChamfer(feat, modelDoc, fj); break;
                case "HoleWzd":
                    TryReadHoleWizard(feat, fj); break;
                case "Blend":
                case "BlendCut":
                case "Loft":
                case "LoftCut":
                    TryReadLoft(feat, modelDoc, fj); break;
                case "Sweep":
                case "SweepCut":
                    TryReadSweep(feat, modelDoc, fj); break;
            }
        }

        // Edges behind AccessSelections -> {count, edges:[BuildEdgeJson] (first MaxDefEdges)}. PRE-feature geometry
        // (the model is rolled back while access is held), i.e. the same frame feature_map's consumed_edges use.
        private void AddDefEdges(object edgesObj, JObject target)
        {
            var edges = edgesObj as object[];
            if (edges == null) return;
            var arr = new JArray();
            int n = 0;
            foreach (var o in edges)
            {
                var e = o as IEdge;
                if (e == null) continue;
                n++;
                if (arr.Count < MaxDefEdges) arr.Add(BuildEdgeJson(e, -1));
            }
            if (n > 0) { target["edge_count"] = n; target["edges"] = arr; }
        }

        private static string RevolveTypeName(int t)
        {
            switch (t)
            {
                case 0: return "one_direction";
                case 1: return "mid_plane";
                case 2: return "two_direction";
                case 3: return "one_direction_360";
                case 4: return "mid_plane_360";
                default: return "two_direction_360";
            }
        }

        private void TryReadRevolve(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IRevolveFeatureData2;
                if (def == null) return;
                var rj = new JObject();
                int t = def.Type;
                rj["type"] = RevolveTypeName(t);
                try { rj["angle_deg"] = Deg(def.GetRevolutionAngle(true)); } catch { }
                if (t == 2 || t == 5) { try { rj["angle2_deg"] = Deg(def.GetRevolutionAngle(false)); } catch { } }
                try { if (def.ReverseDirection) rj["reversed"] = true; } catch { }
                try { if (!def.Merge) rj["merge"] = false; } catch { }
                try
                {
                    if (def.IsThinFeature())
                    {
                        var th = new JObject();
                        th["type"] = def.ThinWallType;      // 0 one dir, 1 opposite, 2 mid plane, 3 two dir
                        th["wall"] = R6(def.GetWallThickness(true));
                        rj["thin"] = th;
                    }
                }
                catch { }
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try
                    {
                        // swSelType ints (ADR-018): 5 datum axis, 1 edge, 10 sketch segment.
                        int at = def.GetAxisType();
                        object axis = def.Axis;
                        var aj = new JObject();
                        if (at == 5) { aj["kind"] = "axis"; var af = axis as IFeature; if (af != null) aj["name"] = af.Name; }
                        else if (at == 1 && axis is IEdge) { aj = BuildEdgeJson((IEdge)axis, -1); aj["kind"] = "edge"; }
                        else if (at == 10 && axis is ISketchSegment) { aj = ReadSegment((ISketchSegment)axis) ?? new JObject(); aj["kind"] = "sketch_segment"; }
                        else aj["kind"] = "sw_type_" + at;
                        rj["axis"] = aj;
                    }
                    catch { }
                    finally { try { def.ReleaseSelectionAccess(); } catch { } }
                }
                fj["revolve"] = rj;
            }
            catch { }
        }

        private void TryReadFillet(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as ISimpleFilletFeatureData2;
                if (def == null) return;           // variable / face-blend fillets use other definitions: omitted
                var fl = new JObject();
                int t = def.Type;                  // swSimpleFilletType_e: 0 constant radius, 2 face, 3 full round
                fl["type"] = t == 0 ? "constant_radius" : (t == 2 ? "face" : (t == 3 ? "full_round" : "type_" + t));
                try { fl["radius"] = R6(def.DefaultRadius); } catch { }
                try { if (def.AsymmetricFillet) { fl["asymmetric"] = true; fl["radius2"] = R6(def.DefaultDistance); } } catch { }
                try { if (def.IsMultipleRadius) fl["multiple_radius"] = true; } catch { }
                try { if (def.PropagateToTangentFaces) fl["tangent_propagation"] = true; } catch { }
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try { AddDefEdges(def.Edges, fl); }
                    catch { }
                    finally { try { def.ReleaseSelectionAccess(); } catch { } }
                }
                fj["fillet"] = fl;
            }
            catch { }
        }

        private void TryReadChamfer(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IChamferFeatureData2;
                if (def == null) return;
                var cj = new JObject();
                int t = def.Type;                  // swChamferType_e: 1 angle-distance, 2 distance-distance, 3 vertex, 16 equal distance
                cj["type"] = t == 1 ? "angle_distance" : (t == 2 ? "distance_distance" : (t == 3 ? "vertex" : (t == 16 ? "equal_distance" : "type_" + t)));
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                try
                {
                    if (t != 3)
                    {
                        try { cj["distance"] = R6(def.GetEdgeChamferDistance(0)); } catch { }
                        if (t == 2) { try { cj["distance2"] = R6(def.GetEdgeChamferDistance(1)); } catch { } }
                        if (t == 1) { try { cj["angle_deg"] = Deg(def.EdgeChamferAngle); } catch { } }
                    }
                    if (acc) { try { AddDefEdges(def.Edges, cj); } catch { } }
                }
                finally { if (acc) { try { def.ReleaseSelectionAccess(); } catch { } } }
                fj["chamfer"] = cj;
            }
            catch { }
        }

        // swWzdHoleStandards_e names (the ones HoleWizard.cs can create) - anything else reads "standard_<n>".
        private static string HoleStandardName(int s)
        {
            switch (s)
            {
                case 0: return "ansi_inch";
                case 1: return "ansi_metric";
                case 8: return "iso";
                case 4: return "din";
                case 9: return "jis";
                case 2: return "bsi";
                default: return "standard_" + s;
            }
        }

        // swWzdHoleTypes_e -> the hole_type family HoleWizard.cs accepts back (hole/counterbore/countersink/tap/slot).
        private static string HoleFamily(int t)
        {
            if ((t >= 31 && t <= 42) || (t >= 46 && t <= 56)) return "tap";
            if ((t >= 57 && t <= 79) || t == 90) return "slot";
            if (t == 2 || t == 7 || (t >= 10 && t <= 21)) return "counterbore";
            if (t == 3 || t == 8 || t == 24 || t == 29 || t == 30 || t == 43 || t == 44 || t == 45) return "countersink";
            return "hole";
        }

        private void TryReadHoleWizard(IFeature feat, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IWizardHoleFeatureData2;
                if (def == null) return;
                var hj = new JObject();
                try { int t = def.Type; hj["type_id"] = t; hj["hole_type"] = HoleFamily(t); } catch { }
                try { int s = def.Standard2; hj["standard"] = s < 0 ? "custom" : HoleStandardName(s); } catch { }
                try { hj["fastener_type_id"] = def.FastenerType2; } catch { }
                try { var sz = def.FastenerSize; if (!string.IsNullOrEmpty(sz)) hj["size"] = sz; } catch { }
                try { hj["diameter"] = R6(def.Diameter); } catch { }
                try { hj["depth"] = R6(def.Depth); } catch { }
                try { hj["end"] = EndConditionName(def.EndCondition); } catch { }
                try { if (def.ReverseDirection) hj["reversed"] = true; } catch { }
                if (hj.Count > 0) fj["hole_wizard"] = hj;
            }
            catch { }
        }

        // Profile/guide names live behind AccessSelections: loft Profiles are IFeature objects (sketches), so
        // their names are emitted; counts come from the plain GetProfileCount/GetGuideCurvesCount.
        private void TryReadLoft(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as ILoftFeatureData;
                if (def == null) return;
                var lj = new JObject();
                try { lj["profiles"] = (int)def.GetProfileCount(); } catch { }
                try { int g = def.GetGuideCurvesCount(); if (g > 0) lj["guide_curves"] = g; } catch { }
                try { if (def.Close) lj["closed"] = true; } catch { }
                try { if (!def.Merge) lj["merge"] = false; } catch { }
                try { if (def.IsThinFeature()) lj["thin"] = true; } catch { }
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try
                    {
                        var profs = def.Profiles as object[];
                        if (profs != null)
                        {
                            var names = new JArray();
                            foreach (var o in profs) { var pf = o as IFeature; if (pf != null) names.Add(pf.Name); }
                            if (names.Count > 0) lj["profile_sketches"] = names;
                        }
                    }
                    catch { }
                    finally { try { def.ReleaseSelectionAccess(); } catch { } }
                }
                fj["loft"] = lj;
            }
            catch { }
        }

        private void TryReadSweep(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as ISweepFeatureData;
                if (def == null) return;
                var sj = new JObject();
                try { int g = def.GetGuideCurvesCount(); if (g > 0) sj["guide_curves"] = g; } catch { }
                try { if (def.CircularProfile) { sj["circular_profile"] = true; sj["profile_diameter"] = R6(def.CircularProfileDiameter); } } catch { }
                try { if (!def.Merge) sj["merge"] = false; } catch { }
                try { if (def.ThinFeature) sj["thin"] = true; } catch { }
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try
                    {
                        var pf = def.Profile as IFeature;
                        if (pf != null) sj["profile_sketch"] = pf.Name;
                        object path = def.Path;
                        var pathFeat = path as IFeature;
                        if (pathFeat != null) { sj["path_sketch"] = pathFeat.Name; }
                        else if (path is object[]) { AddDefEdges(path, sj); sj["path_kind"] = "edges"; }
                    }
                    catch { }
                    finally { try { def.ReleaseSelectionAccess(); } catch { } }
                }
                if (sj.Count > 0) fj["sweep"] = sj;
            }
            catch { }
        }

        // Extra extrude/cut keys merged into the existing `extrude` object (ReadExtrude owns end/depth/reversed):
        //   draft_deg [+draft_outward], both_directions + direction2:{end,depth?,draft_deg?}, thin:{type,wall[,wall2]},
        //   start:{type[,offset]}, merge:false, up_to:{kind, name?/face?/point?} for up-to end conditions.
        private void TryReadExtrudeExtras(IFeature feat, IModelDoc2 modelDoc, JObject ej)
        {
            try
            {
                var ed = feat.GetDefinition() as IExtrudeFeatureData2;
                if (ed == null) return;
                try
                {
                    if (ed.GetDraftWhileExtruding(true))
                    {
                        ej["draft_deg"] = Deg(ed.GetDraftAngle(true));
                        if (ed.GetDraftOutward(true)) ej["draft_outward"] = true;
                    }
                }
                catch { }
                try
                {
                    if (ed.BothDirections)
                    {
                        ej["both_directions"] = true;
                        var d2 = new JObject();
                        int e2 = ed.GetEndCondition(false);
                        d2["end"] = EndConditionName(e2);
                        if (e2 == 0) d2["depth"] = R6(ed.GetDepth(false));
                        try { if (ed.GetDraftWhileExtruding(false)) d2["draft_deg"] = Deg(ed.GetDraftAngle(false)); } catch { }
                        ej["direction2"] = d2;
                    }
                }
                catch { }
                try
                {
                    if (ed.IsThinFeature())
                    {
                        var th = new JObject();
                        int tt = ed.ThinWallType;      // 0 one dir, 1 one dir reversed, 2 mid plane, 3 two dir
                        th["type"] = tt;
                        th["wall"] = R6(ed.GetWallThickness(true));
                        if (tt == 3) th["wall2"] = R6(ed.GetWallThickness(false));
                        ej["thin"] = th;
                    }
                }
                catch { }
                try
                {
                    int ft = ed.FromType;              // swExtrudeFrom_e: 0 sketch plane (default), 1 face/surface/plane, 2 vertex, 3 offset
                    if (ft != 0)
                    {
                        var st = new JObject();
                        st["type"] = ft == 1 ? "surface_face_plane" : (ft == 2 ? "vertex" : (ft == 3 ? "offset" : "type_" + ft));
                        if (ft == 3) st["offset"] = R6(ed.FromOffsetDistance);
                        ej["start"] = st;
                    }
                }
                catch { }
                try { if (!ed.Merge) ej["merge"] = false; } catch { }

                // Up-to entity: only for the end conditions that reference one (vertex 3, surface 4, offset-from-
                // surface 5, body 7, selection 10). The reference sits behind AccessSelections.
                int endc = ed.GetEndCondition(true);
                if (endc == 3 || endc == 4 || endc == 5 || endc == 7 || endc == 10)
                {
                    bool acc = false;
                    try { acc = ed.AccessSelections(modelDoc, null); } catch { }
                    if (acc)
                    {
                        try
                        {
                            int rt;
                            object r = ed.GetEndConditionReference(true, out rt);
                            var uj = new JObject();
                            var face = r as IFace2;
                            var rf = r as IFeature;
                            var body = r as IBody2;
                            var vtx = r as IVertex;
                            if (face != null) { uj = BuildFaceJson(face, -1); uj.Remove("i"); uj["kind"] = "face"; }
                            else if (rf != null) { uj["kind"] = "feature"; uj["name"] = rf.Name; }
                            else if (body != null) { uj["kind"] = "body"; try { uj["name"] = body.Name; } catch { } }
                            else if (vtx != null)
                            {
                                uj["kind"] = "vertex";
                                var pt = vtx.GetPoint() as double[];
                                if (pt != null && pt.Length >= 3) uj["point"] = new JArray { R6(pt[0]), R6(pt[1]), R6(pt[2]) };
                            }
                            else if (r != null) uj["kind"] = "ref_type_" + rt;      // swSelectionReferenceTypes_e
                            if (uj.Count > 0) ej["up_to"] = uj;
                        }
                        catch { }
                        finally { try { ed.ReleaseSelectionAccess(); } catch { } }
                    }
                }
            }
            catch { }
        }
    }
}
