using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

class LocalLauncher
{
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string python = Path.Combine(root, "runtime", "python", "pythonw.exe");
            string bootstrap = Path.Combine(root, "bootstrap.py");
            if (!File.Exists(python) || !File.Exists(bootstrap))
                throw new IOException("程序文件缺失，请保持 exe 与 runtime 文件夹在同一目录。");
            bool test = Array.IndexOf(args, "--startup-test") >= 0;
            Process process = Process.Start(new ProcessStartInfo(python,
                "-B \"" + bootstrap + "\"" + (test ? " --startup-test" : "")) {
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true
            });
            if (test) { process.WaitForExit(); return process.ExitCode; }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "漫画超分启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}