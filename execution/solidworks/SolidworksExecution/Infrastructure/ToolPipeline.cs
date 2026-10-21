using System;
using System.Diagnostics;
using SolidworksExecution.Models;
using SolidworksExecution.Services;

namespace SolidworksExecution.Infrastructure
{
    /// <summary>
    /// The one place every tool call passes through: look up the tool, replay duplicates, reject a
    /// stale state_version, run it, and log the timing. Service methods still carry their own
    /// (now redundant) guards; those are removed incrementally, never needed for correctness.
    /// Must run on the STA thread (the handlers touch COM).
    /// </summary>
    public static class ToolPipeline
    {
        /// <returns>null when the tool name is unknown (caller emits the BadRequest).</returns>
        public static ExecutionResponse Run(ToolRequest request, SolidWorksService service, IOperationGuard guard)
        {
            ToolEntry entry;
            if (!ToolRegistry.TryGet(request.Tool, out entry))
                return null;

            if (guard.IsDuplicate(request.OperationId))
                return guard.GetDuplicate(request.OperationId);

            if (entry.CheckStateVersion && !guard.IsStateVersionValid(request.StateVersion))
                return ResponseBuilder.Failed(request.OperationId, guard.GetCurrentStateVersion(),
                    ErrorCodes.InvalidStateVersion, "Incoming state_version does not match current state.");

            var baseline = entry.Diagnose ? service.CaptureDiagnosticsBaseline() : null;
            var sw = Stopwatch.StartNew();
            ExecutionResponse response;
            // Feature tools run with modal rebuild/feature dialogs suppressed (restored afterwards), so a
            // failing feature returns its error in `diagnostics` instead of freezing the STA thread.
            using (entry.Diagnose ? service.SuppressDialogs() : null)
                response = entry.Invoke(service, request);
            if (entry.Diagnose && response != null && response.Status == "COMPLETED")
            {
                var diag = service.CollectDiagnostics(baseline);
                if (diag != null)
                {
                    response.Diagnostics = diag;
                    // The tool ran, but SolidWorks reports a feature it JUST made in error: not verified.
                    var fresh = diag["new_feature_errors"] as Newtonsoft.Json.Linq.JArray;
                    if (fresh != null && fresh.Count > 0) response.Verified = false;
                }
            }
            ExecLog.Write($"   {request.Tool} op={request.OperationId} took {sw.ElapsedMilliseconds} ms");
            return response;
        }
    }
}
