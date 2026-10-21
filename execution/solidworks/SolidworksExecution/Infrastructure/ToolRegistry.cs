using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using SolidworksExecution.Models;
using SolidworksExecution.Services;

namespace SolidworksExecution.Infrastructure
{
    public sealed class ToolEntry
    {
        public string Name { get; set; }
        public bool CheckStateVersion { get; set; }
        public bool Diagnose { get; set; }
        public Func<SolidWorksService, ToolRequest, ExecutionResponse> Invoke { get; set; }
    }

    /// <summary>The set of tools, built once from [Tool] attributes on SolidWorksService.</summary>
    public static class ToolRegistry
    {
        private static readonly Dictionary<string, ToolEntry> _tools = Build();

        public static IEnumerable<string> Names => _tools.Keys.OrderBy(n => n, StringComparer.Ordinal);

        public static bool TryGet(string name, out ToolEntry entry)
        {
            if (name == null) { entry = null; return false; }
            return _tools.TryGetValue(name, out entry);
        }

        private static Dictionary<string, ToolEntry> Build()
        {
            var map = new Dictionary<string, ToolEntry>(StringComparer.Ordinal);
            var methods = typeof(SolidWorksService).GetMethods(BindingFlags.Public | BindingFlags.Instance);
            foreach (var m in methods)
            {
                var attr = m.GetCustomAttribute<ToolAttribute>();
                if (attr == null) continue;
                if (map.ContainsKey(attr.Name))
                    throw new InvalidOperationException("Duplicate [Tool] name: " + attr.Name);
                var invoke = (Func<SolidWorksService, ToolRequest, ExecutionResponse>)Delegate.CreateDelegate(
                    typeof(Func<SolidWorksService, ToolRequest, ExecutionResponse>), null, m);
                map[attr.Name] = new ToolEntry
                {
                    Name = attr.Name,
                    CheckStateVersion = attr.CheckStateVersion,
                    Diagnose = attr.Diagnose,
                    Invoke = invoke,
                };
            }
            return map;
        }
    }
}
