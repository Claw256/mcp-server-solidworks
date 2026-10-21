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

        // ---- modal-dialog guard ----------------------------------------------------------------------------
        // A rebuild warning or a feature error normally opens a MODAL dialog, which freezes the STA thread
        // (and so every request) until someone clicks it. The API documents the remedy for exactly this:
        // read swShowErrorsEveryRebuild, set it false around the rebuild, restore it afterwards
        // (ISldWorks::GetUserPreferenceToggle / IModelDoc2::ShowFeatureErrorDialog). The failure is not
        // lost: the response's `diagnostics` reports new feature errors. Scoped to ONE tool call and always
        // restored, so the user's own SolidWorks settings are never left changed.
        // SOLIDPILOT_SUPPRESS_DIALOGS=0 turns it off (the user then sees the dialogs, and a hang is possible).
        public static bool DialogSuppressionEnabled
        {
            get
            {
                var v = System.Environment.GetEnvironmentVariable("SOLIDPILOT_SUPPRESS_DIALOGS");
                return v == null || (v.Trim() != "0" && !v.Trim().Equals("false", StringComparison.OrdinalIgnoreCase));
            }
        }

        private static readonly swUserPreferenceToggle_e[] DialogToggles =
        {
            swUserPreferenceToggle_e.swShowErrorsEveryRebuild,      // rebuild-warning dialogs
            swUserPreferenceToggle_e.swSketchPromptToCloseSketch,   // "the sketch is open, use contour?" prompts
        };

        /// <summary>Suppress feature/rebuild dialogs until disposed. Never throws; a no-op when disabled.</summary>
        public IDisposable SuppressDialogs()
        {
            if (!DialogSuppressionEnabled || _solidWorks == null) return null;
            return new DialogScope(_solidWorks, _solidWorks.IActiveDoc2 as IModelDoc2);
        }

        private sealed class DialogScope : IDisposable
        {
            private readonly ISldWorks _app;
            private readonly IModelDoc2 _doc;
            private readonly List<KeyValuePair<swUserPreferenceToggle_e, bool>> _saved =
                new List<KeyValuePair<swUserPreferenceToggle_e, bool>>();
            private bool? _savedFeatureDialog;

            public DialogScope(ISldWorks app, IModelDoc2 doc)
            {
                _app = app; _doc = doc;
                foreach (var t in DialogToggles)
                {
                    try
                    {
                        bool prev = _app.GetUserPreferenceToggle((int)t);
                        _saved.Add(new KeyValuePair<swUserPreferenceToggle_e, bool>(t, prev));
                        _app.SetUserPreferenceToggle((int)t, false);
                    }
                    catch { /* best effort: a toggle that cannot be set is simply left alone */ }
                }
                try
                {
                    if (_doc != null) { _savedFeatureDialog = _doc.ShowFeatureErrorDialog; _doc.ShowFeatureErrorDialog = false; }
                }
                catch { _savedFeatureDialog = null; }
            }

            public void Dispose()
            {
                foreach (var kv in _saved)
                {
                    try { _app.SetUserPreferenceToggle((int)kv.Key, kv.Value); } catch { }
                }
                try
                {
                    // the active document may have changed during the tool (e.g. open_document): restore on the one we touched
                    if (_doc != null && _savedFeatureDialog.HasValue) _doc.ShowFeatureErrorDialog = _savedFeatureDialog.Value;
                }
                catch { }
            }
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
