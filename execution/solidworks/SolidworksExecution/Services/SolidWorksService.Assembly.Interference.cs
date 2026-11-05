using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;


namespace SolidworksExecution.Services
{
    // SolidWorksService partial: assembly diagnostics for analyze_assembly — interference detection,
    // per-component status and the richer mate readback. All best-effort reads: one unreadable
    // component/mate/interference never fails the whole read, and a key is emitted only when it was read.
    // Enum ints inlined per ADR-018 (values reflected from swconst 33.1.2.4 = SolidWorks 2025).
    public partial class SolidWorksService
    {
        private const int InterferenceLimitDefault = 20;
        private const int InterferenceLimitMax = 200;

        // ---- selection save/restore (interference detection opens a PropertyManager that disturbs it) ----

        private static List<KeyValuePair<object, int>> CaptureSelection(IModelDoc2 modelDoc)
        {
            var saved = new List<KeyValuePair<object, int>>();
            try
            {
                var selMgr = modelDoc.SelectionManager as ISelectionMgr;
                int n = selMgr?.GetSelectedObjectCount2(-1) ?? 0;
                for (int i = 1; i <= n; i++)
                {
                    object o = selMgr.GetSelectedObject6(i, -1);
                    if (o != null) saved.Add(new KeyValuePair<object, int>(o, selMgr.GetSelectedObjectMark(i)));
                }
            }
            catch { }
            return saved;
        }

        private static void RestoreSelection(IModelDoc2 modelDoc, List<KeyValuePair<object, int>> saved)
        {
            try
            {
                modelDoc.ClearSelection2(true);
                if (saved == null || saved.Count == 0) return;
                var selMgr = modelDoc.SelectionManager as ISelectionMgr;
                foreach (var grp in saved.GroupBy(s => s.Value))
                {
                    var sd = selMgr.CreateSelectData();
                    sd.Mark = grp.Key;
                    var wrapped = grp.Select(s => new DispatchWrapper(s.Key)).ToArray();
                    modelDoc.Extension.MultiSelect2(wrapped, true, sd);
                }
            }
            catch { }
        }

        private static double Round6(double v) { return Math.Round(v, 6); }

        // ---- interference detection ----------------------------------------------------------------------

        // analyze_assembly(interference). Opens the Interference Detection PropertyManager (the documented
        // way to get volumes — the non-PM ToolsCheckInterference2 returns only components/faces), reads
        // the interferences, then ALWAYS restores the manager options, closes the PM (Done) and puts the
        // selection back. Returns null when SolidWorks gives no manager. Volume in mm^3; bbox/center in assembly space, meters (like transforms).
        private JObject ReadInterference(IModelDoc2 modelDoc, IAssemblyDoc asm, bool coincident,
            bool subassembliesAsComponents, int limit)
        {
            limit = Math.Max(1, Math.Min(limit, InterferenceLimitMax));
            var saved = CaptureSelection(modelDoc);
            var undo = new List<Action>();
            IInterferenceDetectionMgr mgr = null;
            bool opened = false;
            try
            {
                asm.ToolsCheckInterference();
                opened = true;
                mgr = asm.InterferenceDetectionManager as IInterferenceDetectionMgr;
                if (mgr == null) return null;          // caller reports INTERFERENCE_FAILED

                // Pin every option that changes the result or the display; remember the old value to restore.
                void Opt<T>(Func<T> get, Action<T> set, T want)
                {
                    try { T old = get(); set(want); undo.Add(() => set(old)); } catch { }
                }
                Opt(() => mgr.TreatCoincidenceAsInterference, v => mgr.TreatCoincidenceAsInterference = v, coincident);
                Opt(() => mgr.TreatSubAssembliesAsComponents, v => mgr.TreatSubAssembliesAsComponents = v, subassembliesAsComponents);
                Opt(() => mgr.IncludeMultibodyPartInterferences, v => mgr.IncludeMultibodyPartInterferences = v, true);
                Opt(() => mgr.IgnoreHiddenBodies, v => mgr.IgnoreHiddenBodies = v, false);
                Opt(() => mgr.ShowIgnoredInterferences, v => mgr.ShowIgnoredInterferences = v, false);
                Opt(() => mgr.UseTransform, v => mgr.UseTransform = v, false);
                Opt(() => mgr.CreateFastenersFolder, v => mgr.CreateFastenersFolder = v, false);       // would add a tree folder
                Opt(() => mgr.MakeInterferingPartsTransparent, v => mgr.MakeInterferingPartsTransparent = v, false);
                Opt(() => mgr.NonInterferingComponentDisplay, v => mgr.NonInterferingComponentDisplay = v, 3); // swNonInterferingComponentDisplay_Current

                var ints = mgr.GetInterferences() as object[];
                var items = new List<JObject>();
                if (ints != null)
                {
                    foreach (var o in ints)
                    {
                        var itf = o as IInterference;
                        if (itf == null) continue;
                        try
                        {
                            var ij = new JObject();
                            var names = new JArray();
                            var comps = itf.Components as object[];
                            if (comps != null)
                                foreach (var c in comps)
                                {
                                    var cc = c as IComponent2;
                                    if (cc != null) names.Add(cc.Name2);
                                }
                            ij["components"] = names;
                            ij["volume_mm3"] = Math.Round(itf.Volume * 1e9, 3);
                            try
                            {
                                var body = itf.GetInterferenceBody() as IBody2;
                                var bb = body?.GetBodyBox() as double[];
                                if (bb != null && bb.Length >= 6)
                                {
                                    ij["bbox"] = new JArray(bb.Take(6).Select(Round6));
                                    ij["center"] = new JArray(Round6((bb[0] + bb[3]) / 2), Round6((bb[1] + bb[4]) / 2), Round6((bb[2] + bb[5]) / 2));
                                }
                            }
                            catch { }
                            try { if (itf.IsFastener) ij["fastener"] = true; } catch { }
                            try { if (itf.IsPossibleInterference) ij["possible"] = true; } catch { }
                            items.Add(ij);
                        }
                        catch { }
                    }
                }

                items = items.OrderByDescending(j => (double)j["volume_mm3"]).ToList();
                double total = items.Sum(j => (double)j["volume_mm3"]);
                var arr = new JArray(items.Take(limit));

                var root = new JObject();
                root["interference_count"] = items.Count;
                root["shown"] = arr.Count;
                root["total_volume_mm3"] = Math.Round(total, 3);
                root["coincident"] = coincident;
                root["subassemblies_as_components"] = subassembliesAsComponents;
                root["interferences"] = arr;
                root["summary"] = SummarizeInterference(items, arr.Count, total);
                return root;
            }
            finally
            {
                for (int i = undo.Count - 1; i >= 0; i--) { try { undo[i](); } catch { } }
                if (opened) { try { mgr?.Done(); } catch { } }       // closes the PropertyManager
                RestoreSelection(modelDoc, saved);
            }
        }

        private static string SummarizeInterference(List<JObject> items, int shown, double totalMm3)
        {
            if (items.Count == 0) return "no interferences";
            var top = items[0];
            string pair = string.Join(" x ", top["components"].Select(t => (string)t));
            return $"{items.Count} interference(s) (showing {shown}), total {Math.Round(totalMm3, 1)} mm3; largest {pair} {top["volume_mm3"]} mm3";
        }

        // ---- per-component status ------------------------------------------------------------------------

        private static void AddFeatureError(IFeature feat, JObject j)
        {
            try
            {
                bool isWarning;
                int code = feat.GetErrorCode2(out isWarning);
                if (code == 0) return;
                j["error_code"] = code;
                j["error"] = FeatureErrorNames.For(code);
                if (isWarning) j["warning"] = true;
            }
            catch { }
        }

        // Component Name2 -> its "Reference" feature's error entry (What's Wrong state), one tree walk.
        private static Dictionary<string, JObject> ComponentReferenceErrors(IModelDoc2 modelDoc)
        {
            var map = new Dictionary<string, JObject>();
            try
            {
                var feat = modelDoc.FirstFeature() as IFeature;
                int guard = 0;
                while (feat != null && guard++ < 10000)
                {
                    if ((feat.GetTypeName2() ?? "") == "Reference")
                    {
                        var comp = feat.GetSpecificFeature2() as IComponent2;
                        if (comp != null)
                        {
                            var e = new JObject();
                            AddFeatureError(feat, e);
                            if (e.Count > 0) map[comp.Name2] = e;
                        }
                    }
                    feat = feat.GetNextFeature() as IFeature;
                }
            }
            catch { }
            return map;
        }

        // Adds: hidden, out_of_date, solve (subassemblies only), constrained (only when not fully
        // defined), and the Reference feature's error/warning. Suppressed components get none of them.
        private static void AddComponentStatus(IComponent2 comp, JObject cj, Dictionary<string, JObject> refErrors)
        {
            try { if (comp.IsSuppressed()) return; } catch { return; }
            try { if (comp.Visible == 0) cj["hidden"] = true; } catch { }                       // swComponentHidden
            try { if (comp.IsDisplayDataOutOfDate() == 2) cj["out_of_date"] = true; } catch { } // swModelOutOfDate
            try
            {
                int s = comp.Solving;                                                           // -1 for part components
                if (s >= 0) cj["solve"] = s == 1 ? "flexible" : "rigid";
            }
            catch { }
            try
            {
                switch (comp.GetConstrainedStatus())                                            // swConstrainedStatus_e
                {
                    case 2: cj["constrained"] = "under"; break;
                    case 4: cj["constrained"] = "over"; break;
                    case 5: cj["constrained"] = "no_solution"; break;
                    case 6: cj["constrained"] = "invalid"; break;
                }
            }
            catch { }
            JObject err;
            if (refErrors != null && refErrors.TryGetValue(comp.Name2, out err))
                foreach (var kv in err) cj[kv.Key] = kv.Value;
        }

        private static string SummarizeComponents(JArray comps)
        {
            int Count(string key) { return comps.Count(c => c[key] != null); }
            var parts = new List<string> { comps.Count + " component(s)" };
            foreach (var key in new[] { "suppressed", "hidden", "out_of_date", "constrained", "error" })
            {
                int n = Count(key);
                if (n > 0) parts.Add(n + " " + (key == "constrained" ? "not fully defined" : key == "error" ? "with errors" : key));
            }
            return string.Join(", ", parts);
        }

        // ---- richer mate readback ------------------------------------------------------------------------

        // Adds to a mate entry: suppressed, flipped (distance/angle), lock_rotation (concentric/profile
        // center), limits {min,max} (limit distance/angle mates, SI m / rad — the same units as `value`),
        // and the mate feature's error/warning. Existing keys are untouched.
        private static void AddMateExtras(IFeature sub, IMate2 mate, int mtype, JObject mj)
        {
            try { if (sub.IsSuppressed()) mj["suppressed"] = true; } catch { }
            AddFeatureError(sub, mj);
            if (mtype == 5 || mtype == 6)
            {
                try { mj["flipped"] = mate.Flipped; } catch { }
            }
            object def = null;
            try { def = sub.GetDefinition(); } catch { }
            if (def == null) return;
            try
            {
                var conc = def as IConcentricMateFeatureData;
                if (conc != null) mj["lock_rotation"] = conc.LockRotation;
                var prof = def as IProfileCenterMateFeatureData;
                if (prof != null) mj["lock_rotation"] = prof.LockRotation;
                var dist = def as IDistanceMateFeatureData;
                if (dist != null && dist.IsAdvancedMate)
                    mj["limits"] = new JObject { ["min"] = Math.Round(dist.MinimumDistance, 9), ["max"] = Math.Round(dist.MaximumDistance, 9) };
                var ang = def as IAngleMateFeatureData;
                if (ang != null && ang.IsAdvancedMate)
                    mj["limits"] = new JObject { ["min"] = Math.Round(ang.MinimumAngle, 9), ["max"] = Math.Round(ang.MaximumAngle, 9) };
            }
            catch { }
        }

        private static string SummarizeMates(JArray mates)
        {
            var parts = new List<string> { mates.Count + " mate(s)" };
            int sup = mates.Count(m => m["suppressed"] != null);
            if (sup > 0) parts.Add(sup + " suppressed");
            var bad = mates.Where(m => m["error"] != null).ToList();
            if (bad.Count > 0)
            {
                string list = string.Join("; ", bad.Take(5).Select(m => (string)m["name"] + " (" + (string)m["error"] + ")"));
                parts.Add(bad.Count + " with errors: " + list + (bad.Count > 5 ? "; ..." : ""));
            }
            return string.Join(", ", parts);
        }
    }
}
