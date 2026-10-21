using System.Linq;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;
using Xunit;

namespace SolidworksExecution.Tests
{
    public class GuardAndRegistryTests
    {
        [Fact]
        public void Registry_discovers_every_tool_once()
        {
            var names = ToolRegistry.Names.ToList();
            Assert.Equal(names.Count, names.Distinct().Count());
            Assert.Contains("extrude_feature", names);
            Assert.Contains("verify_state", names);
            Assert.True(names.Count >= 41);
        }

        [Fact]
        public void Activate_document_opts_out_of_state_version_check()
        {
            ToolEntry e;
            Assert.True(ToolRegistry.TryGet("activate_document", out e));
            Assert.False(e.CheckStateVersion);
            Assert.True(ToolRegistry.TryGet("extrude_feature", out e));
            Assert.True(e.CheckStateVersion);
        }

        [Fact]
        public void Unknown_tool_is_not_found()
        {
            ToolEntry e;
            Assert.False(ToolRegistry.TryGet("no_such_tool", out e));
            Assert.False(ToolRegistry.TryGet(null, out e));
        }

        [Fact]
        public void ResponseBuilder_failed_carries_code_and_does_not_advance_state()
        {
            var r = ResponseBuilder.Failed("op1", 7, ErrorCodes.ToolTimeout, "slow");
            Assert.Equal("FAILED", r.Status);
            Assert.Equal(7, r.StateVersion);
            Assert.Equal("TOOL_TIMEOUT", r.Error.Code);
            Assert.False(r.Verified);
        }

        [Fact]
        public void Guard_replays_duplicates_and_advances_version()
        {
            var g = OperationGuard.Instance;
            var before = g.GetCurrentStateVersion();
            var id = "test-" + System.Guid.NewGuid();
            Assert.False(g.IsDuplicate(id));
            g.RegisterCompleted(id, new ExecutionResponse { OperationId = id, Status = "COMPLETED" });
            Assert.True(g.IsDuplicate(id));
            Assert.Equal("DUPLICATE", g.GetDuplicate(id).Status);
            Assert.Equal(before + 1, g.GetCurrentStateVersion());
        }

        [Fact]
        public void Guard_history_is_bounded()
        {
            var g = OperationGuard.Instance;
            var first = "bound-first-" + System.Guid.NewGuid();
            g.RegisterCompleted(first, new ExecutionResponse { OperationId = first, Status = "COMPLETED" });
            for (int i = 0; i < 5100; i++)
            {
                var id = "bound-" + i + "-" + System.Guid.NewGuid();
                g.RegisterCompleted(id, new ExecutionResponse { OperationId = id, Status = "COMPLETED" });
            }
            Assert.False(g.IsDuplicate(first));   // oldest evicted
        }

        [Fact]
        public void StaExecutor_times_out_without_hanging_the_caller()
        {
            var sta = StaExecutor.Instance;
            Assert.Throws<System.TimeoutException>(() =>
                sta.Run(() => { System.Threading.Thread.Sleep(600); return 1; }, System.TimeSpan.FromMilliseconds(50)));
            // the worker recovers once the slow job ends
            Assert.Equal(2, sta.Run(() => 2, System.TimeSpan.FromSeconds(5)));
        }
    }
}
