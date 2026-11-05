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
    // SolidWorksService partial: read-only model view capture (export_image) — lets the adapter hand
    // Claude a picture of the built part to compare with the source drawing.
    public partial class SolidWorksService
    {
        // swStandardViews_e: front 1, back 2, left 3, right 4, top 5, bottom 6, isometric 7.
        private static int StandardViewId(string view)
        {
            switch ((view ?? "isometric").ToLowerInvariant())
            {
                case "front": return (int)swStandardViews_e.swFrontView;
                case "back": return (int)swStandardViews_e.swBackView;
                case "left": return (int)swStandardViews_e.swLeftView;
                case "right": return (int)swStandardViews_e.swRightView;
                case "top": return (int)swStandardViews_e.swTopView;
                case "bottom": return (int)swStandardViews_e.swBottomView;
                case "isometric": case "iso": return (int)swStandardViews_e.swIsometricView;
                case "current": return 0;
                default: return -1;
            }
        }

        // Which MODEL axis the active view shows pointing screen-right / screen-up, and whether perspective is on —
        // read back from IModelView.Transform (model space -> view plane; screen origin top-left, +x right, +y
        // down) rather than assumed from the view name: the end-user may have redefined the standard views
        // (IModelDoc2.GetStandardViewRotation docs), and a perspective view is not a flat projection. The axes
        // are omitted unless the view is axis-aligned. Best effort: an empty object means "could not read".
        private JObject ReadViewFrame(IModelDoc2 modelDoc)
        {
            var frame = new JObject();
            try
            {
                var mv = modelDoc.ActiveView as IModelView;
                if (mv == null) return frame;
                frame["has_perspective"] = mv.HasPerspective();
                var mu = _solidWorks.GetMathUtility() as IMathUtility;
                var t = mv.Transform;
                if (mu == null || t == null) return frame;

                Func<double, double, double, double[]> toScreen = (x, y, z) =>
                {
                    var pt = mu.CreatePoint(new double[] { x, y, z }) as IMathPoint;
                    var mapped = pt != null ? pt.MultiplyTransform(t) as IMathPoint : null;
                    return mapped != null ? mapped.ArrayData as double[] : null;
                };
                double[] o = toScreen(0, 0, 0);
                double[][] ax = { toScreen(1, 0, 0), toScreen(0, 1, 0), toScreen(0, 0, 1) };
                if (o == null || o.Length < 2 || ax[0] == null || ax[1] == null || ax[2] == null) return frame;

                var dx = new double[3];
                var dy = new double[3];
                for (int i = 0; i < 3; i++) { dx[i] = ax[i][0] - o[0]; dy[i] = ax[i][1] - o[1]; }
                int r = 0, u = 0;
                for (int i = 1; i < 3; i++) if (Math.Abs(dx[i]) > Math.Abs(dx[r])) r = i;
                for (int i = 1; i < 3; i++) if (Math.Abs(dy[i]) > Math.Abs(dy[u])) u = i;
                bool aligned = r != u
                    && Math.Abs(dx[r]) > 0.98 * Math.Sqrt(dx[r] * dx[r] + dy[r] * dy[r])
                    && Math.Abs(dy[u]) > 0.98 * Math.Sqrt(dx[u] * dx[u] + dy[u] * dy[u]);
                frame["axis_aligned"] = aligned;
                if (aligned)
                {
                    string[] names = { "X", "Y", "Z" };
                    frame["right"] = (dx[r] >= 0 ? "+" : "-") + names[r];
                    frame["up"] = (dy[u] <= 0 ? "+" : "-") + names[u];   // screen y points DOWN
                }
            }
            catch { /* best effort: the capture itself must not fail because of this */ }
            return frame;
        }

        // swViewDisplayMode_e values export_image's `style` maps to (enum cast to int, ADR-018). "shaded" keeps the
        // edges drawn (the SolidWorks default look, easiest to read); "hidden_lines" shows hidden edges greyed.
        private static int StyleDisplayMode(string style)
        {
            switch ((style ?? "").ToLowerInvariant())
            {
                case "shaded": return (int)swViewDisplayMode_e.swViewDisplayMode_ShadedWithEdges;
                case "hidden_lines": return (int)swViewDisplayMode_e.swViewDisplayMode_HiddenLinesGrayed;
                case "wireframe": return (int)swViewDisplayMode_e.swViewDisplayMode_Wireframe;
                default: return -1;
            }
        }

        // What one capture asks for, resolved to live objects BEFORE anything in the document is changed.
        private sealed class ImageCaptureOptions
        {
            public int ViewId;
            public int DisplayMode = -1;                  // -1 = leave the display style alone
            public string SectionPlane;                   // null = no section view
            public double SectionOffsetM;
            public bool SectionReverse;
            public List<object> ZoomTargets = new List<object>();        // IFace2 / IEdge / IFeature
            public List<object> HighlightTargets = new List<object>();
        }

        // The view state a capture can disturb: orientation + translation + scale (what ShowNamedView2 and
        // ViewZoomtofit2 change) and the display style. Copied as plain arrays, so it survives the changes.
        private sealed class ViewSnapshot
        {
            public double[] Orientation;
            public double[] Translation;
            public double Scale;
            public int DisplayMode;
        }

        private ViewSnapshot ReadViewSnapshot(IModelDoc2 modelDoc)
        {
            try
            {
                var mv = modelDoc.ActiveView as IModelView;
                if (mv == null) return null;
                var o = mv.Orientation3 != null ? mv.Orientation3.ArrayData as double[] : null;
                var t = mv.Translation3 != null ? mv.Translation3.ArrayData as double[] : null;
                if (o == null || t == null) return null;
                return new ViewSnapshot { Orientation = o, Translation = t, Scale = mv.Scale2, DisplayMode = mv.DisplayMode };
            }
            catch { return null; }
        }

        private bool RestoreViewSnapshot(IModelDoc2 modelDoc, ViewSnapshot s, bool displayChanged)
        {
            try
            {
                var mv = modelDoc.ActiveView as IModelView;
                var mu = _solidWorks.GetMathUtility() as IMathUtility;
                if (mv == null || mu == null) return false;
                if (displayChanged) mv.DisplayMode = s.DisplayMode;
                mv.Orientation3 = mu.CreateTransform(s.Orientation) as MathTransform;   // orientation > scale > translation
                mv.Scale2 = s.Scale;
                mv.Translation3 = mu.CreateVector(s.Translation) as MathVector;
                modelDoc.GraphicsRedraw2();
                return true;
            }
            catch { return false; }
        }

        private sealed class SelectedItem
        {
            public object Obj;
            public int Mark;
        }

        // The user's current selection, so a capture that selects (highlight, zoom_to, section plane) can put it back.
        private List<SelectedItem> ReadSelection(IModelDoc2 modelDoc)
        {
            var list = new List<SelectedItem>();
            var selMgr = modelDoc.SelectionManager as ISelectionMgr;
            int count = selMgr != null ? selMgr.GetSelectedObjectCount2(-1) : 0;
            for (int i = 1; i <= count; i++)
            {
                object obj = selMgr.GetSelectedObject6(i, -1);
                if (obj != null) list.Add(new SelectedItem { Obj = obj, Mark = selMgr.GetSelectedObjectMark(i) });
            }
            return list;
        }

        // Replace the selection with `items`; returns how many could NOT be re-selected (only entities and features
        // are restorable here — e.g. a selected sketch segment or component is not).
        private int WriteSelection(IModelDoc2 modelDoc, List<SelectedItem> items)
        {
            modelDoc.ClearSelection2(true);
            var sd = (modelDoc.SelectionManager as ISelectionMgr)?.CreateSelectData();
            int failed = 0;
            foreach (var it in items)
            {
                bool ok = false;
                var ent = it.Obj as IEntity;
                if (ent != null && sd != null)
                {
                    sd.Mark = it.Mark;
                    ok = ent.Select4(true, sd);
                }
                else
                {
                    var feat = it.Obj as IFeature;
                    if (feat != null) ok = feat.Select2(true, it.Mark);
                }
                if (!ok) failed++;
            }
            return failed;
        }

        // Resolve zoom_to / highlight references to live objects. Face/edge indices are the analyze_model
        // (analysis_type 'faces' / 'edges') enumeration. Read-only: a bad reference fails the call before any state changes.
        private bool ResolveImageTargets(IModelDoc2 modelDoc, JObject zoomTo, JObject highlight,
            ImageCaptureOptions opt, out string code, out string msg)
        {
            code = null; msg = null;
            if (zoomTo == null && highlight == null) return true;
            var partDoc = modelDoc as IPartDoc;
            if (partDoc == null)
            {
                code = "WRONG_DOCUMENT_TYPE"; msg = "zoom_to / highlight require a part document.";
                return false;
            }
            List<IFace2> faces = null;
            List<IEdge> edges = null;
            try
            {
                string err = null;
                Action<List<object>, int> addFace = (into, i) =>
                {
                    if (faces == null) faces = FlattenFaces(partDoc);
                    if (i < 0 || i >= faces.Count) err = $"face index {i} out of range 0..{faces.Count - 1} (analyze_model analysis_type='faces' `i`).";
                    else into.Add(faces[i]);
                };
                Action<List<object>, int> addEdge = (into, i) =>
                {
                    if (edges == null) edges = FlattenEdges(partDoc);
                    if (i < 0 || i >= edges.Count) err = $"edge index {i} out of range 0..{edges.Count - 1} (analyze_model analysis_type='edges' `i`).";
                    else into.Add(edges[i]);
                };
                Action<List<object>, string> addFeature = (into, name) =>
                {
                    var names = new List<string>();
                    var feat = FindFeatureByName(modelDoc, name, names);
                    if (feat == null) err = $"No feature named '{name}'. Available: {string.Join(", ", names)}";
                    else into.Add(feat);
                };

                if (zoomTo != null)
                {
                    int given = 0;
                    foreach (var key in new[] { "face", "edge", "feature", "bbox_mm" }) if (zoomTo[key] != null) given++;
                    if (given != 1)
                    {
                        code = "INVALID_PARAMETER"; msg = "zoom_to needs exactly one of face, edge, feature, bbox_mm.";
                        return false;
                    }
                    if (zoomTo["face"] != null) addFace(opt.ZoomTargets, zoomTo.Value<int>("face"));
                    else if (zoomTo["edge"] != null) addEdge(opt.ZoomTargets, zoomTo.Value<int>("edge"));
                    else if (zoomTo["feature"] != null) addFeature(opt.ZoomTargets, zoomTo.Value<string>("feature"));
                    else
                    {
                        // [xmin,ymin,zmin,xmax,ymax,zmax] in mm: zoom to every face whose (approximate) box meets it.
                        var b = zoomTo["bbox_mm"] as JArray;
                        if (b == null || b.Count != 6)
                        {
                            code = "INVALID_PARAMETER"; msg = "zoom_to.bbox_mm needs [xmin,ymin,zmin,xmax,ymax,zmax] in mm.";
                            return false;
                        }
                        var lo = new double[3]; var hi = new double[3];
                        for (int i = 0; i < 3; i++)
                        {
                            lo[i] = Math.Min((double)b[i], (double)b[i + 3]) / 1000.0;
                            hi[i] = Math.Max((double)b[i], (double)b[i + 3]) / 1000.0;
                        }
                        if (faces == null) faces = FlattenFaces(partDoc);
                        foreach (var f in faces)
                        {
                            var fb = f.GetBox() as double[];
                            if (fb == null || fb.Length < 6) continue;
                            bool hit = true;
                            for (int i = 0; i < 3; i++)
                                if (fb[i + 3] < lo[i] || fb[i] > hi[i]) { hit = false; break; }
                            if (hit) opt.ZoomTargets.Add(f);
                        }
                        if (opt.ZoomTargets.Count == 0)
                        {
                            code = "INVALID_PARAMETER"; msg = "zoom_to.bbox_mm: no face of the part meets that box.";
                            return false;
                        }
                    }
                    if (err != null) { code = err.StartsWith("No feature") ? "FEATURE_NOT_FOUND" : "INVALID_PARAMETER"; msg = err; return false; }
                }

                if (highlight != null)
                {
                    foreach (var t in highlight["faces"] as JArray ?? new JArray()) addFace(opt.HighlightTargets, (int)t);
                    if (err == null)
                        foreach (var t in highlight["edges"] as JArray ?? new JArray()) addEdge(opt.HighlightTargets, (int)t);
                    if (err == null)
                        foreach (var t in highlight["features"] as JArray ?? new JArray()) addFeature(opt.HighlightTargets, (string)t);
                    if (err != null) { code = err.StartsWith("No feature") ? "FEATURE_NOT_FOUND" : "INVALID_PARAMETER"; msg = err; return false; }
                    if (opt.HighlightTargets.Count == 0)
                    {
                        code = "INVALID_PARAMETER"; msg = "highlight needs at least one of faces, edges, features.";
                        return false;
                    }
                }
                return true;
            }
            catch (Exception ex) when (ex is FormatException || ex is InvalidCastException || ex is OverflowException || ex is ArgumentException)
            {
                code = "INVALID_PARAMETER"; msg = "zoom_to / highlight malformed: " + ex.Message;
                return false;
            }
        }

        // The state-changing part of export_image: apply section / style / view / zoom / highlight, read the view
        // frame, SaveBMP. Returns an error (code/message) or null. The CALLER restores view, selection and section.
        private string ApplyAndSaveImage(IModelDoc2 modelDoc, ImageCaptureOptions opt, string filePath, int width, int height,
            out JObject frame, out string errCode, out bool sectionMade)
        {
            frame = new JObject(); errCode = null; sectionMade = false;
            if (opt.SectionPlane != null)
            {
                var mvm = modelDoc.ModelViewManager as IModelViewManager;
                if (mvm == null) { errCode = "EXPORT_FAILED"; return "Model view manager unavailable; section not applied."; }
                if (mvm.GetSectionViewData("") != null)
                { errCode = "EXPORT_FAILED"; return "A section view is already active in the document; clear it first (it would be replaced and not restored)."; }
                if (!SelectPlaneFlexible(modelDoc, opt.SectionPlane, false, 1))
                { errCode = "INVALID_PARAMETER"; return $"section.plane '{opt.SectionPlane}' not found (front, top, right or a reference plane name)."; }
                var plane = (modelDoc.SelectionManager as ISelectionMgr)?.GetSelectedObject6(1, -1);
                var sv = mvm.CreateSectionViewData() as ISectionViewData;
                if (plane == null || sv == null) { errCode = "EXPORT_FAILED"; return "Could not build the section view data."; }
                sv.FirstPlane = plane;
                sv.FirstOffset = opt.SectionOffsetM;
                sv.FirstReverseDirection = opt.SectionReverse;
                sv.ShowSectionCap = true;
                sv.GraphicsOnlySection = true;      // no model rebuild: a display-only section
                sv.Redraw = true;                   // CreateSectionView then validates the section bodies
                bool created = mvm.CreateSectionView((SectionViewData)sv);
                modelDoc.ClearSelection2(true);
                if (!created) { errCode = "EXPORT_FAILED"; return "SolidWorks could not create the section view (plane/offset outside the part?)."; }
                sectionMade = true;
            }

            if (opt.DisplayMode >= 0)
            {
                var mv = modelDoc.ActiveView as IModelView;
                if (mv != null) mv.DisplayMode = opt.DisplayMode;
            }
            if (opt.ViewId > 0)
                modelDoc.ShowNamedView2("", opt.ViewId);   // ViewId wins when both are given
            modelDoc.ViewZoomtofit2();
            if (opt.ZoomTargets.Count > 0)
            {
                if (SelectTargets(modelDoc, opt.ZoomTargets) > 0) modelDoc.ViewZoomToSelection();
                modelDoc.ClearSelection2(true);
            }
            if (opt.HighlightTargets.Count > 0)
                SelectTargets(modelDoc, opt.HighlightTargets);     // selection colour marks them in the capture
            frame = ReadViewFrame(modelDoc);

            if (!modelDoc.SaveBMP(filePath, width, height))
            { errCode = "EXPORT_FAILED"; return "SaveBMP returned false. Ensure the path is writable and the document window is visible."; }
            return null;
        }

        // Replace the selection with the given faces / edges / features; returns how many were selected.
        private int SelectTargets(IModelDoc2 modelDoc, List<object> targets)
        {
            modelDoc.ClearSelection2(true);
            var sd = (modelDoc.SelectionManager as ISelectionMgr)?.CreateSelectData();
            int n = 0;
            foreach (var t in targets)
            {
                var ent = t as IEntity;
                if (ent != null && sd != null) { if (ent.Select4(true, sd)) n++; }
                else
                {
                    var feat = t as IFeature;
                    if (feat != null && feat.Select2(true, 0)) n++;
                }
            }
            return n;
        }

        private static JObject ParseObjectParam(JObject p, string key)
        {
            var tok = p?[key];
            if (tok == null || tok.Type == JTokenType.Null) return null;
            if (tok.Type == JTokenType.String)
            {
                string s = tok.Value<string>();
                return string.IsNullOrWhiteSpace(s) ? null : JObject.Parse(s);   // ADR-022: structured params arrive as JSON strings
            }
            return tok as JObject ?? throw new FormatException(key + " must be a JSON object.");
        }

        // export_image — saves the ACTIVE document's graphics view as a .bmp (IModelDoc2.SaveBMP is the
        // documented path; SaveAs3 image formats are not). Optional `style` (display mode), `section` (a
        // graphics-only section view), `zoom_to` and `highlight` (selection colour) make the picture legible.
        // Everything it changes — view orientation/zoom, display style, selection, section — is put back in
        // `finally`; the model is never touched and state_version does not move. The adapter converts the BMP to PNG
        // for the model and draws the grid / face-edge index labels over it.
        [Tool("export_image")]
        public ExecutionResponse ExportImage(ToolRequest request)
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
                string view = p?.Value<string>("view") ?? "isometric";
                string filePath = p?.Value<string>("file_path");
                int width = p?.Value<int?>("width") ?? 1024;
                int height = p?.Value<int?>("height") ?? 768;
                string style = p?.Value<string>("style");

                if (string.IsNullOrEmpty(filePath) || !filePath.EndsWith(".bmp", StringComparison.OrdinalIgnoreCase))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "file_path is required and must end in .bmp (full output path).");
                int viewId = StandardViewId(view);
                if (viewId < 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", $"view '{view}' unknown. Use isometric, front, back, left, right, top, bottom or current.");
                if (width < 64 || height < 64 || width > 4096 || height > 4096)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "width/height must be between 64 and 4096 px.");

                var opt = new ImageCaptureOptions { ViewId = viewId };
                if (!string.IsNullOrEmpty(style))
                {
                    opt.DisplayMode = StyleDisplayMode(style);
                    if (opt.DisplayMode < 0)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", $"style '{style}' unknown. Use shaded, hidden_lines or wireframe.");
                }

                JObject section, zoomTo, highlight;
                try
                {
                    section = ParseObjectParam(p, "section");
                    zoomTo = ParseObjectParam(p, "zoom_to");
                    highlight = ParseObjectParam(p, "highlight");
                }
                catch (Exception ex) when (ex is Newtonsoft.Json.JsonException || ex is FormatException)
                {
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "section / zoom_to / highlight must be JSON objects: " + ex.Message);
                }
                if (section != null)
                {
                    opt.SectionPlane = section.Value<string>("plane");
                    if (string.IsNullOrWhiteSpace(opt.SectionPlane))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER", "section requires 'plane' (front, top, right or a reference plane name).");
                    opt.SectionOffsetM = (section.Value<double?>("offset_mm") ?? 0.0) / 1000.0;
                    opt.SectionReverse = section.Value<bool?>("reverse") ?? false;
                }

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (section != null && modelDoc is IDrawingDoc)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE", "section requires a part or assembly document.");

                string errCode, errMsg;
                if (!ResolveImageTargets(modelDoc, zoomTo, highlight, opt, out errCode, out errMsg))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), errCode, errMsg);

                string dir = System.IO.Path.GetDirectoryName(filePath);
                if (!string.IsNullOrEmpty(dir) && !System.IO.Directory.Exists(dir))
                    System.IO.Directory.CreateDirectory(dir);

                // Snapshot, change, capture, and ALWAYS put the view / selection / section back.
                var warnings = new List<string>();
                var viewBefore = ReadViewSnapshot(modelDoc);
                bool touchesSelection = opt.SectionPlane != null || opt.ZoomTargets.Count > 0 || opt.HighlightTargets.Count > 0;
                List<SelectedItem> selBefore = touchesSelection ? ReadSelection(modelDoc) : null;
                JObject frame = new JObject();
                bool sectionMade = false;
                try
                {
                    errMsg = ApplyAndSaveImage(modelDoc, opt, filePath, width, height, out frame, out errCode, out sectionMade);
                }
                finally
                {
                    if (sectionMade)
                    {
                        try { (modelDoc.ModelViewManager as IModelViewManager)?.RemoveSectionView(); }
                        catch { warnings.Add("section view could not be removed - remove it in SolidWorks"); }
                    }
                    if (selBefore != null && WriteSelection(modelDoc, selBefore) > 0)
                        warnings.Add("part of the original selection could not be restored");
                    if (viewBefore != null && !RestoreViewSnapshot(modelDoc, viewBefore, opt.DisplayMode >= 0))
                        warnings.Add("the original view orientation/zoom could not be restored");
                }
                if (errMsg != null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), errCode, errMsg);

                var features = new List<string> { filePath, "view_frame=" + frame.ToString(Newtonsoft.Json.Formatting.None) };
                if (warnings.Count > 0)
                    features.Add("capture_warnings=" + new JArray(warnings).ToString(Newtonsoft.Json.Formatting.None));
                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion(),
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion(),
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = modelDoc is IDrawingDoc ? "DRAWING" : "PART",
                        ActiveSketch = null,
                        Features = features,
                        Dimensions = new List<string>()
                    },
                    Error = null
                };
                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), "UNEXPECTED_ERROR", ex.Message);
            }
        }
    }
}
