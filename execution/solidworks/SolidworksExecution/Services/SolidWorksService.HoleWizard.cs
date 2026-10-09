using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;


namespace SolidworksExecution.Services
{
    // SolidWorksService partial: Hole Wizard (standard counterbore / countersink / straight / tapped holes).
    public partial class SolidWorksService
    {
        // StandardIndex (swWzdHoleStandards_e) by tool name.
        private static int? HoleWizardStandardIndex(string standard)
        {
            switch ((standard ?? "").ToLowerInvariant())
            {
                case "ansi_inch":   return (int)swWzdHoleStandards_e.swStandardAnsiInch;
                case "ansi_metric": return (int)swWzdHoleStandards_e.swStandardAnsiMetric;
                case "iso":         return (int)swWzdHoleStandards_e.swStandardISO;
                case "din":         return (int)swWzdHoleStandards_e.swStandardDIN;
                case "jis":         return (int)swWzdHoleStandards_e.swStandardJIS;
                default:            return null;
            }
        }

        // Default FastenerTypeIndex (swWzdHoleStandardFastenerTypes_e VALUES, from the API docs) per
        // standard + hole type. The FastenerTypeIndex MUST belong to the chosen standard and hole type or
        // SolidWorks rejects the call (HOLE_WIZARD_FAILED) — so a wrong standard/fastener combo fails.
        //   hole        = screw-clearance hole      tap         = tapped hole
        //   counterbore = socket-head-cap-screw cbore   countersink = flat-head (CTSK) countersink
        private static int? HoleWizardDefaultFastener(int standardIndex, string holeType)
        {
            switch (standardIndex)
            {
                case 0: // ANSI inch
                    return holeType == "hole" ? 22 : holeType == "tap" ? 27 : holeType == "counterbore" ? 9 : 13;
                case 1: // ANSI metric
                    return holeType == "hole" ? 40 : holeType == "tap" ? 43 : holeType == "counterbore" ? 33 : 35;
                case 8: // ISO
                    return holeType == "hole" ? 144 : holeType == "tap" ? 147 : holeType == "counterbore" ? 139 : 140;
                case 4: // DIN
                    return holeType == "hole" ? 73 : holeType == "tap" ? 76 : holeType == "counterbore" ? 67 : 68;
                case 9: // JIS
                    return holeType == "hole" ? 161 : holeType == "tap" ? 164 : holeType == "counterbore" ? 156 : 158;
                default:
                    return null;
            }
        }

        // The i-th solid-body face (same enumeration as analyze_model(faces) / create_sketch face_index).
        private IFace2 HoleWizardFaceByIndex(IModelDoc2 modelDoc, int faceIndex)
        {
            var partDoc = modelDoc as IPartDoc;
            var allFaces = new List<IFace2>();
            object[] bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
            if (bodies != null)
                foreach (var b in bodies)
                {
                    var body = b as IBody2;
                    if (body == null) continue;
                    object[] fs = body.GetFaces() as object[];
                    if (fs == null) continue;
                    foreach (var f in fs) { var fa = f as IFace2; if (fa != null) allFaces.Add(fa); }
                }
            if (faceIndex < 0 || faceIndex >= allFaces.Count) return null;
            return allFaces[faceIndex];
        }

        // Places a standard Hole Wizard hole (IFeatureManager.HoleWizard5) at one or more points on a planar face.
        // Selection method (per the HoleWizard5 remarks): "To add a hole at one or more locations, call
        // IModelDocExtension.SelectByRay with Mark = 0 for each location" — so every point is picked as a
        // ray against the target face (ray starts 2 mm above the face along its normal, runs into it); the
        // hit face + point defines the hole centre. face_index / face_x/y/z is used only to read the face
        // normal (the face must be planar) so the ray direction is known.
        public ExecutionResponse HoleWizard(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;

                string holeType = (p?.Value<string>("hole_type") ?? "").ToLowerInvariant();
                int genericType;
                switch (holeType)
                {
                    case "counterbore": genericType = (int)swWzdGeneralHoleTypes_e.swWzdCounterBore; break;
                    case "countersink": genericType = (int)swWzdGeneralHoleTypes_e.swWzdCounterSink; break;
                    case "hole":        genericType = (int)swWzdGeneralHoleTypes_e.swWzdHole; break;
                    case "tap":         genericType = (int)swWzdGeneralHoleTypes_e.swWzdTap; break;
                    default:
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", "hole_type must be one of: counterbore, countersink, hole, tap.");
                }

                int? standardIdx = HoleWizardStandardIndex(p?.Value<string>("standard"));
                if (standardIdx == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "standard must be one of: ansi_inch, ansi_metric, iso, din, jis.");

                // fastener_type: an int (swWzdHoleStandardFastenerTypes_e value) wins; otherwise the small
                // default table for the standard + hole_type is used. It must match standard and hole type.
                int? fastener = null;
                JToken ft = p?["fastener_type"];
                if (ft != null && ft.Type == JTokenType.Integer) fastener = ft.Value<int>();
                else if (ft != null && ft.Type == JTokenType.String)
                {
                    int parsedFt;
                    string fts = ft.Value<string>();
                    if (int.TryParse(fts, out parsedFt)) fastener = parsedFt;
                    else if (!string.IsNullOrEmpty(fts) && fts != "default")
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", "fastener_type must be an integer swWzdHoleStandardFastenerTypes_e value (or omitted for the default of the standard + hole_type).");
                }
                if (fastener == null) fastener = HoleWizardDefaultFastener(standardIdx.Value, holeType);
                if (fastener == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "No default fastener for this standard; pass fastener_type explicitly.");

                string size = p?.Value<string>("size");
                if (string.IsNullOrEmpty(size))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "size is required (e.g. 'M6'; it must be valid for the standard + fastener).");

                string endName = (p?.Value<string>("end_condition") ?? "blind").ToLowerInvariant();
                if (endName != "blind" && endName != "through_all")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "end_condition must be 'blind' or 'through_all'.");
                short endType = endName == "through_all"
                    ? (short)swEndConditions_e.swEndCondThroughAll
                    : (short)swEndConditions_e.swEndCondBlind;

                double diameter = p?.Value<double?>("diameter") ?? 0.0;
                double depth = p?.Value<double?>("depth") ?? 0.0;
                if (endName == "blind" && depth <= 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "depth (> 0 meters) is required for a blind hole. Pass end_condition='through_all' otherwise.");

                // points: JSON array of [x,y,z] model-space hole centres (a real array, or a JSON string).
                JArray pts = null;
                JToken ptsTok = p?["points"];
                if (ptsTok is JArray) pts = (JArray)ptsTok;
                else if (ptsTok != null && ptsTok.Type == JTokenType.String)
                {
                    try { pts = JArray.Parse(ptsTok.Value<string>()); }
                    catch { pts = null; }
                }
                if (pts == null || pts.Count == 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "points is required: a JSON array of [x,y,z] hole centres lying ON the target planar face, e.g. [[0.01,0.05,0]].");
                var centres = new List<double[]>();
                foreach (var pt in pts)
                {
                    var arr = pt as JArray;
                    if (arr == null || arr.Count != 3)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", "Each entry of points must be [x,y,z] in meters.");
                    centres.Add(new[] { arr[0].Value<double>(), arr[1].Value<double>(), arr[2].Value<double>() });
                }

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (!(modelDoc is IPartDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE", "hole_wizard needs an active PART document.");

                // Exit any active sketch (like extrude_feature) so the selections below are model selections.
                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                // Resolve the target face -> its unit normal (planar faces only).
                IFace2 face = null;
                int? faceIndex = p?.Value<int?>("face_index");
                if (faceIndex != null)
                {
                    face = HoleWizardFaceByIndex(modelDoc, faceIndex.Value);
                    if (face == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND", $"Face index {faceIndex.Value} out of range. Call analyze_model(faces) for valid indices.");
                }
                else if (p != null && p["face_x"] != null && p["face_y"] != null && p["face_z"] != null)
                {
                    double fx = p.Value<double>("face_x"), fy = p.Value<double>("face_y"), fz = p.Value<double>("face_z");
                    modelDoc.ClearSelection2(true);
                    bool faceSel = modelDoc.Extension.SelectByID2("", "FACE", fx, fy, fz, false, 0, null, 0);
                    if (faceSel)
                        face = (modelDoc.SelectionManager as ISelectionMgr)?.GetSelectedObject6(1, -1) as IFace2;
                    modelDoc.ClearSelection2(true);
                    if (face == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND", $"No face found at ({fx}, {fy}, {fz}). Provide a point ON the planar face or use face_index.");
                }
                else
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "Provide face_index (from analyze_model(faces)) or face_x/face_y/face_z (a point ON the planar face).");

                double[] n = face.Normal as double[];
                if (n == null || n.Length < 3 || (n[0] == 0 && n[1] == 0 && n[2] == 0))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "The target face is not planar (Hole Wizard holes need a planar face).");

                // Select each hole centre as a ray against the face (Mark = 0). The sign of IFace2.Normal
                // relative to the outward direction can depend on the face sense, so a miss is retried
                // once with the ray flipped before failing.
                modelDoc.ClearSelection2(true);
                for (int i = 0; i < centres.Count; i++)
                {
                    var c = centres[i];
                    bool ok = false;
                    for (int attempt = 0; attempt < 2 && !ok; attempt++)
                    {
                        double s = attempt == 0 ? 1.0 : -1.0;
                        ok = modelDoc.Extension.SelectByRay(
                            c[0] + s * n[0] * 0.002, c[1] + s * n[1] * 0.002, c[2] + s * n[2] * 0.002,
                            -s * n[0], -s * n[1], -s * n[2],
                            0.0001, (int)swSelectType_e.swSelFACES, i > 0, 0, 0);
                    }
                    if (!ok)
                    {
                        modelDoc.ClearSelection2(true);
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "POINT_NOT_ON_FACE", $"points[{i}] ({c[0]}, {c[1]}, {c[2]}) did not hit a face. Each point must lie ON the target planar face.");
                    }
                }

                // Value1..Value12 depend on the hole type (see HoleWizard5 remarks); -1 = ignored.
                double[] v = new double[12];
                for (int k = 0; k < 12; k++) v[k] = -1;
                double cbDia = p?.Value<double?>("cbore_diameter") ?? -1;
                double cbDepth = p?.Value<double?>("cbore_depth") ?? -1;
                double csDia = p?.Value<double?>("csink_diameter") ?? -1;
                // csink_angle arrives in DEGREES (model-facing convention); HoleWizard5 takes radians.
                double? csAngDeg = p?.Value<double?>("csink_angle");
                double csAng = csAngDeg != null ? csAngDeg.Value * Math.PI / 180.0 : -1;
                double length = 0.0;
                switch (genericType)
                {
                    case (int)swWzdGeneralHoleTypes_e.swWzdCounterBore:
                        v[0] = cbDia; v[1] = cbDepth;             // counterbore diameter / depth
                        break;
                    case (int)swWzdGeneralHoleTypes_e.swWzdCounterSink:
                        v[0] = csDia; v[1] = csAng;               // near countersink diameter / angle
                        v[2] = 0; v[8] = 1;                       // head clearance, head clearance type (as in the API example)
                        break;
                    case (int)swWzdGeneralHoleTypes_e.swWzdTap:
                        length = -1;                              // recorder quirk: -1 at the Length slot for straight taps
                        double? threadDepth = p?.Value<double?>("thread_depth");
                        v[0] = threadDepth ?? -1;                 // tap thread depth
                        break;
                    default:                                      // plain hole: Value1..7 left at -1
                        break;
                }

                bool reverse = p?.Value<bool?>("reverse") ?? false;

                IFeature feature = modelDoc.FeatureManager.HoleWizard5(
                    genericType, standardIdx.Value, fastener.Value, size, endType,
                    diameter, depth, length,
                    v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11],
                    "", reverse, true, true, true, true, false) as IFeature;

                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "HOLE_WIZARD_FAILED",
                        $"HoleWizard5 returned null. Check that standard '{p?.Value<string>("standard")}', fastener_type {fastener.Value} and size '{size}' are a valid combination for hole_type '{holeType}', and that the points lie on a planar face of a solid body.");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { feature.Name },
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = BuildBodySummary(modelDoc),
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }
    }
}
