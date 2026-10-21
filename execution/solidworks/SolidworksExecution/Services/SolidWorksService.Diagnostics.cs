using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace SolidworksExecution.Services
{
    // Read-only health read-back around a mutating tool: which features SolidWorks reports in error
    // (IFeature.GetErrorCode2 - the What's Wrong icon), how many What's Wrong items there are, and how
    // the solid's volume moved. Never throws and never changes state: a failure here yields null and the
    // tool's own response stands unchanged.
    public partial class SolidWorksService
    {
        /// <summary>Snapshot taken BEFORE a mutating tool so its effect can be isolated from pre-existing errors.</summary>
        public JObject CaptureDiagnosticsBaseline()
        {
            try
            {
                var doc = _solidWorks?.IActiveDoc2 as IModelDoc2;
                if (doc == null) return null;
                var b = new JObject();
                b["errors"] = new JArray(ReadFeatureErrors(doc).Select(e => (string)e["key"]));
                double? v = ReadVolume(doc);
                if (v.HasValue) b["volume_m3"] = v.Value;
                return b;
            }
            catch { return null; }
        }

        /// <summary>The post-tool read-back, diffed against <paramref name="baseline"/> (may be null).</summary>
        public JObject CollectDiagnostics(JObject baseline)
        {
            try
            {
                var doc = _solidWorks?.IActiveDoc2 as IModelDoc2;
                if (doc == null) return null;
                var known = new HashSet<string>(
                    (baseline?["errors"] as JArray)?.Select(t => (string)t) ?? Enumerable.Empty<string>());
                var all = ReadFeatureErrors(doc);
                var fresh = all.Where(e => !known.Contains((string)e["key"])).ToList();

                var d = new JObject();
                d["feature_errors_total"] = all.Count;
                var errs = new JArray();
                foreach (var e in fresh) { e.Remove("key"); errs.Add(e); }
                d["new_feature_errors"] = errs;
                try { d["whats_wrong"] = doc.Extension.GetWhatsWrongCount(); } catch { }
                double? v = ReadVolume(doc);
                if (v.HasValue)
                {
                    d["volume_m3"] = Sig6(v.Value);
                    double? before = baseline?["volume_m3"]?.Value<double?>();
                    if (before.HasValue) d["volume_delta_m3"] = Sig6(v.Value - before.Value);
                }
                return d;
            }
            catch { return null; }
        }

        // One entry per feature whose GetErrorCode2 is non-zero. `key` identifies it across the
        // before/after snapshots (name + code) and is stripped before the data leaves this class.
        private static List<JObject> ReadFeatureErrors(IModelDoc2 doc)
        {
            var list = new List<JObject>();
            object[] features = doc.FeatureManager.GetFeatures(true) as object[];
            if (features == null) return list;
            foreach (var o in features)
            {
                var f = o as IFeature;
                if (f == null) continue;
                bool isWarning;
                int code;
                try { code = f.GetErrorCode2(out isWarning); }
                catch { continue; }
                if (code == 0) continue;
                var e = new JObject();
                e["feature"] = f.Name;
                try { e["type"] = f.GetTypeName2(); } catch { }
                e["code"] = code;
                e["error"] = Enum.GetName(typeof(swFeatureError_e), code) ?? ("code " + code);
                e["warning"] = isWarning;
                e["key"] = f.Name + "|" + code;
                list.Add(e);
            }
            return list;
        }

        private static double? ReadVolume(IModelDoc2 doc)
        {
            if (!(doc is IPartDoc)) return null;                    // assemblies: mass reads are slow and not the question
            int st = 0;
            object raw = doc.GetMassProperties2(ref st);
            double[] mp = raw as double[];
            if (mp == null && raw is object[] oa) mp = Array.ConvertAll(oa, x => (double)x);
            return (mp != null && mp.Length >= 4) ? (double?)mp[3] : null;
        }

        // Volumes are ~1e-5 m^3; six decimals would round them to zero, so keep significant digits.
        private static double Sig6(double v)
        {
            return double.Parse(v.ToString("G6", System.Globalization.CultureInfo.InvariantCulture),
                System.Globalization.CultureInfo.InvariantCulture);
        }
    }
}
