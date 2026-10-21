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

            var sw = Stopwatch.StartNew();
            var response = entry.Invoke(service, request);
            ExecLog.Write($"   {request.Tool} op={request.OperationId} took {sw.ElapsedMilliseconds} ms");
            return response;
        }
    }
}
