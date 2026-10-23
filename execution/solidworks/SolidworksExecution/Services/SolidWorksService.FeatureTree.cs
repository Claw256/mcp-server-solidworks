using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;

namespace SolidworksExecution.Services
{
    public partial class SolidWorksService
    {
        // Feature types that are system scaffolding rather than user-created features. Reuses the
        // analyze_model noise list and adds the origin, camera and assembly mate container.
        // Built on first use: static field initializers across partial-class files have no defined order.
        private static HashSet<string> _nonUserFeatureTypes;
        private static HashSet<string> NonUserFeatureTypes
        {
            get
            {
                return _nonUserFeatureTypes ?? (_nonUserFeatureTypes = new HashSet<string>(NoiseFeatureTypes)
                {
                    "OriginProfileFeature", "CameraFolder", "Camera", "MateGroup", "ReferenceFolder"
                });
            }
        }

        /// <summary>
        /// The ORDERED user-created features of a part/assembly, in feature-tree order: sketches, features and
        /// user reference geometry. Excludes the origin, the three default planes (the first three top-level
        /// RefPlane features), and the Annotations/Sensors/Material/Lights/Cameras/history/comment folders.
        /// Features grouped inside a user folder are included (one level, deduplicated by name), matching
        /// the analyze_model tree walk. Shared by feature_tree and truncate_feature_tree.
        /// </summary>
        private List<IFeature> GetUserFeatures(IModelDoc2 modelDoc)
        {
            var result = new List<IFeature>();
            var seen = new HashSet<string>();
            int defaultPlanesSeen = 0;
            var feat = modelDoc.FirstFeature() as IFeature;
            int guard = 0;
            while (feat != null && guard++ < 5000)
            {
                string type = feat.GetTypeName2() ?? "";
                bool isDefaultPlane = false;
                if (type == "RefPlane" && defaultPlanesSeen < 3) { defaultPlanesSeen++; isDefaultPlane = true; }

                if (!isDefaultPlane && !NonUserFeatureTypes.Contains(type) && seen.Add(feat.Name))
                    result.Add(feat);

                // Sub-features: only the contents of a folder (a sketch under its extrude is already listed
                // at top level, so name-dedup would skip it anyway).
                if (type.IndexOf("Folder", StringComparison.OrdinalIgnoreCase) >= 0 || type == "FtrFolder")
                {
                    var sub = feat.GetFirstSubFeature() as IFeature;
                    int subGuard = 0;
                    while (sub != null && subGuard++ < 2000)
                    {
                        string stype = sub.GetTypeName2() ?? "";
                        if (!NonUserFeatureTypes.Contains(stype) && seen.Add(sub.Name))
                            result.Add(sub);
                        sub = sub.GetNextSubFeature() as IFeature;
                    }
                }
                feat = feat.GetNextFeature() as IFeature;
            }
            return result;
        }

        private List<string> GetUserFeatureNames(IModelDoc2 modelDoc)
        {
            return GetUserFeatures(modelDoc).Select(f => f.Name).ToList();
        }

        private static string DocTypeName(IModelDoc2 modelDoc)
        {
            // swDocASSEMBLY = 2 (literal; ADR-018 keeps swconst enums out of runtime types).
            return modelDoc.GetType() == 2 ? "ASSEMBLY" : "PART";
        }

        // -----------------------------------------------------------------------
        // feature_tree — read-only: the ordered list of user-created features of the active document.
        // Like verify_state it checks but does NOT bump state_version.
        // -----------------------------------------------------------------------
        [Tool("feature_tree")]
        public ExecutionResponse FeatureTree(ToolRequest request)
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
                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var names = GetUserFeatureNames(modelDoc);
                return new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion(),
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion(),
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = DocTypeName(modelDoc),
                        ActiveSketch = null,
                        Features = names,
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = new JObject { ["feature_count"] = names.Count },
                    Error = null
                };
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

        // -----------------------------------------------------------------------
        // truncate_feature_tree — DESTRUCTIVE: keep the first keep_count user features (tree order) and delete
        // every later one, last first so children go before their parents, then rebuild once. The rollback bar
        // is moved to the end first. Stops at the first failure and reports it (remaining state in the message).
        // -----------------------------------------------------------------------
        [Tool("truncate_feature_tree")]
        public ExecutionResponse TruncateFeatureTree(ToolRequest request)
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
                JToken kc = p != null ? p["keep_count"] : null;
                if (kc == null || kc.Type == JTokenType.Null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "keep_count is required (integer >= 0: how many user features to keep).");
                if (kc.Type != JTokenType.Integer || kc.Value<int>() < 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "keep_count must be an integer >= 0.");
                int keep = kc.Value<int>();

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                // Deleting while the bar is rolled back would leave later features behind it; go to the end.
                try { modelDoc.FeatureManager.EditRollback((int)swMoveRollbackBarTo_e.swMoveRollbackBarToEnd, ""); } catch { }

                var names = GetUserFeatureNames(modelDoc);
                if (keep > names.Count)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"keep_count {keep} is larger than the {names.Count} user features in the tree: [{string.Join(", ", names)}].");

                int deleted = 0;
                for (int i = names.Count - 1; i >= keep; i--)
                {
                    string name = names[i];
                    var feature = FindFeatureByName(modelDoc, name, null);
                    if (feature == null) continue; // already gone with a parent; the final check proves the result

                    modelDoc.ClearSelection2(true);
                    if (!feature.Select2(false, 0))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SELECTION_FAILED",
                            $"truncate_feature_tree stopped: could not select '{name}' for deletion ({deleted} deleted so far). " +
                            $"Remaining: [{string.Join(", ", GetUserFeatureNames(modelDoc))}].");

                    // Children only (not Absorbed): a sketch absorbed by a kept extrude must survive; a sketch
                    // being removed is its own entry in the list and is deleted in its turn.
                    bool ok = modelDoc.Extension.DeleteSelection2((int)swDeleteSelectionOptions_e.swDelete_Children);
                    if (!ok)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SELECTION_FAILED",
                            $"truncate_feature_tree stopped: SolidWorks refused to delete '{name}' ({deleted} deleted so far). " +
                            $"Remaining: [{string.Join(", ", GetUserFeatureNames(modelDoc))}].");
                    deleted++;
                }

                modelDoc.EditRebuild3();

                var remaining = GetUserFeatureNames(modelDoc);
                if (remaining.Count != keep || !remaining.SequenceEqual(names.Take(keep)))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SELECTION_FAILED",
                        $"truncate_feature_tree finished with an unexpected tree: wanted {keep} features [{string.Join(", ", names.Take(keep))}], " +
                        $"found {remaining.Count} [{string.Join(", ", remaining)}].");

                ExecLog.Write($"truncate_feature_tree: kept {keep}, deleted {deleted}");

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
                        DocumentType = DocTypeName(modelDoc),
                        ActiveSketch = null,
                        Features = remaining,
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = new JObject { ["feature_count"] = remaining.Count, ["deleted"] = deleted },
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
