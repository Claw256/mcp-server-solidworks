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

        // export_image — saves the ACTIVE document's graphics view as a .bmp (IModelDoc2.SaveBMP is the
        // documented path; SaveAs3 image formats are not). Changes the VIEW ORIENTATION only (never the
        // model), does not touch state_version. The adapter converts the BMP to PNG for the model.
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

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                string dir = System.IO.Path.GetDirectoryName(filePath);
                if (!string.IsNullOrEmpty(dir) && !System.IO.Directory.Exists(dir))
                    System.IO.Directory.CreateDirectory(dir);

                if (viewId > 0)
                    modelDoc.ShowNamedView2("", viewId);   // ViewId wins when both are given
                modelDoc.ViewZoomtofit2();

                if (!modelDoc.SaveBMP(filePath, width, height))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EXPORT_FAILED", "SaveBMP returned false. Ensure the path is writable and the document window is visible.");

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
                        Features = new List<string> { filePath },
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
