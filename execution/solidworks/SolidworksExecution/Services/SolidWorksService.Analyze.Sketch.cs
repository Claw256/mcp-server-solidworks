using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;

namespace SolidworksExecution.Services
{
    // SolidWorksService partial: sketch HEALTH for analyze_model - solve status, per-segment status, relations.
    // Read-only; every read is best-effort (a failure omits the key).
    public partial class SolidWorksService
    {
        private const int MaxSketchRelations = 150;

        // swConstrainedStatus_e (ints; ADR-018): 1 unknown, 2 under, 3 fully, 4 over, 5 no solution, 6 invalid, 7 autosolve off.
        private static string ConstrainedStatusName(int s)
        {
            switch (s)
            {
                case 2: return "under_defined";
                case 3: return "fully_defined";
                case 4: return "over_defined";
                case 5: return "no_solution";
                case 6: return "invalid_solution";
                case 7: return "autosolve_off";
                default: return "unknown";
            }
        }

        // swConstraintType_e values that appear in practice; anything else reads "type_<n>".
        private static string RelationTypeName(int t)
        {
            switch (t)
            {
                case 1: return "distance";
                case 2: return "angle";
                case 3: return "radius";
                case 4: return "horizontal";
                case 5: return "vertical";
                case 6: return "tangent";
                case 7: return "parallel";
                case 8: return "perpendicular";
                case 9: return "coincident";
                case 10: return "concentric";
                case 11: return "symmetric";
                case 12: return "midpoint";
                case 13: return "intersection";
                case 14: return "equal";
                case 15: return "diameter";
                case 16: return "offset_edge";
                case 17: return "fixed";
                case 25: return "horizontal_points";
                case 26: return "vertical_points";
                case 27: return "collinear";
                case 28: return "coradial";
                case 32: return "use_edge";
                case 42: return "merge_points";
                case 44: return "arc_length";
                case 47: return "sketch_offset";
                case 56: return "intersection_pt";
                case 57: return "patterned";
                case 60: return "fit_spline";
                default: return "type_" + t;
            }
        }

        // One relation: {type, entities:[{seg:i} | {point:[x,y]} | {other:true}]}. `seg` is the index into the
        // sketch's `segments` array when that array is available (detail mode), else the segment kind is given.
        private JObject ReadRelation(ISketchRelation rel, JArray segments)
        {
            var rj = new JObject();
            try { rj["type"] = RelationTypeName(rel.GetRelationType()); } catch { return null; }
            var ents = new JArray();
            try
            {
                var arr = rel.GetEntities() as object[];
                if (arr != null)
                    foreach (var o in arr)
                    {
                        if (o == null) continue;
                        var seg = o as ISketchSegment;
                        var pt = o as ISketchPoint;
                        if (seg != null)
                        {
                            var sj = ReadSegment(seg);
                            int idx = -1;
                            if (sj != null && segments != null)
                                for (int i = 0; i < segments.Count; i++)
                                    if (JToken.DeepEquals(segments[i], sj)) { idx = i; break; }
                            if (idx >= 0) ents.Add(new JObject { ["seg"] = idx });
                            else ents.Add(new JObject { ["kind"] = sj?.Value<string>("kind") ?? "other" });
                        }
                        else if (pt != null)
                            ents.Add(new JObject { ["point"] = new JArray { R6(pt.X), R6(pt.Y) } });
                        else
                            ents.Add(new JObject { ["other"] = true });
                    }
            }
            catch { }
            rj["entities"] = ents;
            return rj;
        }

        private JArray ReadRelations(ISketchRelationManager mgr, int filter, JArray segments, out bool truncated)
        {
            truncated = false;
            var list = new JArray();
            var rels = mgr.GetRelations(filter) as object[];
            if (rels == null) return list;
            foreach (var o in rels)
            {
                var rel = o as ISketchRelation;
                if (rel == null) continue;
                if (list.Count >= MaxSketchRelations) { truncated = true; break; }
                var rj = ReadRelation(rel, segments);
                if (rj != null) list.Add(rj);
            }
            return list;
        }

        // Detail mode (analysis_type='sketch'): status + the segments that are NOT fully defined + relations.
        //   health: {status, segment_status:{under_defined:[i..]|...}?, relations:[...], relations_truncated?,
        //            over_defining?:[...], dangling_count?}
        // segStatus is parallel to the `segments` array (same indices).
        private JObject ReadSketchHealth(ISketch sketch, JArray segments, List<int> segStatus)
        {
            var h = new JObject();
            try { h["status"] = ConstrainedStatusName(sketch.GetConstrainedStatus()); } catch { }

            if (segStatus != null)
            {
                var byStatus = new JObject();
                for (int i = 0; i < segStatus.Count; i++)
                {
                    if (segStatus[i] == 3) continue;                  // fully defined = the unremarkable case
                    string n = ConstrainedStatusName(segStatus[i]);
                    var arr = byStatus[n] as JArray;
                    if (arr == null) { arr = new JArray(); byStatus[n] = arr; }
                    arr.Add(i);
                }
                if (byStatus.Count > 0) h["segment_status"] = byStatus;
            }

            try
            {
                var mgr = sketch.RelationManager as ISketchRelationManager;
                if (mgr != null)
                {
                    bool trunc;
                    h["relations"] = ReadRelations(mgr, 0, segments, out trunc);          // swAll
                    if (trunc) h["relations_truncated"] = true;
                    if (h.Value<string>("status") == "over_defined")
                    {
                        bool t2;
                        var over = ReadRelations(mgr, 2, segments, out t2);               // swOverDefining
                        if (over.Count > 0) h["over_defining"] = over;
                    }
                    try { int dang = mgr.GetRelationsCount(1); if (dang > 0) h["dangling_count"] = dang; } catch { }   // swDangling
                }
            }
            catch { }
            return h;
        }
    }
}
