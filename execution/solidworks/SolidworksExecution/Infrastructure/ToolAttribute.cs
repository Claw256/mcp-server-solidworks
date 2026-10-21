using System;

namespace SolidworksExecution.Infrastructure
{
    /// <summary>
    /// Marks a SolidWorksService method as an MCP-visible tool. <see cref="ToolRegistry"/> discovers
    /// these at startup, so adding a tool is one attribute instead of an edit to a dispatch switch.
    /// </summary>
    [AttributeUsage(AttributeTargets.Method, AllowMultiple = false)]
    public sealed class ToolAttribute : Attribute
    {
        public string Name { get; }

        /// <summary>
        /// When true (the default) the pipeline rejects a request whose state_version does not match
        /// the guard's current value. Tools that deliberately work across versions (activate_document)
        /// opt out.
        /// </summary>
        public bool CheckStateVersion { get; set; } = true;

        /// <summary>
        /// When true the pipeline reads the model's health before and after the tool and attaches
        /// `diagnostics` (new feature errors, What's Wrong count, volume delta) to a COMPLETED response.
        /// Set on tools that create or change features; read-only and sketch tools leave it off.
        /// </summary>
        public bool Diagnose { get; set; }

        public ToolAttribute(string name) { Name = name; }
    }
}
