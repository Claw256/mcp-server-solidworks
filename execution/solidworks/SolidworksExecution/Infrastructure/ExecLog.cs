using System;
using System.IO;

namespace SolidworksExecution.Infrastructure
{
    // Minimal file logger (P0.6-mini). Writes next to the exe as execution.log.
    // Must NEVER throw — logging failures are swallowed.
    public static class ExecLog
    {
        private static readonly object _lock = new object();

        public static readonly string FilePath =
            Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "execution.log");

        // One generation is kept (execution.log.1) once the log passes MaxBytes, so a long-running
        // server cannot fill the disk. Called under _lock; failures are swallowed by Write.
        private const long MaxBytes = 5L * 1024 * 1024;

        private static void Rotate()
        {
            var info = new FileInfo(FilePath);
            if (!info.Exists || info.Length <= MaxBytes) return;
            var old = FilePath + ".1";
            if (File.Exists(old)) File.Delete(old);
            File.Move(FilePath, old);
        }

        public static void Write(string message)
        {
            try
            {
                lock (_lock)
                {
                    Rotate();
                    File.AppendAllText(FilePath,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + "  " + message + Environment.NewLine);
                }
            }
            catch
            {
                // logging must never break execution
            }
        }
    }
}
