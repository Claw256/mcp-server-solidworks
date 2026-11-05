using System;
using System.IO;

namespace SolidworksExecution.Services
{
    /// <summary>
    /// Pure (no COM) helpers for export_document / batch_export: the format -> file-extension contract and the
    /// post-export file check. SaveAs3 infers the format from the extension, so a mismatched extension would
    /// silently write a different format than the caller asked for.
    /// </summary>
    public static class ExportFormats
    {
        /// <summary>swExportDataSheetsToExport_e.swExportData_ExportAllSheets (ADR-018: inlined int).</summary>
        public const int PdfAllSheets = 1;

        /// <summary>Extensions SolidWorks accepts for a format (first = the one batch_export writes); null = format not known here.</summary>
        public static string[] ExtensionsFor(string format)
        {
            switch ((format ?? "").Trim().ToUpperInvariant())
            {
                case "STEP": return new[] { ".step", ".stp" };
                case "IGES": return new[] { ".igs" };
                case "STL": return new[] { ".stl" };
                case "PDF": return new[] { ".pdf" };
                case "DWG": return new[] { ".dwg" };
                case "DXF": return new[] { ".dxf" };
                default: return null;
            }
        }

        /// <summary>
        /// Validates <paramref name="filePath"/>'s extension against <paramref name="format"/>. IGES's .iges is corrected
        /// to .igs (SolidWorks' translator only recognizes .igs); any other mismatch returns false with an error message.
        /// Formats this class does not know are passed through unchanged.
        /// </summary>
        public static bool CheckExtension(string format, string filePath, out string correctedPath, out string error)
        {
            correctedPath = filePath;
            error = null;
            string[] allowed = ExtensionsFor(format);
            if (allowed == null || string.IsNullOrEmpty(filePath)) return true;

            string ext = (Path.GetExtension(filePath) ?? "").ToLowerInvariant();
            if (Array.IndexOf(allowed, ext) >= 0) return true;

            if (allowed[0] == ".igs" && ext == ".iges")
            {
                correctedPath = Path.ChangeExtension(filePath, "igs");
                return true;
            }

            error = "file_path extension '" + (ext.Length == 0 ? "(none)" : ext) + "' does not match format '"
                + format.Trim().ToUpperInvariant() + "' (expected " + string.Join(" or ", allowed)
                + "). SolidWorks picks the output format from the extension.";
            return false;
        }

        /// <summary>True when the file exists, is non-empty and was written at or after <paramref name="startedUtc"/> (minus 2 s clock slack) - so a stale file from an earlier export cannot pass.</summary>
        public static bool IsFreshNonEmptyFile(string path, DateTime startedUtc, out string reason)
        {
            reason = null;
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) { reason = "no file was written"; return false; }
            var info = new FileInfo(path);
            if (info.Length <= 0) { reason = "the file is empty (0 bytes)"; return false; }
            if (info.LastWriteTimeUtc < startedUtc.AddSeconds(-2)) { reason = "the file was not rewritten (it predates this export)"; return false; }
            return true;
        }
    }
}
