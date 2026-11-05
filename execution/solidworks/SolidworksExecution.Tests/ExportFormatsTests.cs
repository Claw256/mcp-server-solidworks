using System;
using System.IO;
using SolidworksExecution.Services;
using Xunit;

namespace SolidworksExecution.Tests
{
    public class ExportFormatsTests
    {
        [Theory]
        [InlineData("STEP", "C:/o/p.step")]
        [InlineData("step", "C:/o/p.STP")]
        [InlineData("IGES", "C:/o/p.igs")]
        [InlineData("STL", "C:/o/p.stl")]
        [InlineData("PDF", "C:/o/p.PDF")]
        [InlineData("DWG", "C:/o/p.dwg")]
        [InlineData("DXF", "C:/o/p.dxf")]
        [InlineData("3MF", "C:/o/p.anything")]   // formats this class does not know pass through
        public void Matching_extension_is_accepted_unchanged(string format, string path)
        {
            string corrected, error;
            Assert.True(ExportFormats.CheckExtension(format, path, out corrected, out error));
            Assert.Null(error);
            Assert.Equal(path, corrected);
        }

        [Theory]
        [InlineData("PDF", "C:/o/p.dxf")]
        [InlineData("DXF", "C:/o/p.dwg")]
        [InlineData("STEP", "C:/o/p.stl")]
        [InlineData("STL", "C:/o/p")]
        public void Mismatched_extension_is_rejected_naming_the_expected_one(string format, string path)
        {
            string corrected, error;
            Assert.False(ExportFormats.CheckExtension(format, path, out corrected, out error));
            Assert.Contains("does not match format", error);
            Assert.Contains(format, error);
        }

        [Fact]
        public void Iges_dot_iges_is_corrected_to_igs()
        {
            string corrected, error;
            Assert.True(ExportFormats.CheckExtension("IGES", "C:/o/part.iges", out corrected, out error));
            Assert.Null(error);
            Assert.Equal("C:/o/part.igs", corrected);
        }

        [Fact]
        public void Pdf_all_sheets_option_matches_the_documented_enum_value()
        {
            Assert.Equal((int)SolidWorks.Interop.swconst.swExportDataSheetsToExport_e.swExportData_ExportAllSheets,
                ExportFormats.PdfAllSheets);
        }

        [Fact]
        public void File_check_rejects_missing_empty_and_stale_files()
        {
            string dir = Path.Combine(Path.GetTempPath(), "exportfmt_" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(dir);
            try
            {
                string reason;
                string missing = Path.Combine(dir, "none.pdf");
                Assert.False(ExportFormats.IsFreshNonEmptyFile(missing, DateTime.UtcNow, out reason));
                Assert.Contains("no file", reason);

                string empty = Path.Combine(dir, "empty.pdf");
                File.WriteAllBytes(empty, new byte[0]);
                Assert.False(ExportFormats.IsFreshNonEmptyFile(empty, DateTime.UtcNow.AddMinutes(-1), out reason));
                Assert.Contains("empty", reason);

                string stale = Path.Combine(dir, "stale.pdf");
                File.WriteAllText(stale, "x");
                File.SetLastWriteTimeUtc(stale, DateTime.UtcNow.AddHours(-1));
                Assert.False(ExportFormats.IsFreshNonEmptyFile(stale, DateTime.UtcNow, out reason));
                Assert.Contains("predates", reason);

                string fresh = Path.Combine(dir, "fresh.pdf");
                DateTime started = DateTime.UtcNow;
                File.WriteAllText(fresh, "x");
                Assert.True(ExportFormats.IsFreshNonEmptyFile(fresh, started, out reason));
                Assert.Null(reason);
            }
            finally { Directory.Delete(dir, true); }
        }
    }
}
