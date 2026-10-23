using Newtonsoft.Json.Linq;
using SolidworksExecution.Services;
using Xunit;

namespace SolidworksExecution.Tests
{
    public class MeshImportOptionsTests
    {
        [Fact]
        public void Defaults_are_graphics_mm()
        {
            string err;
            var o = MeshImportOptions.Parse(new JObject(), out err);
            Assert.Null(err);
            Assert.Equal(0, o.ModelType);
            Assert.Equal(0, o.UnitValue);
            Assert.False(o.AsMeshBody);
        }

        [Theory]
        [InlineData("graphics", 0)]
        [InlineData("surface", 1)]
        [InlineData("SOLID", 2)]
        public void Mesh_as_maps_to_enum(string v, int expected)
        {
            string err;
            var o = MeshImportOptions.Parse(new JObject { ["mesh_as"] = v }, out err);
            Assert.Equal(expected, o.ModelType);
            Assert.Equal(expected != 0, o.AsMeshBody);
        }

        [Theory]
        [InlineData("mm", 0)]
        [InlineData("cm", 1)]
        [InlineData("m", 2)]
        [InlineData("in", 3)]
        public void Units_map_to_length_unit(string v, int expected)
        {
            string err;
            var o = MeshImportOptions.Parse(new JObject { ["units"] = v }, out err);
            Assert.Equal(expected, o.UnitValue);
        }

        [Fact]
        public void Invalid_values_list_allowed()
        {
            string err;
            Assert.Null(MeshImportOptions.Parse(new JObject { ["mesh_as"] = "brep" }, out err));
            Assert.Contains("graphics, surface, solid", err);
            Assert.Null(MeshImportOptions.Parse(new JObject { ["units"] = "ft" }, out err));
            Assert.Contains("mm, cm, m, in", err);
        }

        [Fact]
        public void Mesh_extensions_detected()
        {
            Assert.True(MeshImportOptions.IsMeshExtension(".STL"));
            Assert.True(MeshImportOptions.IsMeshExtension(".3mf"));
            Assert.False(MeshImportOptions.IsMeshExtension(".step"));
            Assert.False(MeshImportOptions.IsMeshExtension(".sldprt"));
        }
    }
}
