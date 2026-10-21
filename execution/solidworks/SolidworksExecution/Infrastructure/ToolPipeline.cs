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
        // Operation ids currently queued or running. A retry that re-sends the SAME id while the first call is
        // still in flight (typically after a timeout) would otherwise queue behind it and build twice, because
        // nothing is registered with the guard until the first call finishes.
        private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> InFlight =
            new System.Collections.Concurrent.ConcurrentDictionary<string, byte>();

        public static bool TryBegin(string operationId) { return InFlight.TryAdd(operationId, 0); }

        public static void End(string operationId) { byte b; InFlight.TryRemove(operationId, out b); }

        /// <returns>null when the tool name is unknown (caller emits the BadRequest).</returns>
        public static ExecutionResponse Run(ToolRequest request, SolidWorksService service, IOperationGuard guard)
        {
            try { return RunCore(request, service, guard); }
            finally { End(request.OperationId); }       // idempotent: also covers ids the controller registered
        }

        private static ExecutionResponse RunCore(ToolRequest request, SolidWorksService service, IOperationGuard guard)
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
