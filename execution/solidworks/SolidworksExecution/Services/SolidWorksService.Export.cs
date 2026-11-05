using System;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;

namespace SolidworksExecution.Services
{
    // Shared SaveAs3 export path for export_document and batch_export.
    public partial class SolidWorksService
    {
        // SaveAs3 into <path>; the format is inferred from the extension. PDF of a drawing is given an
        // IExportPdfData that exports ALL sheets (default is the active sheet only) and does not open the viewer.
        // DXF/DWG pass no export data: the 2025 API has no export-data object for them, so version, units,
        // line styles and multi-sheet behaviour come from the user's SolidWorks DXF/DWG export preferences.
        // Compile-checked only (live-only behaviour).
        private bool SaveAsExport(IModelDoc2 modelDoc, string format, string path, out int errors, out int warnings)
        {
            errors = 0;
            warnings = 0;
            object exportData = null;
            if (format == "PDF")
            {
                var pdf = _solidWorks.GetExportFileData(
                    (int)swExportDataFileType_e.swExportPdfData) as ExportPdfData;
                if (pdf != null)
                {
                    var drawingDoc = modelDoc as IDrawingDoc;
                    if (drawingDoc != null)
                    {
                        try
                        {
                            bool sheetsSet = pdf.SetSheets(ExportFormats.PdfAllSheets, drawingDoc.GetSheetNames());
                            if (!sheetsSet) ExecLog.Write("export PDF: IExportPdfData.SetSheets(all) returned false; SolidWorks default sheet selection applies");
                        }
                        catch (Exception ex) { ExecLog.Write($"export PDF: SetSheets threw {ex.Message}"); }
                    }
                    pdf.ViewPdfAfterSaving = false;
                    exportData = pdf;
                }
            }
            return modelDoc.Extension.SaveAs3(path,
                (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                exportData, null, ref errors, ref warnings);
        }

        // Null when the export wrote a fresh non-empty file; otherwise the reason (SaveAs3 can report success
        // without writing, or leave an older file in place).
        private static string CheckExportedFile(string path, DateTime startedUtc)
        {
            string reason;
            return ExportFormats.IsFreshNonEmptyFile(path, startedUtc, out reason) ? null : reason;
        }
    }
}
