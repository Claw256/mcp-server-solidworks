using SolidworksExecution.Models;

namespace SolidworksExecution.Infrastructure
{
    public static class ResponseBuilder
    {
        public static ExecutionResponse Failed(string operationId, int stateVersion, string code, string message)
        {
            return new ExecutionResponse
            {
                OperationId = operationId,
                Status = "FAILED",
                Verified = false,
                StateVersion = stateVersion,
                CadState = null,
                Error = new ExecutionError { Code = code, Message = message },
            };
        }
    }

    /// <summary>Error codes owned by the pipeline layer (tool-level codes still live with the tools).</summary>
    public static class ErrorCodes
    {
        public const string InvalidStateVersion = "INVALID_STATE_VERSION";
        public const string ToolTimeout = "TOOL_TIMEOUT";
        public const string UnknownTool = "UNKNOWN_TOOL";
        public const string Unexpected = "UNEXPECTED_ERROR";
    }
}
