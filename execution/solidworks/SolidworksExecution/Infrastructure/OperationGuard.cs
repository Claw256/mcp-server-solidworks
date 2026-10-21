using System.Collections.Concurrent;
using System.Threading;
using SolidworksExecution.Models;

namespace SolidworksExecution.Infrastructure
{
    public class OperationGuard : IOperationGuard
    {
        private static readonly OperationGuard _instance = new OperationGuard();
        public static OperationGuard Instance => _instance;

        private readonly ConcurrentDictionary<string, ExecutionResponse> _completed
            = new ConcurrentDictionary<string, ExecutionResponse>();

        // Bound the replay history: a long session would otherwise grow this forever. Oldest first out.
        private const int MaxRemembered = 5000;
        private readonly ConcurrentQueue<string> _order = new ConcurrentQueue<string>();

        private volatile int _currentStateVersion = 0;

        private OperationGuard() { }

        public bool IsDuplicate(string operationId)
        {
            return _completed.ContainsKey(operationId);
        }

        public ExecutionResponse GetDuplicate(string operationId)
        {
            if (_completed.TryGetValue(operationId, out var original))
            {
                return new ExecutionResponse
                {
                    OperationId = operationId,
                    Status = "DUPLICATE",
                    LastKnownStateVersion = _currentStateVersion,
                    CadState = null,
                    Error = null
                };
            }
            return null;
        }

        public bool IsStateVersionValid(int incomingStateVersion)
        {
            return incomingStateVersion == _currentStateVersion;
        }

        public void RegisterCompleted(string operationId, ExecutionResponse response)
        {
            if (_completed.TryAdd(operationId, response))
            {
                _order.Enqueue(operationId);
                while (_completed.Count > MaxRemembered && _order.TryDequeue(out var oldest))
                {
                    ExecutionResponse dropped;
                    _completed.TryRemove(oldest, out dropped);
                }
            }
            else
            {
                _completed[operationId] = response;
            }
            Interlocked.Increment(ref _currentStateVersion);
        }

        public int GetCurrentStateVersion()
        {
            return _currentStateVersion;
        }
    }
}
