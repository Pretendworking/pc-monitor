using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Threading;

internal static class MonitorLauncher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindow(string className, string windowName);
    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr window, int command);

    private static void StartSensorsMinimized()
    {
        IntPtr existing = FindWindow(null, "Libre Hardware Monitor");
        if (existing != IntPtr.Zero)
        {
            ShowWindowAsync(existing, 6);
            return;
        }
        if (Process.GetProcessesByName("LibreHardwareMonitor").Length > 0) return;
        try
        {
            // Use Libre's user-authorized startup task rather than directly
            // launching its elevated EXE and prompting on every widget launch.
            string scheduler = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "schtasks.exe");
            using (Process task = Process.Start(new ProcessStartInfo(scheduler,
                "/Run /TN \"LibreHardwareMonitor\"") {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true
            }))
            {
                if (task == null || !task.WaitForExit(3000) || task.ExitCode != 0) return;
                for (int attempt = 0; attempt < 30; attempt++)
                {
                    IntPtr sensorWindow = FindWindow(null, "Libre Hardware Monitor");
                    if (sensorWindow != IntPtr.Zero)
                    {
                        ShowWindowAsync(sensorWindow, 6);
                        break;
                    }
                    Thread.Sleep(200);
                }
            }
        }
        catch (Exception) { /* Widget remains usable without hardware sensors. */ }
    }

    [STAThread]
    private static void Main(string[] args)
    {
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(folder, @".venv\Scripts\pythonw.exe");
        string script = Path.Combine(folder, "pc_monitor.py");
        if (args.Length > 0 && args[0] == "--check")
        {
            Environment.Exit(File.Exists(python) && File.Exists(script) ? 0 : 2);
            return;
        }
        IntPtr window = FindWindow(null, "PC Monitor settings");
        if (window == IntPtr.Zero) window = FindWindow(null, "PC Monitor");
        if (window != IntPtr.Zero)
        {
            if (IsIconic(window)) ShowWindow(window, 9);
            SetForegroundWindow(window);
            StartSensorsMinimized();
            return;
        }
        try
        {
            if (!File.Exists(python) || !File.Exists(script))
                throw new FileNotFoundException("Create the project's .venv and install requirements.txt first. See README.md.");
            Process.Start(new ProcessStartInfo(python, "\"" + script + "\"") {
                WorkingDirectory = folder, UseShellExecute = false, CreateNoWindow = true
            });
            StartSensorsMinimized();
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "PC Monitor", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }
}
