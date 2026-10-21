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
        public void Only_feature_mutating_tools_request_diagnostics()
        {
            ToolEntry e;
            foreach (var name in new[] { "extrude_feature", "hole_wizard", "edit_feature", "add_mate" })
            {
                Assert.True(ToolRegistry.TryGet(name, out e));
                Assert.True(e.Diagnose, name);
            }
            foreach (var name in new[] { "verify_state", "analyze_model", "create_sketch", "export_document" })
            {
                Assert.True(ToolRegistry.TryGet(name, out e));
                Assert.False(e.Diagnose, name);
            }
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
        public void Dialog_suppression_is_on_by_default_and_the_env_var_turns_it_off()
        {
            const string name = "SOLIDPILOT_SUPPRESS_DIALOGS";
            var original = System.Environment.GetEnvironmentVariable(name);
            try
            {
                System.Environment.SetEnvironmentVariable(name, null);
                Assert.True(SolidworksExecution.Services.SolidWorksService.DialogSuppressionEnabled);
                foreach (var off in new[] { "0", "false", "FALSE", " 0 " })
                {
                    System.Environment.SetEnvironmentVariable(name, off);
                    Assert.False(SolidworksExecution.Services.SolidWorksService.DialogSuppressionEnabled, off);
                }
                System.Environment.SetEnvironmentVariable(name, "1");
                Assert.True(SolidworksExecution.Services.SolidWorksService.DialogSuppressionEnabled);
            }
            finally { System.Environment.SetEnvironmentVariable(name, original); }
        }

        [Fact]
        public void A_job_abandoned_by_a_timeout_before_it_started_never_runs_later()
        {
            var sta = StaExecutor.Instance;
            bool ranLater = false;
            var blocker = System.Threading.Tasks.Task.Run(() => sta.Run(() => { System.Threading.Thread.Sleep(500); return 1; }));
            System.Threading.Thread.Sleep(100);                       // let the blocker occupy the worker
            var ex = Assert.Throws<ToolTimeoutException>(() =>
                sta.Run(() => { ranLater = true; return 2; }, System.TimeSpan.FromMilliseconds(50)));
            Assert.False(ex.Started);                                // it never began
            blocker.Wait();
            System.Threading.Thread.Sleep(200);                      // the worker is free now: the abandoned job must stay dead
            Assert.False(ranLater);
            Assert.Equal(3, sta.Run(() => 3, System.TimeSpan.FromSeconds(5)));   // and the worker is healthy
        }

        [Fact]
        public void A_timeout_on_a_running_job_reports_started_and_the_worker_survives_its_late_finish()
        {
            var sta = StaExecutor.Instance;
            var ex = Assert.Throws<ToolTimeoutException>(() =>
                sta.Run(() => { System.Threading.Thread.Sleep(300); return 1; }, System.TimeSpan.FromMilliseconds(50)));
            Assert.True(ex.Started);
            System.Threading.Thread.Sleep(400);                      // its late Set() must not have broken the worker
            Assert.Equal(4, sta.Run(() => 4, System.TimeSpan.FromSeconds(5)));
        }

        [Fact]
        public void The_same_operation_id_cannot_be_in_flight_twice()
        {
            var id = "inflight-" + System.Guid.NewGuid();
            Assert.True(ToolPipeline.TryBegin(id));
            Assert.False(ToolPipeline.TryBegin(id));                 // a retry while the first is still running
            ToolPipeline.End(id);
            ToolPipeline.End(id);                                    // idempotent
            Assert.True(ToolPipeline.TryBegin(id));
            ToolPipeline.End(id);
        }

        [Fact]
        public void StaExecutor_times_out_without_hanging_the_caller()
        {
            var sta = StaExecutor.Instance;
            Assert.ThrowsAny<System.TimeoutException>(() =>
                sta.Run(() => { System.Threading.Thread.Sleep(600); return 1; }, System.TimeSpan.FromMilliseconds(50)));
            // the worker recovers once the slow job ends
            Assert.Equal(2, sta.Run(() => 2, System.TimeSpan.FromSeconds(5)));
        }
    }
}
