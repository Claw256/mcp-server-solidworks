using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;

namespace SolidworksExecution.Services
{
    // SolidWorksService partial: analyze_model(overview) - ONE read of how the part is modelled - plus the shared
    // mass-properties / bbox / configuration readers it (and the mass_properties and bbox modes) use. Read-only.
    public partial class SolidWorksService
    {
        // Sketch/feature reads that fail are omitted, never fatal: the overview is a digest, not a transaction.
        private const int OverviewMaxConfigs = 12;      // per-feature, per-configuration suppression only up to here
        private const int OverviewMaxCustomProps = 40;

        // IModelDocExtension.CreateMassProperty2 -> IMassProperty2 (SI: m, m^2, m^3, kg, kg/m^3, kg*m^2).
        // Returns null when the object cannot be built (no solid body, surface-only part, API refusal) or when the
        // user has something SELECTED - CreateMassProperty2 evaluates the pre-selected bodies/components, so a stray
        // selection would silently narrow the answer. Callers then fall back to the legacy array (whole document).
        private static JObject ReadMassProperty2(IModelDoc2 doc)
        {
            try
            {
                var selMgr = doc.SelectionManager as ISelectionMgr;
                if (selMgr != null && selMgr.GetSelectedObjectCount2(-1) > 0) return null;
                var mp = doc.Extension.CreateMassProperty2() as IMassProperty2;
                if (mp == null) return null;
                mp.UseSystemUnits = true;                   // the default; explicit so a doc setting can never leak in
                var j = new JObject();
                j["volume"] = mp.Volume;
                j["surface_area"] = mp.SurfaceArea;
                var com = mp.CenterOfMass as double[];
                if (com == null || com.Length < 3) return null;
                j["cx"] = com[0]; j["cy"] = com[1]; j["cz"] = com[2];
                j["mass"] = mp.Mass;
                j["density"] = mp.Density;
                var pm = mp.PrincipalMomentsOfInertia as double[];
                if (pm != null && pm.Length >= 3) j["pmoi"] = new JArray { pm[0], pm[1], pm[2] };
                return j;
            }
            catch { return null; }
        }

        // The pre-existing whole-document read, kept as the fallback so mass_properties keeps working where
        // CreateMassProperty2 returns null. [0..2]=CG, [3]=volume, [4]=surface area (the rest is undocumented).
        private static JObject ReadMassLegacy(IModelDoc2 doc)
        {
            int massStatus = 0;
            object rawProps = doc.GetMassProperties2(ref massStatus);
            double[] props = rawProps as double[];
            if (props == null && rawProps is object[] objArr)
                props = Array.ConvertAll(objArr, o => (double)o);
            if (props == null || props.Length < 5) return null;
            var j = new JObject();
            j["volume"] = props[3];
            j["surface_area"] = props[4];
            j["cx"] = props[0]; j["cy"] = props[1]; j["cz"] = props[2];
            return j;
        }

        // volume/surface_area/cx/cy/cz always (callers parse them); mass/density/pmoi only from IMassProperty2.
        private static JObject ReadMassProps(IModelDoc2 doc)
        {
            return ReadMassProperty2(doc) ?? ReadMassLegacy(doc);
        }

        // EXACT axis-aligned extents of all solid bodies via IBody2.GetExtremePoint (IPartDoc.GetPartBox and
        // IBody2.GetBodyBox are documented as approximate, so they are NOT used). Meters, model axes; null = no body.
        private static JObject ReadBBox(IPartDoc partDoc)
        {
            object[] bodies = partDoc.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
            if (bodies == null || bodies.Length == 0) return null;
            double[] lo = { double.MaxValue, double.MaxValue, double.MaxValue };
            double[] hi = { double.MinValue, double.MinValue, double.MinValue };
            double[][] dirs = { new[] { 1.0, 0, 0 }, new[] { 0, 1.0, 0 }, new[] { 0, 0, 1.0 } };
            foreach (var b in bodies)
            {
                var body = b as IBody2;
                if (body == null) continue;
                for (int ax = 0; ax < 3; ax++)
                {
                    foreach (double sign in new[] { 1.0, -1.0 })
                    {
                        double ox, oy, oz;
                        if (!body.GetExtremePoint(dirs[ax][0] * sign, dirs[ax][1] * sign,
                                                  dirs[ax][2] * sign, out ox, out oy, out oz))
                            continue;
                        double v = ax == 0 ? ox : (ax == 1 ? oy : oz);
                        if (v < lo[ax]) lo[ax] = v;
                        if (v > hi[ax]) hi[ax] = v;
                    }
                }
            }
            if (lo[0] == double.MaxValue) return null;
            var bb = new JObject();
            bb["min"] = new JArray(Math.Round(lo[0], 6), Math.Round(lo[1], 6), Math.Round(lo[2], 6));
            bb["max"] = new JArray(Math.Round(hi[0], 6), Math.Round(hi[1], 6), Math.Round(hi[2], 6));
            bb["size"] = new JArray(Math.Round(hi[0] - lo[0], 6), Math.Round(hi[1] - lo[1], 6),
                                    Math.Round(hi[2] - lo[2], 6));
            return bb;
        }

        // PART / ASSEMBLY / DRAWING from IModelDoc2.GetType() (swDocumentTypes_e as ints: 1, 2, 3; ADR-018).
        private static string DocTypeNameOf(IModelDoc2 doc)
        {
            int t = doc.GetType();
            return t == 3 ? "DRAWING" : (t == 2 ? "ASSEMBLY" : "PART");
        }

        private static List<string> ReadConfigurationNames(IModelDoc2 doc)
        {
            var list = new List<string>();
            try
            {
                var raw = doc.GetConfigurationNames() as Array;
                if (raw != null)
                    foreach (var o in raw) { var s = o as string; if (!string.IsNullOrEmpty(s)) list.Add(s); }
            }
            catch { }
            return list;
        }

        // File-level custom properties ("" = not configuration-specific): name -> evaluated value.
        private static JObject ReadCustomProperties(IModelDoc2 doc)
        {
            var props = new JObject();
            try
            {
                var mgr = doc.Extension.get_CustomPropertyManager("");
                var names = mgr?.GetNames() as Array;
                if (names == null) return props;
                foreach (var o in names)
                {
                    var n = o as string;
                    if (string.IsNullOrEmpty(n) || props.Count >= OverviewMaxCustomProps) continue;
                    string val, resolved; bool wasResolved, link;
                    try
                    {
                        mgr.Get6(n, false, out val, out resolved, out wasResolved, out link);
                        props[n] = string.IsNullOrEmpty(resolved) ? val : resolved;
                    }
                    catch { }
                }
            }
            catch { }
            return props;
        }

        // The one-read digest. Shapes (SI: meters / radians / kg):
        //   {document:{title,type}, feature_count, features:[{name,type,suppressed?,suppressed_in?:[cfg],
        //    error?:{code,error,warning}, dimensions?:[{name,value_si}], in_folder?}], rollback_at_end,
        //    rolled_back_from?, equations:[...], bodies:{count,faces,edges}, bbox:{min,max,size},
        //    mass:{volume_m3,surface_area_m2,cg:[x,y,z],mass_kg?,density_kg_m3?,principal_moments_kgm2?},
        //    configs:{names,active,per_feature_suppression}, custom_properties:{...}}
        private JObject BuildOverview(IModelDoc2 modelDoc, IPartDoc partDoc)
        {
            var root = new JObject();
            root["document"] = new JObject { ["title"] = modelDoc.GetTitle(), ["type"] = DocTypeName(modelDoc) };

            // Configurations first: the feature walk uses the names for per-configuration suppression.
            var cfgNames = ReadConfigurationNames(modelDoc);
            var cfg = new JObject();
            cfg["names"] = new JArray(cfgNames);
            try { cfg["active"] = (modelDoc.ConfigurationManager.ActiveConfiguration as IConfiguration)?.Name; } catch { }
            bool perFeatureCfg = cfgNames.Count > 1 && cfgNames.Count <= OverviewMaxConfigs;
            cfg["per_feature_suppression"] = perFeatureCfg;
            var cfgArg = perFeatureCfg ? cfgNames.ToArray() : null;

            var featArr = new JArray();
            var seen = new HashSet<string>();
            var seenDims = new HashSet<string>();
            string rolledFrom = null;
            var feat = modelDoc.FirstFeature() as IFeature;
            int guard = 0;
            while (feat != null && guard++ < 5000)
            {
                AddOverviewFeature(feat, modelDoc, seen, seenDims, cfgArg, cfgNames, featArr, null, ref rolledFrom);
                var sub = feat.GetFirstSubFeature() as IFeature;
                int subGuard = 0;
                while (sub != null && subGuard++ < 2000)
                {
                    AddOverviewFeature(sub, modelDoc, seen, seenDims, cfgArg, cfgNames, featArr, feat.Name, ref rolledFrom);
                    sub = sub.GetNextSubFeature() as IFeature;
                }
                feat = feat.GetNextFeature() as IFeature;
            }
            root["feature_count"] = featArr.Count;
            root["features"] = featArr;
            // Rollback bar: any feature behind the bar reports IsRolledBack. A read tool never moves the bar,
            // so this only REPORTS it - an agent that finds it mid-tree should not build on a stale model.
            root["rollback_at_end"] = rolledFrom == null;
            if (rolledFrom != null) root["rolled_back_from"] = rolledFrom;
            root["equations"] = ReadEquations(modelDoc);

            object[] bodies = partDoc.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
            int faces = 0, edges = 0, bodyCount = 0;
            if (bodies != null)
                foreach (var b in bodies)
                {
                    var body = b as IBody2;
                    if (body == null) continue;
                    bodyCount++;
                    faces += body.GetFaceCount();
                    edges += body.GetEdgeCount();
                }
            root["bodies"] = new JObject { ["count"] = bodyCount, ["faces"] = faces, ["edges"] = edges };
            try { var bb = ReadBBox(partDoc); if (bb != null) root["bbox"] = bb; } catch { }

            var mp = ReadMassProps(modelDoc);
            if (mp != null)
            {
                var mj = new JObject();
                mj["volume_m3"] = Sig6(mp.Value<double>("volume"));
                mj["surface_area_m2"] = Sig6(mp.Value<double>("surface_area"));
                mj["cg"] = new JArray(R6(mp.Value<double>("cx")), R6(mp.Value<double>("cy")), R6(mp.Value<double>("cz")));
                if (mp["mass"] != null) mj["mass_kg"] = Sig6(mp.Value<double>("mass"));
                if (mp["density"] != null) mj["density_kg_m3"] = Sig6(mp.Value<double>("density"));
                var pm = mp["pmoi"] as JArray;
                if (pm != null) mj["principal_moments_kgm2"] = new JArray(Sig6(pm[0].Value<double>()), Sig6(pm[1].Value<double>()), Sig6(pm[2].Value<double>()));
                root["mass"] = mj;
            }
            root["configs"] = cfg;
            var cp = ReadCustomProperties(modelDoc);
            if (cp.Count > 0) root["custom_properties"] = cp;
            return root;
        }

        private void AddOverviewFeature(IFeature feat, IModelDoc2 modelDoc, HashSet<string> seen, HashSet<string> seenDims,
            string[] cfgArg, List<string> cfgNames, JArray featArr, string folder, ref string rolledFrom)
        {
            string type = feat.GetTypeName2() ?? "";
            if (NoiseFeatureTypes.Contains(type) || !seen.Add(feat.Name)) return;

            var fj = new JObject();
            fj["name"] = feat.Name;
            fj["type"] = type;
            if (folder != null) fj["in_folder"] = folder;
            try { if (feat.IsSuppressed()) fj["suppressed"] = true; } catch { }
            try { if (rolledFrom == null && feat.IsRolledBack()) rolledFrom = feat.Name; } catch { }

            // Per-configuration suppression: one IsSuppressed2(swSpecifyConfiguration=3, names) call per feature
            // returns a bool per configuration; only the configurations where it IS suppressed are listed.
            if (cfgArg != null)
            {
                try
                {
                    var flags = feat.IsSuppressed2(3, cfgArg) as Array;
                    if (flags != null)
                    {
                        var off = new JArray();
                        int i = 0;
                        foreach (var f in flags) { if (i < cfgNames.Count && f is bool && (bool)f) off.Add(cfgNames[i]); i++; }
                        if (off.Count > 0) fj["suppressed_in"] = off;
                    }
                }
                catch { }
            }

            var err = ReadFeatureError(feat);
            if (err != null)
            {
                err.Remove("feature"); err.Remove("type"); err.Remove("key");
                fj["error"] = err;
            }

            try
            {
                var dims = ReadDisplayDimensions(feat, seenDims);
                if (dims.Count > 0) fj["dimensions"] = dims;
            }
            catch { }
            featArr.Add(fj);
        }
    }
}
