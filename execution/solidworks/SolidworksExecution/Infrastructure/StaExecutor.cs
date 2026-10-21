using System;
using System.Collections.Concurrent;
using System.Threading;

namespace SolidworksExecution.Infrastructure
{
    /// <summary>
    /// Runs delegates on a single, long-lived, dedicated STA thread. (P0.1)
    ///
    /// SolidWorks COM objects must be called from an STA apartment. OWIN / Web API request
    /// threads are MTA, and while COM marshaling lets lightweight calls (e.g. FeatureExtrusion3)
    /// limp across the apartment boundary, heavier or more apartment-sensitive calls fail off-STA:
    /// InsertSheetMetalBaseFlange2 silently returned null and add_edge_feature (edge fillet)
    /// deadlocked (ContextSwitchDeadlock). Routing ALL COM-touching work through one STA thread —
    /// including the GetActiveObject attach — makes the RCW live on the STA, fixing both.
    ///
    /// A single worker thread also serializes COM access, which suits the single-writer,
    /// process-global state model the OperationGuard already assumes.
    /// </summary>
    /// <summary>A timeout. <see cref="Started"/> says whether the job had begun (and may still be running) or never ran.</summary>
    public sealed class ToolTimeoutException : TimeoutException
    {
        public bool Started { get; }
        public ToolTimeoutException(bool started, string message) : base(message) { Started = started; }
    }

    public sealed class StaExecutor
    {
        public static readonly StaExecutor Instance = new StaExecutor();

        private readonly BlockingCollection<Action> _queue = new BlockingCollection<Action>();

        private StaExecutor()
        {
            var thread = new Thread(Worker)
            {
                IsBackground = true,
                Name = "SolidWorksStaThread"
            };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
        }

        private void Worker()
        {
            // Each job sets its own completion event and captures its own exception, so a throwing
            // job never tears down the worker loop.
            foreach (var work in _queue.GetConsumingEnumerable())
            {
                try { work(); }
                catch { /* a job's own failure is captured by Run; nothing may stop the worker loop */ }
            }
        }

        /// <summary>Marshal <paramref name="func"/> onto the STA thread and block until it returns.</summary>
        public T Run<T>(Func<T> func) { return Run(func, Timeout.InfiniteTimeSpan); }

        /// <summary>
        /// As <see cref="Run{T}(Func{T})"/>, but gives up waiting after <paramref name="timeout"/> and throws
        /// <see cref="TimeoutException"/>. The job itself cannot be cancelled (COM): it keeps the worker
        /// busy until it returns, and later jobs queue behind it.
        /// </summary>
        public T Run<T>(Func<T> func, TimeSpan timeout)
        {
            T result = default(T);
            Exception captured = null;
            // 0 = queued, 1 = started, 2 = abandoned before it started. The caller that times out CLAIMS a
            // still-queued job (0 -> 2) so it never runs afterwards: a request that was told "timed out" must
            // not mutate the model later, when the worker finally frees up.
            int state = 0;
            // Not disposed: after a timeout the job may still finish and Set() it, and a disposed event would throw there.
            var done = new ManualResetEventSlim(false);
            _queue.Add(() =>
            {
                if (Interlocked.CompareExchange(ref state, 1, 0) != 0) { done.Set(); return; }
                try { result = func(); }
                catch (Exception ex) { captured = ex; }
                finally { done.Set(); }
            });
            if (!done.Wait(timeout))
            {
                bool started = Interlocked.CompareExchange(ref state, 2, 0) != 0;
                throw new ToolTimeoutException(started, "STA job exceeded " + timeout);
            }
            if (state == 2) throw new ToolTimeoutException(false, "STA job abandoned before it started");
            if (captured != null)
                throw new InvalidOperationException("STA execution failed: " + captured.Message, captured);
            return result;
        }
    }
}
